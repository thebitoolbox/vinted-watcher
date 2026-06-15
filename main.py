import html
import json
import os
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np
import open_clip
import torch
from PIL import Image
from curl_cffi import requests

CONFIG_PATH = Path("watch_config.json")
STATE_PATH = Path("seen_items.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MAX_ALERTS_PER_MESSAGE = int(os.getenv("MAX_ALERTS", "10"))

VINTED_BASE_URL = "https://www.vinted.pl"
VINTED_CATALOG_URL = "https://www.vinted.pl/api/v2/catalog/items"

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "ViT-B-32")
CLIP_PRETRAINED = os.getenv("CLIP_PRETRAINED", "laion2b_s34b_b79k")

MODEL, _, PREPROCESS = open_clip.create_model_and_transforms(
    CLIP_MODEL_NAME,
    pretrained=CLIP_PRETRAINED,
)
MODEL = MODEL.to(DEVICE).eval()

REFERENCE_EMBEDDINGS: Dict[str, List[np.ndarray]] = {}


def build_session() -> requests.Session:
    session = requests.Session(impersonate="chrome")
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.vinted.pl/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


SESSION = build_session()


def warmup_session() -> None:
    response = SESSION.get(VINTED_BASE_URL, timeout=HTTP_TIMEOUT)
    response.raise_for_status()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> Dict[str, Any]:
    payload = load_json(
        CONFIG_PATH,
        {
            "searches": [],
            "exclude_keywords": [],
        },
    )

    searches = payload.get("searches", [])
    if not isinstance(searches, list):
        raise ValueError("watch_config.json ma niepoprawny format: 'searches' musi być listą")

    exclude_keywords = payload.get("exclude_keywords", [])
    if not isinstance(exclude_keywords, list):
        raise ValueError("watch_config.json ma niepoprawny format: 'exclude_keywords' musi być listą")

    return payload


def load_state() -> Dict[str, Any]:
    return load_json(
        STATE_PATH,
        {
            "updated_at": 0,
            "seen_ids": [],
            "seen_by_search": {},
        },
    )


def save_state(state: Dict[str, Any]) -> None:
    state["seen_ids"] = sorted(list(set(state.get("seen_ids", []))))
    normalized_seen_by_search = {}

    for key, values in state.get("seen_by_search", {}).items():
        normalized_seen_by_search[key] = sorted(list(set(values)))

    state["seen_by_search"] = normalized_seen_by_search
    save_json(STATE_PATH, state)


def normalize_text(text: str) -> str:
    return str(text).casefold()


def load_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def build_params(search: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "search_text": keyword,
        "per_page": int(search.get("per_page") or 20),
        "page": 1,
        "order": search.get("order") or "newest_first",
    }

    if search.get("price_to") is not None:
        params["price_to"] = search["price_to"]

    if search.get("price_from") is not None:
        params["price_from"] = search["price_from"]

    if search.get("currency"):
        params["currency"] = search["currency"]

    brand_ids = search.get("brand_ids") or []
    if brand_ids:
        params["brand_ids[]"] = brand_ids

    catalog_ids = search.get("catalog_ids") or []
    if catalog_ids:
        params["catalog_ids[]"] = catalog_ids

    size_ids = search.get("size_ids") or []
    if size_ids:
        params["size_ids[]"] = size_ids

    status_ids = search.get("status_ids") or []
    if status_ids:
        params["status_ids[]"] = status_ids

    color_ids = search.get("color_ids") or []
    if color_ids:
        params["color_ids[]"] = color_ids

    material_ids = search.get("material_ids") or []
    if material_ids:
        params["material_ids[]"] = material_ids

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


def get_item_description(item: Dict[str, Any]) -> str:
    return str(
        item.get("description")
        or item.get("item_description")
        or item.get("brief_description")
        or ""
    )


def get_item_url(item: Dict[str, Any]) -> str:
    raw = str(item.get("url") or item.get("path") or "")

    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    if raw.startswith("/"):
        return f"{VINTED_BASE_URL}{raw}"

    return raw


