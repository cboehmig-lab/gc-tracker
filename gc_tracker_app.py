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
KEYWORDS_FILE  = DATA_DIR / "gc_keywords.json"

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

# Load persisted cookies if available
COOKIE_FILE = DATA_DIR / "gc_cookies.json"

def _load_cookies():
    if COOKIE_FILE.exists():
        try:
            cookies = json.loads(COOKIE_FILE.read_text())
            _http.cookies.update(cookies)
        except Exception:
            pass

def _save_cookies():
    try:
        COOKIE_FILE.write_text(json.dumps(dict(_http.cookies)))
    except Exception:
        pass

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


def load_keywords() -> list:
    if KEYWORDS_FILE.exists():
        try:
            return json.loads(KEYWORDS_FILE.read_text())
        except Exception:
            pass
    return []


def save_keywords(kw: list):
    KEYWORDS_FILE.write_text(json.dumps(sorted(set(kw))))


# ── GC scraping ───────────────────────────────────────────────────────────────

PAGE_SIZE = 240

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


ALGOLIA_APP_ID  = "7AQ22QS8RJ"
ALGOLIA_API_KEY = "d04d765e552eb08aff3601eae8f2b729"
ALGOLIA_INDEX   = "cD-guitarcenter"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "x-algolia-application-id": ALGOLIA_APP_ID,
    "x-algolia-api-key":        ALGOLIA_API_KEY,
    "Content-Type":             "application/json",
}

def fetch_page(store_name: str = None, page: int = 1) -> dict:
    """Fetch one page of used inventory via Algolia API.
    If store_name is provided, filters to that store.
    If store_name is None, fetches ALL used inventory nationwide."""
    import time as _time
    ts = int(_time.time())
    facet_filters = [
        "categoryPageIds:Used",
        "condition.lvl0:Used",
    ]
    if store_name:
        facet_filters.append([f"stores:{store_name}"])
    payload = {"requests": [{
        "indexName":     ALGOLIA_INDEX,
        "analyticsTags": ["Did Not Search"],
        "facetFilters":  facet_filters,
        "facets":        ["*"],
        "hitsPerPage":   240,
        "maxValuesPerFacet": 10,
        "numericFilters": [f"startDate<={ts}"],
        "page":          page - 1,
        "query":         "",
        "ruleContexts":  ["used-page", "primary_itemtype", "extension_itemtype"],
        "attributesToRetrieve": ["*"],
    }]}
    r = _http.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def _parse_products_v1(data: any, store_name: str) -> list[dict]:
    """SUPERSEDED — old version of parse_products, kept for safety. 
    The active version is defined further below and extracts brand, categories, etc."""
    # Handle both old HTML string format and new Algolia dict format
    if isinstance(data, str):
        return _parse_products_html(data, store_name)

    products = []
    try:
        results = data.get("results", [])
        if not results:
            return []
        hits = results[0].get("hits", [])
        for hit in hits:
            sku   = str(hit.get("objectID") or hit.get("sku") or "").strip()
            name  = _clean_name(hit.get("name") or hit.get("title") or "")
            if not sku or not name:
                continue
            price_raw = hit.get("price") or hit.get("salePrice") or 0
            try:    price = float(price_raw) if price_raw else None
            except: price = None
            url = hit.get("url") or hit.get("pdpUrl") or ""
            if url and not url.startswith("http"):
                url = "https://www.guitarcenter.com" + url
            # Condition from hit
            condition = hit.get("condition") or ""
            if isinstance(condition, dict):
                condition = condition.get("lvl0") or condition.get("lvl1") or ""
            condition = _parse_condition(condition) if condition else ""
            products.append({
                "id":        sku,
                "name":      name,
                "price":     price,
                "store":     store_name,
                "url":       url,
                "condition": condition,
            })
    except Exception:
        pass
    return products


def _parse_products_html(html: str, store_name: str) -> list[dict]:
    """Legacy HTML parser — kept as fallback."""
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
            url_key = url.split("?")[0]
            condition = condition_map.get(url_key, "")
            if not condition:
                raw_cond = offers.get("itemCondition", "")
                parsed = _parse_condition(raw_cond)
                condition = parsed if parsed.lower() not in ("used", "") else ""
            if name and sku:
                products.append({"id": sku, "name": name, "price": price,
                                  "store": store_name, "url": url,
                                  "condition": condition})
        if products:
            return products
    return []


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


def parse_products(data, store_name: str = None) -> list[dict]:
    """Parse products from Algolia API response. store_name can be None for all-stores queries."""
    if isinstance(data, dict):
        products = []
        try:
            results = data.get("results", [])
            if not results:
                return []
            hits = results[0].get("hits", [])
            for hit in hits:
                sku   = str(hit.get("objectID") or "").strip()
                name  = _clean_name(hit.get("displayName") or hit.get("name") or "")
                if not sku or not name:
                    continue
                price_raw = hit.get("price") or hit.get("listPrice") or 0
                try:    price = float(price_raw) if price_raw else None
                except: price = None
                seo_url = hit.get("seoUrl") or ""
                url = ("https://www.guitarcenter.com" + seo_url) if seo_url else ""
                # Brand
                brand = hit.get("brand") or ""
                # Condition: "Used > Great" → "Great"
                condition = hit.get("condition") or {}
                if isinstance(condition, dict):
                    lvl1 = condition.get("lvl1") or condition.get("lvl0") or ""
                    condition = lvl1.split(">")[-1].strip() if ">" in lvl1 else lvl1
                elif isinstance(condition, str):
                    condition = condition.split(">")[-1].strip()
                condition = _parse_condition(condition) if condition else ""
                # Category from categories array: [{lvl0: "Guitars", lvl1: "Guitars > Electric Guitars", ...}]
                cats = hit.get("categories") or []
                cats_slug = hit.get("categoriesSlug") or {}
                if cats and isinstance(cats, list) and isinstance(cats[0], dict):
                    category    = cats[0].get("lvl0") or ""
                    subcategory = cats_slug.get("lvl1") or ""
                    # Fallback: parse lvl1 from full hierarchy if slug not available
                    if not subcategory:
                        lvl1_full = cats[0].get("lvl1") or ""
                        subcategory = lvl1_full.split(">")[-1].strip() if ">" in lvl1_full else ""
                else:
                    category, subcategory = "", ""
                # Date listed from creationDate (millisecond timestamp)
                creation_ts = hit.get("creationDate") or 0
                try:
                    from datetime import datetime as _dt
                    date_str = _dt.utcfromtimestamp(float(creation_ts) / 1000).strftime("%Y-%m-%d") if creation_ts else ""
                except Exception:
                    date_str = ""
                # Location: storeName gives "Austin, TX" format
                location = hit.get("storeName") or store_name or ""
                # Store: from hit's stores array when querying all stores
                hit_stores = hit.get("stores") or []
                store = store_name or (hit_stores[0] if hit_stores else "")
                # Image ID for thumbnail hover
                image_id = hit.get("imageId") or ""
                products.append({
                    "id":          sku,
                    "name":        name,
                    "brand":       brand,
                    "price":       price,
                    "store":       store,
                    "location":    location,
                    "url":         url,
                    "condition":   condition,
                    "category":    category,
                    "subcategory": subcategory,
                    "date_listed": date_str,
                    "image_id":    image_id,
                })
        except Exception:
            pass
        return products

    # Legacy HTML format fallback (kept for safety — not actively called)
    html = data
    condition_map = _extract_conditions_from_listing(html)
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            d = json.loads(block)
        except Exception:
            continue
        if d.get("@type") != "CollectionPage":
            continue
        items = d.get("mainEntity", {}).get("itemListElement", [])
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
            url_key = url.split("?")[0]
            condition = condition_map.get(url_key, "")
            if not condition:
                raw_cond = offers.get("itemCondition", "")
                parsed = _parse_condition(raw_cond)
                condition = parsed if parsed.lower() not in ("used", "") else ""
            if name and sku:
                products.append({"id": sku, "name": name, "price": price,
                                  "store": store_name, "url": url,
                                  "condition": condition,
                                  "brand": "", "category": "", "subcategory": "",
                                  "location": store_name, "date_listed": ""})
        if products:
            return products
    return []


def _parse_algolia_products(nd: dict, store_name: str, condition_map: dict) -> list[dict]:
    """Walk __NEXT_DATA__ looking for Algolia product hits."""
    products = []

    def walk(obj, depth=0):
        if depth > 15 or products:
            return
        if isinstance(obj, dict):
            # Look for Algolia hits array
            hits = obj.get("hits") or obj.get("results") or []
            if isinstance(hits, list) and hits and isinstance(hits[0], dict):
                for hit in hits:
                    sku  = str(hit.get("objectID") or hit.get("sku") or hit.get("productId") or "").strip()
                    name = _clean_name(hit.get("name") or hit.get("title") or "")
                    if not sku or not name:
                        continue
                    price_raw = hit.get("price") or hit.get("salePrice") or hit.get("listPrice") or 0
                    try:    price = float(price_raw) if price_raw else None
                    except: price = None
                    url = hit.get("url") or hit.get("pdpUrl") or f"https://www.guitarcenter.com/used/{sku}.gc"
                    if not url.startswith("http"):
                        url = "https://www.guitarcenter.com" + url
                    url_key = url.split("?")[0]
                    condition = condition_map.get(url_key, "")
                    if not condition:
                        condition = hit.get("condition") or hit.get("itemCondition") or ""
                        condition = _parse_condition(condition) if condition else ""
                    products.append({"id": sku, "name": name, "price": price,
                                     "store": store_name, "url": url, "condition": condition})
                return
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    walk(nd)
    return products





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


def scrape_store(store_name: str, send, stop_event: threading.Event) -> tuple[list[dict], set]:
    """Returns (all_products_found, ids_seen_this_store)."""
    all_products, ids_seen = [], set()
    page = 1
    while page <= 50:
        if stop_event.is_set():
            send({"type": "progress", "msg": f"  [{store_name}] stopped."})
            break
        try:
            data = fetch_page(store_name, page)
        except Exception as e:
            if "404" in str(e):
                send({"type": "progress", "msg": f"  [{store_name}] not found — removing from store list."})
                _remove_invalid_store(store_name)
            else:
                send({"type": "progress", "msg": f"  [{store_name}] error: {e}"})
            break
        products = parse_products(data, store_name)
        if not products:
            break
        if all(p["id"] in ids_seen for p in products):
            break
        for p in products:
            if p["id"] not in ids_seen:
                all_products.append(p)
                ids_seen.add(p["id"])
        # Algolia tells us total pages via nbPages
        try:
            nb_pages = data.get("results", [{}])[0].get("nbPages", 1)
            if page >= nb_pages:
                break
        except Exception:
            if len(products) < PAGE_SIZE:
                break
        page += 1
        # No sleep needed between Algolia API pages
    send({"type": "progress", "msg": f"  [{store_name}] {len(all_products)} items"})
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

_COLS    = ["Status", "Date Listed", "Item Name", "Brand", "Condition", "Category", "Subcategory", "Price", "Location", "Link"]
_WIDTHS  = [8, 14, 50, 16, 14, 22, 22, 12, 18, 70]
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
            date_listed = item.get("date_listed") or ""
            ws.cell(r, 1, "New"); ws.cell(r, 2, _fmt_date(date_listed) if date_listed else ts)
            ws.cell(r, 3, item["name"]); ws.cell(r, 4, item.get("brand", ""))
            ws.cell(r, 5, item.get("condition", ""))
            ws.cell(r, 6, item.get("category", "")); ws.cell(r, 7, item.get("subcategory", ""))
            pc = ws.cell(r, 8, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 9, item.get("location") or item.get("store", ""))
            lc = ws.cell(r, 10, item["url"] or "")
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
            date_listed = item.get("date_listed") or ""
            ws.cell(r, 1, "New"); ws.cell(r, 2, _fmt_date(date_listed) if date_listed else ts)
            ws.cell(r, 3, item["name"]); ws.cell(r, 4, item.get("brand", ""))
            ws.cell(r, 5, item.get("condition", ""))
            ws.cell(r, 6, item.get("category", "")); ws.cell(r, 7, item.get("subcategory", ""))
            pc = ws.cell(r, 8, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 9, item.get("location") or item.get("store", ""))
            lc = ws.cell(r, 10, item["url"] or "")
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
    reset_pw = "Beatle909!"
    if data.get("password") != reset_pw:
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
        "info":      get_store_info(),
    })

