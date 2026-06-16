import html
import json
import os
import time
import unicodedata
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Set

import numpy as np
import open_clip
import torch
from PIL import Image
from curl_cffi import requests
from deep_translator import GoogleTranslator
from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0

CONFIG_PATH = Path("watch_config.json")
STATE_PATH = Path("seen_items.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
TELEGRAM_TIMEOUT = int(os.getenv("TELEGRAM_TIMEOUT", "60"))
MAX_ALERTS_PER_MESSAGE = int(os.getenv("MAX_ALERTS", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0"))

TRANSLATE_TARGET_LANG = os.getenv("TRANSLATE_TARGET_LANG", "en").strip() or "en"
TRANSLATE_MAX_TEXT_LEN = int(os.getenv("TRANSLATE_MAX_TEXT_LEN", "1200"))
SKIP_TRANSLATE_LANGS = {"pl", "en"}

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
TIMINGS: Dict[str, float] = {}
COUNTERS: Dict[str, int] = {}


def add_timing(name: str, started_at: float) -> None:
    TIMINGS[name] = TIMINGS.get(name, 0.0) + (perf_counter() - started_at)


def inc_counter(name: str, value: int = 1) -> None:
    COUNTERS[name] = COUNTERS.get(name, 0) + value


def log_timing_summary() -> None:
    print("[TIMING] ---- summary ----")
    total = sum(TIMINGS.values())

    for key, value in sorted(TIMINGS.items(), key=lambda x: x[1], reverse=True):
        pct = (value / total * 100) if total > 0 else 0
        print(f"[TIMING] {key}={value:.2f}s ({pct:.1f}%)")

    print(f"[TIMING] total_measured={total:.2f}s")

    if COUNTERS:
        print("[TIMING] ---- counters ----")
        for key, value in sorted(COUNTERS.items()):
            print(f"[TIMING] {key}={value}")


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
    started = perf_counter()
    response = SESSION.get(VINTED_BASE_URL, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    add_timing("warmup_session", started)
    inc_counter("http_requests_total")


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


def load_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    text = str(text or "")
    text = unicodedata.normalize("NFKC", text)
    text = strip_accents(text)
    text = text.casefold()
    text = " ".join(text.split())
    return text.strip()


@lru_cache(maxsize=5000)
def detect_language_cached(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized or len(normalized) < 8:
        return "unknown"

    try:
        return detect(normalized)
    except LangDetectException:
        return "unknown"
    except Exception as exc:
        print(f"[WARN] language detection failed: {exc}")
        return "unknown"


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


def build_item_text(item: Dict[str, Any]) -> str:
    return (get_item_title(item) + " " + get_item_description(item)).strip()


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


@lru_cache(maxsize=5000)
def translate_text_cached(text: str, target_lang: str) -> str:
    text = normalize_text(text)
    if not text:
        return ""

    clipped = text[:TRANSLATE_MAX_TEXT_LEN]
    started = perf_counter()
    inc_counter("translate_calls_total")

    try:
        translator = GoogleTranslator(source="auto", target=target_lang)
        translated = translator.translate(text=clipped)
        return str(translated or "").strip()
    except Exception as exc:
        print(f"[WARN] translation failed: {exc}")
        return clipped
    finally:
        add_timing("translate_text", started)


def should_try_translation(search: Dict[str, Any]) -> bool:
    if search.get("translate", False):
        return True
    languages = load_string_list(search.get("languages", []))
    return len(languages) > 0


def get_translated_item_text(search: Dict[str, Any], item: Dict[str, Any]) -> str:
    if not should_try_translation(search):
        return ""

    cached = str(item.get("_translated_text") or "").strip()
    if cached:
        return cached

    raw_text = build_item_text(item)
    if not raw_text.strip():
        return ""

    detected_lang = detect_language_cached(raw_text)
    item["_detected_lang"] = detected_lang

    if detected_lang in SKIP_TRANSLATE_LANGS:
        item["_translated_text"] = raw_text
        return raw_text

    translated = translate_text_cached(raw_text, TRANSLATE_TARGET_LANG)
    if translated:
        item["_translated_text"] = translated
    return translated


def matches_keywords_in_text(text: str, keywords: List[str]) -> bool:
    haystack = normalize_text(text)

    for keyword in keywords:
        keyword_norm = normalize_text(keyword)
        if keyword_norm and keyword_norm in haystack:
            return True

    return False


def should_exclude_item(search: Dict[str, Any], item: Dict[str, Any], exclude_keywords: List[str]) -> bool:
    raw_text = build_item_text(item)

    if matches_keywords_in_text(raw_text, exclude_keywords):
        return True

    translated_text = get_translated_item_text(search, item)
    if translated_text and matches_keywords_in_text(translated_text, exclude_keywords):
        return True

    return False


def matches_keywords(search: Dict[str, Any], item: Dict[str, Any], keywords: List[str]) -> bool:
    raw_text = build_item_text(item)

    if matches_keywords_in_text(raw_text, keywords):
        return True

    translated_text = get_translated_item_text(search, item)
    if translated_text and matches_keywords_in_text(translated_text, keywords):
        return True

    return False


def download_image(url: str) -> Optional[Image.Image]:
    if not url:
        return None

    started = perf_counter()
    inc_counter("http_requests_total")
    inc_counter("image_download_calls_total")

    try:
        response = SESSION.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        return image.convert("RGB")
    except Exception as exc:
        print(f"[WARN] image download failed url={url} err={exc}")
        return None
    finally:
        add_timing("download_image", started)


def encode_pil_image(image: Image.Image) -> np.ndarray:
    started = perf_counter()
    image_input = PREPROCESS(image).unsqueeze(0).to(DEVICE)

    with torch.no_grad():
        image_features = MODEL.encode_image(image_input)
        image_features /= image_features.norm(dim=-1, keepdim=True)

    add_timing("encode_image", started)
    inc_counter("encode_image_calls_total")
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

    started = perf_counter()
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
    add_timing(f"load_reference_embeddings:{search_id}", started)
    return refs


def image_similarity_score(item: Dict[str, Any], ref_embeddings: List[np.ndarray]) -> float:
    if not ref_embeddings:
        return 0.0

    started = perf_counter()
    image_url = get_item_image_url(item)
    image = download_image(image_url)
    if image is None:
        add_timing("image_similarity_score", started)
        return 0.0

    try:
        item_embedding = encode_pil_image(image)
        score = max(cosine_similarity(item_embedding, ref) for ref in ref_embeddings)
        return score
    except Exception as exc:
        print(f"[WARN] image encode failed url={image_url} err={exc}")
        return 0.0
    finally:
        add_timing("image_similarity_score", started)
        inc_counter("image_similarity_calls_total")


def get_retry_delay(attempt: int, base_delay: float = 1.0) -> float:
    return base_delay * (2 ** (attempt - 1))


def fetch_items_for_keyword(search: Dict[str, Any], keyword: str) -> List[Dict[str, Any]]:
    params = build_params(search, keyword)
    retries = int(search.get("retries") or 3)
    base_delay = float(search.get("retry_delay") or 1.0)
    last_exc: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        started = perf_counter()
        inc_counter("http_requests_total")
        inc_counter("vinted_catalog_calls_total")

        try:
            response = SESSION.get(VINTED_CATALOG_URL, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            add_timing("http_vinted_catalog", started)
            return extract_items(payload)
        except Exception as exc:
            add_timing("http_vinted_catalog", started)
            last_exc = exc
            print(
                f"[WARN] search={search.get('id')} keyword={keyword!r} "
                f"attempt={attempt}/{retries} failed: {exc}"
            )

            if attempt < retries:
                delay = get_retry_delay(attempt, base_delay)
                print(f"[INFO] retrying in {delay:.1f}s")
                time.sleep(delay)
                TIMINGS["request_sleep"] = TIMINGS.get("request_sleep", 0.0) + delay

    if last_exc:
        raise last_exc

    return []


def get_search_keywords(search: Dict[str, Any]) -> List[str]:
    keywords: List[str] = []

    base_query = str(search.get("base_query") or "").strip()
    if base_query:
        keywords.append(base_query)

    for keyword in load_string_list(search.get("manual_keywords", [])):
        keywords.append(keyword)

    deduped_keywords: List[str] = []
    seen_keywords: Set[str] = set()

    for keyword in keywords:
        normalized = normalize_text(keyword)
        if not normalized:
            continue
        if normalized in seen_keywords:
            continue
        seen_keywords.add(normalized)
        deduped_keywords.append(keyword)

    return deduped_keywords


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


def search_items(search: Dict[str, Any]) -> List[Dict[str, Any]]:
    started = perf_counter()
    results: List[Dict[str, Any]] = []
    seen_local: Set[str] = set()
    deduped_keywords = get_search_keywords(search)

    for keyword in deduped_keywords:
        keyword_started = perf_counter()

        try:
            items = fetch_items_for_keyword(search, keyword)
            print(f"[DEBUG] search={search.get('id')} keyword={keyword!r} items={len(items)}")
        except Exception as exc:
            print(f"[WARN] search={search.get('id')} keyword={keyword!r} failed: {exc}")
            add_timing(f"search_keyword:{search.get('id')}:{keyword}", keyword_started)
            continue

        for item in items:
            item_id = get_item_id(item)
            if not item_id:
                continue
            if item_id in seen_local:
                continue
            seen_local.add(item_id)
            results.append(item)

        if REQUEST_DELAY > 0:
            time.sleep(REQUEST_DELAY)
            TIMINGS["request_sleep"] = TIMINGS.get("request_sleep", 0.0) + REQUEST_DELAY

        add_timing(f"search_keyword:{search.get('id')}:{keyword}", keyword_started)

    results = filter_by_price(search, results)

    if deduped_keywords and search.get("require_keyword_match", True):
        filter_started = perf_counter()
        before_count = len(results)
        matched: List[Dict[str, Any]] = []

        for item in results:
            raw_text = build_item_text(item)

            if matches_keywords_in_text(raw_text, deduped_keywords):
                matched.append(item)
                continue

            translated_text = get_translated_item_text(search, item)
            if translated_text and matches_keywords_in_text(translated_text, deduped_keywords):
                matched.append(item)

        results = matched
        removed_count = before_count - len(results)
        if removed_count > 0:
            print(f"[INFO] search={search.get('id')} keyword_filter_removed={removed_count}")

        add_timing(f"keyword_match_filter:{search.get('id')}", filter_started)

    add_timing(f"search_items:{search.get('id')}", started)
    return results


def apply_image_filter(search: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    started = perf_counter()
    ref_embeddings = load_reference_embeddings(search)
    if not ref_embeddings:
        add_timing(f"apply_image_filter:{search.get('id')}", started)
        return items

    threshold = float(search.get("image_similarity_threshold") or 0)
    if threshold <= 0:
        add_timing(f"apply_image_filter:{search.get('id')}", started)
        return items

    manual_keywords = get_search_keywords(search)

    kept: List[Dict[str, Any]] = []
    scored = 0

    for item in items:
        text_match = matches_keywords(search, item, manual_keywords) if manual_keywords else False
        score = image_similarity_score(item, ref_embeddings)
        item["_image_similarity"] = score
        scored += 1

        if text_match or score >= threshold:
            kept.append(item)

    print(
        f"[INFO] search={search.get('id')} image_scored={scored} kept={len(kept)} threshold={threshold}"
    )
    add_timing(f"apply_image_filter:{search.get('id')}", started)
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

        line = "• <b>" + title + "</b> — " + price
        if isinstance(image_score, (float, int)) and image_score > 0:
            line += f" (img score: {float(image_score):.3f})"
        parts.append(line)

        translated_text = str(item.get("_translated_text") or "").strip()
        if translated_text and normalize_text(translated_text) != normalize_text(build_item_text(item)):
            translated_short = html.escape(translated_text[:220])
            parts.append("<i>Tłumaczenie:</i> " + translated_short)

        parts.append(url)
        parts.append("")

    if len(new_items) > MAX_ALERTS_PER_MESSAGE:
        extra_count = len(new_items) - MAX_ALERTS_PER_MESSAGE
        parts.append("")
        parts.append("... i jeszcze " + str(extra_count) + " kolejnych")

    return chr(10).join(parts)


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Brak TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID, pomijam wysyłkę.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    retries = 2

    for attempt in range(1, retries + 1):
        started = perf_counter()
        inc_counter("http_requests_total")
        inc_counter("telegram_calls_total")

        try:
            response = SESSION.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
            response.raise_for_status()
            add_timing("telegram_send", started)
            print(f"[INFO] Telegram sent on attempt {attempt}")
            return True
        except Exception as exc:
            add_timing("telegram_send", started)
            print(f"[WARN] Telegram send failed attempt={attempt}/{retries} err={exc}")

            if attempt < retries:
                delay = get_retry_delay(attempt, 2.0)
                print(f"[INFO] Retrying Telegram in {delay:.1f}s")
                time.sleep(delay)
                TIMINGS["request_sleep"] = TIMINGS.get("request_sleep", 0.0) + delay

    print("[ERROR] Telegram message not sent after retries")
    return False


def main() -> None:
    total_started = perf_counter()

    bootstrap_started = perf_counter()
    warmup_session()
    config = load_config()
    searches = config.get("searches", [])
    global_exclude_keywords = load_string_list(config.get("exclude_keywords", []))
    add_timing("bootstrap", bootstrap_started)

    state_started = perf_counter()
    state = load_state()
    add_timing("load_state", state_started)

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

        search_started = perf_counter()
        print(f"[INFO] Running search: {search_id}")

        per_search_exclude = load_string_list(search.get("exclude_keywords", []) or [])
        exclude_keywords = global_exclude_keywords + per_search_exclude

        step_started = perf_counter()
        items = search_items(search)
        add_timing(f"step_search_items:{search_id}", step_started)

        if exclude_keywords:
            step_started = perf_counter()
            before_count = len(items)
            items = [item for item in items if not should_exclude_item(search, item, exclude_keywords)]
            filtered_out = before_count - len(items)
            if filtered_out > 0:
                print(f"[INFO] search={search_id} excluded={filtered_out}")
            add_timing(f"step_exclude_filter:{search_id}", step_started)

        if search.get("reference_images"):
            step_started = perf_counter()
            items = apply_image_filter(search, items)
            add_timing(f"step_image_filter:{search_id}", step_started)

        step_started = perf_counter()
        new_items = filter_new_items(search_id, items, seen_global, seen_by_search)
        add_timing(f"step_filter_new:{search_id}", step_started)

        print(f"[INFO] search={search_id} fetched={len(items)} new={len(new_items)}")

        if new_items:
            step_started = perf_counter()
            alert_text = format_alert(search, new_items)
            sent = send_telegram_message(alert_text)
            if sent:
                all_alerts_sent += 1
            else:
                print(f"[WARN] search={search_id} telegram_send_failed")
            add_timing(f"step_alerting:{search_id}", step_started)

        step_started = perf_counter()
        mark_items_seen(search_id, items, seen_global, seen_by_search)
        add_timing(f"step_mark_seen:{search_id}", step_started)

        add_timing(f"full_search:{search_id}", search_started)

    save_started = perf_counter()
    state["seen_ids"] = sorted(seen_global)
    state["seen_by_search"] = {k: sorted(v) for k, v in seen_by_search.items()}
    state["updated_at"] = int(time.time())
    save_state(state)
    add_timing("save_state", save_started)

    add_timing("main_total", total_started)
    print(f"[INFO] Done. Alerts sent: {all_alerts_sent}")
    log_timing_summary()


if __name__ == "__main__":
    main()