def get_item_price_value(item: Dict[str, Any]) -> Optional[float]:
    price = item.get("price")

    if isinstance(price, dict):
        amount = price.get("amount") or price.get("value")
        try:
            return float(str(amount).replace(",", "."))
        except Exception:
            return None

    if price is not None:
        try:
            return float(str(price).replace(",", "."))
        except Exception:
            return None

    total_price = item.get("total_item_price") or item.get("price_numeric")
    if total_price is not None:
        try:
            return float(str(total_price).replace(",", "."))
        except Exception:
            return None

    return None


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


def get_item_image_url(item: Dict[str, Any]) -> str:
    photos = item.get("photos")
    if isinstance(photos, list) and photos:
        first = photos[0]
        if isinstance(first, dict):
            for key in ("url", "full_size_url", "large_url", "image_url"):
                value = first.get(key)
                if value:
                    return str(value)

    photo = item.get("photo")
    if isinstance(photo, dict):
        for key in ("url", "full_size_url", "large_url", "image_url"):
            value = photo.get(key)
            if value:
                return str(value)

    return ""


def should_exclude_item(item: Dict[str, Any], exclude_keywords: List[str]) -> bool:
    haystack = normalize_text(get_item_title(item) + " " + get_item_description(item))

    for keyword in exclude_keywords:
        keyword_norm = normalize_text(keyword)
        if keyword_norm and keyword_norm in haystack:
            return True

    return False


def matches_keywords(item: Dict[str, Any], keywords: List[str]) -> bool:
    haystack = normalize_text(get_item_title(item) + " " + get_item_description(item))

    for keyword in keywords:
        keyword_norm = normalize_text(keyword)
        if keyword_norm and keyword_norm in haystack:
            return True

    return False


def download_image(url: str) -> Optional[Image.Image]:
    if not url:
        return None

    try:
        response = SESSION.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        return image.convert("RGB")
    except Exception as exc:
        print(f"[WARN] image download failed url={url} err={exc}")
        return None


def encode_pil_image(image: Image.Image) -> np.ndarray:
    image_input = PREPROCESS(image).unsqueeze(0).to(DEVICE)

    with torch.no_grad():
        image_features = MODEL.encode_image(image_input)
        image_features /= image_features.norm(dim=-1, keepdim=True)

    return image_features[0].detach().cpu().numpy()


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def load_reference_embeddings(search: Dict[str, Any]) -> List[np.ndarray]:
    search_id = str(search.get("id") or "").strip()
    if not search_id:
        return []

    cached = REFERENCE_EMBEDDINGS.get(search_id)
    if cached is not None:
        return cached

    refs: List[np.ndarray] = []

    for path_str in load_string_list(search.get("reference_images", [])):
        path = Path(path_str)
        if not path.exists():
            print(f"[WARN] reference image not found: {path}")
            continue

        try:
            image = Image.open(path).convert("RGB")
            refs.append(encode_pil_image(image))
        except Exception as exc:
            print(f"[WARN] failed to load reference image {path}: {exc}")

    REFERENCE_EMBEDDINGS[search_id] = refs
    return refs


def image_similarity_score(item: Dict[str, Any], ref_embeddings: List[np.ndarray]) -> float:
    if not ref_embeddings:
        return 0.0

    image_url = get_item_image_url(item)
    image = download_image(image_url)
    if image is None:
        return 0.0

    try:
        item_embedding = encode_pil_image(image)
    except Exception as exc:
        print(f"[WARN] image encode failed url={image_url} err={exc}")
        return 0.0

    return max(cosine_similarity(item_embedding, ref) for ref in ref_embeddings)


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
            print(f"[DEBUG] search={search.get('id')} keyword={keyword!r} items={len(items)}")
        except Exception as exc:
            print(f"[WARN] search={search.get('id')} keyword={keyword!r} failed: {exc}")
            continue

        for item in items:
            item_id = get_item_id(item)
            if not item_id:
                continue
            if item_id in seen_local:
                continue
            seen_local.add(item_id)
            results.append(item)

        time.sleep(float(search.get("request_delay") or 0.8))

    if deduped_keywords and search.get("require_keyword_match", True):
        before_count = len(results)
        results = [item for item in results if matches_keywords(item, deduped_keywords)]
        removed_count = before_count - len(results)
        if removed_count > 0:
            print(f"[INFO] search={search.get('id')} keyword_filter_removed={removed_count}")

    return results


