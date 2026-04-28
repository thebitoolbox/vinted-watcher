import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup

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
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def fetch_vinted_page(url: str) -> str:
    if not url:
        raise ValueError("Brak VINTED_SEARCH_URL w zmiennych środowiskowych.")

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


def extract_items(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
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
                "price": "",
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
        "disable_web_page_preview": True,
    }

    response = requests.post(api_url, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def build_message(new_items: List[Dict[str, str]]) -> str:
    lines = [f"🛍️ Vinted watcher: {len(new_items)} nowych ofert"]

    for item in new_items[:MAX_ALERTS]:
        lines.append("")
        lines.append(f"• {item['title']}")
        lines.append(item["url"])

    if len(new_items) > MAX_ALERTS:
        lines.append("")
        lines.append(f"… i jeszcze {len(new_items) - MAX_ALERTS} kolejnych")

    return "\n".join(lines)


def main() -> None:
    html = fetch_vinted_page(SEARCH_URL)
    current_items = extract_items(html)

    if not current_items:
        print("=== HTML DEBUG START ===")
        print(html[:2000])
        print("=== HTML DEBUG END ===")
        raise RuntimeError(
            "Nie udało się wyciągnąć ofert. Sprawdź HTML zwrócony przez Vinted i selektory w extract_items()."
        )

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
