import html
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path("seen_items.json")
CONFIG_FILE = Path("watch_config.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
)
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "10"))


def load_config() -> List[Dict[str, str]]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("Brak pliku watch_config.json")

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    searches = data.get("searches", [])
    active = [s for s in searches if s.get("enabled", True) and s.get("url")]

    if not active:
        raise ValueError("Brak aktywnych wyszukiwań w watch_config.json")

    return active


def load_seen_items() -> Dict[str, List[str]]:
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("seen_by_search", {})
    except Exception:
        return {}


def save_seen_items(seen_by_search: Dict[str, Set[str]]) -> None:
    payload = {
        "updated_at": int(time.time()),
        "seen_by_search": {k: sorted(v) for k, v in seen_by_search.items()},
    }
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


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
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()

        if "/items/" not in href:
            continue

        match = re.search(r"/items/(\d+)", href)
        if not match:
            continue

        item_id = match.group(1)
        if item_id in seen_ids:
            continue

        seen_ids.add(item_id)
        url = absolute_url(href)

        title = (
            a.get("title")
            or a.get_text(" ", strip=True)
            or "Nowa oferta"
        ).strip()

        title = re.sub(r"\s+", " ", title)

        items.append(
            {
                "id": item_id,
                "title": title[:180],
                "url": url,
            }
        )

    return items


def send_telegram_message(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID.")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(api_url, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def build_message(search_name: str, new_items: List[Dict[str, str]]) -> str:
    lines = [
        f"🛍️ <b>{html.escape(search_name)}</b>",
        f"Nowe oferty: <b>{len(new_items)}</b>"
    ]

    for item in new_items[:MAX_ALERTS]:
        lines.append("")
        lines.append(f"• {html.escape(item['title'])}")
        lines.append(item["url"])

    if len(new_items) > MAX_ALERTS:
        lines.append("")
        lines.append(f"… i jeszcze {len(new_items) - MAX_ALERTS} kolejnych")

    return "\n".join(lines)


def normalize_search_key(search: Dict[str, str]) -> str:
    return search.get("name") or search["url"]


def main() -> None:
    searches = load_config()
    seen_raw = load_seen_items()
    seen_by_search: Dict[str, Set[str]] = {
        key: set(values) for key, values in seen_raw.items()
    }

    any_new = 0

    for search in searches:
        search_name = search.get("name", "Bez nazwy")
        search_url = search["url"]
        search_key = normalize_search_key(search)

        html_text = fetch_vinted_page(search_url)
        items = extract_items(html_text)

        if not items:
            print(f"[WARN] Brak ofert dla: {search_name}")
            print("=== HTML DEBUG START ===")
            print(html_text[:2000])
            print("=== HTML DEBUG END ===")
            continue

        current_ids = {item["id"] for item in items}
        previous_ids = seen_by_search.get(search_key, set())
        new_items = [item for item in items if item["id"] not in previous_ids]

        if not previous_ids:
            seen_by_search[search_key] = current_ids
            print(f"[INIT] {search_name}: zapisano {len(current_ids)} ofert bez alertu.")
            continue

        if new_items:
            send_telegram_message(build_message(search_name, new_items))
            any_new += len(new_items)
            print(f"[ALERT] {search_name}: {len(new_items)} nowych ofert.")

        seen_by_search[search_key] = previous_ids | current_ids

    save_seen_items(seen_by_search)
    print(f"Gotowe. Łącznie nowych ofert: {any_new}")


if __name__ == "__main__":
    main()
