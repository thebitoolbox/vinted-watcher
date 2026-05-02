import html
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Set

import requests

CONFIG_PATH = Path("watch_config.json")
STATE_PATH = Path("seen_items.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MAX_ALERTS_PER_MESSAGE = int(os.getenv("MAX_ALERTS", "10"))

VINTED_CATALOG_URL = "https://www.vinted.pl/api/v2/catalog/items"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.vinted.pl/",
        }
    )
    return session


SESSION = build_session()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> List[Dict[str, Any]]:
    payload = load_json(CONFIG_PATH, {"searches": []})
    searches = payload.get("searches", [])

    if not isinstance(searches, list):
        raise ValueError("watch_config.json ma niepoprawny format: 'searches' musi być listą")

    return searches


def load_state() -> Dict[str, Any]:
    return load_json(
        STATE_PATH,
        {
            "seen_ids": [],
            "seen_by_search": {},
            "last_run_ts": None,
        },
    )


def save_state(state: Dict[str, Any]) -> None:
    state["seen_ids"] = sorted(list(set(state.get("seen_ids", []))))
    normalized_seen_by_search = {}

    for key, values in state.get("seen_by_search", {}).items():
        normalized_seen_by_search[key] = sorted(list(set(values)))

    state["seen_by_search"] = normalized_seen_by_search
    save_json(STATE_PATH, state)


def build_params(search: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "search_text": keyword,
        "per_page": 20,
        "page": 1,
        "order": "newest_first",
    }

    if search.get("price_to") is not None:
        params["price_to"] = search["price_to"]

    if search.get("currency"):
        params["currency"] = search["currency"]

    brand_ids = search.get("brand_ids") or []
    if brand_ids:
        params["brand_ids[]"] = brand_ids

    return params


def extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("items", [])
    if isinstance(items, list):
        return items
    return []


def get_item_id(item: Dict[str, Any]) -> str:
    item_id = item.get("id")
    return str(item_id) if item_id is not None else ""


def get_item_title(item: Dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or "Bez tytułu")


def get_item_url(item: Dict[str, Any]) -> str:
    raw = str(item.get("url") or item.get("path") or "")

    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    if raw.startswith("/"):
        return f"https://www.vinted.pl{raw}"

    return raw


def get_item_price(item: Dict[str, Any]) -> str:
    price = item.get("price")

    if isinstance(price, dict):
        amount = price.get("amount") or price.get("value")
        currency = price.get("currency_code") or price.get("currency")
        if amount is not None and currency:
            return f"{amount} {currency}"
        if amount is not None:
            return str(amount)

    if price is not None:
        return str(price)

    total_price = item.get("total_item_price") or item.get("price_numeric")
    if total_price is not None:
        return str(total_price)

    return "brak ceny"


def fetch_items_for_keyword(search: Dict[str, Any], keyword: str) -> List[Dict[str, Any]]:
    params = build_params(search, keyword)
    response = SESSION.get(VINTED_CATALOG_URL, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return extract_items(payload)


def search_items(search: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_local: Set[str] = set()

    keywords: List[str] = []
    base_query = str(search.get("base_query") or "").strip()
    if base_query:
        keywords.append(base_query)

    for keyword in search.get("manual_keywords", []) or []:
        keyword = str(keyword).strip()
        if keyword:
            keywords.append(keyword)

    deduped_keywords: List[str] = []
    seen_keywords: Set[str] = set()

    for keyword in keywords:
        key = keyword.casefold()
        if key in seen_keywords:
            continue
        seen_keywords.add(key)
        deduped_keywords.append(keyword)

    for keyword in deduped_keywords:
        try:
            items = fetch_items_for_keyword(search, keyword)
        except Exception as exc:
            print(f"[WARN] search={search.get('id')} keyword={keyword!r} failed: {exc}")
            continue

        for item in items:
            item_id = get_item_id(item)
            if not item_id or item_id in seen_local:
                continue
            seen_local.add(item_id)
            results.append(item)

        time.sleep(0.4)

    return results


def filter_new_items(
    search_id: str,
    items: List[Dict[str, Any]],
    seen_global: Set[str],
    seen_by_search: Dict[str, Set[str]],
) -> List[Dict[str, Any]]:
    new_items: List[Dict[str, Any]] = []
    seen_for_this_search = seen_by_search.setdefault(search_id, set())

    for item in items:
        item_id = get_item_id(item)
        if not item_id:
            continue
        if item_id in seen_global:
            continue
        if item_id in seen_for_this_search:
            continue
        new_items.append(item)

    return new_items


def mark_items_seen(
    search_id: str,
    items: List[Dict[str, Any]],
    seen_global: Set[str],
    seen_by_search: Dict[str, Set[str]],
) -> None:
    seen_for_this_search = seen_by_search.setdefault(search_id, set())

    for item in items:
        item_id = get_item_id(item)
        if not item_id:
            continue
        seen_global.add(item_id)
        seen_for_this_search.add(item_id)


def format_alert(search: Dict[str, Any], new_items: List[Dict[str, Any]]) -> str:
    search_name = html.escape(str(search.get("name") or search.get("id") or "Vinted search"))

    lines = [
        f"🔔 <b>{search_name}</b>",
        f"Nowe oferty: <b>{len(new_items)}</b>",
        "",
    ]

    for item in new_items[:MAX_ALERTS_PER_MESSAGE]:
        title = html.escape(get_item_title(item))
        price = html.escape(get_item_price(item))
        url = html.escape(get_item_url(item))

        lines.append(f"• <b>{title}</b> — {price}")
        lines.append(url)
        lines.append("")

    if len(new_items) > MAX_ALERTS_PER_MESSAGE:
        lines.append("")
        lines.append(f"... i jeszcze {len(new_items) - MAX_ALERTS_PER_MESSAGE} kolejnych")

    return chr(10).join(lines)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID, pomijam wysyłkę.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = SESSION.post(url, json=payload, timeout=HTTP_TIMEOUT)
    response.raise_for_status()


def main() -> None:
    searches = load_config()
    state = load_state()

    seen_global: Set[str] = set(str(x) for x in state.get("seen_ids", []))
    seen_by_search: Dict[str, Set[str]] = {
        key: set(str(v) for v in values)
        for key, values in state.get("seen_by_search", {}).items()
    }

    enabled_searches = [s for s in searches if s.get("enabled", True)]
    print(f"[INFO] Loaded searches: {len(enabled_searches)}")

    all_alerts_sent = 0

    for search in enabled_searches:
        search_id = str(search.get("id") or "").strip()
        if not search_id:
            print("[WARN] Pomijam search bez id")
            continue

        print(f"[INFO] Running search: {search_id}")
        items = search_items(search)
        new_items = filter_new_items(search_id, items, seen_global, seen_by_search)

        print(f"[INFO] search={search_id} fetched={len(items)} new={len(new_items)}")

        if new_items:
            alert_text = format_alert(search, new_items)
            send_telegram_message(alert_text)
            all_alerts_sent += 1

        mark_items_seen(search_id, items, seen_global, seen_by_search)

    state["seen_ids"] = sorted(seen_global)
    state["seen_by_search"] = {k: sorted(v) for k, v in seen_by_search.items()}
    state["last_run_ts"] = int(time.time())
    save_state(state)

    print(f"[INFO] Done. Alerts sent: {all_alerts_sent}")


if __name__ == "__main__":
    main()