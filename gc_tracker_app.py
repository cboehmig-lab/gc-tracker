#!/usr/bin/env python3
"""
Guitar Center Used Inventory Tracker — Web App
------------------------------------------------
Run with:  python3 gc_tracker_app.py
Then open: http://localhost:5050
"""

import json, os, re, sys, time, threading, queue, webbrowser, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from pathlib import Path


def _sleep(base: float, jitter: float = 0.5):
    """Sleep for base ± jitter seconds to avoid looking like a bot."""
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))

try:
    from flask import (Flask, request, jsonify, Response, stream_with_context,
                       session, redirect, send_file)
except ImportError:
    sys.exit("Missing Flask. Run:  pip3 install flask requests openpyxl")

try:
    import requests as http
except ImportError:
    sys.exit("Missing requests. Run:  pip3 install flask requests openpyxl")

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("Missing openpyxl. Run:  pip3 install openpyxl")

# ── Paths & config ────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
DATA_DIR       = Path(os.environ.get("DATA_DIR", SCRIPT_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE     = DATA_DIR / "gc_state.json"
OUTPUT_FILE    = DATA_DIR / "gc_new_inventory.xlsx"
STORES_CACHE   = DATA_DIR / "gc_stores_cache.json"
FAVORITES_FILE = DATA_DIR / "gc_favorites.json"
CAT_CACHE_FILE = DATA_DIR / "gc_category_cache.json"
WATCHLIST_FILE = DATA_DIR / "gc_watchlist.json"

PORT        = int(os.environ.get("PORT", 5050))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# ── HTTP session ──────────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_HEADERS = {
    "User-Agent":                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Cache-Control":             "max-age=0",
}

_http = http.Session()
_http.headers.update(_HEADERS)

def _rotate_ua():
    """Pick a random User-Agent for the next request."""
    _http.headers["User-Agent"] = random.choice(_USER_AGENTS)

# ── Category cache ────────────────────────────────────────────────────────────
_cat_cache: dict = {}

def _load_cat_cache():
    global _cat_cache
    if CAT_CACHE_FILE.exists():
        try:
            _cat_cache = json.loads(CAT_CACHE_FILE.read_text())
        except Exception:
            _cat_cache = {}

def _save_cat_cache():
    try:
        CAT_CACHE_FILE.write_text(json.dumps(_cat_cache))
    except Exception:
        pass

# ── Store list ────────────────────────────────────────────────────────────────

FALLBACK_STORES: list[str] = []  # Populated from GC live data via Validate Stores


def get_store_list() -> list[str]:
    cached = []
    if STORES_CACHE.exists():
        try:
            cached = json.loads(STORES_CACHE.read_text()).get("stores", [])
        except Exception:
            pass
    blocklist = _get_blocklist()
    return sorted(set(cached) - blocklist)


_US_STATES = [
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc",
]

def _fetch_state_stores(state: str) -> list[str]:
    """Fetch store city names for a single state from stores.guitarcenter.com.
    Only extracts names from confirmed store-page URLs matching /{state}/{city}/{id},
    which is the only reliable signal — headings and link text pick up nav garbage."""
    try:
        r = _http.get(f"https://stores.guitarcenter.com/{state}/", timeout=10)
        if r.status_code != 200:
            return []
        html = r.text
        # Only trust URLs in the form /state/city-slug/numeric-id
        # These uniquely identify actual store pages; nav links never match this pattern.
        slug_to_name = {}
        for slug in re.findall(
            rf'href="/{re.escape(state)}/([a-z][a-z0-9\-]+)/(\d+)(?:/[^"]*)?"',
            html
        ):
            city_slug, store_id = slug
            # Convert slug to display name: "south-austin" → "South Austin"
            name = " ".join(w.capitalize() for w in city_slug.split("-"))
            slug_to_name[city_slug] = name
        return list(slug_to_name.values())
    except Exception:
        return []


def _extract_stores_from_used_page(html: str) -> list[str]:
    """Extract all store names from GC's used inventory page filter facets.
    The __NEXT_DATA__ blob or page HTML contains the complete list of valid store
    names exactly as the filters=stores: parameter expects them."""
    stores = []

    # Strategy 1: __NEXT_DATA__ — find facet values for the 'stores' facet
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            nd = json.loads(m.group(1))
            # Walk looking for arrays of facet values near a 'stores' key
            def find_store_facets(obj, depth=0):
                if depth > 12: return
                if isinstance(obj, dict):
                    # Look for facet arrays keyed by 'stores' or containing store-like values
                    for k, v in obj.items():
                        if k.lower() in ('stores', 'store') and isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    val = item.get('displayValue') or item.get('value') or item.get('name') or ''
                                elif isinstance(item, str):
                                    val = item
                                else:
                                    continue
                                if isinstance(val, str) and 2 < len(val) < 60:
                                    stores.append(val)
                        elif isinstance(v, (dict, list)):
                            find_store_facets(v, depth + 1)
                elif isinstance(obj, list):
                    for item in obj:
                        find_store_facets(item, depth + 1)
            find_store_facets(nd)
        except Exception:
            pass

    # Strategy 2: look for displayValue patterns near "stores" in the raw JSON
    if len(stores) < 10:
        # Find JSON arrays that look like store facets
        for m2 in re.finditer(r'"(?:stores|store)"[^[]*(\[[^\]]{100,}\])', html, re.DOTALL):
            try:
                arr = json.loads(m2.group(1))
                for item in arr:
                    if isinstance(item, dict):
                        val = item.get('displayValue') or item.get('value') or ''
                        if isinstance(val, str) and 2 < len(val) < 60:
                            stores.append(val)
            except Exception:
                pass

    return stores


def refresh_store_list(send_progress=None) -> list[str]:
    """Fetch authoritative store list from GC's used inventory page filter facets,
    then fall back to state-by-state scraping. Removes blocklisted stores."""
    live_names = []

    # Strategy 1: fetch GC's used inventory page and extract store names from filter facets
    # This is the gold standard — these are the exact names the filter system accepts
    try:
        r = _http.get("https://www.guitarcenter.com/Used/", timeout=20)
        if r.status_code == 200:
            live_names = _extract_stores_from_used_page(r.text)
    except Exception:
        pass

    # Strategy 2: scrape stores.guitarcenter.com state by state in parallel
    if len(live_names) < 50:
        try:
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch_state_stores, st): st for st in _US_STATES}
                for future in as_completed(futures):
                    try:
                        live_names.extend(future.result())
                    except Exception:
                        pass
        except Exception:
            pass

    # Strategy 3: main stores page URL pattern
    if len(live_names) < 20:
        try:
            r = _http.get("https://www.guitarcenter.com/Stores/", timeout=15)
            r.raise_for_status()
            html = r.text
            for slug in re.findall(r'href="https?://stores\.guitarcenter\.com/([a-z]{2})/([a-z][a-z0-9\-]+)/(\d+)"', html):
                _, city_slug, _ = slug
                name = " ".join(w.capitalize() for w in city_slug.split("-"))
                live_names.append(name)
        except Exception:
            pass

    # Strip nav garbage
    _NAV_GARBAGE = {
        "find your local guitar center store", "my account", "sign in", "track order",
        "returns", "faqs", "store locator", "guitar center lessons", "guitar center",
        "home", "shop all", "new arrivals", "top sellers", "on sale", "price drop",
        "used", "vintage", "sell your gear", "financing", "outlet", "deals",
        "daily pick", "gc pro", "lessons", "repairs", "rentals", "riffs blog",
        "accessibility statement", "privacy policy", "terms of use", "site map",
        "careers", "about", "contact us", "press room", "service", "support",
        "all rights reserved", "california transparency", "do not sell",
    }
    live_names = [
        n for n in live_names
        if n.strip().lower() not in _NAV_GARBAGE
        and len(n.strip()) >= 3
        and not any(bad in n.lower() for bad in ("guitar center", "my account", "sign in",
                                                   "track order", "©", "all rights"))
    ]

    blocklist = _get_blocklist()
    merged = sorted(set(live_names) - blocklist)
    STORES_CACHE.write_text(json.dumps({
        "stores":      merged,
        "live_count":  len(set(live_names)),
        "updated":     datetime.now().isoformat(),
    }))
    return merged


def get_store_info() -> dict:
    """Return metadata about the store list (count, last updated, live vs fallback)."""
    if STORES_CACHE.exists():
        try:
            d = json.loads(STORES_CACHE.read_text())
            return {
                "count":      len(d.get("stores", [])),
                "live_count": d.get("live_count", 0),
                "updated":    d.get("updated", ""),
            }
        except Exception:
            pass
    return {"count": len(FALLBACK_STORES), "live_count": 0, "updated": ""}


# ── Favorites ─────────────────────────────────────────────────────────────────

def load_favorites() -> list[str]:
    if FAVORITES_FILE.exists():
        try:
            return json.loads(FAVORITES_FILE.read_text())
        except Exception:
            pass
    return []


def save_favorites(favs: list[str]):
    FAVORITES_FILE.write_text(json.dumps(sorted(set(favs))))


def load_watchlist() -> dict:
    """Returns {sku: {name, price, store, url, condition, category, date_added, sold}}"""
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    return {}


def save_watchlist(wl: dict):
    WATCHLIST_FILE.write_text(json.dumps(wl, indent=2))


# ── GC scraping ───────────────────────────────────────────────────────────────

PAGE_SIZE = 24

def _fmt_date(d: str) -> str:
    """Convert YYYY-MM-DD to M/D/YY."""
    try:
        from datetime import date
        dt = date.fromisoformat(d[:10])
        return f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        return d


def _clean_name(name: str) -> str:
    """Strip redundant 'Used ' prefix from item names."""
    name = name.strip()
    if name.lower().startswith("used "):
        name = name[5:].strip()
    return name


def fetch_page(store_name: str, page: int) -> str:
    _rotate_ua()
    query = f"filters=stores:{store_name.replace(' ', '%20')}"
    url   = f"https://www.guitarcenter.com/Used/?{query}&page={page}"
    r = _http.get(url, timeout=20)
    r.raise_for_status()
    # Save first page of first store as debug dump for condition pattern inspection
    debug_file = DATA_DIR / "gc_debug_listing.html"
    if page == 1 and not debug_file.exists():
        debug_file.write_text(r.text)
    return r.text


_CONDITION_MAP = {
    "new":          "New",
    "likenew":      "Like New",
    "excellent":    "Excellent",
    "great":        "Great",
    "verygood":     "Very Good",
    "good":         "Good",
    "fair":         "Fair",
    "poor":         "Poor",
    "usedcondition":"Used",
    "refurbished":  "Refurbished",
    "blemished":    "Blemished",
}

def _parse_condition(raw: str) -> str:
    """Normalise a schema.org itemCondition URL or plain text to a readable label."""
    if not raw:
        return ""
    # Strip schema.org URL prefix, e.g. "https://schema.org/GoodCondition" → "GoodCondition"
    key = raw.split("/")[-1].lower().replace("condition", "").replace(" ", "").replace("-", "")
    return _CONDITION_MAP.get(key, raw.split("/")[-1])  # fall back to raw tail if unknown


def _extract_conditions_from_listing(html: str) -> dict:
    """
    Build a map of {url → condition} from the GC 24-item listing page.
    Card structure (from screenshot):
      Available at: City, ST
      Condition: Good
    Condition comes AFTER 'Available at:' — look forward within 200 chars.
    """
    # 1. Extract ordered URLs from JSON-LD
    urls = []
    ld_end = 0
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            if data.get("@type") == "CollectionPage":
                for entry in data.get("mainEntity", {}).get("itemListElement", []):
                    url = entry.get("item", {}).get("url", "").split("?")[0]
                    if url:
                        urls.append(url)
                ld_end = m.end()
                break
        except Exception:
            pass

    if not urls or not ld_end:
        return {}

    card_html = html[ld_end:]

    cond_re = re.compile(
        r'Condition:\s*(?:<!--[^>]*>\s*)*([A-Z][A-Za-z ]{1,19}?)(?:\s*[<\n\r])',
        re.DOTALL
    )

    # Find all "Available at:" — one per card, condition follows within ~200 chars
    avail_anchors = [m.start() for m in re.finditer(r'Available\s*at:', card_html)]

    conditions = []
    if len(avail_anchors) >= len(urls):
        for anchor_pos in avail_anchors[:len(urls)]:
            # Look FORWARD up to 200 chars after "Available at:" for the condition
            chunk = card_html[anchor_pos:anchor_pos + 200]
            m = cond_re.search(chunk)
            conditions.append(m.group(1).strip() if m else "")
    else:
        # Fallback: positional scan after ld_end
        for m in cond_re.finditer(card_html):
            conditions.append(m.group(1).strip())
            if len(conditions) == len(urls):
                break

    # Diagnostics
    try:
        # Show what's after the first Available at:
        after_sample = ""
        if avail_anchors:
            after_sample = card_html[avail_anchors[0]:avail_anchors[0]+200].replace("\n", "\\n")
        (DATA_DIR / "gc_condition_diag.json").write_text(json.dumps({
            "generated": datetime.now().isoformat(),
            "url_count": len(urls),
            "avail_anchor_count": len(avail_anchors),
            "conditions_found": sum(1 for c in conditions if c),
            "sample_conditions": conditions[:5],
            "html_after_first_available_at": after_sample,
        }, indent=2))
    except Exception:
        pass

    return {url: cond for url, cond in zip(urls, conditions) if cond}


def parse_products(html: str, store_name: str) -> list[dict]:
    # Pre-scan the listing HTML for per-item conditions (visible text on product cards)
    condition_map = _extract_conditions_from_listing(html)

    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if data.get("@type") != "CollectionPage":
            continue
        items = data.get("mainEntity", {}).get("itemListElement", [])
        if not items:
            continue
        products = []
        for entry in items:
            item = entry.get("item", {})
            name = _clean_name(item.get("name", ""))
            sku  = item.get("sku",  "").strip()
            url  = item.get("url",  "").strip()
            offers = item.get("offers", {})
            raw  = offers.get("price", "")
            try:    price = float(raw) if raw else None
            except: price = None
            # Prefer condition from the visible card HTML; fall back to JSON-LD itemCondition
            url_key = url.split("?")[0]
            condition = condition_map.get(url_key, "")
            if not condition:
                raw_cond = offers.get("itemCondition", "")
                parsed = _parse_condition(raw_cond)
                # Don't use the generic "Used" fallback — it's meaningless
                condition = parsed if parsed.lower() not in ("used", "") else ""
            if name and sku:
                products.append({"id": sku, "name": name, "price": price,
                                  "store": store_name, "url": url,
                                  "condition": condition})
        return products
    return []



def _clean_gc_cat(s: str) -> str:
    """Strip 'Used ' prefix from GC category breadcrumb names."""
    s = s.strip()
    if s.lower().startswith("used "):
        s = s[5:].strip()
    return s


def _find_breadcrumbs_in_json(data, depth: int = 0):
    """Recursively search for a breadcrumb array inside __NEXT_DATA__ JSON."""
    if depth > 8:
        return None
    if isinstance(data, dict):
        for key in ("breadcrumbs", "breadcrumb", "breadCrumbs", "Breadcrumbs",
                    "crumbs", "navCrumbs", "categoryPath", "categories"):
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 2:
                name_keys = ("name", "displayName", "label", "text", "title")
                if all(isinstance(v, dict) and any(k in v for k in name_keys) for v in val):
                    return val
        for v in data.values():
            if isinstance(v, (dict, list)):
                result = _find_breadcrumbs_in_json(v, depth + 1)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                result = _find_breadcrumbs_in_json(item, depth + 1)
                if result:
                    return result
    return None