@app.route("/api/stores/refresh", methods=["POST"])
@login_required
def api_stores_refresh():
    stores = refresh_store_list()
    info   = get_store_info()
    return jsonify({"stores": stores,
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
    """Return cached inventory for selected stores with server-side
    pagination, sorting, and filtering.  Sends only one page at a time
    so the browser never has to hold 80K items in memory."""
    data = request.json or {}
    stores = data.get("stores", [])
    search_all = bool(data.get("all_stores"))
    if not stores and not search_all:
        return jsonify({"items": [], "no_store_data": True})

    # Pagination params
    page     = max(int(data.get("page", 1)), 1)
    per_page = min(max(int(data.get("per_page", 50)), 10), 200)

    # Sort params  (defaults: date descending = newest first)
    sort_field = data.get("sort_field", "date")
    sort_dir   = data.get("sort_dir", "desc")
    fav_stores = set(data.get("fav_stores", []))

    # Filter params — all dropdowns are multi-select arrays
    fq       = (data.get("filter_q") or "").lower().strip()
    f_brands = data.get("filter_brands") or []
    f_conds  = data.get("filter_conditions") or []
    f_cats   = data.get("filter_categories") or []
    f_subs   = data.get("filter_subcategories") or []
    f_watched = bool(data.get("filter_watched"))
    f_want_only = bool(data.get("filter_want_list_only"))
    force_fav_sort = bool(data.get("force_fav_sort"))

    _load_cat_cache()
    state      = load_state()
    item_dates = state.get("item_dates", {})
    # Watchlist and keywords now come from the client (localStorage)
    wl_ids     = set(data.get("watchlist_ids", []))
    keywords   = data.get("keywords", [])
    new_ids    = set(data.get("new_ids", []))
    store_set  = set(stores) if not search_all else None

    # ── Keyword matching helper ───────────────────────────────────────────
    import re as _re
    _kw_compiled = []
    for kw in keywords:
        kw_stripped = kw.strip()
        if kw_stripped.startswith('"') and kw_stripped.endswith('"') and len(kw_stripped) > 2:
            # Exact substring match (quoted)
            _kw_compiled.append(("exact", kw_stripped[1:-1].lower()))
        elif "," in kw_stripped:
            # All-terms match (comma-separated)
            terms = [t.strip().lower() for t in kw_stripped.split(",") if t.strip()]
            if terms:
                _kw_compiled.append(("all", terms))
        else:
            # Simple contains
            _kw_compiled.append(("contains", kw_stripped.lower()))

    def _kw_match(name_l, brand_l):
        text = name_l + " " + brand_l
        for mode, val in _kw_compiled:
            if mode == "exact" and val in text:
                return True
            elif mode == "all" and all(t in text for t in val):
                return True
            elif mode == "contains" and val in text:
                return True
        return False

    # Check if any cache entries have store field
    has_store_data = any(v.get("store") for v in _cat_cache.values())
    if not has_store_data:
        return jsonify({"items": [], "no_store_data": True,
                        "message": "Run 'Check for New Items' once to populate store data."})

    # ── Build full item list for selected stores (lightweight dicts) ──────
    all_items = []
    brand_counts = {}; cond_set = set(); cat_set = set(); subcat_set = set()
    for sku, cached in _cat_cache.items():
        if store_set is not None and cached.get("store") not in store_set:
            continue
        if not cached.get("available", True):
            continue
        price_raw  = cached.get("price", 0) or 0
        name       = cached.get("name", "")
        brand      = cached.get("brand", "")
        location   = cached.get("location") or cached.get("store", "")
        category   = cached.get("category", "")
        subcategory= cached.get("subcategory", "")
        condition  = cached.get("condition", "")
        date_raw   = cached.get("date_listed") or item_dates.get(sku, "")
        store      = cached.get("store", "")

        # Collect filter options from ALL items (pre-filter)
        if brand:      brand_counts[brand] = brand_counts.get(brand, 0) + 1
        if condition:  cond_set.add(condition)
        if category:   cat_set.add(category)
        if subcategory: subcat_set.add(subcategory)

        # Check keyword match
        name_lower = name.lower()
        brand_lower = brand.lower()
        kw_hit = _kw_match(name_lower, brand_lower) if _kw_compiled else False

        all_items.append({
            "id":         sku,
            "name":       name,
            "brand":      brand,
            "price":      f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":  price_raw,
            "price_drop": 0,
            "store":      store,
            "location":   location,
            "url":        cached.get("url", ""),
            "category":   category,
            "subcategory":subcategory,
            "condition":  condition,
            "date":       _fmt_date(date_raw),
            "date_raw":   date_raw,
            "image_id":   cached.get("image_id", ""),
            "watched":    sku in wl_ids,
            "isNew":      sku in new_ids,
            "kwMatch":    kw_hit,
            "isFav":      store in fav_stores if fav_stores else False,
        })

    total_unfiltered = len(all_items)
    new_count_unfiltered = sum(1 for i in all_items if i.get("isNew"))

    # ── Apply filters ─────────────────────────────────────────────────────
    filtered = all_items
    if fq:
        # Quoted = exact phrase, otherwise all words must appear (AND)
        if fq.startswith('"') and fq.endswith('"') and len(fq) > 2:
            phrase = fq[1:-1]
            filtered = [i for i in filtered if
                        phrase in (i["name"] or "").lower() or
                        phrase in (i["brand"] or "").lower()]
        else:
            words = fq.split()
            def _text_match(i):
                text = " ".join(((i["name"] or ""), (i["brand"] or ""),
                                 (i["store"] or ""), (i["location"] or ""),
                                 (i["category"] or ""), (i["subcategory"] or ""))).lower()
                return all(w in text for w in words)
            filtered = [i for i in filtered if _text_match(i)]
    if f_want_only:
        filtered = [i for i in filtered if i["kwMatch"]]
    if f_watched:
        filtered = [i for i in filtered if i["watched"]]
    if f_brands:
        brand_set_filter = set(f_brands)
        filtered = [i for i in filtered if i["brand"] in brand_set_filter]
    if f_conds:
        cond_set_filter = set(f_conds)
        filtered = [i for i in filtered if i["condition"] in cond_set_filter]
    if f_cats:
        cat_set_filter = set(f_cats)
        filtered = [i for i in filtered if i["category"] in cat_set_filter]
    if f_subs:
        sub_set_filter = set(f_subs)
        filtered = [i for i in filtered if i["subcategory"] in sub_set_filter]

    # Subcategory options scoped to current category filter
    scoped_subcats = set()
    if f_cats:
        cat_set_scope = set(f_cats)
        for i in all_items:
            if i["category"] in cat_set_scope and i["subcategory"]:
                scoped_subcats.add(i["subcategory"])

    # ── Sort ──────────────────────────────────────────────────────────────
    # NEW items float to top ONLY on default sort (no explicit column click)
    # When user explicitly clicks a sort column, sort purely by that column
    reverse = (sort_dir == "desc")
    user_sorted = bool(data.get("user_sorted"))

    if sort_field == "price":
        filtered.sort(key=lambda x: x.get("price_raw") or 0, reverse=reverse)
    elif sort_field == "date":
        filtered.sort(key=lambda x: x.get("date_raw") or "", reverse=reverse)
    else:
        filtered.sort(key=lambda x: (x.get(sort_field) or "").lower(), reverse=reverse)

    # Only apply NEW-on-top tier for the default (non-user-clicked) sort
    if not user_sorted:
        filtered.sort(key=lambda x: 0 if x.get("isNew") else 1)

    total_filtered = len(filtered)
    total_pages    = max(1, -(-total_filtered // per_page))  # ceil division
    page           = min(page, total_pages)
    start          = (page - 1) * per_page
    page_items     = filtered[start:start + per_page]

    return jsonify({
        "items":            page_items,
        "page":             page,
        "per_page":         per_page,
        "total_count":      total_filtered,
        "total_unfiltered": total_unfiltered,
        "total_pages":      total_pages,
        "new_count":        new_count_unfiltered,
        "no_store_data":    False,
        # Filter option lists (always full set so dropdowns stay populated)
        "brands":           [{"name": b, "count": c} for b, c in sorted(brand_counts.items(), key=lambda x: -x[1])],
        "conditions":       sorted(cond_set, key=lambda c: {"Excellent":0,"Great":1,"Good":2,"Fair":3,"Poor":4}.get(c, 5)),
        "categories":       sorted(cat_set),
        "subcategories":    sorted(scoped_subcats) if f_cats else sorted(subcat_set),
    })


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
            "brand":      cached.get("brand", data.get("brand", "")),
            "price":      cached.get("price", 0),
            "store":      cached.get("store", data.get("store", "")),
            "location":   cached.get("location") or cached.get("store", data.get("store", "")),
            "url":        cached.get("url", data.get("url", "")),
            "condition":  cached.get("condition", ""),
            "category":   cached.get("category", ""),
            "subcategory":cached.get("subcategory", ""),
            "date_added": datetime.now().strftime("%Y-%m-%d"),
            "date_listed":cached.get("date_listed", ""),
            "image_id":   cached.get("image_id", ""),
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
            "brand":      w.get("brand", ""),
            "price":      f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":  price_raw,
            "price_drop": 0,
            "store":      w.get("store", ""),
            "location":   w.get("location") or w.get("store", ""),
            "url":        w.get("url", ""),
            "category":   w.get("category", ""),
            "subcategory":w.get("subcategory", ""),
            "condition":  w.get("condition", ""),
            "date":       _fmt_date(w.get("date_listed") or item_dates.get(sku, w.get("date_added",""))),
            "date_raw":   w.get("date_listed") or item_dates.get(sku, w.get("date_added","")),
            "image_id":   w.get("image_id", ""),
            "isNew":      False,
            "watched":    True,
            "sold":       w.get("sold", False),
        })
    # Sold items at bottom
    items.sort(key=lambda x: x["sold"])
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/keywords", methods=["GET"])
@login_required
def api_keywords_get():
    return jsonify({"keywords": load_keywords()})


@app.route("/api/keywords", methods=["POST"])
@login_required
def api_keywords_post():
    data = request.json or {}
    action = data.get("action", "")
    kw_list = load_keywords()
    if action == "add":
        word = (data.get("keyword") or "").strip()
        if word and word.lower() not in [k.lower() for k in kw_list]:
            kw_list.append(word)
            save_keywords(kw_list)
    elif action == "remove":
        word = (data.get("keyword") or "").strip().lower()
        kw_list = [k for k in kw_list if k.lower() != word]
        save_keywords(kw_list)
    elif action == "clear":
        kw_list = []
        save_keywords(kw_list)
    return jsonify({"keywords": load_keywords()})


@app.route("/api/state")
@login_required
def api_state():
    _load_cat_cache()
    total_items = sum(1 for v in _cat_cache.values() if v.get("available", True))
    last_scan_file = DATA_DIR / "gc_last_scan.txt"
    last_scan = last_scan_file.read_text().strip() if last_scan_file.exists() else None
    return jsonify({
        "total_items":  total_items,
        "excel_exists": OUTPUT_FILE.exists(),
        "is_first_run": total_items == 0,
        "last_scan":    last_scan,
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
    # Empty stores = nationwide scan (used by both baseline and Check for New)
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_run, args=(selected, baseline), daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/set-cookies", methods=["POST"])
@login_required
def api_set_cookies():
    """Import browser cookies into the HTTP session."""
    cookie_string = request.json.get("cookies", "")
    count = 0
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            _http.cookies.set(k.strip(), v.strip(), domain=".guitarcenter.com")
            count += 1
    _save_cookies()
    return jsonify({"imported": count, "status": "Cookies set — try running a store now."})



@login_required
def api_export_data():
    """Export all data files as a JSON bundle for migration."""
    bundle = {}
    for name, path in [
        ("state",     STATE_FILE),
        ("cat_cache", CAT_CACHE_FILE),
        ("stores",    STORES_CACHE),
        ("favorites", FAVORITES_FILE),
        ("watchlist", WATCHLIST_FILE),
        ("keywords",  KEYWORDS_FILE),
    ]:
        if path.exists():
            try:
                bundle[name] = json.loads(path.read_text())
            except Exception:
                pass
    from flask import Response
    return Response(
        json.dumps(bundle),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=gc_data_export.json"}
    )


@app.route("/api/import-data", methods=["POST"])
@login_required
def api_import_data():
    """Import a data bundle exported from another instance."""
    bundle = request.json or {}
    written = []
    mapping = {
        "state":     STATE_FILE,
        "cat_cache": CAT_CACHE_FILE,
        "stores":    STORES_CACHE,
        "favorites": FAVORITES_FILE,
        "watchlist": WATCHLIST_FILE,
        "keywords":  KEYWORDS_FILE,
    }
    for name, path in mapping.items():
        if name in bundle:
            path.write_text(json.dumps(bundle[name]))
            written.append(name)
    global _cat_cache
    _cat_cache = {}
    _load_cat_cache()
    return jsonify({"imported": written, "status": "Import complete — reload the page."})


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
            # Extract thumbnail image
            img = item.get("image", "")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "") or img.get("contentUrl", "")

            items.append({"title": name, "url": url, "price": price,
                          "location": loc, "date": date, "cityId": city_id,
                          "image": img or ""})
        if items:
            break  # Found and parsed the ItemList, done

    return items


def _cl_search(query: str, cities: list = None) -> list[dict]:
    """Search Craigslist musical instruments across US cities."""
    import time as _time
    results   = []
    seen_urls = set()
    search_cities = cities if cities else _CL_CITIES

    def _search_city(city_id):
        try:
            # Each thread gets its own session to avoid thread-safety issues
            s = http.Session()
            s.headers.update({
                "User-Agent": random.choice(_USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            })
            # Small random delay to avoid hammering CL simultaneously
            _time.sleep(random.uniform(0.05, 0.3))
            url = (f"https://{city_id}.craigslist.org/search/msa"
                   f"?query={http.utils.quote(query)}&sort=date")
            r = s.get(url, timeout=12)
            if r.status_code == 200:
                return _cl_parse_html(r.text, city_id)
        except Exception:
            pass
        return []

    with ThreadPoolExecutor(max_workers=10) as pool:
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
    """Test Algolia API fetch for a store."""
    store = request.args.get("store", "Austin")
    try:
        data     = fetch_page(store, 1)
        products = parse_products(data, store)
        results  = data.get("results", [{}])
        first    = results[0] if results else {}
        return jsonify({
            "store":          store,
            "nb_hits":        first.get("nbHits", 0),
            "nb_pages":       first.get("nbPages", 0),
            "products_found": len(products),
            "sample":         products[:3],
            "raw_hit_sample": first.get("hits", [{}])[:2],
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
    # Force-release lock after a short delay to prevent stuck state
    def _force_unlock():
        import time; time.sleep(5)
        if _lock.locked():
            try: _lock.release()
            except RuntimeError: pass
    threading.Thread(target=_force_unlock, daemon=True).start()
    return jsonify({"status": "stopping"})

@app.route("/api/populate-store-data", methods=["POST"])
@login_required
def api_populate_store_data():
    """One-time migration: scan stores to tag cache entries with their store name."""
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    data = request.json or {}
    stores = data.get("stores", [])  # empty = all stores
    t = threading.Thread(target=_populate_store_data, args=(stores,), daemon=True)
    t.start()
    return jsonify({"status": "started"})


def _populate_store_data(selected_stores: list = None):
    """Fetch pages of each store and tag cache entries with their store name."""
    def send(msg): _q.put(msg)
    try:
        _load_cat_cache()
        stores = selected_stores if selected_stores else get_store_list()
        total  = len(stores)
        updated = 0
        label = f"{total} selected store(s)" if selected_stores else f"all {total} stores"
        send({"type": "progress", "msg": f"Tagging cache entries for {label}…"})
        send({"type": "progress", "msg": "You can stop at any time — progress is saved as it goes."})

        for i, store in enumerate(stores, 1):
            if _stop_event.is_set():
                send({"type": "progress", "msg": "⏹ Stopped."})
                break
            if i % 20 == 1:
                send({"type": "progress", "msg": f"  [{i}/{total}] {store}…"})
            try:
                page = 1
                while page <= 50:
                    data = fetch_page(store, page)
                    products = parse_products(data, store)
                    if not products:
                        break
                    for p in products:
                        sku = p["id"]
                        if sku in _cat_cache and not _cat_cache[sku].get("store"):
                            _cat_cache[sku]["store"] = store
                            _cat_cache[sku]["name"]  = _cat_cache[sku].get("name") or p.get("name","")
                            _cat_cache[sku]["url"]   = _cat_cache[sku].get("url")  or p.get("url","")
                            _cat_cache[sku]["price"] = _cat_cache[sku].get("price") or p.get("price",0)
                            _cat_cache[sku]["brand"] = _cat_cache[sku].get("brand") or p.get("brand","")
                            _cat_cache[sku]["location"] = _cat_cache[sku].get("location") or p.get("location","")
                            _cat_cache[sku]["category"] = _cat_cache[sku].get("category") or p.get("category","")
                            _cat_cache[sku]["subcategory"] = _cat_cache[sku].get("subcategory") or p.get("subcategory","")
                            _cat_cache[sku]["date_listed"] = _cat_cache[sku].get("date_listed") or p.get("date_listed","")
                            updated += 1
                    if len(products) < PAGE_SIZE:
                        break
                    page += 1
                    _sleep(1.0, 0.5)
            except Exception:
                pass
            _sleep(1.5, 0.8)

        _save_cat_cache()
        send({"type": "progress", "msg": f"\n✓ Done — {updated} cache entries tagged with store names."})
        send({"type": "done", "baseline": False, "stopped": _stop_event.is_set(),
              "scanned": total, "new_count": 0, "new_items": [], "all_items": [],
              "gap_fill": True, "fixed": updated})
    except Exception as e:
        send({"type": "done", "error": str(e), "scanned": 0, "new_count": 0, "new_items": []})
    finally:
        _lock.release()



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
        run_time   = datetime.utcnow().isoformat() + "Z"

        stores_to_scan = selected_stores if not baseline else []
        nationwide = baseline or len(stores_to_scan) == 0
        label = "nationwide scan" if nationwide else f"{len(stores_to_scan)} store(s)"
        send({"type":"progress","msg":f"Starting {label}…"})

        all_products, ids_this_run = [], set()

        if nationwide:
            # ── Nationwide: query ALL used inventory via parallel page fetches ────
            PARALLEL_WORKERS = 15  # concurrent API requests
            send({"type":"progress","msg":"Fetching all used inventory nationwide via API…"})
            # First fetch page 1 to learn total pages
            try:
                data1 = fetch_page(None, 1)
            except Exception as e:
                send({"type":"progress","msg":f"  API error on page 1: {e}"})
                data1 = None
            if data1:
                products1 = parse_products(data1, None)
                for p in products1:
                    if p["id"] not in ids_this_run:
                        all_products.append(p)
                        ids_this_run.add(p["id"])
                try:
                    nb_pages = data1.get("results", [{}])[0].get("nbPages", 1)
                    nb_hits  = data1.get("results", [{}])[0].get("nbHits", 0)
                    send({"type":"progress","msg":f"  {nb_hits:,} items across {nb_pages} pages — fetching {PARALLEL_WORKERS} pages at a time…"})
                except Exception:
                    nb_pages = 1
                # Fetch remaining pages in parallel batches
                remaining = list(range(2, min(nb_pages + 1, 1001)))
                def _fetch_one_page(pg):
                    if _stop_event.is_set():
                        return pg, None, None
                    try:
                        d = fetch_page(None, pg)
                        return pg, d, None
                    except Exception as exc:
                        return pg, None, exc
                batch_idx = 0
                while batch_idx < len(remaining) and not _stop_event.is_set():
                    batch = remaining[batch_idx:batch_idx + PARALLEL_WORKERS]
                    batch_idx += len(batch)
                    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
                        futures = {pool.submit(_fetch_one_page, pg): pg for pg in batch}
                        for fut in as_completed(futures):
                            pg, data, err = fut.result()
                            if err:
                                send({"type":"progress","msg":f"  API error on page {pg}: {err}"})
                                continue
                            if data is None:
                                continue
                            products = parse_products(data, None)
                            for p in products:
                                if p["id"] not in ids_this_run:
                                    all_products.append(p)
                                    ids_this_run.add(p["id"])
                    # Progress update after each batch
                    pages_done = min(batch_idx + 1, nb_pages)
                    send({"type":"progress","msg":f"  page {pages_done}/{nb_pages}… ({len(all_products):,} items so far)"})
                if _stop_event.is_set():
                    send({"type":"progress","msg":"⏹ Stopped by user."})
            send({"type":"progress","msg":f"  Fetched {len(all_products):,} items total."})
        else:
            # ── Normal scan: query selected stores in parallel ────────────────
            STORE_WORKERS = 10
            send({"type":"progress","msg":f"Scanning {len(stores_to_scan)} stores ({STORE_WORKERS} at a time)…"})
            completed = [0]
            lock = threading.Lock()
            def _scan_one_store(store):
                if _stop_event.is_set():
                    return store, [], set()
                _rotate_ua()
                products, ids = scrape_store(store, send, _stop_event)
                with lock:
                    completed[0] += 1
                    send({"type":"progress","msg":f"  [{completed[0]}/{len(stores_to_scan)}] {store} — {len(products)} items"})
                return store, products, ids
            with ThreadPoolExecutor(max_workers=STORE_WORKERS) as pool:
                futures = {pool.submit(_scan_one_store, s): s for s in stores_to_scan}
                for fut in as_completed(futures):
                    if _stop_event.is_set():
                        send({"type":"progress","msg":"⏹ Stopped by user."})
                        break
                    store, products, ids = fut.result()
                    for p in products:
                        if p["id"] not in ids_this_run:
                            all_products.append(p)
                    ids_this_run |= ids

        # ── Apply data from Algolia API to products, tracking price drops ────────
        # Categories, condition, brand all come from the API now — no page scraping needed.
        for p in all_products:
            sku    = p["id"]
            cached = _cat_cache.get(sku, {})
            cat    = p.get("category") or cached.get("category", "")
            subcat = p.get("subcategory") or cached.get("subcategory", "")
            condition = p.get("condition") or cached.get("condition", "")
            brand     = p.get("brand") or cached.get("brand", "")
            location  = p.get("location") or cached.get("location", p.get("store", ""))
            # Price drop detection
            new_price  = p.get("price") or 0
            last_price = cached.get("price") or 0
            price_drop = (last_price - new_price) if (last_price and new_price and new_price < last_price) else 0
            _cat_cache[sku] = {
                "category":          cat,
                "subcategory":       subcat,
                "condition":         condition,
                "brand":             brand,
                "name":              p.get("name", ""),
                "url":               p.get("url", ""),
                "store":             p.get("store", ""),
                "location":          location,
                "price":             new_price,
                "available":         True,
                "date_listed":       p.get("date_listed") or cached.get("date_listed", ""),
                "image_id":          p.get("image_id") or cached.get("image_id", ""),
            }
            p["category"]    = cat
            p["subcategory"] = subcat
            p["condition"]   = condition
            p["brand"]       = brand
            p["location"]    = location
            p["price_drop"]  = price_drop
        _save_cat_cache()

        # ── Mark sold items (not found in this scan) ────────────────────────────
        if not _stop_event.is_set():
            scanned_store_set = set(stores_to_scan)
            wl = load_watchlist()
            wl_changed = False
            for sku, cached in _cat_cache.items():
                if sku in ids_this_run:
                    continue
                # Nationwide scan: any item not found is gone
                # Store scan: only mark items from scanned stores
                if nationwide or cached.get("store") in scanned_store_set:
                    if cached.get("available", True):
                        cached["available"] = False
                        if sku in wl:
                            wl[sku]["sold"] = True
                            wl_changed = True
            if wl_changed:
                save_watchlist(wl)
            _save_cat_cache()

        # ── Update watchlist with latest data ─────────────────────────────────
        wl = load_watchlist()
        changed = False
        for sku, item in wl.items():
            if sku in _cat_cache and not wl[sku].get("sold"):
                cached = _cat_cache[sku]
                wl[sku].update({
                    "price":      cached.get("price", item.get("price")),
                    "condition":  cached.get("condition", item.get("condition", "")),
                    "brand":      cached.get("brand", item.get("brand", "")),
                    "location":   cached.get("location", item.get("location", "")),
                    "category":   cached.get("category", item.get("category", "")),
                    "subcategory":cached.get("subcategory", item.get("subcategory", "")),
                    "date_listed":cached.get("date_listed", item.get("date_listed", "")),
                })
                changed = True
        if changed:
            save_watchlist(wl)

        send({"type":"progress","msg":f"  {len(all_products):,} products scanned."})
        _save_cat_cache()
        # Record scan completion time
        (DATA_DIR / "gc_last_scan.txt").write_text(run_time)

        def fmt(p):
            date_src = p.get("date_listed") or _cat_cache.get(p["id"], {}).get("date_listed", "")
            return {
                "id":         p["id"],
                "name":       p["name"],
                "brand":      p.get("brand", ""),
                "price":      f"${p['price']:,.2f}" if p["price"] else "",
                "price_raw":  p.get("price") or 0,
                "price_drop": p.get("price_drop", 0),
                "store":      p["store"],
                "location":   p.get("location") or p.get("store", ""),
                "url":        p["url"],
                "category":   p.get("category", ""),
                "subcategory":p.get("subcategory", ""),
                "condition":  p.get("condition", ""),
                "date":       _fmt_date(date_src),
                "date_raw":   date_src,
                "image_id":   p.get("image_id") or _cat_cache.get(p["id"], {}).get("image_id", ""),
            }

        # Send all item IDs so client can compare against previous snapshot
        all_ids = [p["id"] for p in all_products]
        # For large scans, don't send full item lists via SSE — client will use server-side browse
        large_scan = len(all_products) > 1000
        items_for_sse = [] if large_scan else [fmt(p) for p in all_products[:500]]
        send({
            "type":       "done",
            "baseline":   baseline,
            "stopped":    _stop_event.is_set(),
            "scanned":    len(all_products),
            "all_ids":    all_ids,
            "items":      items_for_sse,
            "use_browse": large_scan,
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
<title>Gear Tracker</title>
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
.left{width:220px;min-width:200px;background:#1a1a1a;border-right:1px solid #2e2e2e;display:flex;flex-direction:column;flex-shrink:0;position:relative;z-index:10}

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

.left-footer{padding:12px;border-top:1px solid #2e2e2e;flex-shrink:0;background:#1a1a1a;position:relative;z-index:2}
#sel-count{font-size:.78rem;color:#666;margin-bottom:8px}
.btn-row{display:flex;gap:8px}
#run-btn{flex:1;padding:10px;background:#c00;color:#fff;border:none;border-radius:5px;font-size:.85rem;font-weight:700;cursor:pointer;white-space:nowrap}
#run-btn:hover{background:#e00}
#run-btn:disabled{background:#444;cursor:not-allowed}
#baseline-btn{padding:10px 12px;background:#222;color:#aaa;border:1px solid #3a3a3a;border-radius:5px;font-size:.8rem;cursor:pointer;white-space:nowrap}
#baseline-btn:hover{border-color:#c00;color:#fff}
#baseline-btn:disabled{opacity:.4;cursor:not-allowed}

/* ── Right panel ── */
.right{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative;z-index:1}

.status-bar{padding:8px 20px;background:#161616;border-bottom:1px solid #2e2e2e;font-size:.78rem;color:#666;display:flex;gap:20px;flex-wrap:wrap;flex-shrink:0}
.status-bar b{color:#bbb}
#global-search-wrap{margin-left:auto;display:flex;align-items:center;gap:4px;flex-shrink:0}
#global-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;width:200px;outline:none}
#global-search:focus{border-color:#c00}
#global-search-btn{background:none;border:1px solid #3a3a3a;border-radius:4px;color:#888;font-size:.72rem;padding:4px 8px;cursor:pointer;line-height:1}
#global-search-btn:hover{border-color:#c00;color:#eee}
#global-search-clear{background:none;border:1px solid #c00;border-radius:4px;color:#f88;font-size:.72rem;padding:4px 8px;cursor:pointer;line-height:1}
#global-search-clear:hover{background:#3a1a1a}

#log{height:52px;overflow-y:auto;padding:6px 20px;font-family:monospace;font-size:.78rem;color:#6dba8d;line-height:1.75;flex-shrink:0;border-bottom:1px solid #2e2e2e}
.log-dim{color:#555}
.log-err{color:#f88}

.results{flex:1;overflow-y:auto}
.results-hdr{padding:8px 16px;font-size:.88rem;font-weight:600;color:#ccc;background:#111;position:sticky;top:0;z-index:1;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{background:#c00;color:#fff;font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:10px}
.cat-sel{padding:5px 8px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;outline:none;cursor:pointer}
.cat-sel:focus{border-color:#c00}
#watchlist-toggle.wl-active,#cl-watchlist-toggle.wl-active{background:#c00;border-color:#c00;color:#fff}
.brand-dd-item{display:flex;align-items:center;padding:6px 12px;cursor:pointer;font-size:.82rem;color:#ccc;gap:6px}
.brand-dd-item:hover{background:#252525}
.brand-dd-item.active{background:#c00;color:#fff}
.brand-dd-item .bcount{margin-left:auto;color:#555;font-size:.72rem}
.brand-dd-item.active .bcount{color:rgba(255,255,255,.7)}
.cond-dd-item{display:flex;align-items:center;padding:6px 12px;cursor:pointer;font-size:.82rem;color:#ccc;gap:8px}
.cond-dd-item:hover{background:#252525}
.cond-dd-item.active{color:#fff}
.cond-dd-check{width:14px;height:14px;border:1px solid #555;border-radius:3px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;font-size:.7rem}
.cond-dd-item.active .cond-dd-check{background:#c00;border-color:#c00;color:#fff}
#res-search-wrap{margin-left:auto;display:flex;align-items:center;gap:6px}
#res-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.8rem;width:180px;outline:none}
#res-search:focus{border-color:#c00}
#res-search-count{font-size:.75rem;color:#555;white-space:nowrap}

table{width:100%;border-collapse:collapse;font-size:.83rem;table-layout:fixed}
th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 10px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:40px;cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
th:hover{color:#ccc}
th.sort-asc::after{content:" ▲";color:#c00;font-size:.6rem}
th.sort-desc::after{content:" ▼";color:#c00;font-size:.6rem}
td{padding:7px 10px;border-bottom:1px solid #1c1c1c;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td:nth-child(1){width:52px;text-align:center;overflow:visible}
td:nth-child(2){width:62px;text-align:center;overflow:visible}
td:nth-child(3){width:30px;text-align:center}
td:nth-child(4){width:22%}
td:nth-child(5),td:nth-child(6),td:nth-child(7),td:nth-child(8),td:nth-child(9),td:nth-child(10),td:nth-child(11),td:nth-child(12){width:calc((78% - 144px) / 8)}
th:nth-child(1){width:52px}
th:nth-child(2){width:62px}
th:nth-child(3){width:30px}
th:nth-child(4){width:22%}
th:nth-child(5),th:nth-child(6),th:nth-child(7),th:nth-child(8),th:nth-child(9),th:nth-child(10),th:nth-child(11),th:nth-child(12){width:calc((78% - 144px) / 8)}
tr:hover td{background:#161616}
td a{color:#6ab0f5;text-decoration:none}
td a:hover{text-decoration:underline}
.brand-link{color:#ccc;cursor:pointer}
.brand-link:hover{color:#ff6666;text-decoration:underline}
.tag{background:#c00;color:#fff;font-size:.65rem;font-weight:700;padding:1px 5px;border-radius:3px}
.tag-kw{background:#0a5c2a;color:#4ade80;font-size:.65rem;font-weight:700;padding:1px 5px;border-radius:3px;border:1px solid #2d6a2d}
.tag-drop{background:#1a3a1a;color:#4ade80;font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;border:1px solid #2d6a2d;white-space:nowrap}
.tag-sold{background:#3a1a1a;color:#f87171;font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;border:1px solid #6a2d2d}
.watch-btn{background:none;border:none;cursor:pointer;color:#444;font-size:1rem;line-height:1;padding:0 2px;transition:color .15s;flex-shrink:0}
.watch-btn:hover{color:#f5c518}
.watch-btn.active{color:#f5c518}
tr.sold-row td{color:#666}
tr.sold-row td a{color:#666}
tr.fav-row td:last-child{color:#4ade80}
.no-res{padding:24px 20px;color:#555;font-size:.85rem}

/* ── Paginator ── */
.paginator{display:flex;align-items:center;justify-content:center;gap:2px;padding:14px 16px;border-top:1px solid #1e1e1e;user-select:none}
.paginator .pg-info{font-size:.75rem;color:#555;margin-right:12px;white-space:nowrap}
.paginator button{background:none;border:1px solid transparent;color:#888;font-size:.78rem;min-width:32px;height:30px;border-radius:5px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0 6px;transition:all .15s;font-weight:500}
.paginator button:hover:not(:disabled):not(.pg-active){background:#1e1e1e;border-color:#333;color:#ddd}
.paginator button:disabled{color:#333;cursor:default}
.paginator button.pg-active{background:#c00;border-color:#c00;color:#fff;font-weight:700}
.paginator button.pg-nav{font-size:.72rem;color:#666;letter-spacing:-.5px}
.paginator button.pg-nav:hover:not(:disabled){color:#ccc;background:#1e1e1e;border-color:#333}
.paginator .pg-ellipsis{color:#444;font-size:.75rem;min-width:24px;text-align:center;line-height:30px}

/* ── Image thumbnail tooltip ── */
#img-tooltip{display:none;position:fixed;z-index:200;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:8px;padding:6px;box-shadow:0 8px 24px rgba(0,0,0,.6);pointer-events:none}
#img-tooltip img{display:block;width:200px;height:200px;object-fit:contain;border-radius:4px;background:#111}

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
#cl-body td:nth-child(1){width:30px;min-width:30px;max-width:30px;text-align:center;overflow:visible}
#cl-body td:nth-child(2){width:52px;min-width:52px;max-width:52px;text-align:center;overflow:visible}
#cl-body td:nth-child(3){width:50%;max-width:none;text-align:left}
#cl-body td:nth-child(4){width:90px;text-align:left}
#cl-body td:nth-child(5){width:140px}
#cl-body td:nth-child(6){width:90px}
#cl-body tr:hover td{background:#161616}
#cl-body tr.cl-fav-result td{background:#1a1f35}
#cl-body tr.cl-fav-result:hover td{background:#252b45}
#cl-body td a{color:#c7d2fe;text-decoration:none}
#cl-body td a:hover{text-decoration:underline}
.cl-empty{padding:32px;color:#555;font-size:.9rem;text-align:center}
.cl-fav-star{color:#f5c518;margin-right:4px;font-size:.8rem}

/* ── Mobile toggle buttons (hidden on desktop) ── */
.mobile-sidebar-toggle{display:none}

/* ══════════════════════════════════════════════════════════════════════════════
   MOBILE RESPONSIVE — all changes scoped inside @media so desktop is untouched
   ══════════════════════════════════════════════════════════════════════════════ */
@media(max-width:820px){

  /* ── Mobile sidebar toggle button ── */
  .mobile-sidebar-toggle{display:flex;align-items:center;gap:8px;padding:11px 16px;background:#1a1a1a;border:none;border-bottom:1px solid #2e2e2e;cursor:pointer;font-size:.85rem;color:#ccc;font-weight:600;width:100%;text-align:left;flex-shrink:0}
  .mobile-sidebar-toggle:hover{background:#222}
  .mobile-sidebar-toggle:active{background:#252525}
  .mobile-sidebar-toggle .toggle-arrow{transition:transform .2s;font-size:.65rem;color:#666}
  .mobile-sidebar-toggle .toggle-arrow.open{transform:rotate(90deg)}
  .mobile-sidebar-toggle .toggle-count{margin-left:auto;font-size:.72rem;color:#666;font-weight:400}

  /* ── Header ── */
  header{padding:10px 14px;gap:8px;flex-wrap:wrap}
  header h1{font-size:1rem}
  #hdr-status{font-size:.72rem;margin-left:auto;min-width:0;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
  #stop-btn{font-size:.75rem;padding:6px 10px}

  /* ── Tabs: shorter labels on mobile ── */
  .app-tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .app-tabs::-webkit-scrollbar{display:none}
  .app-tab{padding:10px 14px;font-size:.78rem;white-space:nowrap;flex-shrink:0}

  /* ── GC Layout: stack vertically ── */
  .layout{flex-direction:column;overflow-y:auto;overflow-x:hidden}

  /* ── GC Left sidebar: collapsible on mobile ── */
  .left{width:100%;min-width:0;max-height:none;border-right:none;border-bottom:1px solid #2e2e2e;overflow:hidden;flex-shrink:0}
  .left.collapsed .search-wrap,
  .left.collapsed #store-list,
  .left.collapsed .left-footer{display:none}
  #store-list{max-height:200px;overflow-y:auto}
  .store-row{padding:8px 12px}
  .store-row label{font-size:.9rem}
  .left-footer{padding:10px 12px}
  #reset-btn{font-size:.72rem}

  /* ── GC Right panel ── */
  .right{overflow:visible;flex:1;min-height:0;display:flex;flex-direction:column}

  /* ── Status bar: stack vertically ── */
  .status-bar{flex-direction:column;gap:6px;padding:8px 12px;align-items:flex-start}
  #global-search-wrap{margin-left:0;width:100%}
  #global-search{width:100%;flex:1}

  /* ── Log ── */
  #log{padding:6px 12px;height:auto;min-height:36px;max-height:60px;font-size:.72rem}

  /* ── Results header / filter toolbar: wrap on mobile ── */
  .results-hdr{padding:8px 10px;gap:6px;flex-wrap:wrap;align-items:center}
  .results-hdr > *{flex-shrink:0}
  #res-search-wrap{margin-left:0;width:100%;order:99}
  #res-search{width:100%;flex:1}
  .cat-sel{font-size:.74rem;padding:6px 10px}

  /* ── Filter dropdown panels: full-width overlay on mobile ── */
  #brand-dropdown,#cond-dropdown,#cat-dropdown,#subcat-dropdown{position:static}
  #brand-dd-panel,#cond-dd-panel,#cat-dd-panel,#subcat-dd-panel{position:fixed!important;left:8px!important;right:8px!important;top:auto!important;bottom:8px!important;width:auto!important;max-height:50vh!important;z-index:200!important;border-radius:10px!important;margin-top:0!important}

  /* ── GC Table: horizontal scroll with min-width ── */
  .results{overflow:auto;-webkit-overflow-scrolling:touch;flex:1}
  table{min-width:900px;table-layout:auto}
  th,td{padding:8px 8px;font-size:.78rem}
  td:nth-child(1){width:40px}
  td:nth-child(2){width:50px}
  td:nth-child(3){width:28px}
  td:nth-child(4){width:auto;min-width:180px;white-space:normal}
  td:nth-child(5),td:nth-child(6),td:nth-child(7),td:nth-child(8),td:nth-child(9),td:nth-child(10),td:nth-child(11),td:nth-child(12){width:auto;min-width:80px}
  th:nth-child(1){width:40px}
  th:nth-child(2){width:50px}
  th:nth-child(3){width:28px}
  th:nth-child(4){width:auto;min-width:180px}
  th:nth-child(5),th:nth-child(6),th:nth-child(7),th:nth-child(8),th:nth-child(9),th:nth-child(10),th:nth-child(11),th:nth-child(12){width:auto;min-width:80px}

  /* ── Paginator ── */
  .paginator{padding:10px 8px;gap:1px;flex-wrap:wrap;justify-content:center}
  .paginator button{min-width:28px;height:28px;font-size:.72rem}
  .paginator .pg-info{font-size:.7rem;margin-right:6px;width:100%;text-align:center;margin-bottom:4px}

  /* ── CL Layout: stack vertically ── */
  #cl-panel{flex-direction:column}
  .cl-left{width:100%;min-width:0;border-right:none;border-bottom:1px solid #2e2e2e;overflow:hidden;flex-shrink:0}
  .cl-left.collapsed .search-wrap,
  .cl-left.collapsed #cl-city-list{display:none}
  #cl-city-list{max-height:200px;overflow-y:auto}
  .cl-city-row{padding:8px 12px}
  .cl-city-row label{font-size:.9rem}

  /* ── CL Right ── */
  .cl-right{flex:1;overflow:auto}
  .cl-search-bar{padding:10px 12px;gap:8px;flex-wrap:wrap}
  #cl-query{width:100%;flex:1 1 100%}
  #cl-search-btn{flex:1}
  #cl-status{width:100%;text-align:center}

  /* ── CL Table: horizontal scroll ── */
  #cl-body{overflow:auto;-webkit-overflow-scrolling:touch}
  #cl-body table{min-width:580px;table-layout:auto}
  #cl-body th,#cl-body td{padding:8px 8px;font-size:.78rem}
  #cl-body td:nth-child(3){white-space:normal;min-width:180px}

  /* ── CL results header ── */
  .cl-results-hdr{flex-wrap:wrap;gap:6px;padding:8px 12px}
  #cl-res-search{width:100%;margin-left:0}

  /* ── Modals: full-width on mobile ── */
  #pw-box{width:calc(100% - 32px)!important;max-width:380px}
  #kw-modal > div:last-child{width:calc(100% - 32px)!important;max-width:420px}
  #first-run-modal > div:nth-child(2){width:calc(100% - 32px)!important;max-width:400px}
  #vs-modal > div:nth-child(2){width:calc(100% - 32px)!important;max-width:380px}

  /* ── Image tooltip: centered at bottom on mobile ── */
  #img-tooltip{top:auto!important;bottom:12px!important;left:50%!important;transform:translateX(-50%)}
  #img-tooltip img{width:180px;height:180px}

  /* ── Touch-friendly sizing ── */
  input[type=checkbox]{width:18px;height:18px}
  .sel-btn,.cl-sel-btn{padding:8px;font-size:.78rem;min-height:36px}
  #run-btn,#baseline-btn{min-height:44px;font-size:.88rem}
  .watch-btn,.fav-btn,.cl-fav-btn{font-size:1.15rem;padding:4px 6px;min-width:34px;min-height:34px;display:inline-flex;align-items:center;justify-content:center}
  button,a{-webkit-tap-highlight-color:transparent}

  /* ── Prevent body overflow ── */
  body{overflow:hidden}
  .app-panel.active{overflow-y:auto;overflow-x:hidden}
}

/* ── Extra small screens (phones in portrait) ── */
@media(max-width:480px){
  header{padding:8px 10px}
  header h1{font-size:.88rem}
  .app-tab{padding:9px 10px;font-size:.72rem}
  .status-bar{font-size:.7rem}
  table{min-width:780px}
  #cl-body table{min-width:520px}
  .results-hdr{gap:4px;padding:6px 8px}
  .cat-sel{font-size:.7rem;padding:5px 8px}
  .paginator button{min-width:24px;height:26px;font-size:.68rem}
}
</style>
</head>
<body>

<!-- Image thumbnail tooltip -->
<div id="img-tooltip"><img src="" alt=""></div>

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
  <div id="pw-overlay" onclick="cancelReset()"></div>
  <div id="pw-box">
    <h2>🗑 Reset All Data</h2>
    <p>This will delete all cached inventory, scan history, and the Excel export. Your want list will be preserved. Enter the password to continue.</p>
    <input type="password" id="pw-input" placeholder="Password"
           onkeydown="if(event.key==='Enter')confirmReset()">
    <div id="pw-err">Incorrect password.</div>
    <div class="pw-btns">
      <button id="pw-cancel" onclick="cancelReset()">Cancel</button>
      <button id="pw-confirm" onclick="confirmReset()">Reset →</button>
    </div>
  </div>
</div>

<div id="first-run-modal" style="display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.7)" onclick="dismissFirstRun()"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;padding:30px 28px;width:400px;z-index:1">
    <h2 style="color:#fff;font-size:1.05rem;margin-bottom:10px">🎸 Welcome to Gear Tracker</h2>
    <p style="color:#999;font-size:.85rem;line-height:1.6;margin-bottom:8px">The inventory database is empty. Click below to build it now. This captures Guitar Center's full used inventory across ~300 stores.</p>
    <p style="color:#777;font-size:.82rem;margin-bottom:20px">Building takes a few minutes.</p>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button onclick="dismissFirstRun()" style="padding:8px 18px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#aaa;font-size:.85rem;cursor:pointer">Later</button>
      <button onclick="dismissFirstRun();runBaseline()" style="padding:8px 18px;background:#c00;border:none;border-radius:5px;color:#fff;font-size:.85rem;font-weight:700;cursor:pointer">Build Now</button>
    </div>
  </div>
</div>

<div id="kw-modal" style="display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.7)" onclick="closeKeywords()"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;padding:24px 24px 20px;width:420px;max-height:80vh;overflow-y:auto;z-index:1">
    <h2 style="color:#fff;font-size:1.05rem;margin-bottom:4px">🔑 Want List</h2>
    <p style="color:#777;font-size:.82rem;margin-bottom:16px;line-height:1.5">Items matching your want list are highlighted in the results. New items that also match sort to the top.<br><br>
      <span style="color:#999">Matching modes:</span><br>
      <span style="color:#4ade80">Wangcaster</span> — matches any item containing "Wangcaster"<br>
      <span style="color:#4ade80">Wang, Caster</span> — matches items containing both words<br>
      <span style="color:#4ade80">"Wang Caster"</span> — exact phrase match only
    </p>
    <div style="display:flex;gap:6px;margin-bottom:16px">
      <input id="kw-input" type="text" placeholder="Add an item to your want list…"
             style="flex:1;padding:8px 12px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#eee;font-size:.9rem;outline:none"
             onkeydown="if(event.key==='Enter')addKeyword()">
      <button onclick="addKeyword()" style="padding:8px 16px;background:#0a5c2a;border:1px solid #2d6a2d;border-radius:5px;color:#4ade80;font-size:.85rem;cursor:pointer;white-space:nowrap">+ Add</button>
    </div>
    <div id="kw-list" style="margin-bottom:16px"></div>
    <div style="display:flex;gap:10px;justify-content:space-between;border-top:1px solid #2e2e2e;padding-top:14px">
      <button onclick="clearAllKeywords()" style="padding:6px 14px;background:#1a1a1a;border:1px solid #5a2a2a;border-radius:5px;color:#a05050;font-size:.78rem;cursor:pointer">Clear Want List</button>
      <button onclick="closeKeywords()" style="padding:6px 18px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#aaa;font-size:.85rem;cursor:pointer">Done</button>
    </div>
  </div>
</div>

<header>
  <h1>🎸 Gear Tracker</h1>
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
  <button class="app-tab cl-tab" onclick="switchTab('cl')">🟣 CL National Musical Instruments Search</button>
</div>

<!-- ══ GC PANEL ══ -->
<div class="app-panel active" id="gc-panel">
<div class="layout">

  <div class="left" id="gc-left">
    <button class="mobile-sidebar-toggle" id="gc-sidebar-toggle" onclick="toggleMobileSidebar('gc')">
      <span class="toggle-arrow" id="gc-toggle-arrow">▶</span>
      Stores
      <span class="toggle-count" id="gc-toggle-count"></span>
    </button>
    <div class="search-wrap" id="search-wrap">
      <input id="search" type="text" placeholder="Filter by location name…" autocomplete="off">
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
        <button id="run-btn" onclick="runTracker()" disabled style="display:none">Check for New</button>
        <button id="baseline-btn" onclick="runBaseline()" title="Scan every GC store nationwide" style="display:none">🌐 Build Baseline</button>
      </div>
      <button id="validate-stores-btn" onclick="validateStores()"
        style="display:none"
        title="Check all stores and remove any that no longer exist">
        ✓ Validate Stores
      </button>
      <button id="populate-store-btn" onclick="populateStoreData()"
        style="display:none"
        title="One-time: tag all cached items with their store (enables instant browse)">
        ⬇ Populate Store Data
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
      <span id="s-last-wrap">Last checked for new gear: <b id="s-last">—</b> <button id="check-now-btn" onclick="runTracker()" style="padding:2px 10px;background:#c00;color:#fff;border:none;border-radius:4px;font-size:.72rem;font-weight:700;cursor:pointer;margin-left:4px;display:none">Check Now</button></span>
      <span>Items: <b id="s-known">—</b></span>
      <span>Stores: <b id="s-stores">—</b></span>
      <div id="global-search-wrap">
        <input id="global-search" type="text" placeholder="Search all stores…"
               onkeydown="if(event.key==='Enter')globalSearch()" autocomplete="off">
        <button id="global-search-btn" onclick="globalSearch()" title="Search all stores">🔍</button>
        <button id="global-search-clear" onclick="clearGlobalSearch()" title="Clear search results" style="display:none">✕</button>
      </div>
      <span id="s-excel" style="display:none"><a style="color:#6ab0f5" href="/download/excel">Download Excel ↗</a></span>
      <span id="s-want-match" style="display:none;color:#4caf50;font-weight:600;font-size:.82rem;cursor:pointer" onclick="searchWantList()" title="Click to view want list matches"></span>
    </div>
    <div id="log"><span class="log-dim">Ready — select stores and click Run, or build a full baseline.</span></div>
    <div class="results" id="res-panel" style="display:none">
      <div class="results-hdr">
        <span id="res-title">New Items</span>
        <span class="badge" id="res-badge"></span>
        <button id="watchlist-toggle" onclick="toggleWatchFilter()"
          class="cat-sel" style="border-color:#3a3a3a;color:#aaa;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
          ★ Watch List
        </button>
        <button onclick="openKeywords()"
          class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
          🎯 Want List
        </button>
        <a id="search-wl-link" onclick="searchWantList()" style="color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;text-decoration:none;margin-left:2px" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">Search Want List</a>
        <div id="brand-dropdown" class="brand-dd" style="display:none;position:relative">
          <button id="brand-dd-btn" class="cat-sel" onclick="toggleBrandDropdown()" style="cursor:pointer;white-space:nowrap">All Brands ▾</button>
          <div id="brand-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:50;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;margin-top:4px;width:260px;max-height:320px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)">
            <div style="padding:6px">
              <input id="brand-dd-search" type="text" placeholder="Search brands…"
                style="width:100%;padding:6px 10px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#eee;font-size:.82rem;outline:none;box-sizing:border-box"
                oninput="filterBrandDropdown()" autocomplete="off">
            </div>
            <div id="brand-dd-list" style="overflow-y:auto;max-height:260px"></div>
          </div>
        </div>
        <div id="cond-dropdown" class="cond-dd" style="display:none;position:relative">
          <button id="cond-dd-btn" class="cat-sel" onclick="toggleCondDropdown()" style="cursor:pointer;white-space:nowrap">All Conditions ▾</button>
          <div id="cond-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:50;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;margin-top:4px;width:220px;max-height:300px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:4px 0">
          </div>
        </div>
        <div id="cat-dropdown" class="cond-dd" style="display:none;position:relative">
          <button id="cat-dd-btn" class="cat-sel" onclick="toggleCatDropdown()" style="cursor:pointer;white-space:nowrap">All Categories ▾</button>
          <div id="cat-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:50;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;margin-top:4px;width:240px;max-height:300px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:4px 0">
          </div>
        </div>
        <div id="subcat-dropdown" class="cond-dd" style="display:none;position:relative">
          <button id="subcat-dd-btn" class="cat-sel" onclick="toggleSubcatDropdown()" style="cursor:pointer;white-space:nowrap">All Subcategories ▾</button>
          <div id="subcat-dd-panel" style="display:none;position:absolute;top:100%;left:0;z-index:50;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;margin-top:4px;width:240px;max-height:300px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:4px 0">
          </div>
        </div>
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
  <div class="cl-left" id="cl-left">
    <button class="mobile-sidebar-toggle" id="cl-sidebar-toggle" onclick="toggleMobileSidebar('cl')">
      <span class="toggle-arrow" id="cl-toggle-arrow">▶</span>
      Cities
      <span class="toggle-count" id="cl-toggle-count"></span>
    </button>
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
    <div class="cl-results-hdr" id="cl-toolbar" style="display:flex;align-items:center;gap:8px">
      <button id="cl-watchlist-toggle" onclick="clToggleWatchFilter()"
        class="cat-sel" style="border-color:#3a3a3a;color:#aaa;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
        ★ Watch List
      </button>
      <button onclick="openKeywords()"
        class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
        🎯 Want List
      </button>
      <a id="cl-search-wl-link" onclick="clSearchWantList()" style="color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;text-decoration:none;margin-left:2px" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">Search Want List</a>
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

// ── Mobile sidebar toggle ────────────────────────────────────────────────────
function _isMobile() { return window.innerWidth <= 820; }

function toggleMobileSidebar(which) {
  const panel = document.getElementById(which === 'gc' ? 'gc-left' : 'cl-left');
  const arrow = document.getElementById(which + '-toggle-arrow');
  const isCollapsed = panel.classList.toggle('collapsed');
  arrow.classList.toggle('open', !isCollapsed);
}

function _updateMobileToggleCounts() {
  const gcCount = document.getElementById('gc-toggle-count');
  if (gcCount) {
    const n = document.querySelectorAll('.store-row input:checked').length;
    gcCount.textContent = n > 0 ? n + ' selected' : '';
  }
  const clCount = document.getElementById('cl-toggle-count');
  if (clCount) {
    const n = document.querySelectorAll('.cl-city-row input:checked').length;
    clCount.textContent = n > 0 ? n + ' selected' : '';
  }
}

// Auto-collapse sidebars on mobile on page load
document.addEventListener('DOMContentLoaded', () => {
  if (_isMobile()) {
    document.getElementById('gc-left').classList.add('collapsed');
    document.getElementById('cl-left').classList.add('collapsed');
  }
});
// Re-check on resize (e.g. rotating phone)
window.addEventListener('resize', () => {
  const gcLeft = document.getElementById('gc-left');
  const clLeft = document.getElementById('cl-left');
  if (!_isMobile()) {
    gcLeft.classList.remove('collapsed');
    clLeft.classList.remove('collapsed');
  }
});

// ── localStorage helpers ─────────────────────────────────────────────────────
function _lsGet(key, fallback) {
  try { const v = localStorage.getItem('gt_' + key); return v ? JSON.parse(v) : fallback; }
  catch(e) { return fallback; }
}
function _lsSet(key, val) {
  try { localStorage.setItem('gt_' + key, JSON.stringify(val)); } catch(e) {}
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('search').addEventListener('input', filterList);
  // Load personal data from localStorage
  favorites = _lsGet('favorites', []);
  window._watchlist = _lsGet('watchlist', {});
  window._clWatchlist = _lsGet('cl_watchlist', {});
  // Migrate any cl: prefixed items from shared watchlist to separate CL watchlist
  Object.keys(window._watchlist).forEach(k => {
    if (k.startsWith('cl:')) {
      if (!window._clWatchlist[k]) window._clWatchlist[k] = window._watchlist[k];
      delete window._watchlist[k];
    }
  });
  _lsSet('watchlist', window._watchlist);
  _lsSet('cl_watchlist', window._clWatchlist);
  window._keywords = _lsGet('keywords', []);
  window._prevSnapshot = new Set(_lsGet('prev_snapshot', []));  // Previous scan's full ID set
  window._newIds = new Set(_lsGet('new_ids', []));  // Items flagged NEW from last Check for New
  clRenderCities(true);  // Select all cities on initial load
  await loadData();
  await loadState();
});

async function loadData() {
  const r = await fetch('/api/stores');
  const d = await r.json();
  allStores = d.stores;
  renderList(new Set(d.stores));  // Select all stores on initial load
  const info = d.info || {};
  const storeLabel = info.count ? info.count : allStores.length;
  document.getElementById('hdr-status').textContent = storeLabel + ' stores available';
  document.getElementById('s-stores').textContent = storeLabel;
  if (allStores.length === 0) {
    appendLog('💡 No stores loaded — click "✓ Validate Stores" to build the store list from GC live data.', 'log-dim');
  }
}

window._lastRunISO = null;
let _relTimeTimer = null;

function _timeAgo(iso) {
  if (!iso) return 'never';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60)   return 'just now';
  if (diff < 120)  return '1 minute ago';
  if (diff < 3600) return Math.floor(diff / 60) + ' minutes ago';
  if (diff < 7200) return '1 hour ago';
  if (diff < 86400) return Math.floor(diff / 3600) + ' hours ago';
  if (diff < 172800) return '1 day ago';
  return Math.floor(diff / 86400) + ' days ago';
}

function _updateRelativeTime() {
  document.getElementById('s-last').textContent = _timeAgo(window._lastRunISO);
  clearInterval(_relTimeTimer);
  _relTimeTimer = setInterval(() => {
    document.getElementById('s-last').textContent = _timeAgo(window._lastRunISO);
  }, 30000); // Update every 30s
}

async function loadState() {
  // Per-user timing from localStorage
  window._lastRunISO = _lsGet('last_run', null);

  // Shared state from server
  const r = await fetch('/api/state');
  const s = await r.json();
  document.getElementById('s-known').textContent = s.total_items.toLocaleString();
  if (s.excel_exists) document.getElementById('s-excel').style.display = 'inline';

  // Display is based on user's own last_run only (no nightly scan)

  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  if (s.is_first_run && !window._prevSnapshot.size) {
    document.getElementById('first-run-modal').style.display = 'flex';
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

function _getCheckedStores() {
  return new Set([...document.querySelectorAll('.store-row input:checked')].map(c => c.value));
}

function toggleFavsFilter() {
  const wasChecked = _getCheckedStores();
  favsOnly = !favsOnly;
  const btn = document.getElementById('favs-btn');
  btn.classList.toggle('active', favsOnly);
  document.getElementById('search').value = '';
  // When switching TO favs view, auto-select all favorites
  if (favsOnly) {
    favorites.forEach(f => wasChecked.add(f));
  }
  renderList(wasChecked);
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
function renderList(preserveChecked) {
  // Capture current selections if not provided
  const checked = preserveChecked || _getCheckedStores();
  const el = document.getElementById('store-list');
  const q  = document.getElementById('search').value.toLowerCase();
  // In favorites mode with a search query, show ALL matching stores so users can find and add new favorites
  let stores = (favsOnly && !q) ? favorites : allStores;

  if (favsOnly && !q && !favorites.length) {
    el.innerHTML = '<div class="empty-msg">No favorites yet.<br>Click ★ next to any store to add it,<br>or type in the search box to find stores.</div>';
    updateCount(); return;
  }

  let filtered = q ? stores.filter(s => s.toLowerCase().includes(q)) : stores;
  // In favorites mode with a search, put favorited stores first
  if (favsOnly && q) {
    const favSet = new Set(favorites);
    filtered.sort((a, b) => (favSet.has(b) ? 1 : 0) - (favSet.has(a) ? 1 : 0));
  }
  el.innerHTML = '';
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/[^a-zA-Z0-9]/g,'_');
    const isChecked = checked.has(name);
    div.innerHTML =
      `<input type="checkbox" id="${id}" value="${name}" ${isChecked ? 'checked' : ''}>` +
      `<label for="${id}">${name}</label>` +
      `<button class="fav-btn ${isFav?'active':''}" title="${isFav?'Remove from':'Add to'} favorites"
        onclick="toggleFav(event,'${name.replace(/'/g,"\\'")}',this)">★</button>`;
    div.querySelector('input').addEventListener('change', updateCount);
    el.appendChild(div);
  });
  updateCount();
}

function filterList() {
  renderList();  // preserves current selections via _getCheckedStores
}

// ── Favorites ─────────────────────────────────────────────────────────────────
function toggleFav(e, name, btn) {
  e.stopPropagation();
  const adding = !favorites.includes(name);
  if (adding) {
    favorites.push(name);
  } else {
    favorites = favorites.filter(f => f !== name);
  }
  favorites.sort();
  _lsSet('favorites', favorites);
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
  _updateMobileToggleCounts();
  // Auto-browse cached inventory when stores are selected
  if (n > 0 && !running && !_globalSearchActive) browseCache();
  else if (n === 0 && !_globalSearchActive) {
    document.getElementById('res-panel').style.display = 'none';
  }
}

// ── Browse cached inventory (server-side pagination) ──────────────────────
let _browseTimer = null;
let _skipBrowse = false;  // Set after a scan to prevent browseCache from overwriting results
let _watchFilterActive = false;
let _globalSearchActive = false;
let _globalSearchQuery = '';
let _wantListSearchActive = false;

// Mode: 'server' = browse with server-side pagination, 'local' = scan/watchlist with client data
let _browseMode = 'server';
// Server-side pagination state
let _srvStores = [];
let _srvPage = 1;
let _srvSortField = 'date';
let _srvSortDir = 'desc';
let _srvTotalCount = 0;
let _srvTotalUnfiltered = 0;
let _srvTotalPages = 1;
let _srvLoading = false;

function _getBrowseFilters() {
  return {
    filter_q:              document.getElementById('res-search').value.trim(),
    filter_brands:         window._selectedBrands || [],
    filter_conditions:     window._selectedConds || [],
    filter_categories:     window._selectedCats || [],
    filter_subcategories:  window._selectedSubs || [],
    filter_watched:        _watchFilterActive,
  };
}

async function _fetchBrowsePage(page) {
  if (_srvLoading) return;
  _srvLoading = true;
  const filters = _getBrowseFilters();
  // In global search mode, override filter_q with the global query and search all stores
  const body = {
    page:       page,
    per_page:   50,
    sort_field: _srvSortField,
    sort_dir:   _srvSortDir,
    user_sorted: window._sortCol !== null,
    fav_stores: favorites,
    keywords:   window._keywords || [],
    watchlist_ids: Object.keys(window._watchlist || {}),
    new_ids:    window._newIds instanceof Set ? [...window._newIds] : (window._newIds || []),
    ...filters,
  };
  if (_globalSearchActive) {
    body.all_stores = true;
    body.filter_q = _globalSearchQuery;
    // force_fav_sort removed — sorting is now purely by user's column choice
    if (_wantListSearchActive) {
      body.filter_want_list_only = true;
    }
  } else {
    body.stores = _srvStores;
  }
  try {
    const r = await fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.no_store_data) {
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Browse Data Yet';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML =
        '<div class="no-res">Click <b>⬇ Populate Store Data</b> in the left panel to tag your existing inventory with store names. This only needs to run once, then selecting stores will instantly show their inventory.</div>';
      ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
      return;
    }
    if (!d.items || (!d.items.length && page === 1)) {
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Items Found';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML = '<div class="no-res">No cached inventory for selected store(s). Run Check for New Items to scan.</div>';
      return;
    }

    _srvPage           = d.page;
    _srvTotalCount     = d.total_count;
    _srvTotalUnfiltered = d.total_unfiltered;
    _srvTotalPages     = d.total_pages;

    // Update header
    const hasFilters = filters.filter_q || (filters.filter_brands && filters.filter_brands.length) || (filters.filter_conditions && filters.filter_conditions.length) || (filters.filter_categories && filters.filter_categories.length) || (filters.filter_subcategories && filters.filter_subcategories.length) || filters.filter_watched;
    const newCount = d.new_count || 0;
    if (_wantListSearchActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} Want List matches nationwide`
        : 'No Want List matches found';
    } else if (_globalSearchActive) {
      const label = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} results for "${_globalSearchQuery}"`
        : `No results for "${_globalSearchQuery}"`;
      document.getElementById('res-title').textContent = hasFilters
        ? `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()} results for "${_globalSearchQuery}"`
        : label;
    } else if (newCount > 0 && !hasFilters) {
      document.getElementById('res-title').textContent = `${_srvTotalUnfiltered.toLocaleString()} Items`;
      document.getElementById('res-badge').textContent = newCount + ' NEW';
    } else if (hasFilters) {
      document.getElementById('res-title').textContent = `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()} Items`;
      document.getElementById('res-badge').textContent = '';
    } else {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} Items` : 'No Items Found';
      document.getElementById('res-badge').textContent = '';
    }
    document.getElementById('res-panel').style.display = 'block';

    // Update filter count
    const countEl = document.getElementById('res-search-count');
    if (hasFilters) {
      countEl.textContent = `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()}`;
    } else {
      countEl.textContent = '';
    }
    const clearBtn = document.getElementById('clear-filters-btn');
    if (clearBtn) clearBtn.style.display = ((filters.filter_brands && filters.filter_brands.length) || (filters.filter_conditions && filters.filter_conditions.length) || (filters.filter_categories && filters.filter_categories.length) || (filters.filter_subcategories && filters.filter_subcategories.length)) ? '' : 'none';

    // Populate filter dropdowns from server-provided options
    _populateFiltersFromServer(d.brands || [], d.conditions || [], d.categories || [], d.subcategories || [], filters);

    // Render table + paginator
    _renderServerTable(d.items);

    // Scroll results to top on page change
    document.querySelector('.results')?.scrollTo(0, 0);

  } finally {
    _srvLoading = false;
  }
}

function _populateFiltersFromServer(brands, conditions, categories, subcategories, currentFilters) {
  _setBrandList(brands);
  const savedBrands = currentFilters.filter_brands || [];
  window._selectedBrands = savedBrands.filter(b => brands.some(br => br.name === b));
  _updateBrandBtn();

  _setCondList(conditions);
  const savedConds = currentFilters.filter_conditions || [];
  window._selectedConds = savedConds.filter(c => conditions.includes(c));
  _updateCondBtn();

  _setCatList(categories);
  const savedCats = currentFilters.filter_categories || [];
  window._selectedCats = savedCats.filter(c => categories.includes(c));
  _updateCatBtn();

  if (subcategories.length && window._selectedCats.length) {
    _setSubList(subcategories);
    const savedSubs = currentFilters.filter_subcategories || [];
    window._selectedSubs = savedSubs.filter(s => subcategories.includes(s));
  } else {
    _setSubList([]);
    window._selectedSubs = [];
  }
  _updateSubcatBtn();
}

function _buildRowHtml(item) {
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
  const isNew = item.isNew || (item.id && window._newIds && window._newIds.has(item.id));
  const rowClass = [isSold ? 'sold-row' : '', item.isFav ? 'fav-row' : ''].filter(Boolean).join(' ');
  return `<tr class="${rowClass}" data-name="${esc(item.name)}" data-brand="${esc(item.brand)}" data-price="${priceNum}" data-store="${esc(item.store)}" data-location="${esc(item.location)}" data-condition="${esc(item.condition)}" data-category="${esc(item.category)}" data-subcategory="${esc(item.subcategory)}" data-image-id="${esc(item.image_id)}">` +
    `<td>${isNew ? '<span class="tag">NEW</span>' : ''}</td>` +
    `<td>${item.kwMatch ? '<span class="tag-kw">WANT</span>' : ''}</td>` +
    `<td>${watchStar}</td>` +
    `<td>${nameCell}${soldBadge}</td>` +
    `<td>${item.brand ? '<span class="brand-link" title="Filter by brand">' + esc(item.brand) + '</span>' : ''}</td>` +
    `<td>${item.price||''}</td>` +
    `<td>${dropCell}</td>` +
    `<td>${esc(item.condition)}</td>` +
    `<td>${esc(item.category)}</td>` +
    `<td>${esc(item.subcategory)}</td>` +
    `<td>${esc(item.date||'')}</td>` +
    `<td>${esc(item.location||item.store)}</td>` +
    `</tr>`;
}

// ── Paginator builder ────────────────────────────────────────────────────────
function _buildPaginatorHtml(currentPage, totalPages, totalCount, perPage) {
  if (totalPages <= 1) return '';
  const startItem = (currentPage - 1) * perPage + 1;
  const endItem   = Math.min(currentPage * perPage, totalCount);

  let html = '<div class="paginator">';
  html += `<span class="pg-info">${startItem.toLocaleString()}–${endItem.toLocaleString()} of ${totalCount.toLocaleString()}</span>`;

  // First / Prev
  html += `<button class="pg-nav" onclick="goToPage(1)" ${currentPage === 1 ? 'disabled' : ''} title="First page">&#x276E;&#x276E;</button>`;
  html += `<button class="pg-nav" onclick="goToPage(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''} title="Previous page">&#x276E;</button>`;

  // Page numbers with smart ellipsis
  const pages = _getPaginatorRange(currentPage, totalPages);
  pages.forEach(p => {
    if (p === '...') {
      html += '<span class="pg-ellipsis">…</span>';
    } else {
      html += `<button class="${p === currentPage ? 'pg-active' : ''}" onclick="goToPage(${p})">${p}</button>`;
    }
  });

  // Next / Last
  html += `<button class="pg-nav" onclick="goToPage(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''} title="Next page">&#x276F;</button>`;
  html += `<button class="pg-nav" onclick="goToPage(${totalPages})" ${currentPage === totalPages ? 'disabled' : ''} title="Last page">&#x276F;&#x276F;</button>`;

  html += '</div>';
  return html;
}

function _getPaginatorRange(current, total) {
  // Always show first 2, last 2, and 2 around current. Fill gaps with ellipsis.
  if (total <= 9) return Array.from({length: total}, (_, i) => i + 1);

  const pages = new Set();
  // First two
  pages.add(1); pages.add(2);
  // Last two
  pages.add(total - 1); pages.add(total);
  // Around current
  for (let i = current - 2; i <= current + 2; i++) {
    if (i >= 1 && i <= total) pages.add(i);
  }

  const sorted = [...pages].sort((a, b) => a - b);
  const result = [];
  for (let i = 0; i < sorted.length; i++) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) {
      result.push('...');
    }
    result.push(sorted[i]);
  }
  return result;
}

function _renderServerTable(items) {
  let html = `<table id="res-table"><thead><tr>
    <th data-col="0"></th>
    <th data-col="kw"></th>
    <th data-col="watch"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Brand</th>
    <th data-col="3">Price</th>
    <th data-col="drop"></th>
    <th data-col="4">Condition</th>
    <th data-col="5">Category</th>
    <th data-col="6">Subcategory</th>
    <th data-col="7">Date Listed</th>
    <th data-col="8">Location</th>
  </tr></thead><tbody>`;
  items.forEach(item => { html += _buildRowHtml(item); });
  html += '</tbody></table>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;

  // Attach sort headers
  if (window._sortCol !== null) {
    const th = document.querySelector(`#res-table th[data-col="${window._sortCol}"]`);
    if (th) th.classList.add(window._sortDir === 1 ? 'sort-asc' : 'sort-desc');
  }
  document.querySelectorAll('#res-table thead th[data-col]').forEach(th => {
    const colIdx = parseInt(th.dataset.col);
    if (!_SORT_COLS[colIdx]) return;
    th.addEventListener('click', () => sortTable(colIdx));
  });
  autoSizeItemColumn();

  // Brand-click delegation: clicking a brand name filters to that brand
  const tbody = document.querySelector('#res-table tbody');
  if (tbody) {
    tbody.addEventListener('click', function(e) {
      const span = e.target.closest('.brand-link');
      if (!span) return;
      const tr = span.closest('tr');
      if (!tr) return;
      const brand = tr.dataset.brand;
      if (brand) selectBrand(brand);
    });
  }
}

async function browseCache() {
  if (_skipBrowse) { _skipBrowse = false; return; }
  clearTimeout(_browseTimer);
  _browseTimer = setTimeout(async () => {
    const stores = getSelected();
    if (!stores.length) return;
    _browseMode = 'server';
    _globalSearchActive = false; _wantListSearchActive = false;
    _globalSearchQuery = '';
    document.getElementById('global-search').value = '';
    document.getElementById('global-search-clear').style.display = 'none';
    _resetWantListLink();
    _srvStores = stores;
    _srvPage = 1;
    // Preserve current sort — don't reset _srvSortField/_srvSortDir/window._sortCol
    document.getElementById('res-search').value = '';
    document.getElementById('res-search-count').textContent = '';
    window._selectedBrands = []; _updateBrandBtn();
    window._selectedConds = []; _updateCondBtn();
    window._selectedCats = []; _updateCatBtn();
    window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
    await _fetchBrowsePage(1);
  }, 300);
}

// ── Watch list ────────────────────────────────────────────────────────────
window._watchlist = {};
window._clWatchlist = {};

// loadWatchlist no longer needed — loaded from localStorage in init

function toggleWatch(id, btn) {
  const isWatched = !!(window._watchlist[id]);
  if (isWatched) {
    delete window._watchlist[id];
  } else {
    const row = btn.closest('tr');
    window._watchlist[id] = {
      name:  row ? row.dataset.name : '',
      store: row ? row.dataset.store : '',
      location: row ? row.dataset.location : '',
      date_added: new Date().toISOString().slice(0,10),
    };
  }
  _lsSet('watchlist', window._watchlist);
  btn.classList.toggle('active', !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
}

function toggleWatchFilter() {
  _watchFilterActive = !_watchFilterActive;
  const btn = document.getElementById('watchlist-toggle');
  btn.classList.toggle('wl-active', _watchFilterActive);

  if (_browseMode === 'server') {
    _srvPage = 1;
    _fetchBrowsePage(1);
  } else {
    window._localPage = 1;
    renderTable();
  }
}

// Legacy showWatchList — now just activates the toggle
async function showWatchList() {
  if (!_watchFilterActive) toggleWatchFilter();
}



function getSelected() {
  return [...document.querySelectorAll('.store-row input:checked')].map(c => c.value);
}


function dismissFirstRun() {
  document.getElementById('first-run-modal').style.display = 'none';
}

// ── Want List ─────────────────────────────────────────────────────────────────
window._keywords = [];

// loadKeywords no longer needed — loaded from localStorage in init

function openKeywords() {
  document.getElementById('kw-modal').style.display = 'flex';
  document.getElementById('kw-input').value = '';
  renderKeywordList();
  setTimeout(() => document.getElementById('kw-input').focus(), 50);
}

function closeKeywords() {
  document.getElementById('kw-modal').style.display = 'none';
  // Refresh whichever tab is active
  const clActive = document.querySelector('.cl-tab.active');
  if (clActive && _clData.length) {
    clRenderResults();
    if (_clWantListFilterActive) clFilterResults();
  } else if (_browseMode === 'server') {
    _fetchBrowsePage(_srvPage);
  } else {
    renderTable();
  }
}

function renderKeywordList() {
  const el = document.getElementById('kw-list');
  if (!window._keywords.length) {
    el.innerHTML = '<div style="color:#555;font-size:.82rem;padding:8px 0">Your want list is empty. Add an item above.</div>';
    return;
  }
  el.innerHTML = window._keywords.map(kw =>
    `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #222">
      <span class="tag-kw" style="font-size:.75rem">${kw.replace(/</g,'&lt;')}</span>
      <span style="flex:1"></span>
      <button onclick="removeKeyword('${kw.replace(/'/g,"\\'")}')"
        style="background:none;border:none;color:#666;font-size:.85rem;cursor:pointer;padding:2px 6px" title="Remove">&#10005;</button>
    </div>`
  ).join('');
}

function addKeyword() {
  const input = document.getElementById('kw-input');
  const word = input.value.trim();
  if (!word) return;
  if (!window._keywords.some(k => k.toLowerCase() === word.toLowerCase())) {
    window._keywords.push(word);
    window._keywords.sort();
    _lsSet('keywords', window._keywords);
  }
  input.value = '';
  renderKeywordList();
  input.focus();
}

function removeKeyword(word) {
  window._keywords = window._keywords.filter(k => k.toLowerCase() !== word.toLowerCase());
  _lsSet('keywords', window._keywords);
  renderKeywordList();
}

function clearAllKeywords() {
  if (!window._keywords.length) return;
  window._keywords = [];
  _lsSet('keywords', window._keywords);
  renderKeywordList();
}

function _itemMatchesKeyword(item) {
  if (!window._keywords.length) return false;
  const text = ((item.name || '') + ' ' + (item.brand || '')).toLowerCase();
  return window._keywords.some(kw => {
    kw = kw.trim();
    if (kw.startsWith('"') && kw.endsWith('"') && kw.length > 2) {
      // Exact substring match
      return text.includes(kw.slice(1, -1).toLowerCase());
    } else if (kw.includes(',')) {
      // All-terms match
      return kw.split(',').map(t => t.trim().toLowerCase()).filter(Boolean).every(t => text.includes(t));
    } else {
      // Simple contains
      return text.includes(kw.toLowerCase());
    }
  });
}

// ── Global search (all stores) ───────────────────────────────────────────────
function globalSearch() {
  const q = document.getElementById('global-search').value.trim();
  if (!q) return;
  _globalSearchActive = true;
  _wantListSearchActive = false;
  _globalSearchQuery = q;
  _browseMode = 'server';
  _srvPage = 1;
  _srvSortField = 'date';
  _srvSortDir = 'desc';
  window._sortCol = null; window._sortDir = 1;
  // Reset filters
  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  window._selectedBrands = []; _updateBrandBtn();
  window._selectedConds = []; _updateCondBtn();
  window._selectedCats = []; _updateCatBtn();
  window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
  _watchFilterActive = false;
  document.getElementById('watchlist-toggle').classList.remove('wl-active');
  // Show the clear button
  document.getElementById('global-search-clear').style.display = '';
  _fetchBrowsePage(1);
}

function clearGlobalSearch() {
  _globalSearchActive = false; _wantListSearchActive = false;
  _globalSearchQuery = '';
  document.getElementById('global-search').value = '';
  document.getElementById('global-search-clear').style.display = 'none';
  _resetWantListLink();
  // Go back to whatever stores are selected
  const stores = getSelected();
  if (stores.length) {
    browseCache();
  } else {
    document.getElementById('res-panel').style.display = 'none';
  }
}

function searchWantList() {
  // Toggle: if already searching want list, clear it
  if (_wantListSearchActive) {
    clearGlobalSearch();
    return;
  }
  if (!window._keywords || !window._keywords.length) {
    openKeywords();
    return;
  }
  _globalSearchActive = true;
  _wantListSearchActive = true;
  _globalSearchQuery = '';
  _browseMode = 'server';
  _srvPage = 1;
  _srvSortField = 'date';
  _srvSortDir = 'desc';
  window._sortCol = null; window._sortDir = 1;
  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  window._selectedBrands = []; _updateBrandBtn();
  window._selectedConds = []; _updateCondBtn();
  window._selectedCats = []; _updateCatBtn();
  window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
  _watchFilterActive = false;
  document.getElementById('watchlist-toggle').classList.remove('wl-active');
  document.getElementById('global-search-clear').style.display = '';
  document.getElementById('global-search').value = '🎯 Want List Search';
  document.getElementById('search-wl-link').textContent = 'Clear Want List Search';
  document.getElementById('search-wl-link').style.color = '#f88';
  _fetchBrowsePage(1);
}

function _resetWantListLink() {
  document.getElementById('search-wl-link').textContent = 'Search Want List';
  document.getElementById('search-wl-link').style.color = '#4ade80';
}

function runBaseline() {
  startRun({stores:[], baseline:true}, true);
}

function cancelReset() {
  document.getElementById('pw-modal').style.display = 'none';
}

function confirmReset() {
  const pw = document.getElementById('pw-input').value;
  if (pw !== 'Beatle909!') {
    document.getElementById('pw-err').style.display = 'block';
    document.getElementById('pw-input').select();
    return;
  }
  document.getElementById('pw-modal').style.display = 'none';
  doReset(pw);
}

// ── Run ───────────────────────────────────────────────────────────────────────
async function runTracker() {
  // Always scan nationwide so snapshot comparison is accurate
  await startRun({stores:[], baseline:false}, false);
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
    let msg;
    try { msg = JSON.parse(e.data); } catch(err) {
      appendLog('Warning: could not parse progress message', 'log-err');
      return;
    }
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      stopBtn.style.display = 'none';
      _skipBrowse = true;  // Prevent browseCache from overwriting scan results
      updateCount(); loadState(); showResults(msg, isBaseline);
    }
  };
  es.onerror = () => {
    // SSE connection dropped — recover gracefully
    es.close();
    if (running) {
      running = false;
      stopBtn.style.display = 'none';
      updateCount(); loadState();
      appendLog('Connection to server lost. Refreshing results…', 'log-dim');
      // Fall back to browse mode to show whatever data was saved
      setTimeout(() => {
        const stores = getSelected();
        if (stores.length) browseCache();
      }, 1000);
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

  const stoppedNote = msg.stopped ? ' (stopped early)' : '';

  // Determine what's new by comparing current scan against previous snapshot
  const allIds = msg.all_ids || [];
  const currentIdSet = new Set(allIds);
  const isFirstRun = window._prevSnapshot.size === 0;
  const newIdSet = new Set();
  if (!isFirstRun) {
    // Normal run: items in current scan but NOT in previous snapshot are new
    allIds.forEach(id => { if (!window._prevSnapshot.has(id)) newIdSet.add(id); });
  }
  // else: first run — everything gets seeded as the baseline snapshot, nothing is "new"
  const newCount = newIdSet.size;

  appendLog(`\\n✓ Done${stoppedNote} — ${msg.scanned.toLocaleString()} items scanned, ${isFirstRun ? 'initial database built' : newCount.toLocaleString() + ' new for you'}.`, 'log-dim');

  // Update per-user state in localStorage — REPLACE snapshot with current scan's IDs
  window._prevSnapshot = currentIdSet;
  _lsSet('prev_snapshot', [...currentIdSet]);
  // Save the NEW ids so they persist across page loads until next Check for New
  window._newIds = newIdSet;
  _lsSet('new_ids', [...newIdSet]);
  window._lastRunISO = new Date().toISOString();
  _lsSet('last_run', window._lastRunISO);
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  // Check if any new items match the want list and show notification
  const wantMatchEl = document.getElementById('s-want-match');
  if (newCount > 0 && window._keywords && window._keywords.length) {
    // We need item details to check want list — fetch from server cache
    fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page:1, per_page:1000, all_stores:true, new_ids:[...newIdSet], keywords:window._keywords, filter_want_list_only:true})
    }).then(r => r.json()).then(d => {
      const wantNewCount = d.total_count || 0;
      if (wantNewCount > 0) {
        wantMatchEl.textContent = '🎯 ' + wantNewCount + ' new want list match' + (wantNewCount > 1 ? 'es' : '') + '!';
        wantMatchEl.style.display = '';
      } else {
        wantMatchEl.style.display = 'none';
      }
    }).catch(() => { wantMatchEl.style.display = 'none'; });
  } else {
    wantMatchEl.style.display = 'none';
  }

  // Refresh shared item count from server
  fetch('/api/state').then(r => r.json()).then(s => {
    document.getElementById('s-known').textContent = s.total_items.toLocaleString();
  }).catch(() => {});

  if (isBaseline) {
    document.getElementById('res-title').textContent = 'Baseline Complete';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML =
      `<div class="no-res">Inventory database built (${msg.scanned.toLocaleString()} items)${stoppedNote}. Check back any time to see what's new!</div>`;
    ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  document.getElementById('res-title').textContent = `${msg.scanned.toLocaleString()} Items${newCount > 0 ? '' : ' (nothing new)'}`;
  document.getElementById('res-badge').textContent = newCount > 0 ? newCount + ' NEW' : '';

  if (msg.scanned === 0) {
    document.getElementById('res-body').innerHTML = '<div class="no-res">Nothing found for selected stores.</div>';
    ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  // For large scans, switch to server-side browse
  if (msg.use_browse) {
    _browseMode = 'server';
    _srvStores = getSelected();
    if (!_srvStores.length) _srvStores = [];
    _srvPage = 1;
    _srvSortField = 'date';
    _srvSortDir = 'desc';
    window._sortCol = null; window._sortDir = 1;
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
    if (!_srvStores.length) {
      _globalSearchActive = false; _wantListSearchActive = false;
      _globalSearchQuery = '';
    }
    _fetchBrowsePage(1);
    return;
  }

  // Small scan: render items client-side, marking isNew per-user
  _browseMode = 'local';
  window._tableData = (msg.items || []).map(item => ({
    ...item,
    isNew: newIdSet.has(item.id),
    kwMatch: _itemMatchesKeyword(item),
  }));
  window._tableData.sort((a, b) => {
    // Only NEW items float to top; everything else by date desc
    const aNew = a.isNew ? 0 : 1;
    const bNew = b.isNew ? 0 : 1;
    if (aNew !== bNew) return aNew - bNew;
    return (b.date_raw || '').localeCompare(a.date_raw || '');
  });
  window._sortCol = null; window._sortDir = 1; window._localPage = 1;
  populateCategoryFilter();
  renderTable();
}

// ── Category filters ──────────────────────────────────────────────────────────
function populateCategoryFilter() {
  // In server mode, filters are populated by _populateFiltersFromServer — this is for local mode only
  if (_browseMode === 'server') return;
  const data = window._tableData || [];
  // Brand filter — count occurrences and sort by count desc
  const brandMap = {};
  data.forEach(i => { if (i.brand) brandMap[i.brand] = (brandMap[i.brand] || 0) + 1; });
  const brandList = Object.entries(brandMap).sort((a,b) => b[1] - a[1]).map(([name, count]) => ({name, count}));
  _setBrandList(brandList);
  window._selectedBrands = [];
  _updateBrandBtn();
  // Condition filter (multi-select) — ordered best to worst
  const _condOrder = {Excellent:0,Great:1,Good:2,Fair:3,Poor:4};
  const conds = [...new Set(data.map(i => i.condition).filter(Boolean))].sort((a,b) => (_condOrder[a]??9) - (_condOrder[b]??9));
  _setCondList(conds);
  window._selectedConds = [];
  _updateCondBtn();
  // Category filter (multi-select)
  const cats = [...new Set(data.map(i => i.category).filter(Boolean))].sort();
  _setCatList(cats);
  window._selectedCats = [];
  _updateCatBtn();
  // Subcategory filter (multi-select) — start hidden
  window._selectedSubs = [];
  _updateSubcatBtn();
  _setSubList([]);
}

function onCatFilterChange() {
  if (_browseMode === 'server') {
    // In server mode, changing category resets subcategory and fetches page 1
    window._selectedSubs = []; _updateSubcatBtn();
    _srvPage = 1;
    _fetchBrowsePage(1);
    return;
  }
  const catArr = window._selectedCats || [];
  const data   = window._tableData || [];
  const subcats = [...new Set(
    data.filter(i => !catArr.length || catArr.includes(i.category || '')).map(i => i.subcategory).filter(Boolean)
  )].sort();
  if (subcats.length && catArr.length) {
    _setSubList(subcats);
  } else {
    _setSubList([]);
  }
  window._selectedSubs = [];
  _updateSubcatBtn();
  filterResults();
}

// ── Table rendering & sorting ─────────────────────────────────────────────────
// col indices: 0=status, 1=name, 2=brand, 3=price, 4=condition, 5=category, 6=subcategory, 7=date, 8=location
const _SORT_COLS = [null, 'name', 'brand', 'price', 'condition', 'category', 'subcategory', 'date', 'location'];
const PAGE_SIZE = 50;
window._localPage = 1;

function renderTable() {
  // In server mode, rendering is handled by _renderServerTable
  if (_browseMode === 'server') return;

  const allData = window._tableData || [];

  // Apply filters to get the filtered set
  const q        = document.getElementById('res-search').value.toLowerCase().trim();
  const brandArr = window._selectedBrands || [];
  const condArr  = window._selectedConds || [];
  const catArr   = window._selectedCats || [];
  const subArr   = window._selectedSubs || [];

  const filtered = allData.filter(item => {
    if (_watchFilterActive && !(window._watchlist || {})[item.id || '']) return false;
    // Text filter: all words must match (AND), or exact phrase if quoted
    if (q) {
      const text = ((item.name||'')+' '+(item.brand||'')+' '+(item.store||'')+' '+(item.location||'')+' '+(item.category||'')+' '+(item.subcategory||'')).toLowerCase();
      if (q.startsWith('"') && q.endsWith('"') && q.length > 2) {
        if (!text.includes(q.slice(1,-1))) return false;
      } else {
        const words = q.split(/\s+/).filter(Boolean);
        if (!words.every(w => text.includes(w))) return false;
      }
    }
    return (!brandArr.length || brandArr.includes(item.brand || '')) &&
           (!condArr.length  || condArr.includes(item.condition || '')) &&
           (!catArr.length   || catArr.includes(item.category || '')) &&
           (!subArr.length   || subArr.includes(item.subcategory || ''));
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  window._localPage = Math.min(window._localPage, totalPages);
  const start = (window._localPage - 1) * PAGE_SIZE;
  const pageItems = filtered.slice(start, start + PAGE_SIZE);

  let html = `<table id="res-table"><thead><tr>
    <th data-col="0"></th>
    <th data-col="kw"></th>
    <th data-col="watch"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Brand</th>
    <th data-col="3">Price</th>
    <th data-col="drop"></th>
    <th data-col="4">Condition</th>
    <th data-col="5">Category</th>
    <th data-col="6">Subcategory</th>
    <th data-col="7">Date Listed</th>
    <th data-col="8">Location</th>
  </tr></thead><tbody>`;
  pageItems.forEach(item => { html += _buildRowHtml(item); });
  html += '</tbody></table>';
  html += _buildPaginatorHtml(window._localPage, totalPages, filtered.length, PAGE_SIZE);
  document.getElementById('res-body').innerHTML = html;

  // Update filter count display
  const countEl = document.getElementById('res-search-count');
  if (q || brandArr.length || condArr.length || catArr.length || subArr.length) {
    countEl.textContent = `${filtered.length} of ${allData.length}`;
  } else {
    countEl.textContent = '';
  }
  const clearBtn = document.getElementById('clear-filters-btn');
  if (clearBtn) clearBtn.style.display = (brandArr.length || condArr.length || catArr.length || subArr.length) ? '' : 'none';

  if (window._sortCol !== null) {
    const th = document.querySelector(`#res-table th[data-col="${window._sortCol}"]`);
    if (th) th.classList.add(window._sortDir === 1 ? 'sort-asc' : 'sort-desc');
  }

  document.querySelectorAll('#res-table thead th[data-col]').forEach(th => {
    const colIdx = parseInt(th.dataset.col);
    if (!_SORT_COLS[colIdx]) return;
    th.addEventListener('click', () => sortTable(colIdx));
  });

  autoSizeItemColumn();
}

function goToPage(page) {
  if (_browseMode === 'server') {
    if (page < 1 || page > _srvTotalPages || page === _srvPage) return;
    _fetchBrowsePage(page);
    return;
  }
  // Local mode
  window._localPage = page;
  renderTable();
  document.querySelector('.results')?.scrollTo(0, 0);
}

function sortTable(colIdx) {
  const field = _SORT_COLS[colIdx];
  if (!field) return;

  if (_browseMode === 'server') {
    // Determine new direction
    const newDir = (window._sortCol === colIdx)
      ? (window._sortDir === 1 ? -1 : 1)
      : (field === 'date' ? -1 : 1);
    window._sortCol = colIdx;
    window._sortDir = newDir;
    _srvSortField = field;
    _srvSortDir = (newDir === -1) ? 'desc' : 'asc';
    _srvPage = 1;
    _fetchBrowsePage(1);
    return;
  }

  window._sortDir = (window._sortCol === colIdx) ? window._sortDir * -1 : (field === 'date' ? -1 : 1);
  window._sortCol = colIdx;
  window._localPage = 1;  // Reset pagination on sort
  const dir = window._sortDir;
  window._tableData.sort((a, b) => {
    let av = a[field] || '', bv = b[field] || '';
    if (field === 'price') {
      av = parseFloat((av+'').replace(/[^0-9.]/g,'')) || 0;
      bv = parseFloat((bv+'').replace(/[^0-9.]/g,'')) || 0;
      return (av - bv) * dir;
    }
    if (field === 'date') {
      av = a['date_raw'] || '';
      bv = b['date_raw'] || '';
      return av.toString().localeCompare(bv.toString()) * dir;
    }
    return av.toString().localeCompare(bv.toString()) * dir;
  });
  renderTable();
}

function autoSizeItemColumn() {
  // Measure the longest visible item name using a hidden canvas for accuracy
  const canvas = autoSizeItemColumn._canvas || (autoSizeItemColumn._canvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  ctx.font = '13.3px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'; // matches td font ~.83rem
  let maxW = 80;
  // In server mode, read names from DOM rows; in local mode, use _tableData
  if (_browseMode === 'server') {
    document.querySelectorAll('#res-table tbody tr').forEach(tr => {
      const name = tr.dataset.name || '';
      const w = ctx.measureText(name).width;
      if (w > maxW) maxW = w;
    });
  } else {
    const data = window._tableData || [];
    if (!data.length) return;
    data.forEach(item => {
      const w = ctx.measureText(item.name || '').width;
      if (w > maxW) maxW = w;
    });
  }
  // Add padding (link underline cursor area + 24px right padding)
  const colW = Math.min(Math.ceil(maxW) + 32, 520); // cap at 520px
  const th = document.querySelector('#res-table th[data-col="1"]');
  if (th) th.style.width = colW + 'px';
  // Also set td widths via col group or direct style on first td of each row
  document.querySelectorAll('#res-table tbody tr td:nth-child(3)').forEach(td => {
    td.style.maxWidth = colW + 'px';
  });
}

// ── Image thumbnail hover ────────────────────────────────────────────────────
(function() {
  const tooltip = document.getElementById('img-tooltip');
  const tooltipImg = tooltip.querySelector('img');
  let hoverTimer = null;
  const HOVER_DELAY = 400; // ms before showing thumbnail

  document.addEventListener('mouseenter', function(e) {
    // GC results
    const gcLink = e.target.closest('#res-body a');
    // CL results
    const clLink = e.target.closest('#cl-body a');
    const link = gcLink || clLink;
    if (!link) return;
    const row = link.closest('tr');
    if (!row) return;

    let imgUrl = '';
    if (gcLink) {
      const imageId = row.dataset.imageId;
      if (imageId) imgUrl = 'https://media.guitarcenter.com/is/image/MMGS7/' + imageId + '-00-600x600.jpg';
    } else if (clLink) {
      imgUrl = row.dataset.clImage || '';
    }
    if (!imgUrl) return;

    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function() {
      tooltipImg.src = imgUrl;
      const rect = link.getBoundingClientRect();
      let left = rect.right + 12;
      let top = rect.top - 40;
      if (left + 220 > window.innerWidth) left = rect.left - 220;
      if (top + 220 > window.innerHeight) top = window.innerHeight - 225;
      if (top < 5) top = 5;
      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
      tooltip.style.display = 'block';
    }, HOVER_DELAY);
  }, true);

  document.addEventListener('mouseleave', function(e) {
    const link = e.target.closest('#res-body a') || e.target.closest('#cl-body a');
    if (!link) return;
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
    tooltipImg.src = '';
  }, true);

  // Also hide on scroll (both panels)
  document.querySelector('.results')?.addEventListener('scroll', function() {
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
  });
  document.getElementById('cl-body')?.addEventListener('scroll', function() {
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
  });
})();

// ── Brand multi-select dropdown (with search) ───────────────────────────────
window._selectedBrands = [];
window._brandList = [];

function toggleBrandDropdown() {
  const panel = document.getElementById('brand-dd-panel');
  if (panel.style.display === 'none') {
    panel.style.display = '';
    document.getElementById('brand-dd-search').value = '';
    _renderBrandList();
    document.getElementById('brand-dd-search').focus();
    setTimeout(() => document.addEventListener('click', _closeBrandOnOutside, true), 0);
  } else {
    _closeBrandDropdown();
  }
}

function _closeBrandDropdown() {
  document.getElementById('brand-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeBrandOnOutside, true);
}

function _closeBrandOnOutside(e) {
  if (!e.target.closest('#brand-dropdown')) _closeBrandDropdown();
}

function filterBrandDropdown() { _renderBrandList(); }

function _renderBrandList() {
  const q = (document.getElementById('brand-dd-search').value || '').toLowerCase();
  const list = document.getElementById('brand-dd-list');
  let html = '';
  window._brandList.forEach(b => {
    if (q && !b.name.toLowerCase().includes(q)) return;
    const isActive = window._selectedBrands.includes(b.name);
    const esc = b.name.replace(/"/g,'&quot;');
    html += '<div class="brand-dd-item' + (isActive ? ' active' : '') + '" data-brand="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>'
         + esc + '<span class="bcount">' + b.count + '</span></div>';
  });
  list.innerHTML = html;
  list.onclick = function(e) {
    const item = e.target.closest('.brand-dd-item');
    if (!item) return;
    _toggleBrand(item.dataset.brand);
  };
}

function _toggleBrand(brand) {
  const idx = window._selectedBrands.indexOf(brand);
  if (idx >= 0) window._selectedBrands.splice(idx, 1);
  else window._selectedBrands.push(brand);
  _updateBrandBtn();
  _renderBrandList();
  filterResults();
}

function selectBrand(brand) {
  // Called from table brand-link clicks — toggle behavior
  const idx = window._selectedBrands.indexOf(brand);
  if (idx >= 0) {
    window._selectedBrands.splice(idx, 1);
  } else {
    window._selectedBrands = [brand];  // Set to just this brand
  }
  _updateBrandBtn();
  filterResults();
}

function _updateBrandBtn() {
  const btn = document.getElementById('brand-dd-btn');
  if (window._selectedBrands.length === 0) btn.textContent = 'All Brands ▾';
  else if (window._selectedBrands.length === 1) btn.textContent = window._selectedBrands[0] + ' ▾';
  else btn.textContent = window._selectedBrands.length + ' Brands ▾';
}

function _setBrandList(brands) {
  window._brandList = brands || [];
  document.getElementById('brand-dropdown').style.display = brands && brands.length ? '' : 'none';
}

// ── Condition multi-select dropdown ──────────────────────────────────────────
window._selectedConds = [];
window._condList = [];

function toggleCondDropdown() {
  const panel = document.getElementById('cond-dd-panel');
  if (panel.style.display === 'none') {
    panel.style.display = '';
    _renderCondList();
    setTimeout(() => document.addEventListener('click', _closeCondOnOutside, true), 0);
  } else {
    _closeCondDropdown();
  }
}

function _closeCondDropdown() {
  document.getElementById('cond-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeCondOnOutside, true);
}

function _closeCondOnOutside(e) {
  if (!e.target.closest('#cond-dropdown')) _closeCondDropdown();
}

function _renderCondList() {
  const panel = document.getElementById('cond-dd-panel');
  let html = '';
  window._condList.forEach(c => {
    const isActive = window._selectedConds.includes(c);
    const esc = c.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-cond="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>'
         + esc + '</div>';
  });
  panel.innerHTML = html;
  panel.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleCond(item.dataset.cond);
  };
}

function _toggleCond(cond) {
  const idx = window._selectedConds.indexOf(cond);
  if (idx >= 0) {
    window._selectedConds.splice(idx, 1);
  } else {
    window._selectedConds.push(cond);
  }
  _updateCondBtn();
  _renderCondList();
  filterResults();
}

function _updateCondBtn() {
  const btn = document.getElementById('cond-dd-btn');
  if (window._selectedConds.length === 0) {
    btn.textContent = 'All Conditions ▾';
  } else if (window._selectedConds.length === 1) {
    btn.textContent = window._selectedConds[0] + ' ▾';
  } else {
    btn.textContent = window._selectedConds.length + ' Conditions ▾';
  }
}

function _setCondList(conditions) {
  window._condList = conditions || [];
  const dd = document.getElementById('cond-dropdown');
  dd.style.display = conditions && conditions.length ? '' : 'none';
}

// ── Category multi-select dropdown ───────────────────────────────────────────
window._selectedCats = [];
window._catList = [];

function toggleCatDropdown() {
  const panel = document.getElementById('cat-dd-panel');
  if (panel.style.display === 'none') {
    panel.style.display = '';
    _renderCatList();
    setTimeout(() => document.addEventListener('click', _closeCatOnOutside, true), 0);
  } else { _closeCatDropdown(); }
}
function _closeCatDropdown() {
  document.getElementById('cat-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeCatOnOutside, true);
}
function _closeCatOnOutside(e) { if (!e.target.closest('#cat-dropdown')) _closeCatDropdown(); }

function _renderCatList() {
  const panel = document.getElementById('cat-dd-panel');
  let html = '';
  window._catList.forEach(c => {
    const isActive = window._selectedCats.includes(c);
    const esc = c.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-val="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>' + esc + '</div>';
  });
  panel.innerHTML = html;
  panel.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleCat(item.dataset.val);
  };
}
function _toggleCat(cat) {
  const idx = window._selectedCats.indexOf(cat);
  if (idx >= 0) window._selectedCats.splice(idx, 1);
  else window._selectedCats.push(cat);
  _updateCatBtn();
  _renderCatList();
  // When categories change, reset subcategories
  window._selectedSubs = [];
  _updateSubcatBtn();
  filterResults();
}
function _updateCatBtn() {
  const btn = document.getElementById('cat-dd-btn');
  if (window._selectedCats.length === 0) btn.textContent = 'All Categories ▾';
  else if (window._selectedCats.length === 1) btn.textContent = window._selectedCats[0] + ' ▾';
  else btn.textContent = window._selectedCats.length + ' Categories ▾';
}
function _setCatList(categories) {
  window._catList = categories || [];
  document.getElementById('cat-dropdown').style.display = categories && categories.length ? '' : 'none';
}

// ── Subcategory multi-select dropdown ────────────────────────────────────────
window._selectedSubs = [];
window._subList = [];

function toggleSubcatDropdown() {
  const panel = document.getElementById('subcat-dd-panel');
  if (panel.style.display === 'none') {
    panel.style.display = '';
    _renderSubList();
    setTimeout(() => document.addEventListener('click', _closeSubOnOutside, true), 0);
  } else { _closeSubDropdown(); }
}
function _closeSubDropdown() {
  document.getElementById('subcat-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeSubOnOutside, true);
}
function _closeSubOnOutside(e) { if (!e.target.closest('#subcat-dropdown')) _closeSubDropdown(); }

function _renderSubList() {
  const panel = document.getElementById('subcat-dd-panel');
  let html = '';
  window._subList.forEach(s => {
    const isActive = window._selectedSubs.includes(s);
    const esc = s.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-val="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>' + esc + '</div>';
  });
  panel.innerHTML = html;
  panel.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleSub(item.dataset.val);
  };
}
function _toggleSub(sub) {
  const idx = window._selectedSubs.indexOf(sub);
  if (idx >= 0) window._selectedSubs.splice(idx, 1);
  else window._selectedSubs.push(sub);
  _updateSubcatBtn();
  _renderSubList();
  filterResults();
}
function _updateSubcatBtn() {
  const btn = document.getElementById('subcat-dd-btn');
  if (window._selectedSubs.length === 0) btn.textContent = 'All Subcategories ▾';
  else if (window._selectedSubs.length === 1) btn.textContent = window._selectedSubs[0] + ' ▾';
  else btn.textContent = window._selectedSubs.length + ' Subcategories ▾';
}
function _setSubList(subcategories) {
  window._subList = subcategories || [];
  document.getElementById('subcat-dropdown').style.display = subcategories && subcategories.length ? '' : 'none';
}

// ── Results filter ────────────────────────────────────────────────────────────
let _filterTimer = null;

function clearFilters() {
  window._selectedBrands = []; _updateBrandBtn();
  window._selectedConds = []; _updateCondBtn();
  window._selectedCats = []; _updateCatBtn();
  window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
  document.getElementById('clear-filters-btn').style.display = 'none';
  // Also turn off watch filter if active
  if (_watchFilterActive) {
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
  }
  filterResults();
}

function filterResults() {
  if (_browseMode === 'server') {
    // Debounce text input, fire immediately for dropdowns
    clearTimeout(_filterTimer);
    _filterTimer = setTimeout(() => {
      _srvPage = 1;
      _fetchBrowsePage(1);
    }, 250);
    return;
  }
  window._localPage = 1;  // Reset pagination on filter change
  renderTable();
}

// ── Populate store data (one-time migration) ──────────────────────────────────
async function populateStoreData() {
  if (running) { appendLog('Stop the current run first.', 'log-err'); return; }
  const selected = getSelected();
  const btn = document.getElementById('populate-store-btn');
  const resp = await fetch('/api/populate-store-data', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stores: selected})
  });
  if (!resp.ok) {
    const e = await resp.json();
    appendLog('Error: ' + e.error, 'log-err');
    return;
  }
  running = true; updateCount();
  btn.textContent = '⏳ Populating…';
  btn.disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  document.getElementById('stop-btn').disabled = false;
  document.getElementById('log').innerHTML = '';
  const label = selected.length ? selected.length + ' store(s)' : 'all stores';
  appendLog('Tagging cached items for ' + label + ' with store names. You can stop at any time.');
  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      document.getElementById('stop-btn').style.display = 'none';
      btn.textContent = '✓ Store Data Populated';
      btn.disabled = false;
      updateCount();
    }
  };
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
  document.getElementById('pw-modal').style.display = 'flex';
  document.getElementById('pw-input').value = '';
  document.getElementById('pw-err').style.display = 'none';
  setTimeout(() => document.getElementById('pw-input').focus(), 50);
}

async function doReset(pw) {
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
  // Clear per-user tracking state (but keep want list, watchlist, favorites)
  window._prevSnapshot = new Set();
  _lsSet('prev_snapshot', []);
  window._newIds = new Set();
  _lsSet('new_ids', []);
  window._lastRunISO = null;
  _lsSet('last_run', null);
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'none';
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

function clRenderCities(selectAll) {
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
    if (selectAll) cb.checked = true;
    cb.addEventListener('change', function() { _updateMobileToggleCounts(); clFilterResults(); });
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
  clFilterResults();  // Also filter results to show only favorites
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
  _updateMobileToggleCounts();
  clFilterResults();
}
function clClearAll() {
  document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = false);
  _updateMobileToggleCounts();
  clFilterResults();
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
    if (!r.ok) {
      const text = await r.text();
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed (HTTP ' + r.status + '). Try selecting fewer cities.</div>';
      return;
    }
    let d;
    try {
      d = await r.json();
    } catch(parseErr) {
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed — server returned an invalid response. This can happen if the request timed out. Try selecting fewer cities.</div>';
      return;
    }
    if (d.error) {
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">' + d.error + '</div>';
      return;
    }
    _clData = d.results || [];
    // Filter results: all words must match (AND), or exact phrase if quoted
    const rawQ = q.trim();
    if (rawQ) {
      let matchFn;
      if (rawQ.startsWith('"') && rawQ.endsWith('"') && rawQ.length > 2) {
        // Exact phrase match
        const phrase = rawQ.slice(1, -1).toLowerCase();
        matchFn = item => (item.title || '').toLowerCase().includes(phrase);
      } else {
        // All words must be present (AND)
        const words = rawQ.toLowerCase().split(/\s+/).filter(Boolean);
        matchFn = item => {
          const t = (item.title || '').toLowerCase();
          return words.every(w => t.includes(w));
        };
      }
      _clData = _clData.filter(matchFn);
    }
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
  const selectedCities = new Set(clGetSelected());
  const rows = document.querySelectorAll('#cl-body tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const textMatch = !q || row.textContent.toLowerCase().includes(q);
    const favMatch = !_clFavsOnly || _clFavs.includes(row.dataset.city || '');
    const cityMatch = selectedCities.size === 0 || selectedCities.has(row.dataset.city || '');
    const watchMatch = !_clWatchFilterActive || !!(window._clWatchlist || {})[row.dataset.clId || ''];
    const wantMatch = !_clWantListFilterActive || _clMatchesWantList(row.querySelector('td:nth-child(3)') ? row.querySelector('td:nth-child(3)').textContent : '');
    const show = textMatch && favMatch && cityMatch && watchMatch && wantMatch;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('cl-count').textContent =
    (q || _clFavsOnly || _clWatchFilterActive || _clWantListFilterActive || selectedCities.size < _clData.length) ? (visible + ' of ' + _clData.length + ' listings') : (_clData.length + ' listings');
}

function _clMatchesWantList(title) {
  if (!window._keywords || !window._keywords.length) return false;
  const text = (title || '').toLowerCase();
  return window._keywords.some(kw => {
    kw = kw.trim();
    if (kw.startsWith('"') && kw.endsWith('"') && kw.length > 2) {
      return text.includes(kw.slice(1, -1).toLowerCase());
    } else if (kw.includes(',')) {
      return kw.split(',').map(t => t.trim().toLowerCase()).filter(Boolean).every(t => text.includes(t));
    } else {
      return text.includes(kw.toLowerCase());
    }
  });
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
  const labels = ['','Want','Item','Price','Location','Date'];
  let html = '<table><thead><tr>';
  labels.forEach((l, i) => {
    if (i === 0) { html += '<th style="width:30px"></th>'; return; }
    if (i === 1) { html += '<th style="width:62px;text-align:center">Want</th>'; return; }
    const sortIdx = i - 2;
    const cls = _clSortCol === sortIdx ? (_clSortDir === 1 ? 'sort-asc' : 'sort-desc') : '';
    html += '<th class="' + cls + '" onclick="clSort(' + sortIdx + ')">' + l + '</th>';
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
    const clId  = 'cl:' + (r.url || r.title || '');
    const isWatched = (window._clWatchlist || {})[clId];
    const watchStar = `<button class="watch-btn ${isWatched ? 'active' : ''}" onclick="clToggleWatch('${clId.replace(/'/g,"\\'")}','${(r.title||'').replace(/'/g,"\\'")}','${(r.url||'').replace(/'/g,"\\'")}','${(r.price||'').replace(/'/g,"\\'")}','${(r.location||'').replace(/'/g,"\\'")}',this)" title="${isWatched ? 'Remove from' : 'Add to'} watch list">${isWatched ? '★' : '☆'}</button>`;
    const wantMatch = _clMatchesWantList(r.title || '');
    const title = r.url
      ? star + '<a href="' + r.url + '" target="_blank" rel="noopener">' + (r.title || '(no title)') + '</a>'
      : star + (r.title || '(no title)');
    html += '<tr class="' + (isFav ? 'cl-fav-result' : '') + '" data-city="' + (r.cityId||'') + '" data-cl-id="' + clId.replace(/"/g,'&quot;') + '" data-cl-image="' + (r.image||'').replace(/"/g,'&quot;') + '">' +
            '<td style="text-align:center">' + watchStar + '</td>' +
            '<td style="text-align:center">' + (wantMatch ? '<span class="tag-kw">WANT</span>' : '') + '</td>' +
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
    _clSortCol = null; _clSortDir = 1;
  } else if (_clSortCol === col) {
    _clSortDir *= -1;
  } else {
    _clSortCol = col; _clSortDir = 1;
  }
  clRenderResults();
}

let _clWatchFilterActive = false;
let _clWantListFilterActive = false;

function clSearchWantList() {
  if (_clWantListFilterActive) {
    _clWantListFilterActive = false;
    document.getElementById('cl-search-wl-link').textContent = 'Search Want List';
    document.getElementById('cl-search-wl-link').style.color = '#4ade80';
    clFilterResults();
    return;
  }
  if (!window._keywords || !window._keywords.length) {
    openKeywords();
    return;
  }
  _clWantListFilterActive = true;
  document.getElementById('cl-search-wl-link').textContent = 'Clear Want List Search';
  document.getElementById('cl-search-wl-link').style.color = '#f88';
  clFilterResults();
}

function clToggleWatchFilter() {
  _clWatchFilterActive = !_clWatchFilterActive;
  const btn = document.getElementById('cl-watchlist-toggle');
  btn.classList.toggle('wl-active', _clWatchFilterActive);
  clFilterResults();
}

function clToggleWatch(id, name, url, price, location, btn) {
  const isWatched = !!(window._clWatchlist[id]);
  if (isWatched) {
    delete window._clWatchlist[id];
  } else {
    window._clWatchlist[id] = {
      name: name, url: url, store: location, price: price,
      date_added: new Date().toISOString().slice(0,10),
    };
  }
  _lsSet('cl_watchlist', window._clWatchlist);
  btn.classList.toggle('active', !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
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
        _q.put({"type": "done", "scanned": 0, "all_ids": [],
                "items": [], "baseline": False, "stopped": False,
                "update_success": success})
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started"})


# ── Nightly scheduled scan ────────────────────────────────────────────────────

def _nightly_scan():
    """Run a full baseline scan to keep the shared inventory cache fresh.
    Runs in background — does NOT affect any user's seen_ids (those are in localStorage)."""
    import time as _time
    while True:
        try:
            # Calculate seconds until next 3:00 AM Eastern (UTC-5 / UTC-4 DST)
            from datetime import datetime, timedelta
            import calendar
            now_utc = datetime.utcnow()
            # 3AM Eastern = 8AM UTC (EST) or 7AM UTC (EDT)
            # Use 8AM UTC as safe default (3AM EST / 4AM EDT)
            target_hour_utc = 8
            target = now_utc.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
            if target <= now_utc:
                target += timedelta(days=1)
            wait_secs = (target - now_utc).total_seconds()
            print(f"  Nightly scan scheduled in {wait_secs/3600:.1f} hours ({target.strftime('%Y-%m-%d %H:%M')} UTC)")
            _time.sleep(wait_secs)

            # Run the scan if no other scan is in progress
            if _lock.acquire(blocking=False):
                try:
                    print("  🌙 Starting nightly inventory scan…")
                    _stop_event.clear()
                    _load_cat_cache()

                    page = 1
                    total = 0
                    while page <= 1000:
                        if _stop_event.is_set():
                            break
                        try:
                            data = fetch_page(None, page)
                        except Exception:
                            break
                        products = parse_products(data, None)
                        if not products:
                            break
                        for p in products:
                            sku = p["id"]
                            _cat_cache[sku] = {
                                "category":    p.get("category", ""),
                                "subcategory": p.get("subcategory", ""),
                                "condition":   p.get("condition", ""),
                                "brand":       p.get("brand", ""),
                                "name":        p.get("name", ""),
                                "url":         p.get("url", ""),
                                "store":       p.get("store", ""),
                                "location":    p.get("location") or p.get("store", ""),
                                "price":       p.get("price") or 0,
                                "available":   True,
                                "date_listed": p.get("date_listed") or _cat_cache.get(sku, {}).get("date_listed", ""),
                                "image_id":    p.get("image_id") or _cat_cache.get(sku, {}).get("image_id", ""),
                            }
                            total += 1
                        try:
                            nb_pages = data.get("results", [{}])[0].get("nbPages", 1)
                            if page >= nb_pages:
                                break
                        except Exception:
                            break
                        page += 1
                    _save_cat_cache()
                    # Record scan completion time so clients know when last check happened
                    scan_time = datetime.utcnow().isoformat() + "Z"
                    (DATA_DIR / "gc_last_scan.txt").write_text(scan_time)
                    print(f"  🌙 Nightly scan complete — {total:,} items updated.")
                finally:
                    _lock.release()
            else:
                print("  🌙 Nightly scan skipped — another scan is in progress.")
        except Exception as e:
            print(f"  🌙 Nightly scan error: {e}")
            _time.sleep(3600)  # Retry in an hour


if __name__ == "__main__":
    _load_cat_cache()
    _load_cookies()
    if not STORES_CACHE.exists():
        print("Building store list…")
        refresh_store_list()

    # Nightly scan removed — "Check for New" is manual only

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
