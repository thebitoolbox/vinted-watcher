import html
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

STATE_FILE = Path("seen_items.json")
CONFIG_FILE = Path("watch_config.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
)
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MAX_ALERTS_PER_MESSAGE = int(os.getenv("MAX_ALERTS_PER_MESSAGE", "10"))
TRANSLATION_ENABLED = os.getenv("TRANSLATION_ENABLED", "true").lower() == "true"


def load_config() -> List[Dict]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("Brak pliku watch_config.json")

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    searches = data.get("searches", [])
    active = [s for s in searches if s.get("enabled", True)]

    if not active:
        raise ValueError("Brak aktywnych wyszukiwań w watch_config.json")

    for search in active:
        if "id" not in search:
            raise ValueError("Każde wyszukiwanie musi mieć pole 'id'")

    return active


def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {
            "updated_at": 0,
            "seen_by_search": {},
            "alerted_items": []
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        return {
            "updated_at": data.get("updated_at", 0),
            "seen_by_search": data.get("seen_by_search", {}),
            "alerted_items": data.get("alerted_items", [])
        }
    except Exception:
        return {
            "updated_at": 0,
            "seen_by_search": {},
            "alerted_items": []
        }


def save_state(seen_by_search: Dict[str, Set[str]], alerted_items: Set[str]) -> None:
    payload = {
        "updated_at": int(time.time()),
        "seen_by_search": {k: sorted(v) for k, v in seen_by_search.items()},
        "alerted_items": sorted(alerted_items)
    }

    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def fetch_vinted_page(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def absolute_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://www.vinted.pl{href}"
    return f"https://www.vinted.pl/{href.lstrip('/')}"


def extract_items(html_text: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "lxml")
    items: List[Dict[str, str]] = []
    local_seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()

        if "/items/" not in href:
            continue

        match = re.search(r"/items/(d+)", href)
        if not match:
            continue

        item_id = match.group(1)
        if item_id in local_seen_ids:
            continue

        local_seen_ids.add(item_id)
        url = absolute_url(href)

        title = (
            a.get("title")
            or a.get_text(" ", strip=True)
            or "Nowa oferta"
        ).strip()

        title = re.sub(r"s+", " ", title)

        items.append({
            "id": item_id,
            "title": title[:180],
            "url": url,
        })

    return items


def send_telegram_message(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(api_url, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def unique_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    result = []

    for value in values:
        cleaned = re.sub(r"s+", " ", str(value).strip())
        if not cleaned:
            continue

        key = cleaned.casefold()
        if key in seen:
            continue

        seen.add(key)
        result.append(cleaned)

    return result


def translate_query(base_query: str, languages: List[str]) -> List[str]:
    if not TRANSLATION_ENABLED or not base_query or not languages:
        return []

    translated_variants = []

    for lang in languages:
        try:
            translated = GoogleTranslator(source="auto", target=lang).translate(text=base_query)
            if translated:
                translated_variants.append(translated.strip())
        except Exception as e:
            print(f"[WARN] Translation failed for '{base_query}' -> {lang}: {e}")

    return translated_variants


def expand_keywords(search: Dict) -> List[str]:
    base_query = search.get("base_query", "").strip()
    manual_keywords = search.get("manual_keywords", [])
    languages = search.get("languages", [])

    variants = []

    if base_query:
        variants.append(base_query)

    variants.extend(translate_query(base_query, languages))
    variants.extend(manual_keywords)

    return unique_preserve_order(variants)


def build_search_url(
    query: str,
    price_to: int | None = None,
    currency: str = "PLN",
    brand_ids: List[int] | None = None
) -> str:
    params: List[Tuple[str, str]] = [
        ("search_text", query),
        ("currency", currency),
    ]

    if price_to is not None:
        params.append(("price_from", "0"))
        params.append(("price_to", str(price_to)))

    if brand_ids:
        for brand_id in brand_ids:
            params.append(("brand_ids[]", str(brand_id)))

    return f"https://www.vinted.pl/catalog?{urlencode(params)}"


def build_message(search_name: str, variant_label: str, new_items: List[Dict[str, str]]) -> str:
    lines = [
        f"🛍️ <b>{html.escape(search_name)}</b>",
        f"Wariant: <b>{html.escape(variant_label)}</b>",
        f"Nowe oferty: <b>{len(new_items)}</b>"
    ]

    for item in new_items[:MAX_ALERTS_PER_MESSAGE]:
        lines.append("")
        lines.append(f"• {html.escape(item['title'])}")
        lines.append(item["url"])

    if len(new_items) > MAX_ALERTS_PER_MESSAGE:
        lines.append("")
        lines.append(f"… i jeszcze {len(new_items) - MAX_ALERTS_PER_MESSAGE} kolejnych")

    return "
".join(lines)


def main() -> None:
    searches = load_config()
    state = load_state()

    seen_by_search: Dict[str, Set[str]] = {
        key: set(values) for key, values in state.get("seen_by_search", {}).items()
    }
    alerted_items: Set[str] = set(state.get("alerted_items", []))

    total_new_alerts = 0

    for search in searches:
        search_id = search["id"]
        search_name = search.get("name", search_id)
        price_to = search.get("price_to")
        currency = search.get("currency", "PLN")
        brand_ids = search.get("brand_ids", [])

        keywords = expand_keywords(search)
        if not keywords:
            print(f"[WARN] Brak keywords dla {search_name}")
            continue

        for keyword in keywords:
            variant_key = f"{search_id}::{keyword.casefold()}"
            search_url = build_search_url(
                query=keyword,
                price_to=price_to,
                currency=currency,
                brand_ids=brand_ids
            )

            try:
                html_text = fetch_vinted_page(search_url)
                items = extract_items(html_text)
            except Exception as e:
                print(f"[ERROR] {search_name} / {keyword}: {e}")
                continue

            if not items:
                print(f"[WARN] Brak ofert dla: {search_name} / {keyword}")
                continue

            current_ids = {item["id"] for item in items}
            previous_ids = seen_by_search.get(variant_key, set())

            if not previous_ids:
                seen_by_search[variant_key] = current_ids
                print(f"[INIT] {search_name} / {keyword}: zapisano {len(current_ids)} ofert bez alertu.")
                continue

            unseen_for_this_search = [item for item in items if item["id"] not in previous_ids]
            globally_new_items = [item for item in unseen_for_this_search if item["id"] not in alerted_items]

            if globally_new_items:
                send_telegram_message(build_message(search_name, keyword, globally_new_items))
                for item in globally_new_items:
                    alerted_items.add(item["id"])
                total_new_alerts += len(globally_new_items)
                print(f"[ALERT] {search_name} / {keyword}: {len(globally_new_items)} nowych globalnie.")

            seen_by_search[variant_key] = previous_ids | current_ids

    save_state(seen_by_search, alerted_items)
    print(f"Gotowe. Łącznie wysłanych nowych alertów: {total_new_alerts}")


if __name__ == "__main__":
    main()