def _extract_condition_from_html(html: str) -> str:
    """Extract condition label from a GC page (listing or product page).
    Prioritises the visible 'Condition: X' text that appears on both page types."""

    _VALID = {"new", "like new", "excellent", "great", "very good", "good", "fair", "poor",
              "blemished", "refurbished", "used"}

    # Strategy A: plain visible text — "Condition: Good" / "Condition: Very Good"
    # This is the most reliable; it's what the shopper sees on the page.
    m = re.search(r'[Cc]ondition\s*[:\-–]\s*([A-Za-z][A-Za-z\s]{1,20}?)(?:\s*[<\n\r,]|$)', html)
    if m:
        val = m.group(1).strip().rstrip(".,;")
        if val.lower() in _VALID:
            return val.title()

    # Strategy B: JSON-LD itemCondition on a Product page
    for block in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            d = json.loads(block)
            offers = None
            if d.get("@type") == "Product":
                offers = d.get("offers", {})
            elif d.get("@type") == "CollectionPage":
                items = d.get("mainEntity", {}).get("itemListElement", [])
                if items:
                    offers = items[0].get("item", {}).get("offers", {})
            if offers:
                raw = offers.get("itemCondition", "")
                if raw:
                    parsed = _parse_condition(raw)
                    if parsed.lower() not in ("used", "usedcondition") and parsed:
                        return parsed
        except Exception:
            pass

    # Strategy C: __NEXT_DATA__ JSON — look for condition keys
    m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m2:
        try:
            nd = json.loads(m2.group(1))
            cond = _find_key_in_json(nd, ("conditionDisplayName", "usedCondition",
                                          "productCondition", "itemCondition", "condition"))
            if cond and str(cond).lower() in _VALID:
                return str(cond).strip().title()
        except Exception:
            pass

    # Strategy D: data attributes / inline JSON strings
    for pat in [
        r'data-condition="([^"]+)"',
        r'"conditionDisplayName"\s*:\s*"([^"]+)"',
        r'"usedCondition"\s*:\s*"([^"]+)"',
    ]:
        m3 = re.search(pat, html)
        if m3:
            val = m3.group(1).strip()
            if val.lower() in _VALID:
                return val.title()

    return ""


def _find_key_in_json(data, keys: tuple, depth: int = 0):
    """Recursively search a JSON structure for any of the given keys."""
    if depth > 10:
        return None
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], str) and data[k]:
                return data[k]
        for v in data.values():
            result = _find_key_in_json(v, keys, depth + 1)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_key_in_json(item, keys, depth + 1)
            if result:
                return result
    return None


def fetch_page_data(url: str, name: str) -> tuple[str, str, str]:
    """Fetch (category, subcategory, condition) from a GC product page URL.
    Tries JSON-LD BreadcrumbList first, then __NEXT_DATA__, then keyword fallback."""
    try:
        r = _http.get(url, timeout=15)
        if r.status_code != 200:
            cat, subcat = classify_by_name(name)
            return cat, subcat, ""
        html = r.text

        condition = _extract_condition_from_html(html)

        # Strategy 1: JSON-LD BreadcrumbList
        for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        ):
            try:
                d = json.loads(block)
                if d.get("@type") == "BreadcrumbList":
                    els = sorted(d.get("itemListElement", []),
                                 key=lambda x: x.get("position", 0))
                    names = []
                    for el in els:
                        n = ((el.get("item") or {}).get("name") or el.get("name") or "").strip()
                        if n and n.lower() not in ("home", "used & vintage", "used"):
                            names.append(_clean_gc_cat(n))
                    if names:
                        cat    = names[0]
                        subcat = names[2] if len(names) >= 3 else (names[1] if len(names) >= 2 else "")
                        return cat, subcat, condition
            except Exception:
                pass

        # Strategy 2: __NEXT_DATA__ JSON blob (Next.js server-side props)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                nd = json.loads(m.group(1))
                crumbs = _find_breadcrumbs_in_json(nd)
                if crumbs:
                    names = []
                    for c in crumbs:
                        n = (c.get("displayName") or c.get("name") or
                             c.get("label") or c.get("text") or "").strip()
                        if n and n.lower() not in ("home", "used & vintage", "used"):
                            names.append(_clean_gc_cat(n))
                    if names:
                        cat    = names[0]
                        subcat = names[2] if len(names) >= 3 else (names[1] if len(names) >= 2 else "")
                        return cat, subcat, condition
            except Exception:
                pass

        cat, subcat = classify_by_name(name)
        return cat, subcat, condition

    except Exception:
        pass

    # Final fallback: keyword classification
    cat, subcat = classify_by_name(name)
    return cat, subcat, ""


# Keep old name as alias for any callers
def fetch_category_from_page(url: str, name: str) -> tuple[str, str]:
    cat, subcat, _ = fetch_page_data(url, name)
    return cat, subcat


def classify_by_name(name: str) -> tuple[str, str]:
    """Infer category and subcategory from product name using keyword matching.
    Returns (category, subcategory). Fast — no HTTP requests required."""
    n = name.lower()

    # ── Wireless Systems (before mic/recording so 'wireless' routes here) ────
    if re.search(r'wireless system|wireless mic|wireless guitar|wireless transmitter'
                 r'|in.ear wireless|iem wireless|\bqlxd\b|\bulgx\b|\bglxd\b|\bgldx\b'
                 r'|\bslxd\b|\bpgxd\b|\bbgxd\b|\bew\d|\batwr\b', n):
        return ("Microphones & Wireless", "Wireless Systems")

    # ── Amplifiers & Cabinets — check BEFORE guitars ──────────────────────────
    # "Guitar Combo Amp", "Guitar Cabinet", "Guitar Amp Head" all contain 'guitar'
    # so we must catch amp-type gear first.
    _amp_kw = re.search(
        r'combo amp|amp combo|amp head|guitar amp|tube amp|solid.state amp|valve amp'
        r'|practice amp|\bcabinet\b|\bcab\b|speaker cab|speaker cabinet'
        r'|\d+\s*[wW]\s*(combo|head|amp)\b|(combo|head)\s*\d+\s*[wW]'
        r'|\bx\d+\b.*amp|\bamp\b.*\bhead\b', n)
    if _amp_kw:
        if re.search(r'\bbass\b', n) and not re.search(r'drum|snare|cymbal', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'keyboard|piano', n):
            return ("Amplifiers & Effects", "Keyboard Amplifiers")
        if re.search(r'acoustic', n):
            return ("Amplifiers & Effects", "Acoustic Amplifiers")
        return ("Amplifiers & Effects", "Guitar Amplifiers")

    # ── Powered Monitors / Studio Monitors / PA Speakers ─────────────────────
    if re.search(r'powered monitor|studio monitor|reference monitor|nearfield|'
                 r'pair.*monitor|monitor.*pair|powered speaker|pa speaker|'
                 r'\blp-\d|kali audio|yamaha hs\d|adam a\d|krk\b|rokit\b|'
                 r'genelec|focal alpha|jbl.*(lsr|305|306|308|310|series3)', n):
        if re.search(r'studio|reference|nearfield|kali|krk|rokit|genelec|focal|adam\b', n):
            return ("Recording", "Studio Monitors")
        return ("Live Sound", "PA Speakers")

    # ── Bass (before guitar) ──────────────────────────────────────────────────
    if re.search(r'\bbass\b', n) and not re.search(r'drum|cymbal|hi.hat|snare|bassoon', n):
        if re.search(r'acoustic|upright|stand.?up|arco|double bass', n):
            return ("Bass", "Acoustic Bass Guitars")
        if re.search(r'amp|amplifier|cabinet|combo|head\b|cab\b', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'pedal|effect|pre.?amp|di\b|direct box', n):
            return ("Amplifiers & Effects", "Bass Effects")
        return ("Bass", "Electric Bass Guitars")

    # ── Guitars ───────────────────────────────────────────────────────────────
    guitar_kw = (r'guitar|stratocaster|strat\b|telecaster|tele\b|les paul|sg\b'
                 r'|flying.?v|explorer\b|jazzmaster|jaguar\b|mustang\b'
                 r'|semi.hollow|hollow.body|archtop|resonator|dobro'
                 r'|banjo|mandolin|ukulele|squier|epiphone|prs\b|gretsch'
                 r'|rickenbacker|es.?[0-9]')
    if re.search(guitar_kw, n):
        if re.search(r'banjo', n):
            return ("Folk & Traditional Instruments", "Banjos")
        if re.search(r'mandolin', n):
            return ("Folk & Traditional Instruments", "Mandolins")
        if re.search(r'ukulele', n):
            return ("Folk & Traditional Instruments", "Ukuleles")
        if re.search(r'acoustic|classical|nylon|parlor|dreadnought|folk|fingerstyle|12.string', n):
            return ("Guitars", "Acoustic Guitars")
        if re.search(r'classical|nylon|spanish', n):
            return ("Guitars", "Classical & Nylon Guitars")
        return ("Guitars", "Electric Guitars")

    # ── Effects & Pedals ──────────────────────────────────────────────────────
    if re.search(r'pedal|effect\b|reverb\b|delay\b|distortion|overdrive|fuzz\b|wah\b'
                 r'|chorus\b|flanger|phaser|compressor|tremolo|boost\b|looper|tuner\b'
                 r'|pedalboard|multi.effect|octave\b|harmonizer|pitch shift', n):
        return ("Amplifiers & Effects", "Effects Pedals & Processors")

    # ── Amplifiers (broader — standalone \bamp\b not caught above) ────────────
    if re.search(r'\bamp\b|amplifier', n):
        if re.search(r'\bbass\b', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'keyboard|piano', n):
            return ("Amplifiers & Effects", "Keyboard Amplifiers")
        return ("Amplifiers & Effects", "Guitar Amplifiers")

    # ── Drums & Percussion ────────────────────────────────────────────────────
    if re.search(r'drum|snare|cymbal|hi.?hat|bass drum|\btom\b|drum kit|drum set'
                 r'|drum throne|djembe|cajon|bongo|conga|percussion|cowbell'
                 r'|tambourine|marimba|xylophone|vibraphone|timpani|electronic drum'
                 r'|volca beats|volca drum|tr.?\d{2,3}|drum machine|beat.*machine', n):
        if re.search(r'electronic|digital|e.?drum|drum machine|volca|tr.?\d', n):
            return ("Drums & Percussion", "Electronic Drums")
        if re.search(r'cymbal|hi.?hat', n):
            return ("Drums & Percussion", "Cymbals")
        if re.search(r'snare', n):
            return ("Drums & Percussion", "Snare Drums")
        if re.search(r'djembe|bongo|conga|cajon|hand drum', n):
            return ("Drums & Percussion", "Hand Drums")
        return ("Drums & Percussion", "Drum Sets")

    # ── Keyboards & MIDI ──────────────────────────────────────────────────────
    if re.search(r'keyboard|piano|organ\b|synth|synthesizer|workstation\b'
                 r'|midi controller|electric piano|stage piano|arranger|clav'
                 r'|wurlitzer|rhodes\b|nord\b|sound module|volca\b|groovebox'
                 r'|roland\b.*\b(jd|juno|jupiter|fa|rd|fp|gaia)'
                 r'|korg\b|yamaha\b.*\b(psr|cp|ck|np|p-\d|montage|motif)', n):
        if re.search(r'midi|controller\b', n):
            return ("Keyboards & MIDI", "MIDI Controllers")
        if re.search(r'synth|synthesizer|volca|groovebox|sound module', n):
            return ("Keyboards & MIDI", "Synthesizers & Sound Modules")
        if re.search(r'organ', n):
            return ("Keyboards & MIDI", "Organs")
        if re.search(r'digital piano|stage piano|acoustic piano', n):
            return ("Keyboards & MIDI", "Digital Pianos")
        return ("Keyboards & MIDI", "Keyboards")

    # ── Recording & Studio ────────────────────────────────────────────────────
    if re.search(r'audio interface|recording interface|usb interface|thunderbolt interface', n):
        return ("Recording", "Audio Interfaces")
    if re.search(r'microphone|condenser mic|dynamic mic|ribbon mic|vocal mic\b', n):
        return ("Recording", "Microphones")
    if re.search(r'\bmic\b', n) and not re.search(r'microphone stand', n):
        return ("Recording", "Microphones")
    if re.search(r'preamp|pre.?amplifier|channel strip|outboard', n):
        return ("Recording", "Preamps & Channel Strips")
    if re.search(r'mixer|mixing console|mixing board|analog mixer|digital mixer', n):
        return ("Recording", "Mixers")
    if re.search(r'headphone|headset|earphone|in.ear monitor|iem\b', n):
        return ("Recording", "Headphones & Monitoring")
    if re.search(r'audio recorder|field recorder|multitrack|interface\b', n):
        return ("Recording", "Audio Interfaces")

    # ── DJ Equipment ─────────────────────────────────────────────────────────
    if re.search(r'\bdj\b|turntable|cdj\b|serato|traktor|rekordbox|dj mixer|dj controller', n):
        return ("DJ Equipment & Lighting", "DJ Equipment")

    # ── Live Sound ────────────────────────────────────────────────────────────
    if re.search(r'\bpa\b|powered speaker|live sound|subwoofer|stage monitor'
                 r'|line array|public address', n):
        return ("Live Sound", "PA Systems")

    # ── Accessories ───────────────────────────────────────────────────────────
    if re.search(r'\bstrap\b|guitar strap|instrument strap', n):
        return ("Accessories", "Straps")
    if re.search(r'\bstring\b|guitar string|bass string', n):
        return ("Accessories", "Strings")
    if re.search(r'\bcase\b|gig bag|hardshell|soft case', n):
        return ("Accessories", "Cases & Bags")
    if re.search(r'\bstand\b|guitar stand|amp stand|keyboard stand', n):
        return ("Accessories", "Stands & Racks")
    if re.search(r'\bcable\b|instrument cable|patch cable|speaker cable', n):
        return ("Accessories", "Cables")
    if re.search(r'\bpick\b|plectrum', n):
        return ("Accessories", "Picks")

    return ("", "")


def fetch_category(sku: str, name: str, url: str) -> tuple[str, str]:
    """Return (category, subcategory) for a product.
    Uses keyword classification from the product name (fast, no HTTP).
    Falls back to cached value if already known."""
    if sku in _cat_cache:
        d = _cat_cache[sku]
        # Re-classify if cache has empty values and we have a name
        if d.get("category") or not name:
            return d.get("category", ""), d.get("subcategory", "")

    cat, subcat = classify_by_name(name)
    _cat_cache[sku] = {"category": cat, "subcategory": subcat}
    return cat, subcat


def scrape_store(store_name: str, seen_ids: set, send, stop_event: threading.Event) -> tuple[list[dict], set]:
    """Returns (all_products_found, ids_seen_this_store)."""
    all_products, ids_seen = [], set()
    page = 1
    while page <= 50:
        if stop_event.is_set():
            send({"type": "progress", "msg": f"  [{store_name}] stopped."})
            break
        send({"type": "progress", "msg": f"  [{store_name}] page {page}…"})
        try:
            html = fetch_page(store_name, page)
        except Exception as e:
            # 404 = store doesn't exist — remove it from cache silently
            if "404" in str(e):
                send({"type": "progress", "msg": f"  [{store_name}] not found — removing from store list."})
                _remove_invalid_store(store_name)
            else:
                send({"type": "progress", "msg": f"  [{store_name}] error: {e}"})
            break
        products = parse_products(html, store_name)
        if not products:
            break
        if all(p["id"] in ids_seen for p in products):   # loop guard — only checks this store's own pages
            break
        for p in products:
            if p["id"] not in ids_seen:
                all_products.append(p)
                ids_seen.add(p["id"])
        if len(products) < PAGE_SIZE:
            break
        page += 1
        _sleep(1.5, 0.8)  # 0.7–2.3s between pages
    return all_products, ids_seen