def filter_by_price(search: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    min_price = search.get("min_price")
    if min_price is None:
        return items

    kept: List[Dict[str, Any]] = []
    removed = 0

    for item in items:
        price_value = get_item_price_value(item)
        if price_value is None or price_value >= float(min_price):
            kept.append(item)
        else:
            removed += 1

    if removed > 0:
        print(f"[INFO] search={search.get('id')} min_price_removed={removed}")

    return kept


def apply_image_filter(search: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ref_embeddings = load_reference_embeddings(search)
    if not ref_embeddings:
        return items

    threshold = float(search.get("image_similarity_threshold") or 0)
    if threshold <= 0:
        return items

    manual_keywords = load_string_list(search.get("manual_keywords", []))
    base_query = str(search.get("base_query") or "").strip()
    if base_query:
        manual_keywords = [base_query] + manual_keywords

    kept: List[Dict[str, Any]] = []
    scored = 0

    for item in items:
        text_match = matches_keywords(item, manual_keywords) if manual_keywords else False
        score = image_similarity_score(item, ref_embeddings)
        item["_image_similarity"] = score
        scored += 1

        if text_match or score >= threshold:
            kept.append(item)

    print(
        f"[INFO] search={search.get('id')} image_scored={scored} kept={len(kept)} threshold={threshold}"
    )
    return kept


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

    parts: List[str] = []
    parts.append("🔔 <b>" + search_name + "</b>")
    parts.append("Nowe oferty: <b>" + str(len(new_items)) + "</b>")
    parts.append("")

    for item in new_items[:MAX_ALERTS_PER_MESSAGE]:
        title = html.escape(get_item_title(item))
        price = html.escape(get_item_price(item))
        url = html.escape(get_item_url(item))
        image_score = item.get("_image_similarity")

        if isinstance(image_score, (float, int)) and image_score > 0:
            parts.append(
                "• <b>" + title + "</b> — " + price + f" (img score: {float(image_score):.3f})"
            )
        else:
            parts.append("• <b>" + title + "</b> — " + price)

        parts.append(url)
        parts.append("")

    if len(new_items) > MAX_ALERTS_PER_MESSAGE:
        extra_count = len(new_items) - MAX_ALERTS_PER_MESSAGE
        parts.append("")
        parts.append("... i jeszcze " + str(extra_count) + " kolejnych")

    return chr(10).join(parts)


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
    warmup_session()

    config = load_config()
    searches = config.get("searches", [])
    global_exclude_keywords = load_string_list(config.get("exclude_keywords", []))

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

        per_search_exclude = load_string_list(search.get("exclude_keywords", []) or [])
        exclude_keywords = global_exclude_keywords + per_search_exclude

        items = search_items(search)
        items = filter_by_price(search, items)

        if exclude_keywords:
            before_count = len(items)
            items = [item for item in items if not should_exclude_item(item, exclude_keywords)]
            filtered_out = before_count - len(items)
            if filtered_out > 0:
                print(f"[INFO] search={search_id} excluded={filtered_out}")

        if search.get("reference_images"):
            items = apply_image_filter(search, items)

        new_items = filter_new_items(search_id, items, seen_global, seen_by_search)

        print(f"[INFO] search={search_id} fetched={len(items)} new={len(new_items)}")

        if new_items:
            alert_text = format_alert(search, new_items)
            send_telegram_message(alert_text)
            all_alerts_sent += 1

        mark_items_seen(search_id, items, seen_global, seen_by_search)

    state["seen_ids"] = sorted(seen_global)
    state["seen_by_search"] = {k: sorted(v) for k, v in seen_by_search.items()}
    state["updated_at"] = int(time.time())
    save_state(state)

    print(f"[INFO] Done. Alerts sent: {all_alerts_sent}")


if __name__ == "__main__":
    main()
