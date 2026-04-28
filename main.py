import json
import os
import time
from pathlib import Path
from typing import Dict, List, Set

import requests

STATE_FILE = Path("seen_items.json")
SEARCH_URL = os.getenv("VINTED_SEARCH_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
)
TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "10"))


def load_seen_items() -> Set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except Exception:
        return set()


def save_seen_items(item_ids: Set[str]) -> None:
    payload = {
        "updated_at": int(time.time()),
        "seen_ids": sorted(item_ids),
    }
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_vinted_page(url: str) -> str:
    if not url:
        raise ValueError("Brak VINTED_SEARCH_URL w zmiennych środowiskowych.")
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_items(html: str) -> List[Dict[str, str]]:
    items = []
    marker = 'data-testid="product-item"'
    chunks = html.split(marker)
    for chunk in chunks[1:]:
        item_id = None
        title = "Nowa oferta"
        url = ""
        price = ""

        for token in ['data-item-id="', 'data-id="', 'id="item_']:
            if token in chunk:
                item_id = chunk.split(token, 1)[1].split('"', 1)[0]
                break

        if 'href="' in chunk:
            href = chunk.split('href="', 1)[1].split('"', 1)[0]
            if href.startswith("/"):
                url = f"https://www.vinted.pl{href}"
            elif href.startswith("http"):
                url = href

        for title_token in ['title="', 'alt="', 'data-title="']:
            if title_token in chunk:
                title = chunk.split(title_token, 1)[1].split('"', 1)[0].strip()
                break

        for price_token in ['data-testid="price"', '€', 'zł']:
            if price_token in chunk:
                price = "Sprawdź cenę w linku"
                break

        if item_id and url:
            items.append(
                {
                    "id": item_id,
                    "title": title[:180],
                    "url": url,
                    "price": price,
                }
            )
    return deduplicate_items(items)


def deduplicate_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    deduped = []
    for item in items:
        item_id = item["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item)
    return deduped


def send_telegram_message(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID.")
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    response = requests.post(api_url, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def build_message(new_items: List[Dict[str, str]]) -> str:
    lines = [f"🛍️ Vinted watcher: {len(new_items)} nowych ofert"]
    for item in new_items[:MAX_ALERTS]:
        lines.append("")
        lines.append(f"• {item['title']}")
        if item["price"]:
            lines.append(item["price"])
        lines.append(item["url"])
    if len(new_items) > MAX_ALERTS:
        lines.append("")
        lines.append(f"… i jeszcze {len(new_items) - MAX_ALERTS} kolejnych")
    return "\n".join(lines)


def main() -> None:
    html = fetch_vinted_page(SEARCH_URL)
    current_items = extract_items(html)
    if not current_items:
        raise RuntimeError("Nie udało się wyciągnąć ofert. Sprawdź SEARCH_URL i selektory w extract_items().")

    seen_ids = load_seen_items()
    current_ids = {item["id"] for item in current_items}
    new_items = [item for item in current_items if item["id"] not in seen_ids]

    if not seen_ids:
        save_seen_items(current_ids)
        print("Pierwsze uruchomienie: zapisano stan bez wysyłania alertu.")
        return

    if new_items:
        send_telegram_message(build_message(new_items))

    save_seen_items(seen_ids | current_ids)
    print(f"Znaleziono {len(new_items)} nowych ofert.")


if __name__ == "__main__":
    main()