def _remove_invalid_store(store_name: str):
    """Remove a store that returned 404 from the stores cache.
    Also saves to a blocklist so it stays removed after refreshes."""
    # Remove from cache
    if STORES_CACHE.exists():
        try:
            d = json.loads(STORES_CACHE.read_text())
            stores = d.get("stores", [])
            if store_name in stores:
                stores.remove(store_name)
                d["stores"] = stores
                STORES_CACHE.write_text(json.dumps(d))
        except Exception:
            pass
    # Add to persistent blocklist
    blocklist_file = DATA_DIR / "gc_invalid_stores.json"
    try:
        blocklist = json.loads(blocklist_file.read_text()) if blocklist_file.exists() else []
        if store_name not in blocklist:
            blocklist.append(store_name)
            blocklist_file.write_text(json.dumps(sorted(blocklist)))
    except Exception:
        pass


def _get_blocklist() -> set:
    """Return the set of stores confirmed invalid (404'd)."""
    blocklist_file = DATA_DIR / "gc_invalid_stores.json"
    try:
        if blocklist_file.exists():
            return set(json.loads(blocklist_file.read_text()))
    except Exception:
        pass
    return set()


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "seen_ids": [], "item_dates": {}}


def save_state(seen_ids: list, run_time: str, item_dates: dict = None):
    STATE_FILE.write_text(json.dumps({
        "last_run":   run_time,
        "seen_ids":   seen_ids,
        "item_dates": item_dates or {},
    }, indent=2))


# ── Excel ─────────────────────────────────────────────────────────────────────

_COLS    = ["Status", "Date Found", "Item Name", "Condition", "Category", "Subcategory", "Price", "Store", "Link"]
_WIDTHS  = [8, 18, 58, 14, 22, 22, 12, 16, 70]
_HDR_FILL = PatternFill("solid", start_color="1F3864", end_color="1F3864")
_HDR_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=11)
_ROW_FONT = Font(name="Arial", size=10)
_NEW_FONT = Font(name="Arial", bold=True, size=10)
_ALT_FILL = PatternFill("solid", start_color="DCE6F1", end_color="DCE6F1")

def _fmt_row(ws, r):
    fill = _ALT_FILL if r % 2 == 0 else None
    for col in range(1, len(_COLS) + 1):
        c = ws.cell(r, col)
        c.font = _ROW_FONT
        if fill: c.fill = fill

def write_excel(new_items: list[dict]):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n  = len(new_items)

    # If existing file has old column count, back it up and start fresh
    if OUTPUT_FILE.exists():
        try:
            wb_check = load_workbook(OUTPUT_FILE)
            if wb_check.active.max_column != len(_COLS):
                backup = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + "_backup" + OUTPUT_FILE.suffix)
                OUTPUT_FILE.rename(backup)
        except Exception:
            pass

    if OUTPUT_FILE.exists():
        wb = load_workbook(OUTPUT_FILE)
        ws = wb.active
        ws.insert_rows(2, amount=n)
        for i, item in enumerate(new_items):
            r = 2 + i
            ws.cell(r, 1, "New"); ws.cell(r, 2, ts); ws.cell(r, 3, item["name"])
            ws.cell(r, 4, item.get("condition", ""))
            ws.cell(r, 5, item.get("category", "")); ws.cell(r, 6, item.get("subcategory", ""))
            pc = ws.cell(r, 7, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 8, item["store"])
            lc = ws.cell(r, 9, item["url"] or "")
            if item["url"]: lc.hyperlink = item["url"]; lc.style = "Hyperlink"
            _fmt_row(ws, r); ws.cell(r, 1).font = _NEW_FONT
        for r in range(2 + n, ws.max_row + 1):
            _fmt_row(ws, r)
    else:
        wb = Workbook(); ws = wb.active
        ws.title = "New Inventory"; ws.freeze_panes = "A2"
        ws.append(_COLS)
        for ci in range(1, len(_COLS) + 1):
            c = ws.cell(1, ci); c.fill = _HDR_FILL; c.font = _HDR_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 22
        for ci, w in enumerate(_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
        for i, item in enumerate(new_items):
            r = 2 + i
            ws.cell(r, 1, "New"); ws.cell(r, 2, ts); ws.cell(r, 3, item["name"])
            ws.cell(r, 4, item.get("condition", ""))
            ws.cell(r, 5, item.get("category", "")); ws.cell(r, 6, item.get("subcategory", ""))
            pc = ws.cell(r, 7, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 8, item["store"])
            lc = ws.cell(r, 9, item["url"] or "")
            if item["url"]: lc.hyperlink = item["url"]; lc.style = "Hyperlink"
            _fmt_row(ws, r); ws.cell(r, 1).font = _NEW_FONT
    wb.save(OUTPUT_FILE)


# ── Flask ─────────────────────────────────────────────────────────────────────

app             = Flask(__name__)
app.secret_key  = os.environ.get("SECRET_KEY", os.urandom(24))
_q              = queue.Queue()
_lock           = threading.Lock()
_stop_event     = threading.Event()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>GC Tracker — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;display:flex;align-items:center;justify-content:center;height:100vh;font-family:-apple-system,sans-serif}
.box{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:10px;padding:40px;width:320px}
h1{color:#fff;font-size:1.2rem;margin-bottom:6px}
p{color:#666;font-size:.85rem;margin-bottom:24px}
input{width:100%;padding:10px 12px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#eee;font-size:.95rem;outline:none;margin-bottom:14px}
input:focus{border-color:#c00}
button{width:100%;padding:11px;background:#c00;color:#fff;border:none;border-radius:5px;font-size:1rem;font-weight:700;cursor:pointer}
button:hover{background:#e00}
.err{color:#f88;font-size:.82rem;margin-bottom:12px}
</style></head>
<body><div class="box">
  <h1>🎸 GC Tracker</h1>
  <p>Enter your password to continue.</p>
  {% if error %}<div class="err">Incorrect password.</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Sign In</button>
  </form>
</div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask import render_template_string
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect("/")
        return render_template_string(LOGIN_PAGE, error=True)
    return render_template_string(LOGIN_PAGE, error=False)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():
    """Delete state, category cache, and Excel file to start fresh."""
    data = request.json or {}
    if data.get("password") != APP_PASSWORD and APP_PASSWORD:
        return jsonify({"error": "Incorrect password."}), 403
    deleted = []
    for f in [STATE_FILE, CAT_CACHE_FILE, OUTPUT_FILE,
              DATA_DIR / "gc_invalid_stores.json",
              DATA_DIR / "gc_condition_diag.json",
              DATA_DIR / "gc_debug_listing.html"]:
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    global _cat_cache
    _cat_cache = {}
    return jsonify({"deleted": deleted, "status": "Reset complete. Ready for a fresh baseline."})

@app.route("/api/clear-blocklist", methods=["POST"])
@login_required
def api_clear_blocklist():
    """Remove the invalid stores blocklist so all stores are re-evaluated."""
    f = DATA_DIR / "gc_invalid_stores.json"
    if f.exists():
        f.unlink()
    return jsonify({"status": "Blocklist cleared. Run Validate Stores to re-check all stores."})

@app.route("/")
@login_required
def index():
    return HTML_TEMPLATE

@app.route("/download/excel")
@login_required
def download_excel():
    if not OUTPUT_FILE.exists():
        return "No Excel file yet — run the tracker first.", 404
    return send_file(OUTPUT_FILE, as_attachment=True,
                     download_name="gc_new_inventory.xlsx")

@app.route("/api/stores")
@login_required
def api_stores():
    return jsonify({
        "stores":    get_store_list(),
        "favorites": load_favorites(),
        "info":      get_store_info(),
    })

@app.route("/api/stores/refresh", methods=["POST"])
@login_required
def api_stores_refresh():
    stores = refresh_store_list()
    info   = get_store_info()
    return jsonify({"stores": stores, "favorites": load_favorites(),
                    "count": len(stores), "info": info})

@app.route("/api/favorites", methods=["POST"])
@login_required
def api_favorites():
    data = request.json
    favs = load_favorites()
    name = data.get("store", "")
    if data.get("action") == "add" and name not in favs:
        favs.append(name)
    elif data.get("action") == "remove" and name in favs:
        favs.remove(name)
    save_favorites(favs)
    return jsonify({"favorites": sorted(favs)})


@app.route("/api/browse", methods=["POST"])
@login_required
def api_browse():
    """Return cached inventory for selected stores — instant, no scraping."""
    stores = request.json.get("stores", [])
    if not stores:
        return jsonify({"items": [], "no_store_data": True})
    _load_cat_cache()
    state      = load_state()
    seen_ids   = set(state.get("seen_ids", []))
    item_dates = state.get("item_dates", {})
    wl         = load_watchlist()
    store_set  = set(stores)

    # Check if any cache entries have store field
    has_store_data = any(v.get("store") for v in _cat_cache.values())
    if not has_store_data:
        return jsonify({"items": [], "no_store_data": True,
                        "message": "Run 'Check for New Items' once to populate store data."})

    items = []
    for sku, cached in _cat_cache.items():
        if cached.get("store") not in store_set:
            continue
        if not cached.get("available", True):
            continue
        price_raw = cached.get("price", 0) or 0
        items.append({
            "id":         sku,
            "name":       cached.get("name", ""),
            "price":      f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":  price_raw,
            "price_drop": 0,
            "store":      cached.get("store", ""),
            "url":        cached.get("url", ""),
            "category":   cached.get("category", ""),
            "subcategory":cached.get("subcategory", ""),
            "condition":  cached.get("condition", ""),
            "date":       _fmt_date(item_dates.get(sku, "")),
            "isNew":      sku not in seen_ids,
            "watched":    sku in wl,
        })
    items.sort(key=lambda x: (not x["isNew"], x.get("date","") or ""))
    return jsonify({"items": items, "count": len(items), "no_store_data": False})


@app.route("/api/watchlist", methods=["GET"])
@login_required
def api_watchlist_get():
    wl = load_watchlist()
    return jsonify({"watchlist": wl})


@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_post():
    data = request.json
    sku  = data.get("id", "")
    action = data.get("action", "")
    if not sku:
        return jsonify({"error": "No id provided"}), 400
    wl = load_watchlist()
    if action == "add":
        _load_cat_cache()
        cached = _cat_cache.get(sku, {})
        wl[sku] = {
            "name":       cached.get("name", data.get("name", "")),
            "price":      cached.get("price", 0),
            "store":      cached.get("store", data.get("store", "")),
            "url":        cached.get("url", data.get("url", "")),
            "condition":  cached.get("condition", ""),
            "category":   cached.get("category", ""),
            "date_added": datetime.now().strftime("%Y-%m-%d"),
            "sold":       False,
        }
    elif action == "remove":
        wl.pop(sku, None)
    save_watchlist(wl)
    return jsonify({"watchlist": wl})


@app.route("/api/watchlist/items", methods=["GET"])
@login_required
def api_watchlist_items():
    """Return watchlist items formatted for display."""
    wl         = load_watchlist()
    item_dates = load_state().get("item_dates", {})
    items = []
    for sku, w in wl.items():
        price_raw = w.get("price", 0) or 0
        items.append({
            "id":         sku,
            "name":       w.get("name", ""),
            "price":      f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":  price_raw,
            "price_drop": 0,
            "store":      w.get("store", ""),
            "url":        w.get("url", ""),
            "category":   w.get("category", ""),
            "subcategory":"",
            "condition":  w.get("condition", ""),
            "date":       _fmt_date(item_dates.get(sku, w.get("date_added",""))),
            "isNew":      False,
            "watched":    True,
            "sold":       w.get("sold", False),
        })
    # Sold items at bottom
    items.sort(key=lambda x: x["sold"])
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/state")
@login_required
def api_state():
    s = load_state()
    return jsonify({
        "last_run":    s.get("last_run"),
        "known_items": len(s.get("seen_ids", [])),
        "excel_exists": OUTPUT_FILE.exists(),
        "is_first_run": not STATE_FILE.exists() or len(s.get("seen_ids", [])) == 0,
    })

@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    data     = request.json
    selected = data.get("stores", [])
    baseline = data.get("baseline", False)
    if not selected and not baseline:
        _lock.release()
        return jsonify({"error": "No stores selected."}), 400
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_run, args=(selected, baseline), daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/cl-search")
@login_required
def api_cl_search():
    q = request.args.get("q", "").strip()
    cities_param = request.args.get("cities", "").strip()
    if not q:
        return jsonify({"error": "No search term provided."})
    cities = [c.strip() for c in cities_param.split(",") if c.strip()] if cities_param else []
    try:
        results = _cl_search(q, cities or None)
        return jsonify({"results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)})


_CL_CITIES = [
    "atlanta","austin","boston","chicago","dallas","denver","detroit",
    "houston","lasvegas","losangeles","miami","minneapolis","nashville",
    "newyork","philadelphia","phoenix","portland","raleigh","sacramento",
    "saltlakecity","sanantonio","sandiego","sfbay","seattle","stlouis",
    "washingtondc","baltimore","charlotte","cleveland","columbus","fortworth",
    "indianapolis","jacksonville","kansascity","memphis","milwaukee",
    "oklahomacity","orlando","pittsburgh","richmond","riverside","tampabay",
    "tucson","tulsa","virginiabeach","albuquerque","boise","buffalo",
    "cincinnati","desmoines","elpaso","fresno","grandrapids","greensboro",
    "hartford","honolulu","knoxville","louisville","madison","neworleans",
    "norfolk","omaha","providence","rochester","spokane","syracuse",
    "toledo","wichita",
]

_CL_LABELS = {
    "sfbay":"SF Bay Area","newyork":"New York","losangeles":"Los Angeles",
    "washingtondc":"Washington DC","saltlakecity":"Salt Lake City",
    "sandiego":"San Diego","sanantonio":"San Antonio","lasvegas":"Las Vegas",
    "tampabay":"Tampa Bay","kansascity":"Kansas City","grandrapids":"Grand Rapids",
    "desmoines":"Des Moines","fortworth":"Fort Worth","oklahomacity":"Oklahoma City",
    "virginiabeach":"Virginia Beach","neworleans":"New Orleans","stlouis":"St. Louis",
}

def _cl_city_label(city_id: str) -> str:
    return _CL_LABELS.get(city_id, city_id.title())

def _cl_fmt_date(iso: str) -> str:
    try:
        from datetime import datetime as dt
        d = dt.fromisoformat(iso.replace("Z",""))
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except Exception:
        return iso[:10] if iso else ""

def _cl_parse_html(html: str, city_id: str) -> list[dict]:
    """Parse CL search results — ItemList JSON-LD + URLs from HTML anchor tags."""
    items = []
    label = _cl_city_label(city_id)

    # Extract post URLs from the HTML — CL puts them in <a class="cl-app-anchor"> or similar
    # Pattern: href="https://cityname.craigslist.org/msa/d/title/1234567890.html"
    post_urls = re.findall(
        r'href="(https?://[a-z]+\.craigslist\.org/[^"]+/d/[^"]+\.html)"',
        html)
    # Dedupe while preserving order
    seen = set()
    post_urls_ordered = []
    for u in post_urls:
        if u not in seen:
            seen.add(u)
            post_urls_ordered.append(u)

    # Find the ItemList JSON-LD block
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue

        entries = data.get("itemListElement", [])
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            item = entry.get("item", {})
            if not isinstance(item, dict):
                continue
            name   = item.get("name", "")
            if not name:
                continue
            # Match URL by position
            url = post_urls_ordered[i] if i < len(post_urls_ordered) else ""
            if not url:
                continue
            offers = item.get("offers", {})
            price  = offers.get("price", "")
            try:    price = f"${float(price):,.0f}" if price else ""
            except: price = str(price)
            avail  = offers.get("availableAtOrFrom", {})
            addr   = avail.get("address", {}) if isinstance(avail, dict) else {}
            hood   = addr.get("addressLocality","") or addr.get("addressRegion","")
            loc    = label
            date   = _cl_fmt_date(
                offers.get("validFrom","") or offers.get("availabilityStarts","") or
                item.get("datePosted","") or item.get("dateCreated","") or
                item.get("uploadDate","")
            )
            items.append({"title": name, "url": url, "price": price,
                          "location": loc, "date": date, "cityId": city_id})
        if items:
            break  # Found and parsed the ItemList, done

    return items


def _cl_search(query: str, cities: list = None) -> list[dict]:
    """Search Craigslist musical instruments across US cities."""
    results   = []
    seen_urls = set()
    search_cities = cities if cities else _CL_CITIES

    def _search_city(city_id):
        try:
            url = (f"https://{city_id}.craigslist.org/search/msa"
                   f"?query={http.utils.quote(query)}&sort=date")
            r = _http.get(url, timeout=12)
            if r.status_code == 200:
                return _cl_parse_html(r.text, city_id)
        except Exception:
            pass
        return []

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_search_city, c): c for c in search_cities}
        for future in as_completed(futures):
            for item in future.result():
                title_key = f"{item['title'].lower().strip()}|{item['price']}|{item['cityId']}"
                if title_key not in seen_urls:
                    seen_urls.add(title_key)
                    results.append(item)

    results.sort(key=lambda x: x.get("date",""), reverse=True)
    return results


@app.route("/api/cl-parse-test")
@login_required
def api_cl_parse_test():
    """Test the CL parser on a live page and show what it finds."""
    city = request.args.get("city", "sfbay")
    q    = request.args.get("q", "telecaster")
    try:
        url  = f"https://{city}.craigslist.org/search/msa?query={http.utils.quote(q)}&sort=date"
        r    = _http.get(url, timeout=12)
        html = r.text
        blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        parsed_blocks = []
        for i, b in enumerate(blocks):
            try:
                d = json.loads(b)
                parsed_blocks.append({
                    "index": i,
                    "type": d.get("@type","") if isinstance(d, dict) else type(d).__name__,
                    "keys": list(d.keys())[:15] if isinstance(d, dict) else [],
                    "sample": b[:1200],
                })
            except Exception as e:
                parsed_blocks.append({"index": i, "parse_error": str(e), "raw": b[:400]})
        # Also show raw keys from first listing for date debugging
        raw_keys = {}
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                d = json.loads(block)
                entries = []
                if isinstance(d, list): entries = d
                elif d.get("@type") == "CollectionPage": entries = d.get("mainEntity",{}).get("itemListElement",[])
                elif d.get("@type") in ("ItemList","ListItem"): entries = [d]
                if entries:
                    item = entries[0].get("item", entries[0]) if isinstance(entries[0], dict) else {}
                    offers = item.get("offers", {})
                    raw_keys = {"item_keys": list(item.keys()), "offer_keys": list(offers.keys())}
                    break
            except Exception:
                pass
        return jsonify({
            "html_size": len(html),
            "json_ld_block_count": len(blocks),
            "blocks": parsed_blocks,
            "first_listing_keys": raw_keys,
            "parser_results_count": len(results),
            "parser_sample": results[:3],
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@login_required
def api_cl_debug():
    """Probe a CL city to find the right section code and response format."""
    city = request.args.get("city", "sfbay")
    q    = request.args.get("q", "telecaster")
    out  = {}
    for section in ["msa", "msg", "mso", "mlt"]:
        # Try plain HTML
        try:
            url = f"https://{city}.craigslist.org/search/{section}?query={http.utils.quote(q)}&sort=date"
            r   = _http.get(url, timeout=10)
            out[f"{section}_html"] = {
                "status": r.status_code,
                "size":   len(r.text),
                "has_results": any(x in r.text for x in ["result-row","cl-search-result","listing-id"]),
                "snippet": r.text[2000:2500],
            }
        except Exception as e:
            out[f"{section}_html"] = {"error": str(e)}
        # Try format=json
        try:
            url = f"https://{city}.craigslist.org/search/{section}?query={http.utils.quote(q)}&sort=date&format=json"
            r   = _http.get(url, timeout=10)
            out[f"{section}_json"] = {
                "status":       r.status_code,
                "content_type": r.headers.get("Content-Type",""),
                "size":         len(r.text),
                "snippet":      r.text[:1000],
            }
        except Exception as e:
            out[f"{section}_json"] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/debug-fetch")
@login_required
def api_debug_fetch():
    """Fetch page 1 for a store and return diagnostic info."""
    store = request.args.get("store", "South Austin")
    try:
        html = fetch_page(store, 1)
        has_json_ld = "CollectionPage" in html
        has_products = len(parse_products(html, store)) > 0
        size = len(html)
        # Check for bot detection signals
        bot_signals = []
        for sig in ["captcha", "robot", "blocked", "access denied", "cloudflare", "challenge"]:
            if sig in html.lower():
                bot_signals.append(sig)
        return jsonify({
            "store": store,
            "html_size": size,
            "has_collection_page_json_ld": has_json_ld,
            "products_parsed": has_products,
            "bot_signals": bot_signals,
            "html_snippet": html[:500],
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@login_required
def api_debug_condition():
    """Inspect the saved listing HTML to find exactly where condition data lives."""
    debug_file = DATA_DIR / "gc_debug_listing.html"
    if not debug_file.exists():
        return jsonify({"error": "No debug file yet — run the tracker once to save a listing page, then visit this URL."})
    html = debug_file.read_text(errors="replace")

    report = {"html_size": len(html)}

    # 1. All "Condition" occurrences in raw HTML (catches server-rendered text)
    condition_hits = []
    for m in re.finditer(r'.{0,60}[Cc]ondition.{0,60}', html):
        txt = m.group(0).strip()
        if txt not in condition_hits:
            condition_hits.append(txt)
        if len(condition_hits) >= 15:
            break
    report["condition_in_raw_html"] = condition_hits

    # 2. Dig into __NEXT_DATA__ and find ALL keys that contain condition-like words
    nd_condition_fields = {}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            nd = json.loads(m.group(1))
            report["has_next_data"] = True
            report["next_data_size"] = len(m.group(1))

            # Walk every key/value pair and collect anything condition-related
            def walk(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        full = f"{path}.{k}" if path else k
                        if any(c in k.lower() for c in ("condition", "grade", "quality", "rating")):
                            nd_condition_fields[full] = str(v)[:120]
                        if isinstance(v, (dict, list)):
                            walk(v, full)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj[:5]):  # only first 5 items
                        walk(item, f"{path}[{i}]")
            walk(nd)
        except Exception as e:
            report["next_data_parse_error"] = str(e)
    else:
        report["has_next_data"] = False

    report["next_data_condition_fields"] = nd_condition_fields

    # 3. All JSON-LD blocks — show the full offers object for first item
    ld_offers = []
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            d = json.loads(block)
            if d.get("@type") == "CollectionPage":
                items = d.get("mainEntity", {}).get("itemListElement", [])
                for entry in items[:3]:
                    item = entry.get("item", {})
                    ld_offers.append({
                        "name": item.get("name", "")[:60],
                        "offers": item.get("offers", {}),
                    })
        except Exception:
            pass
    report["jsonld_first_3_offers"] = ld_offers

    # 4. Show raw HTML snippet around first .gc product URL
    m2 = re.search(r'https?://www\.guitarcenter\.com/Used/[^"\'<>\s]+\.gc', html)
    if m2:
        start = max(0, m2.start() - 300)
        end = min(len(html), m2.end() + 600)
        report["html_around_first_product_url"] = html[start:end]

    return jsonify(report)

@app.route("/api/debug-condition/reset", methods=["GET", "POST"])
@login_required
def api_debug_condition_reset():
    debug_file = DATA_DIR / "gc_debug_listing.html"
    if debug_file.exists():
        debug_file.unlink()
    return jsonify({"status": "cleared"})

@app.route("/api/debug-condition/diag")
@login_required
def api_debug_condition_diag():
    """Read the condition extraction diagnostic log."""
    diag_file = DATA_DIR / "gc_condition_diag.json"
    if not diag_file.exists():
        return jsonify({"error": "No diagnostic file yet — run the tracker first."})
    return diag_file.read_text()

@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    _stop_event.set()
    return jsonify({"status": "stopping"})

@app.route("/api/validate-stores", methods=["POST"])
@login_required
def api_validate_stores():
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_validate_stores, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@login_required
def api_fill_gaps():
    """Re-scrape listing pages for selected stores to fill missing condition/category data."""
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    data = request.json or {}
    stores = data.get("stores", [])
    if not stores:
        _lock.release()
        return jsonify({"error": "No stores selected."}), 400
    t = threading.Thread(target=_fill_gaps, args=(stores,), daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/progress")
@login_required
def api_progress():
    def generate():
        while True:
            try:
                msg = _q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "done": break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


def _check_store_url(store_name: str) -> tuple[bool, str]:
    """Check if a store name works in the GC filter URL.
    Returns (is_valid, working_name). Tries variations if the original fails."""
    def _try(name: str) -> bool:
        try:
            query = f"filters=stores:{name.replace(' ', '%20')}"
            url   = f"https://www.guitarcenter.com/Used/?{query}&page=1"
            r = _http.get(url, timeout=10, allow_redirects=True)
            return r.status_code != 404
        except Exception:
            return True  # network error — assume valid

    if _try(store_name):
        return True, store_name

    # Try stripping state suffix (e.g. "Albany NY" → "Albany")
    parts = store_name.rsplit(' ', 1)
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isupper():
        bare = parts[0]
        if _try(bare):
            return True, bare

    return False, store_name


def _validate_stores():
    """Check every store with a page-1 fetch, auto-fix names, remove 404s, then rebuild."""
    def send(msg): _q.put(msg)
    try:
        stores = get_store_list()
        total  = len(stores)
        removed = []
        renamed = []
        send({"type": "progress", "msg": f"Step 1: Validating {total} stores…"})
        send({"type": "progress", "msg": "About 0.5s per store. You can stop at any time."})

        updated_stores = list(stores)
        for i, store in enumerate(stores, 1):
            if _stop_event.is_set():
                send({"type": "progress", "msg": "⏹ Stopped by user."})
                break
            if i % 25 == 1:
                send({"type": "progress", "msg": f"  [{i}/{total}] checking…"})
            is_valid, working_name = _check_store_url(store)
            if not is_valid:
                _remove_invalid_store(store)
                removed.append(store)
                if store in updated_stores:
                    updated_stores.remove(store)
                send({"type": "progress", "msg": f"  ✗ Removed: {store}"})
            elif working_name != store:
                idx = updated_stores.index(store) if store in updated_stores else -1
                if idx >= 0:
                    updated_stores[idx] = working_name
                renamed.append(f"{store} → {working_name}")
                send({"type": "progress", "msg": f"  ✎ Renamed: {store} → {working_name}"})
            _sleep(0.5, 0.3)  # 0.2–0.8s between store checks

        # Save corrected names back to cache
        if renamed:
            try:
                d = json.loads(STORES_CACHE.read_text()) if STORES_CACHE.exists() else {}
                d["stores"] = sorted(set(updated_stores))
                STORES_CACHE.write_text(json.dumps(d))
            except Exception:
                pass

        if removed:
            send({"type": "progress", "msg": f"\n  Removed {len(removed)}: {', '.join(removed)}"})
        if renamed:
            send({"type": "progress", "msg": f"  Renamed {len(renamed)}: {', '.join(renamed)}"})
        if not removed and not renamed:
            send({"type": "progress", "msg": "\n  All stores validated — none removed or renamed."})

        if not _stop_event.is_set():
            send({"type": "progress", "msg": "\nStep 2: Rebuilding store list from GC's live data…"})
            try:
                new_stores = refresh_store_list()
                send({"type": "progress", "msg": f"  ✓ Store list rebuilt — {len(new_stores)} stores."})
            except Exception as e:
                send({"type": "progress", "msg": f"  Rebuild failed: {e}"})

        final_stores = get_store_list()
        send({"type": "progress", "msg": f"\n✓ Done — {len(final_stores)} valid stores in list."})
        send({"type": "done", "baseline": False, "stopped": _stop_event.is_set(),
              "scanned": total, "new_count": 0, "new_items": [], "all_items": [],
              "gap_fill": True, "fixed": len(removed)})
    except Exception as e:
        send({"type": "done", "error": str(e), "scanned": 0, "new_count": 0, "new_items": []})
    finally:
        _lock.release()


def _fill_gaps(selected_stores: list[str]):
    """Fetch individual product pages for items missing category or condition data."""
    def send(msg): _q.put(msg)
    try:
        _load_cat_cache()

        # Find cache entries that need fixing — missing category OR empty condition
        gaps = {
            sku: data for sku, data in _cat_cache.items()
            if data.get("url")
            and (not data.get("category") or not data.get("condition"))
        }

        total = len(gaps)
        if total == 0:
            send({"type": "progress", "msg": "No gaps found — all items already have category and condition data."})
            send({"type": "done", "baseline": False, "stopped": False,
                  "scanned": 0, "new_count": 0, "new_items": [], "all_items": [],
                  "gap_fill": True, "fixed": 0})
            return

        send({"type": "progress", "msg": f"Found {total} items with missing data. Fetching product pages in parallel…"})
        send({"type": "progress", "msg": f"(You can stop at any time.)"})

        fixed = 0
        gap_list = list(gaps.items())

        def _fetch_gap(item):
            sku, data = item
            url  = data.get("url", "")
            name = data.get("name", "")
            try:
                _sleep(0.3, 0.2)  # 0.1–0.5s jitter
                cat, subcat, condition = fetch_page_data(url, name)
                return sku, cat, subcat, condition
            except Exception:
                return sku, "", "", ""

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_gap, item): item for item in gap_list}
            for future in as_completed(futures):
                if _stop_event.is_set():
                    send({"type": "progress", "msg": "⏹ Stopped by user."})
                    break
                sku, cat, subcat, condition = future.result()
                data = gaps[sku]
                _cat_cache[sku].update({
                    "category":          cat or data.get("category", ""),
                    "subcategory":       subcat or data.get("subcategory", ""),
                    "condition":         condition or data.get("condition", ""),
                    "condition_fetched": True,
                })
                fixed += 1
                if fixed % 10 == 0:
                    send({"type": "progress", "msg": f"  …{fixed}/{total} items updated"})

        _save_cat_cache()
        send({"type": "progress", "msg": f"\n✓ Done — {fixed} item(s) updated. Re-run your stores to see the refreshed data."})
        send({"type": "done", "baseline": False, "stopped": _stop_event.is_set(),
              "scanned": fixed, "new_count": 0, "new_items": [], "all_items": [],
              "gap_fill": True, "fixed": fixed})
    except Exception as e:
        send({"type": "done", "error": str(e), "scanned": 0, "new_count": 0, "new_items": []})
    finally:
        _lock.release()



def _run(selected_stores: list[str], baseline: bool):
    def send(msg): _q.put(msg)
    try:
        state      = load_state()
        seen_ids   = set(state["seen_ids"])
        item_dates = dict(state.get("item_dates", {}))
        run_time   = datetime.now().isoformat()
        ts         = datetime.now().strftime("%Y-%m-%d")

        stores_to_scan = get_store_list() if baseline else selected_stores
        label = "baseline scan" if baseline else f"{len(stores_to_scan)} store(s)"
        send({"type":"progress","msg":f"Starting {label} — {len(stores_to_scan)} stores total…"})
        if baseline:
            send({"type":"progress","msg":"⏳ This may take 2–4 hours with anti-bot delays. Feel free to leave it running!"})

        all_products, ids_this_run = [], set()
        for i, store in enumerate(stores_to_scan, 1):
            if _stop_event.is_set():
                send({"type":"progress","msg":"⏹ Stopped by user."})
                break
            send({"type":"progress","msg":f"\n[{i}/{len(stores_to_scan)}] {store}"})
            _rotate_ua()  # rotate User-Agent each store
            products, ids = scrape_store(store, seen_ids, send, _stop_event)
            for p in products:
                if p["id"] not in ids_this_run:
                    all_products.append(p)
            ids_this_run |= ids
            # Extra pause between stores during baseline to avoid bot detection
            if baseline and not _stop_event.is_set():
                _sleep(4.0, 2.0)  # 2–6s between stores during baseline

        # ── Classify categories (parallel) & use listing-page condition ─────────
        # Condition is already parsed from the listing page in parse_products — free, no HTTP.
        # Category requires the individual product page — only fetch for NEW items not in seen_ids,
        # and only if not already cached. Existing items keep their cached category.
        new_item_ids = {p["id"] for p in all_products if p["id"] not in seen_ids}
        needs_cat = [p for p in all_products
                     if p["id"] in new_item_ids
                     and not baseline and p.get("url")
                     and not _cat_cache.get(p["id"], {}).get("category")]

        if needs_cat:
            send({"type": "progress", "msg": f"\nFetching categories for {len(needs_cat)} item(s) (parallel)…"})

            def _fetch_cat(p):
                try:
                    _sleep(0.2, 0.2)  # 0–0.4s jitter so parallel requests stagger
                    cat, subcat, _ = fetch_page_data(p["url"], p.get("name", ""))
                    return p["id"], cat, subcat
                except Exception:
                    return p["id"], "", ""

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_fetch_cat, p): p for p in needs_cat}
                done_count = 0
                for future in as_completed(futures):
                    if _stop_event.is_set():
                        break
                    sku, cat, subcat = future.result()
                    _cat_cache.setdefault(sku, {}).update({"category": cat, "subcategory": subcat})
                    done_count += 1
                    if done_count % 10 == 0:
                        send({"type": "progress", "msg": f"  …{done_count}/{len(needs_cat)} categories fetched"})

        # Apply all data to products, tracking price drops
        for p in all_products:
            sku    = p["id"]
            cached = _cat_cache.get(sku, {})
            cat    = cached.get("category") or classify_by_name(p.get("name", ""))[0]
            subcat = cached.get("subcategory") or classify_by_name(p.get("name", ""))[1]
            condition = p.get("condition") or cached.get("condition", "")
            # Price drop detection
            new_price  = p.get("price") or 0
            last_price = cached.get("price") or 0
            price_drop = (last_price - new_price) if (last_price and new_price and new_price < last_price) else 0
            _cat_cache[sku] = {
                "category":          cat,
                "subcategory":       subcat,
                "condition":         condition,
                "condition_fetched": True,
                "name":              p.get("name", ""),
                "url":               p.get("url", ""),
                "store":             p.get("store", ""),
                "price":             new_price,
                "available":         True,
            }
            p["category"]    = cat
            p["subcategory"] = subcat
            p["condition"]   = condition
            p["price_drop"]  = price_drop
        _save_cat_cache()

        # ── Mark sold items (not found in this scan for scanned stores) ──────────
        if not baseline and not _stop_event.is_set():
            # Only mark items as sold if we scanned ALL their store's pages
            scanned_store_set = set(stores_to_scan)
            for sku, cached in _cat_cache.items():
                if cached.get("store") in scanned_store_set and sku not in ids_this_run:
                    if cached.get("available", True):
                        cached["available"] = False
                        # Flag in watchlist as sold
                        wl = load_watchlist()
                        if sku in wl:
                            wl[sku]["sold"] = True
                            save_watchlist(wl)
            _save_cat_cache()

        # ── Update watchlist with latest prices ───────────────────────────────
        wl = load_watchlist()
        changed = False
        for sku, item in wl.items():
            if sku in _cat_cache and not wl[sku].get("sold"):
                cached = _cat_cache[sku]
                wl[sku].update({
                    "price":     cached.get("price", item.get("price")),
                    "condition": cached.get("condition", item.get("condition","")),
                })
                changed = True
        if changed:
            save_watchlist(wl)

        # ── Record first-seen dates for new items ─────────────────────────────
        for p in all_products:
            if p["id"] not in seen_ids and p["id"] not in item_dates:
                item_dates[p["id"]] = ts

        new_items = [p for p in all_products if p["id"] not in seen_ids]
        save_state(list(seen_ids | ids_this_run), run_time, item_dates)
        if new_items:
            write_excel(new_items)

        def fmt(p):
            return {
                "id":         p["id"],
                "name":       p["name"],
                "price":      f"${p['price']:,.2f}" if p["price"] else "",
                "price_raw":  p.get("price") or 0,
                "price_drop": p.get("price_drop", 0),
                "store":      p["store"],
                "url":        p["url"],
                "category":   p.get("category", ""),
                "subcategory":p.get("subcategory", ""),
                "condition":  p.get("condition", ""),
                "date":       _fmt_date(item_dates.get(p["id"], "")),
            }

        new_ids = {p["id"] for p in new_items}
        send({
            "type":       "done",
            "baseline":   baseline,
            "stopped":    _stop_event.is_set(),
            "scanned":    len(all_products),
            "new_count":  len(new_items),
            "new_items":  [fmt(p) for p in new_items],
            "all_items":  [] if baseline else
                          [fmt(p) for p in all_products if p["id"] not in new_ids],
        })
    except Exception as e:
        send({"type":"done","error":str(e),"scanned":0,"new_count":0,"new_items":[]})
    finally:
        _lock.release()


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gear Finder</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;height:100vh;display:flex;flex-direction:column}

header{background:#c00;padding:12px 24px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:1.2rem;font-weight:700;color:#fff}
#stop-btn{display:none;padding:7px 14px;background:#fff;color:#c00;border:none;border-radius:5px;font-size:.82rem;font-weight:700;cursor:pointer;white-space:nowrap}
#stop-btn:hover{background:#ffe0e0}
#stop-btn:disabled{opacity:.6;cursor:not-allowed}
#hdr-status{font-size:.8rem;color:#ffbbbb;margin-left:auto}

/* ── Top tabs ── */
.app-tabs{display:flex;background:#0d0d0d;border-bottom:1px solid #2e2e2e;flex-shrink:0}
.app-tab{padding:11px 28px;font-size:.85rem;font-weight:600;color:#666;cursor:pointer;border:none;background:none;border-bottom:3px solid transparent;margin-bottom:-1px;letter-spacing:.2px;transition:color .15s}
.app-tab:hover{color:#ccc}
.app-tab.gc-tab.active{color:#ff4444;border-bottom-color:#c00}
.app-tab.cl-tab.active{color:#c7d2fe;border-bottom-color:#a5b4fc}
.app-panel{display:none;flex:1;overflow:hidden}
.app-panel.active{display:flex}

.layout{display:flex;flex:1;overflow:hidden}

/* ── Left panel ── */
.left{width:220px;min-width:200px;background:#1a1a1a;border-right:1px solid #2e2e2e;display:flex;flex-direction:column;flex-shrink:0}

.sel-btns{display:flex;gap:6px;margin-top:8px}
.sel-btn{flex:1;padding:5px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#aaa;font-size:.75rem;cursor:pointer}
.sel-btn:hover{border-color:#c00;color:#fff}

.search-wrap{padding:10px 12px;border-bottom:1px solid #2e2e2e;flex-shrink:0}
#search{width:100%;padding:7px 11px;border-radius:5px;background:#252525;border:1px solid #3a3a3a;color:#eee;font-size:.875rem;outline:none}
#search:focus{border-color:#c00}

#store-list{flex:1;overflow-y:auto;padding:4px 0}
.store-row{display:flex;align-items:center;padding:6px 12px;gap:8px;cursor:pointer}
.store-row:hover{background:#222}
.store-row input[type=checkbox]{accent-color:#c00;flex-shrink:0;cursor:pointer}
.store-row label{flex:1;font-size:.855rem;cursor:pointer}
.store-row.hidden{display:none}
.fav-btn{background:none;border:none;cursor:pointer;font-size:1rem;line-height:1;padding:0 4px;color:#444;flex-shrink:0;transition:color .15s}
.fav-btn.active{color:#f5c518}
.fav-btn:hover{color:#f5c518}

.empty-msg{padding:24px 16px;color:#555;font-size:.85rem;text-align:center}

.left-footer{padding:12px;border-top:1px solid #2e2e2e;flex-shrink:0}
#sel-count{font-size:.78rem;color:#666;margin-bottom:8px}
.btn-row{display:flex;gap:8px}
#run-btn{flex:1;padding:10px;background:#c00;color:#fff;border:none;border-radius:5px;font-size:.85rem;font-weight:700;cursor:pointer;white-space:nowrap}
#run-btn:hover{background:#e00}
#run-btn:disabled{background:#444;cursor:not-allowed}
#baseline-btn{padding:10px 12px;background:#222;color:#aaa;border:1px solid #3a3a3a;border-radius:5px;font-size:.8rem;cursor:pointer;white-space:nowrap}
#baseline-btn:hover{border-color:#c00;color:#fff}
#baseline-btn:disabled{opacity:.4;cursor:not-allowed}

/* ── Right panel ── */
.right{flex:1;display:flex;flex-direction:column;overflow:hidden}

.status-bar{padding:8px 20px;background:#161616;border-bottom:1px solid #2e2e2e;font-size:.78rem;color:#666;display:flex;gap:20px;flex-wrap:wrap;flex-shrink:0}
.status-bar b{color:#bbb}

#log{height:52px;overflow-y:auto;padding:6px 20px;font-family:monospace;font-size:.78rem;color:#6dba8d;line-height:1.75;flex-shrink:0;border-bottom:1px solid #2e2e2e}
.log-dim{color:#555}
.log-err{color:#f88}

.results{flex:1;overflow-y:auto}
.results-hdr{padding:8px 16px;font-size:.88rem;font-weight:600;color:#ccc;background:#111;position:sticky;top:0;z-index:1;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{background:#c00;color:#fff;font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:10px}
.cat-sel{padding:5px 8px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;outline:none;cursor:pointer}
.cat-sel:focus{border-color:#c00}
#res-search-wrap{margin-left:auto;display:flex;align-items:center;gap:6px}
#res-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.8rem;width:180px;outline:none}
#res-search:focus{border-color:#c00}
#res-search-count{font-size:.75rem;color:#555;white-space:nowrap}

table{width:100%;border-collapse:collapse;font-size:.83rem;table-layout:auto}
th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 10px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:40px;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:#ccc}
th.sort-asc::after{content:" ▲";color:#c00;font-size:.6rem}
th.sort-desc::after{content:" ▼";color:#c00;font-size:.6rem}
td{padding:7px 10px;border-bottom:1px solid #1c1c1c;color:#ddd;max-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td:nth-child(1){width:62px;min-width:62px;max-width:62px}
td:nth-child(2){max-width:260px;width:35%}
tr:hover td{background:#161616}
td a{color:#6ab0f5;text-decoration:none}
td a:hover{text-decoration:underline}
.tag{background:#c00;color:#fff;font-size:.65rem;font-weight:700;padding:1px 5px;border-radius:3px}
.tag-drop{background:#1a3a1a;color:#4ade80;font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;border:1px solid #2d6a2d;white-space:nowrap}
.tag-sold{background:#3a1a1a;color:#f87171;font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;border:1px solid #6a2d2d}
.watch-btn{background:none;border:none;cursor:pointer;color:#444;font-size:1rem;line-height:1;padding:0 2px;transition:color .15s;flex-shrink:0}
.watch-btn:hover{color:#f5c518}
.watch-btn.active{color:#f5c518}
tr.sold-row td{color:#666}
tr.sold-row td a{color:#666}
.no-res{padding:24px 20px;color:#555;font-size:.85rem}

/* ── Password modal ── */
#pw-modal{display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center}
#pw-overlay{position:absolute;inset:0;background:rgba(0,0,0,.7)}
#pw-box{position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;padding:30px 28px;width:340px;z-index:1}
#pw-box h2{color:#fff;font-size:1.05rem;margin-bottom:6px}
#pw-box p{color:#777;font-size:.82rem;margin-bottom:18px;line-height:1.5}
#pw-input{width:100%;padding:9px 12px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#eee;font-size:.95rem;outline:none;margin-bottom:10px}
#pw-input:focus{border-color:#c00}
#pw-err{color:#f88;font-size:.8rem;margin-bottom:10px;display:none}
.pw-btns{display:flex;gap:8px}
.pw-btns button{flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:none}
#pw-cancel{background:#2a2a2a;color:#aaa;border:1px solid #3a3a3a!important}
#pw-cancel:hover{color:#fff}
#pw-confirm{background:#c00;color:#fff}
#pw-confirm:hover{background:#e00}
/* ── CL Search tab ── */
#cl-panel{flex-direction:row}
.cl-left{width:220px;min-width:200px;background:#1a1a1a;border-right:1px solid #2e2e2e;display:flex;flex-direction:column;flex-shrink:0}
.cl-left .search-wrap{padding:10px 12px;border-bottom:1px solid #2e2e2e;flex-shrink:0}
#cl-city-search{width:100%;padding:7px 11px;border-radius:5px;background:#252525;border:1px solid #3a3a3a;color:#eee;font-size:.875rem;outline:none}
#cl-city-search:focus{border-color:#a5b4fc}
.cl-sel-btns{display:flex;gap:6px;margin-top:8px}
.cl-sel-btn{flex:1;padding:5px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#aaa;font-size:.75rem;cursor:pointer}
.cl-sel-btn:hover{border-color:#a5b4fc;color:#fff}
.cl-sel-btn.active{border-color:#a5b4fc;color:#c7d2fe;background:#1a1f35}
#cl-city-list{flex:1;overflow-y:auto;padding:4px 0}
.cl-city-row{display:flex;align-items:center;padding:6px 12px;gap:8px;cursor:pointer}
.cl-city-row:hover{background:#222}
.cl-city-row input[type=checkbox]{accent-color:#a5b4fc;flex-shrink:0;cursor:pointer}
.cl-city-row label{flex:1;font-size:.855rem;cursor:pointer}
.cl-fav-btn{background:none;border:none;cursor:pointer;font-size:1rem;line-height:1;padding:0 4px;color:#444;flex-shrink:0;transition:color .15s}
.cl-fav-btn.active{color:#f5c518}
.cl-fav-btn:hover{color:#f5c518}
.cl-left-footer{padding:12px;border-top:1px solid #2e2e2e;flex-shrink:0}
.cl-right{display:flex;flex-direction:column;flex:1;overflow:hidden}
.cl-search-bar{padding:12px 16px;border-bottom:1px solid #2e2e2e;display:flex;gap:10px;align-items:center;flex-shrink:0;background:#111}
#cl-query{flex:1;padding:9px 14px;border-radius:6px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.95rem;outline:none}
#cl-query:focus{border-color:#a5b4fc}
#cl-search-btn{padding:9px 20px;background:#a5b4fc;color:#fff;border:none;border-radius:6px;font-size:.88rem;font-weight:700;cursor:pointer;white-space:nowrap}
#cl-search-btn:hover{background:#818cf8}
#cl-search-btn:disabled{opacity:.6;cursor:not-allowed}
#cl-status{font-size:.8rem;color:#c7d2fe;padding:0 4px}
.cl-results-hdr{padding:10px 16px;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:10px;flex-shrink:0;background:#141414}
#cl-count{font-size:.85rem;color:#c7d2fe;font-weight:600}
#cl-res-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;outline:none;margin-left:auto;width:200px}
#cl-res-search:focus{border-color:#a5b4fc}
#cl-body{flex:1;overflow-y:auto}
#cl-body table{width:100%;border-collapse:collapse;font-size:.83rem;table-layout:auto}
#cl-body th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 10px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}
#cl-body th:hover{color:#ccc}
#cl-body th.sort-asc::after{content:" ▲";color:#a5b4fc;font-size:.6rem}
#cl-body th.sort-desc::after{content:" ▼";color:#a5b4fc;font-size:.6rem}
#cl-body td{padding:7px 10px;border-bottom:1px solid #1c1c1c;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0}
#cl-body td:nth-child(1){width:45%;max-width:400px}
#cl-body td:nth-child(2){width:90px}
#cl-body td:nth-child(3){width:200px}
#cl-body td:nth-child(4){width:90px}
#cl-body tr:hover td{background:#161616}
#cl-body tr.cl-fav-result td{background:#1a1f35}
#cl-body tr.cl-fav-result:hover td{background:#252b45}
#cl-body td a{color:#c7d2fe;text-decoration:none}
#cl-body td a:hover{text-decoration:underline}
.cl-empty{padding:32px;color:#555;font-size:.9rem;text-align:center}
.cl-fav-star{color:#f5c518;margin-right:4px;font-size:.8rem}
</style>
</head>
<body>

<!-- Password modal -->
<!-- Validate stores modal -->
<div id="vs-modal" style="display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.7)" onclick="cancelValidate()"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;padding:30px 28px;width:360px;z-index:1">
    <h2 style="color:#fff;font-size:1.05rem;margin-bottom:8px">✓ Validate Stores</h2>
    <p style="color:#777;font-size:.82rem;margin-bottom:18px;line-height:1.6">Clear the invalid-stores blocklist before validating?<br><br>
    <b style="color:#ccc">Yes (recommended)</b> — re-checks all stores including any previously removed ones.<br><br>
    <b style="color:#ccc">No</b> — only checks stores currently in your list.</p>
    <div style="display:flex;gap:8px">
      <button onclick="cancelValidate()" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:1px solid #3a3a3a;background:#2a2a2a;color:#aaa">Cancel</button>
      <button onclick="startValidate(false)" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;background:#444;color:#eee">No</button>
      <button onclick="startValidate(true)" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;background:#c00;color:#fff">Yes</button>
    </div>
  </div>
</div>

<div id="pw-modal">
  <div id="pw-overlay" onclick="cancelBaseline()"></div>
  <div id="pw-box">
    <h2>🌐 Build Nationwide Baseline</h2>
    <p>This scan covers ~300 stores and takes 30–60 minutes. Enter the password to continue.</p>
    <input type="password" id="pw-input" placeholder="Password"
           onkeydown="if(event.key==='Enter')confirmBaseline()">
    <div id="pw-err">Incorrect password.</div>
    <div class="pw-btns">
      <button id="pw-cancel" onclick="cancelBaseline()">Cancel</button>
      <button id="pw-confirm" onclick="confirmBaseline()">Continue →</button>
    </div>
  </div>
</div>

<header>
  <h1>🎸 Gear Finder</h1>
  <button id="stop-btn" onclick="stopRun()">⏹ Stop Running</button>
  <span id="hdr-status">Loading…</span>
</header>

<div id="update-banner" style="display:none;background:#1a3a1a;border-bottom:1px solid #2a6a2a;padding:8px 24px;align-items:center;gap:12px;font-size:.82rem;color:#8fc98f">
  ⬆ Update available: v<span id="update-version"></span> —
  <button onclick="installUpdate()" style="padding:4px 12px;background:#2a6a2a;color:#8fc98f;border:1px solid #3a8a3a;border-radius:4px;cursor:pointer;font-size:.8rem">Install Update</button>
  <button onclick="document.getElementById('update-banner').style.display='none'" style="padding:4px 8px;background:none;color:#666;border:none;cursor:pointer;font-size:.8rem">✕</button>
</div>

<div class="app-tabs">
  <button class="app-tab gc-tab active" onclick="switchTab('gc')">🎸 GC Used Inventory</button>
  <button class="app-tab cl-tab" onclick="switchTab('cl')">🟣 CL National Search</button>
</div>

<!-- ══ GC PANEL ══ -->
<div class="app-panel active" id="gc-panel">
<div class="layout">

  <div class="left">
    <div class="search-wrap" id="search-wrap">
      <input id="search" type="text" placeholder="Search stores…" autocomplete="off">
      <div class="sel-btns">
        <button class="sel-btn" id="favs-btn" onclick="toggleFavsFilter()">★ Favorites</button>
        <button class="sel-btn" onclick="selectAll()">Select All</button>
        <button class="sel-btn" onclick="clearAll()">Clear All</button>
      </div>
    </div>

    <div id="store-list"></div>

    <div class="left-footer">
      <div id="sel-count">0 stores selected</div>
      <div class="btn-row">
        <button id="run-btn"      onclick="runTracker()" disabled>Check for New</button>
        <button id="baseline-btn" onclick="runBaseline()" title="Scan every GC store nationwide">🌐 Build Baseline</button>
      </div>
      <button id="validate-stores-btn" onclick="validateStores()"
        style="margin-top:8px;width:100%;padding:7px;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:5px;color:#777;font-size:.75rem;cursor:pointer"
        title="Check all stores and remove any that no longer exist">
        ✓ Validate Stores
      </button>
      <button id="reset-btn" onclick="resetData()"
        style="margin-top:6px;width:100%;padding:7px;background:#1a1a1a;border:1px solid #5a2a2a;border-radius:5px;color:#a05050;font-size:.75rem;cursor:pointer"
        title="Delete all cached data and start fresh">
        🗑 Reset All Data
      </button>
    </div>
  </div>

  <div class="right">
    <div class="status-bar">
      <span>Last run: <b id="s-last">—</b></span>
      <span>Known items: <b id="s-known">—</b></span>
      <span>Stores: <b id="s-stores">—</b></span>
      <span id="s-excel" style="display:none"><a style="color:#6ab0f5" href="/download/excel">Download Excel ↗</a></span>
      <button id="watchlist-btn" onclick="showWatchList()"
        style="margin-left:auto;padding:5px 12px;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:4px;color:#aaa;font-size:.78rem;cursor:pointer;white-space:nowrap">
        ★ My Watch List
      </button>
    </div>
    <div id="log"><span class="log-dim">Ready — select stores and click Run, or build a full baseline.</span></div>
    <div class="results" id="res-panel" style="display:none">
      <div class="results-hdr">
        <span id="res-title">New Items</span>
        <span class="badge" id="res-badge"></span>
        <select id="cond-filter" class="cat-sel" style="display:none" onchange="filterResults()">
          <option value="">All Conditions</option>
        </select>
        <select id="cat-filter" class="cat-sel" style="display:none" onchange="onCatFilterChange()">
          <option value="">All Categories</option>
        </select>
        <select id="subcat-filter" class="cat-sel" style="display:none" onchange="filterResults()">
          <option value="">All Subcategories</option>
        </select>
        <button id="clear-filters-btn" onclick="clearFilters()"
          style="display:none;padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #c00;color:#f88;font-size:.78rem;cursor:pointer;white-space:nowrap">
          ✕ Clear Filters
        </button>
        <div id="res-search-wrap">
          <input id="res-search" type="text" placeholder="Filter results…" oninput="filterResults()" autocomplete="off">
          <span id="res-search-count"></span>
        </div>
      </div>
      <div id="res-body"></div>
    </div>
  </div>

</div>
</div><!-- end gc-panel -->

<!-- ══ CL PANEL ══ -->
<div class="app-panel" id="cl-panel">

  <!-- Left sidebar: city list -->
  <div class="cl-left">
    <div class="search-wrap cl-left">
      <input id="cl-city-search" type="text" placeholder="Search cities…" autocomplete="off" oninput="clFilterCities()">
      <div class="cl-sel-btns">
        <button class="cl-sel-btn" id="cl-favs-btn" onclick="clToggleFavs()">★ Favorites</button>
        <button class="cl-sel-btn" onclick="clSelectAll()">Select All</button>
        <button class="cl-sel-btn" onclick="clClearAll()">Clear All</button>
      </div>
    </div>
    <div id="cl-city-list"></div>
  </div>

  <!-- Right content: search bar + results -->
  <div class="cl-right">
    <div class="cl-search-bar">
      <input id="cl-query" type="text" placeholder="e.g. telecaster, les paul, fender twin…" autocomplete="off"
        onkeydown="if(event.key==='Enter') clSearch()">
      <span id="cl-status"></span>
      <button id="cl-search-btn" onclick="clSearch()">Search</button>
    </div>
    <div class="cl-results-hdr" id="cl-results-hdr" style="display:none">
      <span id="cl-count"></span>
      <input id="cl-res-search" type="text" placeholder="Filter results…" oninput="clFilterResults()" autocomplete="off">
    </div>
    <div id="cl-body"><div class="cl-empty">Select cities on the left, enter a search term, and click Search.</div></div>
  </div>

</div>

<script>
let allStores = [], favorites = [], running = false;
const BASELINE_PW = 'Beatle909!';

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('search').addEventListener('input', filterList);
  clRenderCities();
  await loadData();
  await loadState();
  await loadWatchlist();
});

async function loadData() {
  const r = await fetch('/api/stores');
  const d = await r.json();
  allStores = d.stores; favorites = d.favorites;
  renderList();
  const info = d.info || {};
  const storeLabel = info.count ? info.count + ' stores' : allStores.length + ' stores';
  document.getElementById('hdr-status').textContent = storeLabel + ' available';
  document.getElementById('s-stores').textContent = storeLabel +
    (info.updated ? ' · checked ' + info.updated.slice(0,10) : ' (fallback list)');
  if (allStores.length === 0) {
    appendLog('💡 No stores loaded — click "✓ Validate Stores" to build the store list from GC live data.', 'log-dim');
  }
}

async function loadState() {
  const r = await fetch('/api/state');
  const s = await r.json();
  document.getElementById('s-last').textContent  = s.last_run ? s.last_run.replace('T',' ').slice(0,16) : 'Never';
  document.getElementById('s-known').textContent = s.known_items.toLocaleString();
  if (s.excel_exists) document.getElementById('s-excel').style.display = 'inline';
  if (s.is_first_run) {
    appendLog('💡 First run detected — click "🌐 Build Baseline" to capture the full current GC used inventory as your starting point.', 'log-dim');
  }
  // Check for updates
  try {
    const vr = await fetch('/api/version');
    const vd = await vr.json();
    if (vd.update_available) {
      const banner = document.getElementById('update-banner');
      if (banner) {
        banner.style.display = 'flex';
        banner.querySelector('#update-version').textContent = vd.latest;
      }
    }
  } catch(e) {}
}

// ── Refresh store list ────────────────────────────────────────────────────────

// ── Mode switching ────────────────────────────────────────────────────────────
let favsOnly = false;

function toggleFavsFilter() {
  favsOnly = !favsOnly;
  const btn = document.getElementById('favs-btn');
  btn.classList.toggle('active', favsOnly);
  document.getElementById('search').value = '';
  renderList();
}

function selectAll() {
  document.querySelectorAll('.store-row:not(.hidden) input[type=checkbox]').forEach(cb => cb.checked = true);
  updateCount();
}
function clearAll() {
  document.querySelectorAll('.store-row input[type=checkbox]').forEach(cb => cb.checked = false);
  updateCount();
}

// ── Render store list ─────────────────────────────────────────────────────────
function renderList() {
  const el = document.getElementById('store-list');
  const q  = document.getElementById('search').value.toLowerCase();
  let stores = favsOnly ? favorites : allStores;

  if (favsOnly && !stores.length) {
    el.innerHTML = '<div class="empty-msg">No favorites yet.<br>Click ★ next to any store to add it.</div>';
    updateCount(); return;
  }

  const filtered = q ? stores.filter(s => s.toLowerCase().includes(q)) : stores;
  el.innerHTML = '';
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/[^a-zA-Z0-9]/g,'_');
    div.innerHTML =
      `<input type="checkbox" id="${id}" value="${name}">` +
      `<label for="${id}">${name}</label>` +
      `<button class="fav-btn ${isFav?'active':''}" title="${isFav?'Remove from':'Add to'} favorites"
        onclick="toggleFav(event,'${name.replace(/'/g,"\\'")}',this)">★</button>`;
    div.querySelector('input').addEventListener('change', updateCount);
    el.appendChild(div);
  });
  updateCount();
}

function filterList() { renderList(); }

// ── Favorites ─────────────────────────────────────────────────────────────────
async function toggleFav(e, name, btn) {
  e.stopPropagation();
  const adding = !favorites.includes(name);
  const r = await fetch('/api/favorites', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({store: name, action: adding ? 'add' : 'remove'})
  });
  const d = await r.json();
  favorites = d.favorites;
  btn.classList.toggle('active', adding);
  btn.title = (adding ? 'Remove from' : 'Add to') + ' favorites';
  if (favsOnly) renderList();
}

// ── Selection ─────────────────────────────────────────────────────────────────
function updateCount() {
  const checked = [...document.querySelectorAll('.store-row input:checked')];
  const n = checked.length;
  document.getElementById('sel-count').textContent = n + ' store' + (n===1?'':'s') + ' selected';
  document.getElementById('run-btn').disabled = (n===0 || running);
  document.getElementById('baseline-btn').disabled = running;
  // Auto-browse cached inventory when stores are selected
  if (n > 0 && !running) browseCache();
  else if (n === 0) {
    document.getElementById('res-panel').style.display = 'none';
  }
}

// ── Browse cached inventory ────────────────────────────────────────────────
let _browseTimer = null;
async function browseCache() {
  clearTimeout(_browseTimer);
  _browseTimer = setTimeout(async () => {
    const stores = getSelected();
    if (!stores.length) return;
    const r = await fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({stores})
    });
    const d = await r.json();
    if (d.no_store_data) {
      // Cache exists but doesn't have per-store data yet — prompt a run
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Browse Data Yet';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML =
        '<div class="no-res">Click <b>Check for New Items</b> once to populate store inventory data. After that, selecting stores will instantly show their inventory.</div>';
      ['cond-filter','cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
      return;
    }
    if (!d.items || !d.items.length) {
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Items Found';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML = '<div class="no-res">No cached inventory for selected store(s). Run Check for New Items to scan.</div>';
      return;
    }
    window._tableData = d.items.map(item => ({...item}));
    window._sortCol = null; window._sortDir = 1;
    const n = d.items.filter(i => i.isNew).length;
    const total = d.items.length;
    document.getElementById('res-title').textContent = n > 0 ? `${n} New · ${total} Total` : `${total} Items`;
    document.getElementById('res-badge').textContent = n > 0 ? n + ' NEW' : '';
    document.getElementById('res-panel').style.display = 'block';
    document.getElementById('res-search').value = '';
    document.getElementById('res-search-count').textContent = '';
    ['cond-filter','cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
    populateCategoryFilter();
    renderTable();
  }, 300);
}

// ── Watch list ────────────────────────────────────────────────────────────
window._watchlist = {};

async function loadWatchlist() {
  try {
    const r = await fetch('/api/watchlist');
    const d = await r.json();
    window._watchlist = d.watchlist || {};
  } catch(e) {}
}

async function toggleWatch(id, btn) {
  const isWatched = !!(window._watchlist[id]);
  const action = isWatched ? 'remove' : 'add';
  // Get item data from table row
  const row = btn.closest('tr');
  const cells = row ? row.querySelectorAll('td') : [];
  const itemData = {
    id,
    name:  row ? row.dataset.name : '',
    store: row ? row.dataset.store : '',
  };
  const r = await fetch('/api/watchlist', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({...itemData, action})
  });
  const d = await r.json();
  window._watchlist = d.watchlist || {};
  btn.classList.toggle('active', !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
}

async function showWatchList() {
  const r = await fetch('/api/watchlist/items');
  const d = await r.json();
  if (!d.items || !d.items.length) {
    appendLog('Your watch list is empty — click ☆ next to any item to add it.', 'log-dim');
    return;
  }
  window._tableData = d.items;
  window._sortCol = null; window._sortDir = 1;
  const soldCount = d.items.filter(i => i.sold).length;
  document.getElementById('res-title').textContent = `Watch List (${d.items.length} items${soldCount ? ', ' + soldCount + ' sold' : ''})`;
  document.getElementById('res-badge').textContent = '';
  document.getElementById('res-panel').style.display = '';
  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  ['cond-filter','cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
  populateCategoryFilter();
  renderTable();
}



function getSelected() {
  return [...document.querySelectorAll('.store-row input:checked')].map(c => c.value);
}


function runBaseline() {
  document.getElementById('pw-modal').style.display = 'flex';
  document.getElementById('pw-input').value = '';
  document.getElementById('pw-err').style.display = 'none';
  setTimeout(() => document.getElementById('pw-input').focus(), 50);
}

function cancelBaseline() {
  document.getElementById('pw-modal').style.display = 'none';
}

function confirmBaseline() {
  const pw = document.getElementById('pw-input').value;
  if (pw !== BASELINE_PW) {
    document.getElementById('pw-err').style.display = 'block';
    document.getElementById('pw-input').select();
    return;
  }
  document.getElementById('pw-modal').style.display = 'none';
  startRun({stores:[], baseline:true}, true);
}

// ── Run ───────────────────────────────────────────────────────────────────────
async function runTracker() {
  const stores = getSelected();
  if (!stores.length) return;
  await startRun({stores}, false);
}

async function stopRun() {
  const btn = document.getElementById('stop-btn');
  btn.textContent = '⏹ Stopping…';
  btn.disabled = true;
  await fetch('/api/stop', {method:'POST'});
}

async function startRun(payload, isBaseline) {
  running = true; updateCount();
  const stopBtn = document.getElementById('stop-btn');
  stopBtn.style.display = 'inline-block';
  stopBtn.disabled = false;
  stopBtn.textContent = '⏹ Stop Running';

  document.getElementById('res-panel').style.display = 'none';
  document.getElementById('log').innerHTML = '';

  const resp = await fetch('/api/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  if (!resp.ok) {
    const e = await resp.json();
    appendLog(e.error, 'log-err');
    running = false; stopBtn.style.display = 'none'; updateCount(); return;
  }

  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      stopBtn.style.display = 'none';
      updateCount(); loadState(); showResults(msg, isBaseline);
    }
  };
}

// ── Results ───────────────────────────────────────────────────────────────────
function showResults(msg, isBaseline) {
  const panel = document.getElementById('res-panel');
  panel.style.display = 'block';

  if (msg.error) {
    document.getElementById('res-title').textContent = 'Error';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML = `<div class="no-res" style="color:#f88">${msg.error}</div>`;
    return;
  }

  const n = msg.new_count;
  const stoppedNote = msg.stopped ? ' (stopped early)' : '';
  appendLog(`\\n✓ Done${stoppedNote} — ${msg.scanned.toLocaleString()} items scanned, ${n} new.`, 'log-dim');

  if (isBaseline && n === 0) {
    document.getElementById('res-title').textContent = 'Baseline Complete';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML =
      `<div class="no-res">Full inventory baseline saved (${msg.scanned.toLocaleString()} items)${stoppedNote}. Run again any time to see what's new!</div>`;
    ['cond-filter','cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  const total = n + (msg.all_items ? msg.all_items.length : 0);
  document.getElementById('res-title').textContent = n > 0 ? `${n} New · ${total} Total` : `${total} Items (nothing new)`;
  document.getElementById('res-badge').textContent  = n > 0 ? n + ' NEW' : '';
  if (n > 0) document.getElementById('s-excel').style.display = 'inline';

  if (total === 0) {
    document.getElementById('res-body').innerHTML = '<div class="no-res">Nothing found for selected stores.</div>';
    ['cond-filter','cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  window._tableData = [];
  (msg.new_items || []).forEach(item => window._tableData.push({isNew:true,  ...item}));
  (msg.all_items  || []).forEach(item => window._tableData.push({isNew:false, ...item}));
  window._sortCol = null; window._sortDir = 1;
  // Reload watchlist so sold flags are fresh
  loadWatchlist().then(() => {
    populateCategoryFilter();
    renderTable();
  });
}

// ── Category filters ──────────────────────────────────────────────────────────
function populateCategoryFilter() {
  const data = window._tableData || [];
  // Condition filter
  const conds = [...new Set(data.map(i => i.condition).filter(Boolean))].sort();
  const condEl = document.getElementById('cond-filter');
  condEl.innerHTML = '<option value="">All Conditions</option>';
  conds.forEach(c => { const o = document.createElement('option'); o.value=o.textContent=c; condEl.appendChild(o); });
  condEl.style.display = conds.length ? '' : 'none';
  condEl.value = '';
  // Category filter
  const cats = [...new Set(data.map(i => i.category).filter(Boolean))].sort();
  const catEl = document.getElementById('cat-filter');
  catEl.innerHTML = '<option value="">All Categories</option>';
  cats.forEach(c => { const o = document.createElement('option'); o.value=o.textContent=c; catEl.appendChild(o); });
  catEl.style.display = cats.length ? '' : 'none';
  catEl.value = '';
  document.getElementById('subcat-filter').style.display = 'none';
}

function onCatFilterChange() {
  const cat   = document.getElementById('cat-filter').value;
  const data  = window._tableData || [];
  const subcats = [...new Set(
    data.filter(i => !cat || i.category === cat).map(i => i.subcategory).filter(Boolean)
  )].sort();
  const subEl = document.getElementById('subcat-filter');
  if (subcats.length && cat) {
    subEl.innerHTML = '<option value="">All Subcategories</option>';
    subcats.forEach(s => { const o = document.createElement('option'); o.value=o.textContent=s; subEl.appendChild(o); });
    subEl.style.display = '';
  } else {
    subEl.style.display = 'none';
  }
  subEl.value = '';
  filterResults();
}

// ── Table rendering & sorting ─────────────────────────────────────────────────
// col indices: 0=status, 1=name, 2=condition, 3=category, 4=subcategory, 5=price, 6=date, 7=store
const _SORT_COLS = [null, 'name', 'price', 'condition', 'category', 'subcategory', 'date', 'store'];

function renderTable() {
  const data = window._tableData || [];
  let html = `<table id="res-table"><thead><tr>
    <th data-col="0"></th>
    <th data-col="watch" style="width:32px"></th>
    <th data-col="1">Item</th>
    <th data-col="drop" style="width:80px"></th>
    <th data-col="2">Price</th>
    <th data-col="3">Condition</th>
    <th data-col="4">Category</th>
    <th data-col="5">Subcategory</th>
    <th data-col="6">Date</th>
    <th data-col="7">Store</th>
  </tr></thead><tbody>`;
  data.forEach(item => {
    const priceNum = parseFloat((item.price||'').replace(/[^0-9.]/g,'')) || 0;
    const esc = s => (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    const nameCell = item.url
      ? `<a href="${item.url}" target="_blank">${esc(item.name)}</a>`
      : esc(item.name);
    const isSold = item.sold || false;
    const isWatched = (window._watchlist || {})[item.id || ''];
    const watchStar = item.id
      ? `<button class="watch-btn ${isWatched ? 'active' : ''}" onclick="toggleWatch('${(item.id||'').replace(/'/g,"\\'")}',this)" title="${isWatched ? 'Remove from' : 'Add to'} watch list">${isWatched ? '★' : '☆'}</button>`
      : '';
    const dropCell = item.price_drop > 0
      ? `<span class="tag-drop">↓ $${Math.round(item.price_drop)}</span>`
      : '';
    const soldBadge = isSold ? ' <span class="tag-sold">Sold</span>' : '';
    html += `<tr class="${isSold ? 'sold-row' : ''}" data-name="${esc(item.name)}" data-price="${priceNum}" data-store="${esc(item.store)}" data-condition="${esc(item.condition)}" data-category="${esc(item.category)}" data-subcategory="${esc(item.subcategory)}">` +
      `<td>${item.isNew ? '<span class="tag">NEW</span>' : ''}</td>` +
      `<td>${watchStar}</td>` +
      `<td>${nameCell}${soldBadge}</td>` +
      `<td>${dropCell}</td>` +
      `<td>${item.price||''}</td>` +
      `<td>${esc(item.condition)}</td>` +
      `<td>${esc(item.category)}</td>` +
      `<td>${esc(item.subcategory)}</td>` +
      `<td>${esc(item.date||'')}</td>` +
      `<td>${esc(item.store)}</td>` +
      `</tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('res-body').innerHTML = html;

  if (window._sortCol !== null) {
    const th = document.querySelector(`#res-table th[data-col="${window._sortCol}"]`);
    if (th) th.classList.add(window._sortDir === 1 ? 'sort-asc' : 'sort-desc');
  }

  document.querySelectorAll('#res-table thead th[data-col]').forEach(th => {
    const colIdx = parseInt(th.dataset.col);
    if (!_SORT_COLS[colIdx]) return;
    th.addEventListener('click', () => sortTable(colIdx));
  });

  filterResults();
  autoSizeItemColumn();
}

function sortTable(colIdx) {
  const field = _SORT_COLS[colIdx];
  if (!field) return;
  window._sortDir = (window._sortCol === colIdx) ? window._sortDir * -1 : 1;
  window._sortCol = colIdx;
  const dir = window._sortDir;
  window._tableData.sort((a, b) => {
    let av = a[field] || '', bv = b[field] || '';
    if (field === 'price') {
      av = parseFloat((av+'').replace(/[^0-9.]/g,'')) || 0;
      bv = parseFloat((bv+'').replace(/[^0-9.]/g,'')) || 0;
      return (av - bv) * dir;
    }
    if (field === 'date') {
      // Sort dates descending by default (newest first), ascending on toggle
      return av.toString().localeCompare(bv.toString()) * dir;
    }
    return av.toString().localeCompare(bv.toString()) * dir;
  });
  renderTable();
}

function autoSizeItemColumn() {
  // Measure the longest visible item name using a hidden canvas for accuracy
  const data = window._tableData || [];
  if (!data.length) return;
  const canvas = autoSizeItemColumn._canvas || (autoSizeItemColumn._canvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  ctx.font = '13.3px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'; // matches td font ~.83rem
  let maxW = 80;
  data.forEach(item => {
    const w = ctx.measureText(item.name || '').width;
    if (w > maxW) maxW = w;
  });
  // Add padding (link underline cursor area + 24px right padding)
  const colW = Math.min(Math.ceil(maxW) + 32, 520); // cap at 520px
  const th = document.querySelector('#res-table th[data-col="1"]');
  if (th) th.style.width = colW + 'px';
  // Also set td widths via col group or direct style on first td of each row
  document.querySelectorAll('#res-table tbody tr td:nth-child(2)').forEach(td => {
    td.style.maxWidth = colW + 'px';
  });
}

// ── Results filter ────────────────────────────────────────────────────────────
function clearFilters() {
  document.getElementById('cond-filter').value = '';
  document.getElementById('cat-filter').value = '';
  const subEl = document.getElementById('subcat-filter');
  subEl.value = '';
  subEl.style.display = 'none';
  document.getElementById('clear-filters-btn').style.display = 'none';
  filterResults();
}

function filterResults() {
  const q      = document.getElementById('res-search').value.toLowerCase().trim();
  const cond   = document.getElementById('cond-filter').value;
  const cat    = document.getElementById('cat-filter').value;
  const subcat = document.getElementById('subcat-filter').value;
  const rows   = document.querySelectorAll('#res-body tbody tr');
  let visible  = 0;
  rows.forEach(row => {
    const show = (!q      || row.textContent.toLowerCase().includes(q)) &&
                 (!cond   || (row.dataset.condition   || '') === cond) &&
                 (!cat    || (row.dataset.category    || '') === cat) &&
                 (!subcat || (row.dataset.subcategory || '') === subcat);
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const countEl = document.getElementById('res-search-count');
  countEl.textContent = (q || cond || cat || subcat) ? `${visible} of ${rows.length}` : '';
  const clearBtn = document.getElementById('clear-filters-btn');
  if (clearBtn) clearBtn.style.display = (cond || cat || subcat) ? '' : 'none';
}

// ── Validate Stores ───────────────────────────────────────────────────────────
function validateStores() {
  if (running) { appendLog('Stop the current run before validating.', 'log-err'); return; }
  document.getElementById('vs-modal').style.display = 'flex';
}

function cancelValidate() {
  document.getElementById('vs-modal').style.display = 'none';
}

async function startValidate(clearBlocklist) {
  document.getElementById('vs-modal').style.display = 'none';

  if (clearBlocklist) {
    await fetch('/api/clear-blocklist', {method: 'POST'});
    appendLog('Blocklist cleared — all stores will be re-evaluated.', 'log-dim');
  }

  const btn = document.getElementById('validate-stores-btn');
  const resp = await fetch('/api/validate-stores', {method: 'POST'});
  if (!resp.ok) {
    const e = await resp.json();
    appendLog('Validate error: ' + e.error, 'log-err');
    return;
  }
  running = true; updateCount();
  btn.textContent = '⏳ Validating…';
  btn.disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  document.getElementById('stop-btn').disabled = false;
  document.getElementById('stop-btn').textContent = '⏹ Stop';
  document.getElementById('log').innerHTML = '';
  appendLog('Checking all stores for 404s, then rebuilding from GC live data…');

  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      document.getElementById('stop-btn').style.display = 'none';
      btn.textContent = '✓ Validate Stores';
      btn.disabled = false;
      updateCount();
      loadData();
    }
  };
}

// ── Auto-updater ──────────────────────────────────────────────────────────────
async function installUpdate() {
  if (running) { appendLog('Stop the current run before updating.', 'log-err'); return; }
  document.getElementById('update-banner').style.display = 'none';
  document.getElementById('log').innerHTML = '';
  appendLog('⬆ Downloading update from GitHub…');
  await fetch('/api/update', {method: 'POST'});
  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close();
      if (msg.update_success) {
        appendLog('✓ Update installed! Please restart the app (close this window and re-run the launcher).', 'log-dim');
      }
    }
  };
}

// ── Reset ─────────────────────────────────────────────────────────────────────
async function resetData() {
  if (running) { appendLog('Stop the current run before resetting.', 'log-err'); return; }
  const pw = prompt('Enter your app password to reset all data (state, cache, Excel):');
  if (pw === null) return;
  if (!pw) return;
  const r = await fetch('/api/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pw})
  });
  const d = await r.json();
  if (!r.ok) {
    appendLog('Reset failed: ' + (d.error || 'unknown error'), 'log-err');
    return;
  }
  appendLog('✓ ' + d.status + (d.deleted.length ? ' Deleted: ' + d.deleted.join(', ') : ''), 'log-dim');
  document.getElementById('s-last').textContent  = 'Never';
  document.getElementById('s-known').textContent = '0';
  document.getElementById('s-excel').style.display = 'none';
}

// ── Log helper ────────────────────────────────────────────────────────────────
function appendLog(text, cls) {
  const log  = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.app-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.app-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('.' + tab + '-tab').classList.add('active');
  document.getElementById(tab + '-panel').classList.add('active');
}

// ── CL City list ──────────────────────────────────────────────────────────────
const CL_CITIES = [
  {id:'albuquerque',label:'Albuquerque'}, {id:'atlanta',label:'Atlanta'},
  {id:'austin',label:'Austin'},           {id:'baltimore',label:'Baltimore'},
  {id:'boise',label:'Boise'},             {id:'boston',label:'Boston'},
  {id:'buffalo',label:'Buffalo'},         {id:'charlotte',label:'Charlotte'},
  {id:'chicago',label:'Chicago'},         {id:'cincinnati',label:'Cincinnati'},
  {id:'cleveland',label:'Cleveland'},     {id:'columbus',label:'Columbus'},
  {id:'dallas',label:'Dallas'},           {id:'denver',label:'Denver'},
  {id:'desmoines',label:'Des Moines'},    {id:'detroit',label:'Detroit'},
  {id:'elpaso',label:'El Paso'},          {id:'fortworth',label:'Fort Worth'},
  {id:'fresno',label:'Fresno'},           {id:'grandrapids',label:'Grand Rapids'},
  {id:'greensboro',label:'Greensboro'},   {id:'hartford',label:'Hartford'},
  {id:'honolulu',label:'Honolulu'},       {id:'houston',label:'Houston'},
  {id:'indianapolis',label:'Indianapolis'},{id:'jacksonville',label:'Jacksonville'},
  {id:'kansascity',label:'Kansas City'},  {id:'knoxville',label:'Knoxville'},
  {id:'lasvegas',label:'Las Vegas'},      {id:'losangeles',label:'Los Angeles'},
  {id:'louisville',label:'Louisville'},   {id:'madison',label:'Madison'},
  {id:'memphis',label:'Memphis'},         {id:'miami',label:'Miami'},
  {id:'milwaukee',label:'Milwaukee'},     {id:'minneapolis',label:'Minneapolis'},
  {id:'nashville',label:'Nashville'},     {id:'neworleans',label:'New Orleans'},
  {id:'newyork',label:'New York'},        {id:'norfolk',label:'Norfolk'},
  {id:'oklahomacity',label:'Oklahoma City'},{id:'omaha',label:'Omaha'},
  {id:'orlando',label:'Orlando'},         {id:'philadelphia',label:'Philadelphia'},
  {id:'phoenix',label:'Phoenix'},         {id:'pittsburgh',label:'Pittsburgh'},
  {id:'portland',label:'Portland'},       {id:'providence',label:'Providence'},
  {id:'raleigh',label:'Raleigh'},         {id:'richmond',label:'Richmond'},
  {id:'riverside',label:'Riverside'},     {id:'rochester',label:'Rochester'},
  {id:'sacramento',label:'Sacramento'},   {id:'saltlakecity',label:'Salt Lake City'},
  {id:'sanantonio',label:'San Antonio'},  {id:'sandiego',label:'San Diego'},
  {id:'sfbay',label:'SF Bay Area'},       {id:'seattle',label:'Seattle'},
  {id:'spokane',label:'Spokane'},         {id:'stlouis',label:'St. Louis'},
  {id:'syracuse',label:'Syracuse'},       {id:'tampabay',label:'Tampa Bay'},
  {id:'toledo',label:'Toledo'},           {id:'tucson',label:'Tucson'},
  {id:'tulsa',label:'Tulsa'},             {id:'virginiabeach',label:'Virginia Beach'},
  {id:'washingtondc',label:'Washington DC'},{id:'wichita',label:'Wichita'},
];

let _clFavs = [];
try { _clFavs = JSON.parse(localStorage.getItem('cl_favs') || '[]'); } catch(e) {}
let _clFavsOnly = false;
let _clData = [];
let _clSortCol = null, _clSortDir = 1;

function clSaveFavs() {
  try { localStorage.setItem('cl_favs', JSON.stringify(_clFavs)); } catch(e) {}
}

function clRenderCities() {
  const q   = (document.getElementById('cl-city-search').value || '').toLowerCase();
  const list = document.getElementById('cl-city-list');
  const cities = _clFavsOnly
    ? CL_CITIES.filter(c => _clFavs.includes(c.id))
    : (q ? CL_CITIES.filter(c => c.label.toLowerCase().includes(q)) : CL_CITIES);

  list.innerHTML = '';
  cities.forEach(c => {
    const isFav = _clFavs.includes(c.id);
    const div = document.createElement('div');
    div.className = 'cl-city-row';
    const cbId = 'cl_cb_' + c.id;
    const cb  = document.createElement('input');
    cb.type = 'checkbox'; cb.id = cbId; cb.value = c.id;
    const lbl = document.createElement('label');
    lbl.htmlFor = cbId; lbl.textContent = c.label;
    const btn = document.createElement('button');
    btn.className = 'cl-fav-btn' + (isFav ? ' active' : '');
    btn.title = (isFav ? 'Remove from' : 'Add to') + ' favorites';
    btn.textContent = '★';
    btn.dataset.cityId = c.id;
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      clToggleFav(c.id, this);
    });
    div.appendChild(cb);
    div.appendChild(lbl);
    div.appendChild(btn);
    list.appendChild(div);
  });
}

function clFilterCities() { clRenderCities(); }

function clToggleFavs() {
  _clFavsOnly = !_clFavsOnly;
  document.getElementById('cl-favs-btn').classList.toggle('active', _clFavsOnly);
  document.getElementById('cl-city-search').value = '';
  clRenderCities();
}

function clToggleFav(id, btn) {
  if (_clFavs.includes(id)) {
    _clFavs = _clFavs.filter(f => f !== id);
    btn.classList.remove('active');
  } else {
    _clFavs.push(id);
    btn.classList.add('active');
  }
  clSaveFavs();
  if (_clFavsOnly) clRenderCities();
}

function clSelectAll() {
  document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = true);
}
function clClearAll() {
  document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = false);
}

function clGetSelected() {
  return [...document.querySelectorAll('#cl-city-list input[type=checkbox]:checked')].map(cb => cb.value);
}

// ── CL Search ─────────────────────────────────────────────────────────────────
async function clSearch() {
  const q = document.getElementById('cl-query').value.trim();
  if (!q) return;
  const selected = clGetSelected();
  const btn = document.getElementById('cl-search-btn');
  const status = document.getElementById('cl-status');
  btn.disabled = true;
  btn.textContent = 'Searching…';
  const cityCount = selected.length || CL_CITIES.length;
  status.textContent = 'Searching ' + cityCount + ' markets…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching…</div>';
  try {
    const cities = selected.length ? selected.join(',') : '';
    const r = await fetch('/api/cl-search?q=' + encodeURIComponent(q) + (cities ? '&cities=' + encodeURIComponent(cities) : ''));
    const d = await r.json();
    if (d.error) {
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">' + d.error + '</div>';
      return;
    }
    _clData = d.results || [];
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed: ' + e.message + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search';
  }
}

function clFilterResults() {
  const q = (document.getElementById('cl-res-search').value || '').toLowerCase();
  const rows = document.querySelectorAll('#cl-body tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const show = !q || row.textContent.toLowerCase().includes(q);
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('cl-count').textContent =
    q ? (visible + ' of ' + _clData.length + ' listings') : (_clData.length + ' listings');
}

function clRenderResults() {
  const hdr  = document.getElementById('cl-results-hdr');
  const body = document.getElementById('cl-body');
  if (!_clData.length) {
    body.innerHTML = '<div class="cl-empty">No listings found. Try a different search term or select more cities.</div>';
    hdr.style.display = 'none';
    return;
  }
  document.getElementById('cl-count').textContent = _clData.length + ' listings';
  document.getElementById('cl-res-search').value = '';
  hdr.style.display = 'flex';

  const cols = ['title','price','location','date','relevance'];
  const labels = ['Item','Price','Location','Date'];
  let html = '<table><thead><tr>';
  labels.forEach((l, i) => {
    const cls = _clSortCol === i ? (_clSortDir === 1 ? 'sort-asc' : 'sort-desc') : '';
    html += '<th class="' + cls + '" onclick="clSort(' + i + ')">' + l + '</th>';
  });
  html += '</tr></thead><tbody>';

  // Favorites first, then rest — within each group, sort by selected col
  const isFavResult = r => _clFavs.includes(r.cityId);

  // Relevance scoring based on current search query
  const rawQuery = (document.getElementById('cl-query').value || '').trim().toLowerCase();
  const queryWords = rawQuery.split(/[ \t]+/).filter(Boolean);
  function relevanceScore(title) {
    const t = (title || '').toLowerCase();
    if (!rawQuery) return 0;
    if (t.includes(rawQuery)) return 3;          // exact phrase
    if (queryWords.every(w => t.includes(w))) return 2;  // all words
    if (queryWords.some(w => t.includes(w))) return 1;   // some words
    return 0;
  }

  let sorted = [..._clData];
  if (_clSortCol !== null) {
    const key = cols[_clSortCol];
    sorted.sort((a, b) => {
      if (key === 'relevance') {
        // For relevance, desc = most relevant first (flip _clSortDir meaning)
        return _clSortDir * (relevanceScore(b.title) - relevanceScore(a.title));
      }
      const av = a[key] || '', bv = b[key] || '';
      if (key === 'price') {
        return _clSortDir * ((parseFloat(String(av).replace(/[^0-9.]/g,'')) || 0) -
                             (parseFloat(String(bv).replace(/[^0-9.]/g,'')) || 0));
      }
      return _clSortDir * String(av).localeCompare(String(bv));
    });
  }

  // Favorites float to top only when no sort is active
  let final;
  if (_clSortCol === null) {
    // Sort by relevance within each tier
    const score = r => relevanceScore(r.title);
    const favResults  = sorted.filter(r =>  isFavResult(r)).sort((a,b) => score(b)-score(a));
    const restResults = sorted.filter(r => !isFavResult(r)).sort((a,b) => score(b)-score(a));
    final = [...favResults, ...restResults];
  } else {
    final = sorted;
  }

  final.forEach(r => {
    const isFav = isFavResult(r);
    const star  = isFav ? '<span class="cl-fav-star">★</span>' : '';
    const title = r.url
      ? star + '<a href="' + r.url + '" target="_blank" rel="noopener">' + (r.title || '(no title)') + '</a>'
      : star + (r.title || '(no title)');
    html += '<tr class="' + (isFav ? 'cl-fav-result' : '') + '" data-city="' + (r.cityId||'') + '">' +
            '<td title="' + (r.title||'').replace(/"/g,'&quot;') + '">' + title + '</td>' +
            '<td>' + (r.price||'') + '</td>' +
            '<td>' + (r.location||'') + '</td>' +
            '<td>' + (r.date||'') + '</td></tr>';
  });
  html += '</tbody></table>';
  body.innerHTML = html;
}

function clSort(col) {
  const isRelevance = cols && cols[col] === 'relevance';
  if (isRelevance && _clSortCol === col) {
    // Second click on Relevance = back to default (fav+relevance, no active sort)
    _clSortCol = null; _clSortDir = 1;
  } else if (_clSortCol === col) {
    _clSortDir *= -1;
  } else {
    _clSortCol = col; _clSortDir = 1;
  }
  clRenderResults();
}
</script>
</body>
</html>"""


# ── Launch ────────────────────────────────────────────────────────────────────

# ── Version & Auto-updater ────────────────────────────────────────────────────

APP_VERSION = "1.0.0"
GITHUB_RAW  = "https://raw.githubusercontent.com/cboehmig-lab/gc-tracker/main"
GITHUB_REPO = "https://github.com/cboehmig-lab/gc-tracker"

def _check_for_update() -> tuple[bool, str]:
    """Check GitHub for a newer version. Returns (update_available, latest_version)."""
    try:
        r = _http.get(f"{GITHUB_RAW}/version.txt", timeout=5)
        if r.status_code == 200:
            latest = r.text.strip()
            if latest != APP_VERSION:
                return True, latest
    except Exception:
        pass
    return False, APP_VERSION

def _do_update(send_progress=None):
    """Download the latest gc_tracker_app.py from GitHub and replace this file."""
    def log(msg):
        if send_progress:
            send_progress(msg)
        else:
            print(msg)
    try:
        log("Downloading update...")
        r = _http.get(f"{GITHUB_RAW}/gc_tracker_app.py", timeout=30)
        r.raise_for_status()
        this_file = Path(__file__).resolve()
        backup = this_file.with_suffix(".py.bak")
        this_file.rename(backup)
        this_file.write_text(r.text, encoding="utf-8")
        log(f"✓ Updated! Backup saved as {backup.name}. Restart the app to use the new version.")
        return True
    except Exception as e:
        log(f"Update failed: {e}")
        return False


@app.route("/api/version")
@login_required
def api_version():
    update_available, latest = _check_for_update()
    return jsonify({
        "current":          APP_VERSION,
        "latest":           latest,
        "update_available": update_available,
        "repo":             GITHUB_REPO,
    })

@app.route("/api/update", methods=["POST"])
@login_required
def api_do_update():
    def _run():
        def send(msg): _q.put({"type": "progress", "msg": msg})
        success = _do_update(send)
        _q.put({"type": "done", "scanned": 0, "new_count": 0,
                "new_items": [], "baseline": False, "stopped": False,
                "update_success": success})
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started"})


if __name__ == "__main__":
    _load_cat_cache()
    if not STORES_CACHE.exists():
        print("Building store list…")
        refresh_store_list()

    # Check for updates silently on startup
    try:
        update_available, latest = _check_for_update()
        if update_available:
            print(f"\n  ⬆  Update available: v{latest} (you have v{APP_VERSION})")
            print(f"  Visit the app and click 'Update Available' to install.\n")
    except Exception:
        pass

    url = f"http://localhost:{PORT}"
    print(f"\n  Guitar Center Tracker v{APP_VERSION} is running!")
    print(f"  Open: {url}")
    print(f"  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
