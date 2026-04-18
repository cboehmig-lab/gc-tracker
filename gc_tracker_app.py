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
WATCHLIST_FILE   = DATA_DIR / "gc_watchlist.json"
KEYWORDS_FILE    = DATA_DIR / "gc_keywords.json"
STORE_COORDS_FILE = DATA_DIR / "gc_store_coords.json"


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
    return [name for name, _ in _fetch_state_stores_with_state(state)]


def _fetch_state_stores_with_state(state: str) -> list[tuple]:
    """Like _fetch_state_stores but returns (name, state_abbr) tuples."""
    try:
        r = _http.get(f"https://stores.guitarcenter.com/{state}/", timeout=10)
        if r.status_code != 200:
            return []
        html = r.text
        # Only trust URLs in the form /state/city-slug/numeric-id
        slug_to_name = {}
        for slug in re.findall(
            rf'href="/{re.escape(state)}/([a-z][a-z0-9\-]+)/(\d+)(?:/[^"]*)?"',
            html
        ):
            city_slug, store_id = slug
            name = " ".join(w.capitalize() for w in city_slug.split("-"))
            slug_to_name[city_slug] = name
        return [(name, state.upper()) for name in slug_to_name.values()]
    except Exception:
        return []


def _build_store_coords(send_progress=None, force: bool = False):
    """Geocode all known stores using Algolia's 'storeName' field as the query.

    For each store, we first pull ONE hit from Algolia to discover the human-readable
    storeName (e.g. 'South Austin, TX' — a real neighborhood Nominatim recognises),
    then geocode that string directly via Nominatim. This is vastly more reliable than
    querying 'Guitar Center {store}' because GC stores aren't POIs in Nominatim's DB,
    but their storeName strings are real geographic places.

    Stores with zero live used inventory (e.g. closed stores still in the cache) can't
    be resolved via Algolia and are reported as 'no items'. They fall back to a
    last-ditch '{store}, {state}' Nominatim query using state context from GC's state
    pages, so permanently-closed stores still have SOME chance of getting coords.

    If force=True, re-geocodes all stores even if already in the coords file.
    Writes gc_store_coords.json. Returns {store: {lat, lng, source}}.
    """
    def _send(msg):
        if send_progress:
            send_progress(msg)

    known_stores = get_store_list()
    total = len(known_stores)
    _send(f"Step 1: Found {total} stores in cache.")

    # Load existing coords so we can skip already-geocoded stores (unless force=True).
    existing: dict = {}
    if STORE_COORDS_FILE.exists():
        try:
            existing = json.loads(STORE_COORDS_FILE.read_text())
        except Exception:
            pass
    coords: dict = dict(existing) if not force else {}

    # Load pre-seeded coords from CSV-derived seed file (committed alongside the app).
    # These use real ZIP-code centroids so they're more accurate than city geocoding.
    # Seed entries are used as-is and skip the Algolia+Nominatim pipeline entirely.
    seed_file = Path(__file__).parent / "gc_store_coords_seed.json"
    if seed_file.exists():
        try:
            seed = json.loads(seed_file.read_text())
            loaded = 0
            for store, data in seed.items():
                if force or store not in coords:
                    coords[store] = data
                    loaded += 1
            _send(f"  Loaded {loaded} pre-seeded coords from gc_store_coords_seed.json.")
        except Exception as e:
            _send(f"  Warning: could not load seed file: {e}")

    # Collect state context as a last-ditch fallback for dead stores
    # (stores with no Algolia items — can't determine storeName from the API).
    _send("Step 2: Scraping state pages for fallback state context…")
    name_to_state: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_state_stores_with_state, st): st for st in _US_STATES}
        for future in as_completed(futures):
            try:
                for name, state in future.result():
                    if name not in name_to_state:
                        name_to_state[name] = state
            except Exception:
                pass

    # Step 3: Hit Algolia once per store to get storeName.
    # Algolia has no rate limit for this volume — ~235 req completes in ~30 sec.
    _send("Step 3: Fetching storeName from Algolia for each active store…")
    store_to_location: dict[str, str] = {}
    no_items: list[str] = []
    # Skip stores already resolved (existing file OR seed). Seed always wins —
    # it comes from real street addresses so we never need to re-geocode it.
    todo = [s for s in known_stores if s not in coords]
    for i, store in enumerate(todo, 1):
        try:
            data = fetch_page(store, 1)
            hits = data.get("results", [{}])[0].get("hits", [])
            if not hits:
                no_items.append(store)
                continue
            sn = (hits[0].get("storeName") or "").strip()
            if sn:
                store_to_location[store] = sn
            else:
                no_items.append(store)
        except Exception as e:
            _send(f"  Algolia error for {store}: {type(e).__name__}")
            no_items.append(store)
        if i % 50 == 0:
            _send(f"  [{i}/{len(todo)}] {len(store_to_location)} storeNames, {len(no_items)} no-items so far…")

    _send(f"  Got storeName for {len(store_to_location)} stores; {len(no_items)} had no items.")
    if no_items:
        sample = ", ".join(no_items[:15])
        more = f" (+{len(no_items)-15} more)" if len(no_items) > 15 else ""
        _send(f"  No-items stores (likely closed): {sample}{more}")

    # Step 4: Geocode each storeName via Nominatim (1 req/sec per ToS).
    # Fresh session with clean API headers — NOT the shared _http session which
    # carries browser-impersonation headers that Nominatim rejects. (v2.1.3 fix)
    nom_session = http.Session()
    nom_session.headers.update({
        "User-Agent": "GCTracker/2.2 (personal tool; non-commercial)",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })

    def _nom(query: str):
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?q={http.utils.quote(query)}&format=json&limit=1&countrycodes=us"
        )
        r = nom_session.get(url, timeout=10)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        return (data[0] if data else None), None

    _send(f"Step 4: Geocoding {len(store_to_location)} storeNames via Nominatim (1/sec)…")
    failed: list[str] = []
    succeeded = 0

    # Primary pass: stores with storeName from Algolia.
    loc_items = sorted(store_to_location.items())
    for i, (store, location) in enumerate(loc_items, 1):
        try:
            result, err = _nom(location)
            if not result:
                # Fallback: strip "at <venue>" suffix from storeName.
                # e.g. "Yonkers at Ridge Hill, NY" → "Yonkers, NY"
                # Handles GC stores named after the shopping center they're in.
                stripped = re.sub(r'\s+at\s+[^,]+', '', location, flags=re.IGNORECASE).strip()
                if stripped != location:
                    time.sleep(1.0)
                    result, err = _nom(stripped)
                    if result:
                        location = stripped  # record the simpler query as source
            if result:
                coords[store] = {
                    "lat": float(result["lat"]),
                    "lng": float(result["lon"]),
                    "source": location,
                }
                succeeded += 1
            else:
                failed.append(f"{store} (storeName={store_to_location[store]}{', '+err if err else ''})")
        except Exception as e:
            failed.append(f"{store} ({type(e).__name__})")

        if i % 25 == 0:
            _send(f"  [{i}/{len(loc_items)}] {succeeded} geocoded so far…")
            STORE_COORDS_FILE.write_text(json.dumps(coords, indent=2))

        time.sleep(1.0)

    # Last-ditch pass: no-items stores, try "{store}, {state}" with state context.
    # These are likely closed, but we give them one shot so the coords file is complete.
    dead_attempted = 0
    dead_ok = 0
    for store in no_items:
        state = name_to_state.get(store, "")
        if not state:
            continue  # nothing we can do without state
        dead_attempted += 1
        query = f"{store}, {state}"
        try:
            result, _err = _nom(query)
            if result:
                coords[store] = {
                    "lat": float(result["lat"]),
                    "lng": float(result["lon"]),
                    "source": f"fallback-no-items: {query}",
                }
                dead_ok += 1
        except Exception:
            pass
        time.sleep(1.0)

    if dead_attempted:
        _send(f"  Last-ditch no-items pass: {dead_ok}/{dead_attempted} resolved via state context.")

    STORE_COORDS_FILE.write_text(json.dumps(coords, indent=2))
    skipped = total - len(todo)
    _send(f"\n✓ Done — {succeeded + dead_ok} newly geocoded, {skipped} skipped (cached), "
          f"{len(no_items)} no-items ({dead_ok} recovered), {len(failed)} failed. "
          f"Total coords: {len(coords)}/{total}.")
    if failed:
        _send(f"  Failed: {', '.join(failed[:20])}{'…' if len(failed)>20 else ''}")
    return coords


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
                price_raw = hit.get("price") or 0
                list_price_raw = hit.get("listPrice") or 0
                # Fall back to listPrice if price is absent (listPrice is the original/regular price)
                if not price_raw and list_price_raw:
                    price_raw = list_price_raw
                try:    price = float(price_raw) if price_raw else None
                except: price = None
                try:    list_price = float(list_price_raw) if list_price_raw else 0.0
                except: list_price = 0.0
                has_price_drop = bool(hit.get("priceDrop", False))
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
                # Date listed from startDate (seconds timestamp) — when
                # the item was published to the storefront.  Falls back to
                # creationDate (milliseconds) if startDate is missing.
                start_ts   = hit.get("startDate") or 0
                creation_ts = hit.get("creationDate") or 0
                try:
                    if start_ts:
                        date_str = datetime.utcfromtimestamp(float(start_ts)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    elif creation_ts:
                        date_str = datetime.utcfromtimestamp(float(creation_ts) / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        date_str = ""
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
                    "id":             sku,
                    "name":           name,
                    "brand":          brand,
                    "price":          price,
                    "list_price":     list_price,
                    "has_price_drop": has_price_drop,
                    "store":          store,
                    "location":       location,
                    "url":            url,
                    "condition":      condition,
                    "category":       category,
                    "subcategory":    subcategory,
                    "date_listed":    date_str,
                    "image_id":       image_id,
                })
        except Exception:
            pass
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
app.secret_key  = os.environ.get("SECRET_KEY", "gc-tracker-default-key-change-me")
_q              = queue.Queue()        # legacy fallback (kept for non-run endpoints)
_run_queues: dict[str, queue.Queue] = {}   # per-run queues keyed by run_id
_run_queues_lock = threading.Lock()
_lock           = threading.Lock()
_stop_event     = threading.Event()

import uuid as _uuid

# ── Device access tracking ─────────────────────────────────────────────────────
_DEVICE_LOG       = DATA_DIR / "gc_device_log.jsonl"
_device_log_lock  = threading.Lock()
_seen_today: set  = set()   # (device_id, date) pairs already written today

def _log_device(device_id: str):
    """Append one line to gc_device_log.jsonl the first time a device is seen each day."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    key   = (device_id, today)
    if key in _seen_today:
        return
    _seen_today.add(key)
    entry = json.dumps({
        "date":       today,
        "time":       datetime.utcnow().strftime("%H:%M:%SZ"),
        "device_id":  device_id,
        "ua":         request.headers.get("User-Agent", "")[:120],
        "ip":         request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
    })
    with _device_log_lock:
        with open(_DEVICE_LOG, "a") as f:
            f.write(entry + "\n")

@app.after_request
def _track_device(response):
    """Set a long-lived device cookie and log first visit of each day."""
    # Only track page/API hits we care about (skip SSE streams & static)
    if request.path.startswith("/api/progress"):
        return response
    device_id = request.cookies.get("gt_device_id")
    if not device_id:
        device_id = str(_uuid.uuid4())
        # 2-year cookie — survives browser restarts
        response.set_cookie("gt_device_id", device_id,
                            max_age=60*60*24*730, httponly=True, samesite="Lax")
    _log_device(device_id)
    return response

def _create_run_queue() -> tuple[str, queue.Queue]:
    """Create a new per-run message queue and return (run_id, queue)."""
    run_id = _uuid.uuid4().hex[:12]
    q = queue.Queue()
    with _run_queues_lock:
        _run_queues[run_id] = q
    return run_id, q

def _get_run_queue(run_id: str) -> queue.Queue | None:
    with _run_queues_lock:
        return _run_queues.get(run_id)

def _cleanup_run_queue(run_id: str):
    with _run_queues_lock:
        _run_queues.pop(run_id, None)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Site access is open — no login required.
        # Individual sensitive endpoints (e.g. /api/reset) enforce their own password.
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

@app.route("/admin/devices")
def admin_devices():
    """Password-protected device access summary page."""
    pw = request.args.get("pw", "")
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if pw != admin_pw:
        return Response(
            '<html><body style="background:#111;color:#eee;font-family:monospace;padding:40px">'
            '<h2>🔒 Access denied</h2>'
            '<form><input name="pw" type="password" placeholder="Password" autofocus '
            'style="padding:8px;background:#222;border:1px solid #444;color:#eee;border-radius:4px">'
            '<button type="submit" style="padding:8px 16px;background:#c00;color:#fff;border:none;'
            'border-radius:4px;cursor:pointer;margin-left:8px">Enter</button></form>'
            '</body></html>', 401, {"Content-Type": "text/html"}
        )

    # Parse log
    entries = []
    if _DEVICE_LOG.exists():
        for line in _DEVICE_LOG.read_text().splitlines():
            line = line.strip()
            if line:
                try: entries.append(json.loads(line))
                except: pass

    # Aggregate
    from collections import defaultdict
    unique_devices  = {e["device_id"] for e in entries}
    by_device       = defaultdict(list)
    by_date         = defaultdict(set)
    for e in entries:
        by_device[e["device_id"]].append(e)
        by_date[e["date"]].add(e["device_id"])

    rows = []
    for did, evts in sorted(by_device.items(), key=lambda x: x[1][-1]["date"], reverse=True):
        last  = evts[-1]
        first = evts[0]
        ua    = last.get("ua", "")
        # Guess platform
        if "iPhone" in ua or "iPad" in ua:    platform = "📱 iOS"
        elif "Android" in ua:                  platform = "📱 Android"
        elif "Macintosh" in ua:                platform = "💻 Mac"
        elif "Windows" in ua:                  platform = "🖥 Windows"
        elif "Linux" in ua:                    platform = "🖥 Linux"
        else:                                  platform = "❓ Unknown"
        rows.append({
            "id":       did[:8] + "…",
            "platform": platform,
            "first":    first["date"],
            "last":     last["date"] + " " + last["time"],
            "days":     len(evts),
            "ip":       last.get("ip", ""),
        })

    # Daily active table
    daily = sorted(by_date.items(), reverse=True)[:30]

    html  = ['<!DOCTYPE html><html><head><meta charset="UTF-8">']
    html += ['<title>Device Log</title>']
    html += ['<style>body{background:#111;color:#ddd;font-family:monospace;padding:24px;font-size:.88rem}']
    html += ['h1{color:#fff;margin-bottom:4px}h2{color:#aaa;font-size:1rem;margin:24px 0 8px}']
    html += ['table{border-collapse:collapse;width:100%;max-width:900px}']
    html += ['th{background:#1e1e1e;padding:8px 12px;text-align:left;border-bottom:2px solid #333;color:#aaa}']
    html += ['td{padding:6px 12px;border-bottom:1px solid #222}tr:hover td{background:#1a1a1a}']
    html += ['</style></head><body>']
    html += [f'<h1>📊 Device Tracker</h1>']
    html += [f'<p style="color:#666">{len(unique_devices)} unique devices &nbsp;·&nbsp; {len(entries)} total day-visits &nbsp;·&nbsp; {len(entries) and entries[-1]["date"]} last activity</p>']

    html += ['<h2>All Devices</h2><table>']
    html += ['<tr><th>ID</th><th>Platform</th><th>First seen</th><th>Last seen</th><th>Days active</th><th>IP</th></tr>']
    for r in rows:
        html += [f'<tr><td>{r["id"]}</td><td>{r["platform"]}</td><td>{r["first"]}</td>'
                 f'<td>{r["last"]}</td><td>{r["days"]}</td><td>{r["ip"]}</td></tr>']
    html += ['</table>']

    html += ['<h2>Daily Active Devices (last 30 days)</h2><table>']
    html += ['<tr><th>Date</th><th>Unique devices</th></tr>']
    for date, devs in daily:
        html += [f'<tr><td>{date}</td><td>{len(devs)}</td></tr>']
    html += ['</table></body></html>']

    return Response("".join(html), content_type="text/html")


def _admin_task_page(title: str, api_path: str, description: str, pw: str,
                     options_html: str = "", extra_body_js: str = "") -> str:
    """Shared HTML template for long-running admin task pages (build-coords, validate-stores).

    options_html: optional HTML snippet inserted above the Run button (e.g. checkboxes)
    extra_body_js: optional JS snippet merged into the POST body object (e.g. "force: document.getElementById('force-cb').checked")
    """
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if pw != admin_pw:
        return None  # caller should return 401
    safe_api  = api_path.replace('"', '')
    safe_title = title.replace('<', '').replace('>', '')
    safe_desc  = description.replace('<', '').replace('>', '')
    safe_pw    = admin_pw.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
    body_extra = f", {extra_body_js}" if extra_body_js else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{safe_title} — GC Tracker Admin</title>
<style>
  body{{background:#111;color:#ddd;font-family:monospace;padding:40px;max-width:800px}}
  h2{{color:#eee;margin-bottom:4px}} p{{color:#888;margin-top:0}}
  button{{padding:8px 20px;background:#c00;color:#fff;border:none;border-radius:5px;
          font-size:1rem;cursor:pointer;margin-top:16px}}
  button:disabled{{background:#555;cursor:default}}
  #log{{margin-top:20px;background:#1a1a1a;border:1px solid #333;border-radius:6px;
        padding:16px;min-height:120px;white-space:pre-wrap;font-size:.82rem;line-height:1.5}}
  .done{{color:#4ade80}} .err{{color:#f88}}
</style></head><body>
<h2>🛠 {safe_title}</h2>
<p>{safe_desc}</p>
{options_html}
<button id="run-btn" onclick="run()">▶ Run Now</button>
<div id="log">Waiting…</div>
<script>
async function run() {{
  const btn = document.getElementById('run-btn');
  const log = document.getElementById('log');
  btn.disabled = true; btn.textContent = '⏳ Running…';
  log.textContent = 'Starting…\\n';
  const resp = await fetch('{safe_api}', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pw: '{safe_pw}'{body_extra}}})
  }});
  if (!resp.ok) {{
    const e = await resp.json().catch(()=>({{}}));
    log.textContent += '❌ Error: ' + (e.error || resp.statusText) + '\\n';
    btn.disabled = false; btn.textContent = '▶ Run Now';
    return;
  }}
  const es = new EventSource('/api/progress');
  es.onmessage = e => {{
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') {{ log.textContent += (msg.msg || '') + '\\n'; log.scrollTop = log.scrollHeight; return; }}
    if (msg.type === 'done') {{
      es.close();
      log.innerHTML += '<span class="done">\\n✓ Done.</span>';
      btn.disabled = false; btn.textContent = '▶ Run Again';
    }}
  }};
  es.onerror = () => {{ es.close(); log.innerHTML += '<span class="err">\\nConnection lost.</span>'; btn.disabled = false; btn.textContent = '▶ Run Now'; }};
}}
</script></body></html>"""


@app.route("/admin/build-coords")
def admin_build_coords():
    """Admin page to geocode all stores and build gc_store_coords.json."""
    pw = request.args.get("pw", "")
    html = _admin_task_page(
        title="Build Store Coordinates",
        api_path="/api/build-store-coords",
        description="Pulls 'storeName' (e.g. 'South Austin, TX') from Algolia for each "
                    "active store, then geocodes that string via Nominatim (~1 req/sec). "
                    "Takes ~5 min. Skips stores already in gc_store_coords.json unless "
                    "'Force re-geocode all' is checked.",
        pw=pw,
        options_html='<label style="display:block;margin-top:14px;color:#bbb;cursor:pointer">'
                     '<input type="checkbox" id="force-cb" style="vertical-align:middle"> '
                     'Force re-geocode all stores (even cached ones)</label>',
        extra_body_js="force: document.getElementById('force-cb').checked",
    )
    if html is None:
        return Response("Unauthorized", status=401)
    return Response(html, content_type="text/html")


@app.route("/admin/validate-stores")
def admin_validate_stores():
    """Admin page to validate and clean up the store list."""
    pw = request.args.get("pw", "")
    html = _admin_task_page(
        title="Validate Stores",
        api_path="/api/validate-stores",
        description="Checks every store for 404s, auto-removes dead stores, "
                    "renames any whose slugs changed, then rebuilds the store list from GC live data. "
                    "Takes ~0.5s per store.",
        pw=pw,
    )
    if html is None:
        return Response("Unauthorized", status=401)
    return Response(html, content_type="text/html")


@app.route("/admin/clear-lock")
def admin_clear_lock():
    """Force-release the global scan lock if it's stuck after a crash.
    Protected by the same RESET_PASSWORD as /admin/devices."""
    pw = request.args.get("pw", "")
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if pw != admin_pw:
        return Response("Unauthorized", status=401)
    if _lock.locked():
        try:
            _lock.release()
            return Response("✓ Lock cleared — scans can now run.", content_type="text/plain")
        except RuntimeError:
            return Response("Lock was already free (release failed).", content_type="text/plain")
    return Response("Lock was not held — nothing to clear.", content_type="text/plain")


@app.route("/admin/listing-patterns")
def admin_listing_patterns():
    """Analyze date_listed distribution across the cached inventory to reveal
    how GC batches new listings — by day, hour-of-day, and minute within hour.
    Protected by the same RESET_PASSWORD as /admin/devices."""
    pw = request.args.get("pw", "")
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if pw != admin_pw:
        return Response("Unauthorized", status=401)

    _load_cat_cache()
    from collections import Counter

    dates, hours, minutes, exact_times, items_no_date = [], [], [], [], 0
    for sku, item in _cat_cache.items():
        dl = item.get("date_listed", "")
        if not dl:
            items_no_date += 1
            continue
        exact_times.append(dl)
        # "2026-04-15T14:23:00Z"
        try:
            date_part  = dl[:10]          # "2026-04-15"
            hour_part  = int(dl[11:13])   # 14
            minute_part= int(dl[14:16])   # 23
            dates.append(date_part)
            hours.append(hour_part)
            minutes.append(minute_part)
        except Exception:
            pass

    total = len(exact_times) + items_no_date
    by_date  = Counter(dates).most_common(60)
    by_hour  = sorted(Counter(hours).items())
    by_minute= sorted(Counter(minutes).items())

    # Look for clustering: what fraction of items land on the exact :00 second?
    on_zero_second = sum(1 for t in exact_times if t.endswith("T00:00:00Z") or t[17:19] == "00")
    on_midnight    = sum(1 for t in exact_times if t[11:19] == "00:00:00")

    # Sample of 40 most recent timestamps (sorted desc)
    recent = sorted(exact_times, reverse=True)[:40]

    S = '<style>body{font-family:monospace;background:#111;color:#ddd;padding:24px;max-width:900px}' \
        'h2{color:#f5c518}table{border-collapse:collapse;width:100%}' \
        'td,th{border:1px solid #333;padding:6px 10px;text-align:right}' \
        'th{background:#222;text-align:center}td:first-child{text-align:left}' \
        '.bar{display:inline-block;background:#c00;height:12px;vertical-align:middle}' \
        '.note{color:#888;font-size:.85em;margin:8px 0}</style>'

    def bar(n, mx):
        w = int(n / mx * 200) if mx else 0
        return f'<span class="bar" style="width:{w}px"></span> {n:,}'

    html = [f'<html><head><title>GC Listing Patterns</title>{S}</head><body>']
    html.append(f'<h2>GC Listing Pattern Analysis</h2>')
    html.append(f'<p class="note">Total items in cache: <b>{total:,}</b> &nbsp;|&nbsp; '
                f'With date_listed: <b>{len(exact_times):,}</b> &nbsp;|&nbsp; '
                f'Missing date: <b>{items_no_date:,}</b></p>')
    html.append(f'<p class="note">Items landing at exactly midnight UTC: <b>{on_midnight:,}</b> '
                f'({on_midnight/len(exact_times)*100:.1f}%)</p>')
    html.append(f'<p class="note">Items with :00 seconds: <b>{on_zero_second:,}</b> '
                f'({on_zero_second/len(exact_times)*100:.1f}%) — '
                f'(high % = timestamps truncated to the minute, not exact)</p>')

    # By hour of day
    mx_h = max(c for _,c in by_hour) if by_hour else 1
    html.append('<h2>Items by Hour of Day (UTC)</h2><table><tr><th>Hour (UTC)</th><th>Count</th><th>Distribution</th></tr>')
    for h, c in by_hour:
        html.append(f'<tr><td>{h:02d}:00</td><td>{c:,}</td><td>{bar(c, mx_h)}</td></tr>')
    html.append('</table>')

    # By minute within the hour
    mx_m = max(c for _,c in by_minute) if by_minute else 1
    html.append('<h2>Items by Minute Within Hour</h2>'
                '<p class="note">Spikes at :00 or other specific minutes = batch publishing</p>'
                '<table><tr><th>Minute</th><th>Count</th><th>Distribution</th></tr>')
    for m, c in by_minute:
        html.append(f'<tr><td>:{m:02d}</td><td>{c:,}</td><td>{bar(c, mx_m)}</td></tr>')
    html.append('</table>')

    # By date (most recent first)
    mx_d = max(c for _,c in by_date) if by_date else 1
    html.append('<h2>Items by Date Listed (top 60)</h2><table><tr><th>Date</th><th>Count</th><th>Distribution</th></tr>')
    for d, c in sorted(by_date, reverse=True):
        html.append(f'<tr><td>{d}</td><td>{c:,}</td><td>{bar(c, mx_d)}</td></tr>')
    html.append('</table>')

    # 40 most recent timestamps raw
    html.append('<h2>40 Most Recent date_listed Values</h2>'
                '<p class="note">Look for identical timestamps (batch) vs spread-out (item-by-item)</p>'
                '<table><tr><th>Timestamp (UTC)</th></tr>')
    for t in recent:
        html.append(f'<tr><td>{t}</td></tr>')
    html.append('</table>')

    html.append('</body></html>')
    return Response("".join(html), content_type="text/html")


@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():
    """Delete inventory state and cache to start fresh.
    Preserves favorites, watchlist, and want list."""
    data = request.json or {}
    reset_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if data.get("password") != reset_pw:
        return jsonify({"error": "Incorrect password."}), 403
    deleted = []
    for f in [STATE_FILE, CAT_CACHE_FILE, OUTPUT_FILE,
              DATA_DIR / "gc_last_scan.txt",
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

    # Per-user filtering: only show items first_seen ≤ this user's last scan time
    user_last_scan = (data.get("user_last_scan") or "").strip()

    # Filter params — all dropdowns are multi-select arrays
    fq       = (data.get("filter_q") or "").lower().strip()
    f_brands = data.get("filter_brands") or []
    f_conds  = data.get("filter_conditions") or []
    f_cats   = data.get("filter_categories") or []
    f_subs   = data.get("filter_subcategories") or []
    f_watched = bool(data.get("filter_watched"))
    f_want_only = bool(data.get("filter_want_list_only"))
    f_price_drop_only = bool(data.get("filter_price_drop_only"))
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
        # Per-user browse gating: hide items first seen after this user's last scan.
        # Ensures each device only sees inventory that existed when IT scanned —
        # items added to cache by another device's newer scan stay hidden here.
        if user_last_scan:
            first_seen = cached.get("first_seen", "")
            if first_seen and first_seen > user_last_scan:
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

        pd_amt   = cached.get("price_drop", 0) or 0
        lp_raw   = cached.get("list_price", 0) or 0
        pd_since = cached.get("price_drop_since", "") or ""
        all_items.append({
            "id":               sku,
            "name":             name,
            "brand":            brand,
            "price":            f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":        price_raw,
            "list_price_raw":   lp_raw,
            "price_drop":       pd_amt,
            "price_drop_since": pd_since,
            "store":            store,
            "location":         location,
            "url":              cached.get("url", ""),
            "category":         category,
            "subcategory":      subcategory,
            "condition":        condition,
            "date":             _fmt_date(date_raw),
            "date_raw":         date_raw,
            "image_id":         cached.get("image_id", ""),
            "watched":          sku in wl_ids,
            "isNew":            sku in new_ids,
            "kwMatch":          kw_hit,
            "isFav":            store in fav_stores if fav_stores else False,
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
    if f_price_drop_only:
        filtered = [i for i in filtered if i.get("price_drop", 0) > 0]
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
    elif sort_field == "price_drop_since":
        filtered.sort(key=lambda x: x.get("price_drop_since") or "", reverse=reverse)
    else:
        filtered.sort(key=lambda x: (x.get(sort_field) or "").lower(), reverse=reverse)

    # Only apply NEW-on-top tier for the default (non-user-clicked) sort
    # Three tiers: new+want-list match → new only → everything else
    if not user_sorted:
        def _priority(x):
            if x.get("isNew") and x.get("kwMatch"): return 0
            if x.get("isNew"):                       return 1
            return 2
        filtered.sort(key=_priority)

    total_filtered = len(filtered)
    total_pages    = max(1, -(-total_filtered // per_page))  # ceil division
    page           = min(page, total_pages)
    start          = (page - 1) * per_page
    page_items     = filtered[start:start + per_page]

    # Count items that are both want-list matches AND new (for the status notification)
    new_want_count = sum(1 for i in filtered if i.get("isNew") and i.get("kwMatch"))
    # Unique stores in filtered result set
    store_count = len(set(i.get("store", "") for i in filtered if i.get("store")))

    return jsonify({
        "items":            page_items,
        "page":             page,
        "per_page":         per_page,
        "total_count":      total_filtered,
        "total_unfiltered": total_unfiltered,
        "total_pages":      total_pages,
        "store_count":      store_count,
        "new_count":        new_count_unfiltered,
        "new_want_count":   new_want_count,
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
        # Pull latest price drop info from live cache if available
        live = _cat_cache.get(sku, {})
        pd_amt   = live.get("price_drop", 0) or w.get("price_drop", 0) or 0
        lp_raw   = live.get("list_price", 0) or w.get("list_price", 0) or 0
        pd_since = live.get("price_drop_since", "") or w.get("price_drop_since", "") or ""
        items.append({
            "id":               sku,
            "name":             w.get("name", ""),
            "brand":            w.get("brand", ""),
            "price":            f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":        price_raw,
            "list_price_raw":   lp_raw,
            "price_drop":       pd_amt,
            "price_drop_since": pd_since,
            "store":            w.get("store", ""),
            "location":         w.get("location") or w.get("store", ""),
            "url":              w.get("url", ""),
            "category":         w.get("category", ""),
            "subcategory":      w.get("subcategory", ""),
            "condition":        w.get("condition", ""),
            "date":             _fmt_date(w.get("date_listed") or item_dates.get(sku, w.get("date_added",""))),
            "date_raw":         w.get("date_listed") or item_dates.get(sku, w.get("date_added","")),
            "image_id":         w.get("image_id", ""),
            "isNew":            False,
            "watched":          True,
            "sold":             w.get("sold", False),
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
    # Device's own last-run timestamp (ISO string from localStorage) — used so
    # each device gets its own NEW window rather than sharing gc_last_scan.txt
    device_last_run = (data.get("device_last_run") or "").strip()
    # Empty stores = nationwide scan (used by both baseline and Check for New)
    # Create a per-run queue so each client gets its own message stream
    run_id, run_q = _create_run_queue()
    # Also mirror to legacy _q for any other endpoints that read it
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_run, args=(selected, baseline, run_id, device_last_run), daemon=True)
    t.start()
    return jsonify({"status": "started", "run_id": run_id})

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



@app.route("/api/export-data")
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
    title_only = request.args.get("title_only", "").lower() in ("1", "true", "yes")
    try:
        results = _cl_search(q, cities or None, title_only=title_only)
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

def _cl_slugify(text: str) -> str:
    """Convert a title to a CL-style URL slug for matching.
    E.g. 'Fender Telecaster 2019 MIM' → 'fender-telecaster-2019-mim'"""
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')

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

    # Build a slug→URL lookup for title-based matching (replaces fragile position-based matching).
    # CL post URLs contain a slugified title: /msa/d/fender-telecaster-2019/1234567890.html
    # We extract the slug and match it against JSON-LD item names.
    _slug_to_urls: dict[str, list[str]] = {}  # slug → [url, ...] (multiple posts can have similar slugs)
    for u in post_urls_ordered:
        try:
            # Extract slug from URL path: /section/d/SLUG/ID.html
            path_parts = u.split('/')
            d_idx = path_parts.index('d') if 'd' in path_parts else -1
            if d_idx >= 0 and d_idx + 1 < len(path_parts):
                slug = path_parts[d_idx + 1]
                if slug not in _slug_to_urls:
                    _slug_to_urls[slug] = []
                _slug_to_urls[slug].append(u)
        except Exception:
            pass

    def _match_url_by_title(name: str) -> str:
        """Find the best matching URL for a JSON-LD item name by comparing title slugs."""
        name_slug = _cl_slugify(name)
        if not name_slug:
            return ""
        name_words = set(name_slug.split('-'))
        best_url = ""
        best_score = 0
        for slug, urls in _slug_to_urls.items():
            if not urls:
                continue
            slug_words = set(slug.split('-'))
            # Score = number of overlapping words (Jaccard-like)
            overlap = len(name_words & slug_words)
            # Require at least 2 word matches to avoid false positives
            if overlap > best_score and overlap >= 2:
                best_score = overlap
                best_url = urls[0]
        # If we found a match, remove the URL from the pool so it can't be reused
        if best_url:
            for slug, urls in _slug_to_urls.items():
                if best_url in urls:
                    urls.remove(best_url)
                    break
        return best_url

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
            # Prefer URL directly from JSON-LD (ListItem.url or item.url/sameAs)
            # — these are authoritative and immune to index-mismatch bugs.
            # Fall back to title-slug matching when JSON-LD omits the URL.
            url = (entry.get("url") or item.get("url") or item.get("sameAs") or "").strip()
            if not url:
                url = _match_url_by_title(name)
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


def _cl_search(query: str, cities: list = None, title_only: bool = False) -> list[dict]:
    """Search Craigslist musical instruments across US cities.
    If title_only=True, adds srchType=T to restrict matches to listing titles."""
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
            srch_param = "&srchType=T" if title_only else ""
            url = (f"https://{city_id}.craigslist.org/search/msa"
                   f"?query={http.utils.quote(query)}&sort=date{srch_param}")
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
        results = _cl_parse_html(html, city)
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

@app.route("/api/cl-debug")
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

@app.route("/api/debug-condition")
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



@app.route("/api/validate-stores", methods=["POST"])
@login_required
def api_validate_stores():
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if request.json.get("pw") != admin_pw:
        return jsonify({"error": "Unauthorized"}), 401
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_validate_stores, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/store-coords")
@login_required
def api_store_coords():
    """Return cached store coordinates JSON (built by /api/build-store-coords)."""
    if STORE_COORDS_FILE.exists():
        try:
            return jsonify(json.loads(STORE_COORDS_FILE.read_text()))
        except Exception:
            pass
    return jsonify({})

@app.route("/api/build-store-coords", methods=["POST"])
@login_required
def api_build_store_coords():
    """Trigger a one-time geocoding run to build gc_store_coords.json.
    Uses the existing SSE stream — progress shows up in the log panel."""
    admin_pw = os.environ.get("RESET_PASSWORD", "Beatle909!")
    if (request.json or {}).get("pw") != admin_pw:
        return jsonify({"error": "Unauthorized"}), 401
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break

    force = bool((request.json or {}).get("force", False))

    def _run():
        try:
            def _send(msg):
                _q.put({"type": "progress", "msg": msg})
            _build_store_coords(_send, force=force)
            _q.put({"type": "done", "baseline": False, "stopped": False,
                    "new_ids": [], "items": []})
        except Exception as e:
            _q.put({"type": "progress", "msg": f"Error: {e}"})
            _q.put({"type": "done", "baseline": False, "stopped": True,
                    "new_ids": [], "items": []})
        finally:
            try: _lock.release()
            except Exception: pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/fill-gaps", methods=["POST"])
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
    run_id = request.args.get("run_id", "")
    run_q = _get_run_queue(run_id) if run_id else None
    # Fall back to legacy global queue for non-run endpoints (populate, validate, etc.)
    q = run_q or _q
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "done":
                    if run_id:
                        _cleanup_run_queue(run_id)
                    break
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



def _run(selected_stores: list[str], baseline: bool, run_id: str = "", device_last_run: str = ""):
    run_q = _get_run_queue(run_id) if run_id else None
    def send(msg):
        if run_q:
            run_q.put(msg)
        _q.put(msg)  # also send to legacy queue for backwards compat
    try:
        run_time   = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

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

        # (cache-ID snapshot removed — NEW detection now uses startDate timestamps)

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
            # Price drop detection — use Algolia's native priceDrop flag + listPrice field
            new_price       = p.get("price") or 0
            new_list_price  = p.get("list_price") or 0
            has_price_drop  = bool(p.get("has_price_drop", False))
            price_drop_amt  = round(new_list_price - new_price, 2) if (has_price_drop and new_list_price > new_price) else 0
            # Track when we FIRST detected this drop (preserves timestamp across scans)
            prev_had_drop   = bool(cached.get("has_price_drop", False))
            if has_price_drop and not prev_had_drop:
                price_drop_since = run_time          # newly dropped this scan
            elif has_price_drop:
                price_drop_since = cached.get("price_drop_since", run_time)  # preserve
            else:
                price_drop_since = ""                # no longer dropped
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
                "list_price":        new_list_price,
                "has_price_drop":    has_price_drop,
                "price_drop":        price_drop_amt,
                "price_drop_since":  price_drop_since,
                "available":         True,
                "date_listed":       p.get("date_listed") or cached.get("date_listed", ""),
                "image_id":          p.get("image_id") or cached.get("image_id", ""),
                # first_seen: when our system first encountered this item
                "first_seen":        cached.get("first_seen", run_time),
            }
            p["category"]    = cat
            p["subcategory"] = subcat
            p["condition"]   = condition
            p["brand"]       = brand
            p["location"]    = location
            p["price_drop"]  = price_drop_amt
            p["list_price"]  = new_list_price

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
        # Read global last-scan time (fallback when device has no history)
        last_scan_file = DATA_DIR / "gc_last_scan.txt"
        global_prev_scan = last_scan_file.read_text().strip() if last_scan_file.exists() else ""
        # Per-device prev_scan: prefer the device's own last-run timestamp sent
        # from localStorage. Falls back to the global scan time so first-time
        # devices on an existing server don't see the entire catalog as NEW.
        prev_scan_time = device_last_run or global_prev_scan
        # Record this scan's completion time globally (for devices with no history)
        last_scan_file.write_text(run_time)

        def fmt(p):
            date_src = p.get("date_listed") or _cat_cache.get(p["id"], {}).get("date_listed", "")
            lp = p.get("list_price") or 0
            return {
                "id":               p["id"],
                "name":             p["name"],
                "brand":            p.get("brand", ""),
                "price":            f"${p['price']:,.2f}" if p["price"] else "",
                "price_raw":        p.get("price") or 0,
                "list_price_raw":   lp,
                "price_drop":       p.get("price_drop", 0),
                "price_drop_since": _cat_cache.get(p["id"], {}).get("price_drop_since", ""),
                "store":            p["store"],
                "location":         p.get("location") or p.get("store", ""),
                "url":              p["url"],
                "category":         p.get("category", ""),
                "subcategory":      p.get("subcategory", ""),
                "condition":        p.get("condition", ""),
                "date":             _fmt_date(date_src),
                "date_raw":         date_src,
                "image_id":         p.get("image_id") or _cat_cache.get(p["id"], {}).get("image_id", ""),
            }

        # ── Per-device new-item detection ─────────────────────────────────────
        # Simple rule: was this item listed after the user's previous scan?
        #   date_listed > prev_scan_time  →  NEW
        #   date_listed ≤ prev_scan_time  →  not new
        # date_listed comes from Algolia's startDate (Unix seconds) or
        # creationDate (Unix ms) for the specific used-item record — both reflect
        # when that used listing was created, not a product catalog date.
        # prev_scan_time is this device's last scan timestamp from localStorage,
        # so each device has its own independent NEW window.
        # Both sides are YYYY-MM-DDTHH:MM:SSZ UTC so string comparison is valid.
        new_ids_list = []
        if not baseline and prev_scan_time:
            for p in all_products:
                item_date = p.get("date_listed") or _cat_cache.get(p["id"], {}).get("date_listed", "")
                if item_date and item_date > prev_scan_time:
                    new_ids_list.append(p["id"])
        send({"type":"progress","msg":f"  {len(new_ids_list):,} new items since last scan."})

        # For large scans, don't send full item lists via SSE — client will use server-side browse
        large_scan = len(all_products) > 1000
        items_for_sse = [] if large_scan else [fmt(p) for p in all_products[:500]]
        send({
            "type":       "done",
            "baseline":   baseline,
            "stopped":    _stop_event.is_set(),
            "scanned":    len(all_products),
            "new_ids":    new_ids_list,
            "scan_time":  run_time,
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
#search:focus{border-color:#c00;box-shadow:0 0 0 3px rgba(204,0,0,.15)}

#store-list{flex:1;overflow-y:auto;padding:4px 0}
.store-row{display:flex;align-items:center;padding:6px 12px;gap:8px;cursor:pointer}
.store-row:hover{background:#222}
.store-row input[type=checkbox]{accent-color:#c00;flex-shrink:0;cursor:pointer}
.store-row label{flex:1;font-size:.855rem;cursor:pointer}
.store-row.hidden{display:none}
.store-dist{font-size:.72rem;color:#555;flex-shrink:0;min-width:44px;text-align:right}
.store-dist-inline{font-size:.72rem;color:#aaa;font-style:italic;font-weight:400}
.fav-btn{background:none;border:none;cursor:pointer;font-size:1rem;line-height:1;padding:0 4px;color:#444;flex-shrink:0;transition:color .15s}
.fav-btn.active{color:#f5c518}
.fav-btn:hover{color:#f5c518}

/* ── ZIP sort row ── */
.zip-sort-row{display:flex;align-items:center;gap:6px;margin-top:7px}
#zip-input{width:80px;flex:none;padding:5px 9px;border-radius:5px;background:#252525;border:1px solid #3a3a3a;color:#eee;font-size:.82rem;outline:none}
#zip-input:focus{border-color:#555;box-shadow:0 0 0 2px rgba(255,255,255,.05)}
#zip-input::placeholder{color:#555}
#zip-sort-btn{padding:5px 9px;background:#222;border:1px solid #3a3a3a;border-radius:5px;color:#888;font-size:.78rem;cursor:pointer;white-space:nowrap;transition:all .15s}
#zip-sort-btn.active{background:#1a2a1a;border-color:#3a6a3a;color:#7bc97b}
#zip-sort-btn:hover{border-color:#555;color:#bbb}

.empty-msg{padding:24px 16px;color:#555;font-size:.85rem;text-align:center}

.left-footer{padding:12px;border-top:1px solid #2e2e2e;flex-shrink:0;background:#1a1a1a;position:relative;z-index:2}
#sel-count{font-size:.78rem;color:#666;margin-bottom:8px}

/* ── Right panel ── */
.right{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative;z-index:1}

.status-bar{padding:8px 20px;background:#161616;border-bottom:1px solid #2e2e2e;font-size:.78rem;color:#666;display:flex;gap:20px;flex-wrap:wrap;flex-shrink:0}
.status-bar b{color:#bbb}
#global-search-wrap{margin-left:auto;display:flex;align-items:center;gap:4px;flex-shrink:0}
#global-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;width:200px;outline:none}
#global-search:focus{border-color:#c00;box-shadow:0 0 0 3px rgba(204,0,0,.15)}
#global-search-btn{background:none;border:1px solid #3a3a3a;border-radius:4px;color:#888;font-size:.72rem;padding:4px 8px;cursor:pointer;line-height:1}
#global-search-btn:hover{border-color:#c00;color:#eee}
#global-search-clear{background:none;border:1px solid #c00;border-radius:4px;color:#f88;font-size:.72rem;padding:4px 8px;cursor:pointer;line-height:1}
#global-search-clear:hover{background:#3a1a1a}

#log{height:52px;overflow-y:auto;padding:6px 20px;font-family:monospace;font-size:.78rem;color:#6dba8d;line-height:1.75;flex-shrink:0;border-bottom:1px solid #2e2e2e}
.log-dim{color:#6dba8d}
.log-err{color:#f88}

.results{flex:1;overflow-y:auto}
.results-hdr{padding:8px 16px;font-size:.88rem;font-weight:600;color:#ccc;background:#111;position:sticky;top:0;z-index:1;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:8px;flex-wrap:wrap;box-shadow:0 2px 10px rgba(0,0,0,.5)}
.badge{background:#c00;color:#fff;font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:10px}.badge:empty{display:none}
.cat-sel{padding:5px 8px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.78rem;outline:none;cursor:pointer}
.cat-sel:focus{border-color:#c00}
#watchlist-toggle.wl-active,#cl-watchlist-toggle.wl-active,
#price-drop-toggle.wl-active,#want-list-toggle.wl-active{background:#2d6a2d;border-color:#4ade80;color:#fff}
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
#res-search:focus{border-color:#c00;box-shadow:0 0 0 3px rgba(204,0,0,.15)}
#res-search-count{font-size:.75rem;color:#555;white-space:nowrap}

table{width:100%;border-collapse:collapse;font-size:.83rem;table-layout:fixed}
th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 10px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:40px;cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
th:hover{color:#ccc}
th.sort-asc::after{content:" ▲";color:#c00;font-size:.6rem}
th.sort-desc::after{content:" ▼";color:#c00;font-size:.6rem}
td{padding:7px 10px;border-bottom:1px solid #1c1c1c;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td:nth-child(1){width:48px;text-align:center;overflow:visible}
td:nth-child(2){width:54px;text-align:center;overflow:visible}
td:nth-child(3){width:30px;text-align:center}
/* col 4 = Item: no explicit width → gets all remaining space; autoSizeItemColumn caps at 520px */
td:nth-child(5),td:nth-child(6),td:nth-child(7),td:nth-child(8),td:nth-child(9),td:nth-child(10),td:nth-child(11){width:max(65px, calc((80% - 132px) / 7))}
th:nth-child(1){width:48px}
th:nth-child(2){width:54px}
th:nth-child(3){width:30px}
th:nth-child(5),th:nth-child(6),th:nth-child(7),th:nth-child(8),th:nth-child(9),th:nth-child(10),th:nth-child(11){width:max(65px, calc((80% - 132px) / 7))}
table.no-new th:nth-child(1),table.no-new td:nth-child(1){display:none}
table.no-want th:nth-child(2),table.no-want td:nth-child(2){display:none}
tr:hover td{background:#1d1d1d}
td a{color:#7bbff7;text-decoration:none}
td a:hover{color:#a8d4ff;text-decoration:underline}
.tag{background:#c00;color:#fff;font-size:.64rem;font-weight:700;padding:2px 7px;border-radius:10px;letter-spacing:.2px}
.tag-kw{background:#0a5c2a;color:#4ade80;font-size:.64rem;font-weight:700;padding:2px 7px;border-radius:10px;border:1px solid #2d6a2d;letter-spacing:.2px}
.price-drop-val{color:#4ade80;cursor:default}
.price-orig{color:#888;text-decoration:line-through;font-size:.85em;margin-right:2px}
.tag-sold{background:#3a1a1a;color:#f87171;font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;border:1px solid #6a2d2d}
.watch-btn{background:none;border:none;cursor:pointer;color:#444;font-size:1rem;line-height:1;padding:0 2px;transition:color .15s;flex-shrink:0}
.watch-btn:hover{color:#f5c518}
.watch-btn.active{color:#f5c518}
tr.sold-row td{color:#666}
tr.sold-row td a{color:#666}
tr.fav-row td:last-child{color:#4ade80}
.no-res{padding:24px 20px;color:#555;font-size:.85rem}

/* ── Paginator ── */
.paginator{display:flex;align-items:center;justify-content:center;gap:2px;padding:14px 16px;border-top:1px solid #1e1e1e;user-select:none;position:sticky;bottom:0;background:#111;z-index:5}
.paginator .pg-info{font-size:.75rem;color:#555;margin-right:12px;white-space:nowrap}
.paginator button{background:none;border:1px solid transparent;color:#888;font-size:.78rem;min-width:32px;height:30px;border-radius:5px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;padding:0 6px;transition:all .15s;font-weight:500}
.paginator button:hover:not(:disabled):not(.pg-active){background:#1e1e1e;border-color:#333;color:#ddd}
.paginator button:disabled{color:#333;cursor:default}
.paginator button.pg-active{background:#c00;border-color:#c00;color:#fff;font-weight:700}
.paginator button.pg-nav{font-size:.72rem;color:#666;letter-spacing:-.5px}
.paginator button.pg-nav:hover:not(:disabled){color:#ccc;background:#1e1e1e;border-color:#333}
.paginator .pg-ellipsis{color:#444;font-size:.75rem;min-width:24px;text-align:center;line-height:30px}

/* ── Thin custom scrollbars ── */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#3e3e3e}

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
#cl-city-search:focus{border-color:#a5b4fc;box-shadow:0 0 0 3px rgba(165,180,252,.15)}
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
#cl-query:focus{border-color:#a5b4fc;box-shadow:0 0 0 3px rgba(165,180,252,.15)}
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
.mobile-filter-toggle{display:none}
.filter-active-dot{display:none}
/* On desktop the filter-collapsible wrapper is invisible to layout so its
   children flow directly in the parent flex row (same as before the wrapper existed) */
.filter-collapsible{display:contents}

/* ══════════════════════════════════════════════════════════════════════════════
   MOBILE RESPONSIVE — all changes scoped inside @media so desktop is untouched
   ══════════════════════════════════════════════════════════════════════════════ */
@media(max-width:820px){

  /* ── Base font bump + iOS tap highlight removal ── */
  body{font-size:1rem;overflow:hidden}
  a,button,input,label,.store-row,.cl-city-row,.app-tab,.mobile-sidebar-toggle,.mobile-filter-toggle,.paginator button{-webkit-tap-highlight-color:transparent}

  /* ── Mobile sidebar toggle button — compact ── */
  .mobile-sidebar-toggle{display:flex;align-items:center;gap:8px;padding:11px 16px;background:linear-gradient(180deg,#1e1e1e,#1a1a1a);border:none;border-bottom:1px solid #2e2e2e;cursor:pointer;font-size:.9rem;color:#ccc;font-weight:600;width:100%;text-align:left;flex-shrink:0;letter-spacing:.1px}
  .mobile-sidebar-toggle:active{background:#252525}
  .mobile-sidebar-toggle .toggle-arrow{transition:transform .2s;font-size:.65rem;color:#888}
  .mobile-sidebar-toggle .toggle-arrow.open{transform:rotate(90deg)}
  .mobile-sidebar-toggle .toggle-count{margin-left:auto;font-size:.75rem;color:#888;font-weight:400}

  /* ── Header: HIDDEN on mobile to save space (stop btn still works via JS) ── */
  header{display:none!important}

  /* ── Tabs: compact ── */
  .app-tabs{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .app-tabs::-webkit-scrollbar{display:none}
  .app-tab{padding:10px 14px;font-size:.84rem;white-space:nowrap;flex-shrink:0}

  /* ── GC Layout: stack vertically ── */
  /* CRITICAL: proper flex height chain so #res-body / #cl-body can scroll.
     Every ancestor must constrain height: flex:1 + min-height:0 + overflow:hidden */
  .layout{flex-direction:column;overflow:hidden;flex:1;min-height:0}
  .app-panel.active{display:flex;flex-direction:column;overflow:hidden}

  /* ── GC Left sidebar: collapsible on mobile ── */
  .left{width:100%;min-width:0;max-height:none;border-right:none;border-bottom:1px solid #2e2e2e;overflow:hidden;flex-shrink:0}
  .left.collapsed .search-wrap,
  .left.collapsed #store-list,
  .left.collapsed .left-footer{display:none}
  #store-list{max-height:220px;overflow-y:auto}
  .store-row{padding:10px 14px;gap:10px}
  .store-row label{font-size:.95rem}
  .left-footer{padding:10px 12px}
  #sel-count{font-size:.82rem}
  #reset-btn{font-size:.78rem}

  /* ── GC Right panel: flex column, results is the ONLY scroller ── */
  .right{overflow:hidden;flex:1;min-height:0;display:flex;flex-direction:column}

  /* ── Status bar: single compact row ── */
  .status-bar{flex-direction:row;flex-wrap:wrap;gap:6px 14px;padding:6px 12px;align-items:center;font-size:.78rem;flex-shrink:0}
  .status-bar b{color:#ccc}
  /* Hide Items and Stores counts on mobile */
  .status-bar > span:nth-child(2),
  .status-bar > span:nth-child(3){display:none}
  #global-search-wrap{margin-left:0;flex:1 1 100%;min-width:0;display:flex;align-items:center;gap:6px}
  #global-search{flex:1;min-width:0;font-size:.88rem;padding:9px 16px;border-radius:22px;background:#1e1e1e}
  #global-search-btn{font-size:.82rem;padding:7px 12px;flex-shrink:0;border-radius:8px}
  #global-search-clear{font-size:.82rem;padding:7px 12px;flex-shrink:0;border-radius:8px}
  #s-want-match{font-size:.78rem!important}

  /* ── Hide Download Excel on mobile ── */
  #s-excel{display:none!important}

  /* ── Log: minimal ── */
  #log{padding:4px 12px;height:auto;min-height:28px;max-height:40px;font-size:.75rem;line-height:1.5;flex-shrink:0}

  /* ── Results header / filter toolbar: compact ── */
  .results-hdr{padding:6px 10px;gap:5px;flex-wrap:wrap;align-items:center;position:relative;top:auto;z-index:auto;flex-shrink:0;border-bottom:1px solid #2e2e2e}
  .results-hdr > *{flex-shrink:0}
  #res-title{font-size:.88rem}
  .badge{font-size:.72rem;padding:2px 7px}
  #res-search-wrap{margin-left:0;width:100%}
  #res-search{width:100%;flex:1;font-size:.84rem;padding:8px 14px;border-radius:20px}
  #res-search-count{font-size:.78rem}
  .cat-sel{font-size:.78rem;padding:6px 10px}
  #search-wl-link{font-size:.78rem}
  #clear-filters-btn{font-size:.78rem}

  /* ── Mobile filter toggle button ── */
  .mobile-filter-toggle{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;background:#1e1e1e;border:1px solid #3a3a3a;border-radius:4px;cursor:pointer;font-size:.78rem;color:#aaa;font-weight:600;margin-left:auto;white-space:nowrap}
  .mobile-filter-toggle:active{background:#252525}
  .mobile-filter-toggle .toggle-arrow{transition:transform .2s;font-size:.6rem;color:#666}
  .mobile-filter-toggle .toggle-arrow.open{transform:rotate(90deg)}
  .filter-active-dot{display:none;width:6px;height:6px;background:#c00;border-radius:50%;flex-shrink:0}
  .filter-active-dot.visible{display:inline-block}

  /* ── Collapsible filter body ── */
  .filter-collapsible{display:flex;flex-wrap:wrap;gap:5px;width:100%;align-items:center}
  .filter-collapsible.collapsed{display:none}

  /* ── Filter dropdown panels: full-width overlay on mobile ── */
  #brand-dropdown,#cond-dropdown,#cat-dropdown,#subcat-dropdown{position:static}
  #brand-dd-panel,#cond-dd-panel,#cat-dd-panel,#subcat-dd-panel{position:fixed!important;left:8px!important;right:8px!important;top:auto!important;bottom:8px!important;width:auto!important;max-height:50vh!important;z-index:200!important;border-radius:12px!important;margin-top:0!important}
  .brand-dd-item{padding:10px 14px;font-size:.9rem}
  .brand-dd-item .bcount{font-size:.8rem}
  .cond-dd-item{padding:10px 14px;font-size:.9rem}

  /* ── GC Table: .results is a flex column; only #res-body scrolls.
     JS sets display:block via inline style — override to flex when visible ── */
  #res-panel:not([style*="none"]){display:flex!important;flex-direction:column!important}
  .results{overflow:hidden;flex:1;min-height:0}
  #res-body{overflow:auto;-webkit-overflow-scrolling:touch;flex:1;min-height:0}
  table{min-width:920px;table-layout:auto;border-collapse:separate;border-spacing:0}
  th{background:#161616;font-size:.78rem;padding:8px 8px;position:sticky;top:0;z-index:10;border-bottom:2px solid #2e2e2e}
  td{padding:8px 8px;font-size:.86rem;border-bottom:1px solid #1e1e1e}
  td:nth-child(1){width:42px}
  td:nth-child(2){width:52px}
  td:nth-child(3){width:30px}
  td:nth-child(4){width:auto;min-width:200px;white-space:normal}
  td:nth-child(5),td:nth-child(6),td:nth-child(7),td:nth-child(8),td:nth-child(9),td:nth-child(10),td:nth-child(11),td:nth-child(12){width:auto;min-width:85px}
  th:nth-child(1){width:42px}
  th:nth-child(2){width:52px}
  th:nth-child(3){width:30px}
  th:nth-child(4){width:auto;min-width:200px}
  th:nth-child(5),th:nth-child(6),th:nth-child(7),th:nth-child(8),th:nth-child(9),th:nth-child(10),th:nth-child(11),th:nth-child(12){width:auto;min-width:85px}
  .tag{font-size:.72rem;padding:2px 6px}
  .tag-kw{font-size:.72rem;padding:2px 6px}
  .tag-sold{font-size:.7rem;padding:3px 6px}
  td a{font-size:.88rem}
  .no-res{font-size:.92rem;padding:28px 20px}

  /* ── Paginator ── */
  .paginator{padding:10px 8px;gap:3px;flex-wrap:wrap;justify-content:center}
  .paginator button{min-width:40px;height:40px;font-size:.88rem;border-radius:8px}
  .paginator .pg-info{font-size:.8rem;margin-right:8px;width:100%;text-align:center;margin-bottom:4px}

  /* ── CL Layout: stack vertically ── */
  #cl-panel{flex-direction:column;overflow:hidden}
  .cl-left{width:100%;min-width:0;border-right:none;border-bottom:1px solid #2e2e2e;overflow:hidden;flex-shrink:0}
  .cl-left.collapsed .search-wrap,
  .cl-left.collapsed #cl-city-list{display:none}
  #cl-city-list{max-height:220px;overflow-y:auto}
  .cl-city-row{padding:10px 14px;gap:10px}
  .cl-city-row label{font-size:.95rem}

  /* ── CL Right: flex column, only #cl-body scrolls ── */
  .cl-right{flex:1;overflow:hidden;min-height:0;display:flex;flex-direction:column}
  .cl-search-bar{padding:8px 12px;gap:8px;flex-wrap:wrap;flex-shrink:0}
  #cl-query{width:100%;flex:1 1 100%;font-size:.92rem;padding:8px 12px}
  #cl-search-btn{flex:1;font-size:.88rem;padding:8px 16px}
  #cl-status{width:100%;text-align:center;font-size:.82rem}

  /* ── CL Table: #cl-body is the sole scroller ── */
  #cl-body{overflow:auto;-webkit-overflow-scrolling:touch;flex:1;min-height:0}
  #cl-body table{min-width:600px;table-layout:auto;border-collapse:separate;border-spacing:0}
  #cl-body th{font-size:.78rem;padding:8px 8px;background:#161616;position:sticky;top:0;z-index:10;border-bottom:2px solid #2e2e2e}
  #cl-body td{padding:8px 8px;font-size:.86rem;border-bottom:1px solid #1e1e1e}
  #cl-body td:nth-child(3){white-space:normal;min-width:200px}

  /* ── CL results header ── */
  .cl-results-hdr{flex-wrap:wrap;gap:6px;padding:6px 12px;flex-shrink:0}
  #cl-count{font-size:.85rem}
  #cl-res-search{width:100%;margin-left:0;font-size:.82rem;padding:6px 10px}
  #cl-search-wl-link{font-size:.78rem}

  /* ── Modals: full-width on mobile ── */
  #pw-box{width:calc(100% - 32px)!important;max-width:380px}
  #kw-modal > div:last-child{width:calc(100% - 32px)!important;max-width:420px}
  #first-run-modal > div:nth-child(2){width:calc(100% - 32px)!important;max-width:400px}
  #vs-modal > div:nth-child(2){width:calc(100% - 32px)!important;max-width:380px}

  /* ── Image tooltip: disabled on mobile ── */
  #img-tooltip{display:none!important}

  /* ── Touch-friendly sizing ── */
  input[type=checkbox]{width:20px;height:20px}
  #search{font-size:.95rem;padding:9px 12px}
  #cl-city-search{font-size:.95rem;padding:9px 12px}
  .sel-btn,.cl-sel-btn{padding:9px 8px;font-size:.84rem;min-height:38px}
  .watch-btn,.fav-btn,.cl-fav-btn{font-size:1.2rem;padding:4px 6px;min-width:36px;min-height:36px;display:inline-flex;align-items:center;justify-content:center}
  button,a{-webkit-tap-highlight-color:transparent}

  /* ── Alternating row stripes for readability ── */
  tr:nth-child(even) td{background:rgba(255,255,255,.02)}
  #cl-body tr:nth-child(even) td{background:rgba(255,255,255,.02)}
}

/* ── Extra small screens (phones in portrait) ── */
@media(max-width:480px){
  header{padding:10px 12px}
  header h1{font-size:1rem}
  .app-tab{padding:10px 12px;font-size:.82rem}
  .status-bar{font-size:.82rem}
  table{min-width:800px}
  #cl-body table{min-width:540px}
  .results-hdr{gap:6px;padding:8px 10px}
  .cat-sel{font-size:.8rem;padding:7px 10px}
  .paginator button{min-width:28px;height:30px;font-size:.78rem}
}

/* ── Mobile card & list views (only active on mobile) ── */
.view-toggle-btn{display:none}
@media(max-width:820px){
  .view-toggle-btn{display:inline-flex;align-items:center;justify-content:center;width:26px;height:22px;padding:0;background:#1e1e1e;border:1px solid #3a3a3a;border-radius:4px;cursor:pointer;font-size:.9rem;color:#aaa;line-height:1;vertical-align:middle;margin-left:2px;flex-shrink:0}
  .view-toggle-btn:active{background:#252525}
  .view-toggle-btn.active{border-color:#c00;color:#ff6666}

  /* ── Card grid ── */
  .card-grid{display:flex;flex-direction:column;gap:0}
  .item-card{display:flex;align-items:stretch;gap:0;padding:10px 12px;border-bottom:1px solid #1e1e1e;background:#111;cursor:default;-webkit-tap-highlight-color:transparent}
  .item-card:active{background:#1a1a1a}
  .item-card.is-new{border-left:3px solid #c00}
  .item-card.is-want{border-left:3px solid #2d6a2d}
  .item-card.is-new.is-want{border-left:3px solid #c00}
  .card-thumb-wrap{width:72px;height:72px;flex-shrink:0;margin-right:12px;border-radius:6px;overflow:hidden;background:#1a1a1a;display:flex;align-items:center;justify-content:center}
  .card-thumb{width:72px;height:72px;object-fit:contain;border-radius:6px;background:#1a1a1a}
  .card-thumb-placeholder{width:72px;height:72px;display:flex;align-items:center;justify-content:center;font-size:1.6rem;color:#333;background:#1a1a1a;border-radius:6px}
  .card-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
  .card-badges{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:1px}
  .card-name{font-size:.9rem;font-weight:600;color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card-name a{color:#eee;text-decoration:none}
  .card-name a:active{color:#7bbff7}
  .card-price{font-size:.95rem;font-weight:700;color:#fff}
  .card-meta{font-size:.75rem;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card-actions{display:flex;align-items:center;justify-content:flex-end;margin-top:2px}
  .card-watch-btn{background:none;border:none;cursor:pointer;color:#444;font-size:1.2rem;padding:4px;min-width:36px;min-height:36px;display:inline-flex;align-items:center;justify-content:center}
  .card-watch-btn.active{color:#f5c518}

  /* ── Compact list view ── */
  .compact-list{display:flex;flex-direction:column;gap:0}
  .compact-row{display:flex;align-items:center;padding:10px 12px;border-bottom:1px solid #1a1a1a;gap:8px}
  .compact-row.is-new{border-left:3px solid #c00}
  .compact-row-left{flex:1;min-width:0;display:flex;align-items:center;gap:6px}
  .compact-row-name{font-size:.84rem;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .compact-row-name a{color:#ddd;text-decoration:none}
  .compact-row-price{font-size:.88rem;font-weight:700;color:#fff;white-space:nowrap;flex-shrink:0}
  .compact-row-watch{background:none;border:none;cursor:pointer;color:#444;font-size:1rem;padding:2px 6px;min-height:36px;display:inline-flex;align-items:center}
  .compact-row-watch.active{color:#f5c518}
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
    <h2>🗑 Reset All Inventory</h2>
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
      <button onclick="dismissFirstRun();runTracker()" style="padding:8px 18px;background:#c00;border:none;border-radius:5px;color:#fff;font-size:.85rem;font-weight:700;cursor:pointer">Scan Now</button>
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
  <h1>🎸 Gear Tracker <span style="font-size:.65rem;font-weight:400;opacity:.6">v2.4.4</span></h1>
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
      <div class="zip-sort-row">
        <button id="zip-sort-btn" onclick="toggleZipSort()" title="Sort stores by distance from ZIP">📍 ZIP Sort</button>
        <input id="zip-input" type="text" maxlength="5" placeholder="ZIP code…"
          autocomplete="postal-code" inputmode="numeric"
          oninput="this.value=this.value.replace(/\D/g,'')"
          onkeydown="if(event.key==='Enter')applyZipSort()">
      </div>
    </div>

    <div id="store-list"></div>

    <div class="left-footer">
      <div id="sel-count">0 stores selected</div>
      <button id="reset-btn" onclick="resetData()"
        style="margin-top:6px;width:100%;padding:7px;background:#1a1a1a;border:1px solid #5a2a2a;border-radius:5px;color:#a05050;font-size:.75rem;cursor:pointer"
        title="Delete all cached data and start fresh">
        🗑 Reset All Inventory
      </button>
    </div>
  </div>

  <div class="right">
    <div class="status-bar">
      <span id="s-last-wrap">Last checked for new gear: <b id="s-last">—</b> <button id="check-now-btn" onclick="runTracker()" style="padding:2px 10px;background:#c00;color:#fff;border:none;border-radius:4px;font-size:.72rem;font-weight:700;cursor:pointer;margin-left:4px;display:none">Check Now</button> <button id="view-toggle-btn" class="view-toggle-btn" onclick="toggleMobileView()" title="Switch card / list view"><span id="view-toggle-icon">⊞</span></button></span>
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
    <div id="log"><span class="log-dim">Ready</span></div>
    <div class="results" id="res-panel" style="display:none">
      <div class="results-hdr">
        <span id="res-title" style="display:none"></span>
        <span class="badge" id="res-badge" style="display:none!important"></span>
        <button class="mobile-filter-toggle" id="gc-filter-toggle" onclick="toggleMobileFilters('gc')">
          <span class="toggle-arrow" id="gc-filter-arrow">▶</span> Filters
          <span class="filter-active-dot" id="gc-filter-dot"></span>
        </button>
        <div id="gc-filter-collapsible" class="filter-collapsible">
        <span id="filter-item-count" style="color:#888;font-size:.78rem;white-space:nowrap;margin-right:6px"></span>
        <button id="price-drop-toggle" onclick="togglePriceDropFilter()"
          class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
          ↓ Price Drops
        </button>
        <button id="watchlist-toggle" onclick="toggleWatchFilter()"
          class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
          ★ Watch List
        </button>
        <button id="want-list-toggle" onclick="searchWantList()"
          class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
          🎯 Want List
        </button>
        <a id="search-wl-link" onclick="openKeywords()" style="display:none;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;text-decoration:none" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">Edit Want List</a>
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
          <input id="res-search" type="text" placeholder="Search results by keyword…" oninput="filterResults();_updateResSearchClear()" autocomplete="off">
          <button id="res-search-clear" onclick="clearResSearch()" title="Clear search" style="display:none;background:none;border:none;color:#888;font-size:.85rem;cursor:pointer;padding:0 4px;line-height:1">✕</button>
          <span id="res-search-count"></span>
        </div>
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

// ── Mobile filter toggle ─────────────────────────────────────────────────────
function toggleMobileFilters(which) {
  const body = document.getElementById(which + '-filter-collapsible');
  const arrow = document.getElementById(which + '-filter-arrow');
  if (!body) return;
  const isCollapsed = body.classList.toggle('collapsed');
  arrow.classList.toggle('open', !isCollapsed);
}

function _updateFilterDot() {
  // Show a red dot on the Filters toggle when any filter is active
  const dot = document.getElementById('gc-filter-dot');
  if (!dot) return;
  const hasFilters = (window._selectedBrands && window._selectedBrands.length) ||
    (window._selectedConds && window._selectedConds.length) ||
    (window._selectedCats && window._selectedCats.length) ||
    (window._selectedSubs && window._selectedSubs.length) ||
    _watchFilterActive ||
    _priceDropFilterActive ||
    (document.getElementById('res-search').value.trim().length > 0);
  dot.classList.toggle('visible', !!hasFilters);
}

// Auto-collapse sidebars and filters on mobile on page load
document.addEventListener('DOMContentLoaded', () => {
  if (_isMobile()) {
    document.getElementById('gc-left').classList.add('collapsed');
    document.getElementById('cl-left').classList.add('collapsed');
    const gcFilters = document.getElementById('gc-filter-collapsible');
    if (gcFilters) gcFilters.classList.add('collapsed');
  }
});
// Re-check on resize (e.g. rotating phone)
window.addEventListener('resize', () => {
  const gcLeft = document.getElementById('gc-left');
  const clLeft = document.getElementById('cl-left');
  const gcFilters = document.getElementById('gc-filter-collapsible');
  if (!_isMobile()) {
    gcLeft.classList.remove('collapsed');
    clLeft.classList.remove('collapsed');
    if (gcFilters) gcFilters.classList.remove('collapsed');
  }
});

// ── localStorage helpers ─────────────────────────────────────────────────────
function _lsGet(key, fallback) {
  try { const v = localStorage.getItem('gt_' + key); return v ? JSON.parse(v) : fallback; }
  catch(e) { return fallback; }
}
function _lsSet(key, val) {
  try {
    localStorage.setItem('gt_' + key, JSON.stringify(val));
  } catch(e) {
    // localStorage full — clear legacy non-critical keys and retry
    console.warn('localStorage full for gt_' + key + ', attempting cleanup…');
    try {
      ['prev_snapshot', 'prev_fp_set'].forEach(k => {
        try { localStorage.removeItem('gt_' + k); } catch(_) {}
      });
      localStorage.setItem('gt_' + key, JSON.stringify(val));
    } catch(e2) {
      console.error('localStorage write failed for gt_' + key + ': ' + e2.message);
    }
  }
}
function _lsSetVerified(key, val) {
  // Write AND verify — critical for large data
  // where a silent failure could cause data loss
  _lsSet(key, val);
  try {
    const readback = localStorage.getItem('gt_' + key);
    if (!readback) {
      console.error('CRITICAL: gt_' + key + ' failed to persist — localStorage may be full');
      return false;
    }
    return true;
  } catch(e) {
    console.error('CRITICAL: gt_' + key + ' readback failed: ' + e.message);
    return false;
  }
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
  window._newIds = new Set(_lsGet('new_ids', []));               // Items flagged NEW from last Check for New
  // Clean up legacy localStorage keys from fingerprint-based detection (no longer used)
  try { localStorage.removeItem('gt_prev_snapshot'); localStorage.removeItem('gt_prev_fp_set'); } catch(e) {}
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
  _baseStoreCount = parseInt(storeLabel) || allStores.length;
  document.getElementById('hdr-status').textContent = storeLabel + ' stores available';
  document.getElementById('s-stores').textContent = storeLabel;
  if (allStores.length === 0) {
    appendLog('💡 No stores loaded yet — a scan will populate them automatically.', 'log-dim');
  }
  // Load store coords and apply ZIP sort if a saved ZIP exists
  _loadStoreCoords();
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

function _fmtDropDate(iso) {
  if (!iso) return '';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 86400)   return 'today';
  if (diff < 172800)  return 'yesterday';
  if (diff < 604800)  return Math.floor(diff / 86400) + ' days ago';
  // Older than a week: show short date
  return new Date(iso).toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

function _updateRelativeTime() {
  document.getElementById('s-last').textContent = _timeAgo(window._lastRunISO);
  const btn = document.getElementById('check-now-btn');
  if (btn) btn.textContent = window._lastRunISO ? 'Check Now' : 'Run Initial Scan';
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
  _baseItemCount = s.total_items || 0;
  document.getElementById('s-known').textContent = _baseItemCount.toLocaleString();
  if (s.excel_exists) document.getElementById('s-excel').style.display = 'inline';

  // Display is based on user's own last_run only (no nightly scan)

  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  if (s.is_first_run && !_lsGet('last_run', null)) {
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

// ── ZIP sort ──────────────────────────────────────────────────────────────────
window._storeCoords  = {};   // {storeName: {lat, lng}}
window._zipSortMode  = false;
window._userLat      = null;
window._userLng      = null;

function _haversine(lat1, lng1, lat2, lng2) {
  const R    = 3958.8;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a    = Math.sin(dLat/2)**2 +
               Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLng/2)**2;
  return Math.round(R * 2 * Math.asin(Math.sqrt(a)));
}

function _storeDistance(name) {
  if (!window._userLat || !window._userLng) return Infinity;
  const c = window._storeCoords[name];
  if (!c) return Infinity;
  return _haversine(window._userLat, window._userLng, c.lat, c.lng);
}

function _setZipStatus(msg, active) {
  const inp = document.getElementById('zip-input');
  const btn = document.getElementById('zip-sort-btn');
  if (msg) {
    inp.placeholder = msg;
    inp.disabled = active;
    btn.disabled = active;
  } else {
    inp.placeholder = 'ZIP code…';
    inp.disabled = false;
    btn.disabled = false;
  }
}

async function _loadStoreCoords() {
  // Load server-side coords (shared for all users, built by admin)
  try {
    const r = await fetch('/api/store-coords');
    window._storeCoords = await r.json();
  } catch(e) {}
  // No auto-restore of ZIP or sort — user must type their ZIP each session
}

async function _geocodeZip(zip, silent=false) {
  if (!zip || zip.length < 5) return false;
  try {
    const r = await fetch(`https://api.zippopotam.us/us/${zip}`);
    if (!r.ok) { if (!silent) appendLog('❌ ZIP not found: ' + zip, 'log-err'); return false; }
    const d = await r.json();
    const place = d.places && d.places[0];
    if (!place) { if (!silent) appendLog('❌ No location for ZIP ' + zip, 'log-err'); return false; }
    window._userLat = parseFloat(place.latitude);
    window._userLng = parseFloat(place.longitude);
    // Don't persist ZIP — user types it fresh each session
    return true;
  } catch(e) {
    if (!silent) appendLog('❌ ZIP lookup failed — check connection.', 'log-err');
    return false;
  }
}

async function applyZipSort() {
  const zip = document.getElementById('zip-input').value.trim();
  if (!zip || zip.length < 5) return;
  const ok = await _geocodeZip(zip);
  if (!ok) return;
  window._zipSortMode = true;
  // (ZIP sort state is not persisted — cleared on page load)
  document.getElementById('zip-sort-btn').classList.add('active');
  document.getElementById('zip-sort-btn').textContent = '↕ A-Z Sort';
  renderList();
}

function toggleZipSort() {
  if (window._zipSortMode) {
    // Turn off — go back to A-Z
    window._zipSortMode = false;
    // (not persisted)
    document.getElementById('zip-sort-btn').classList.remove('active');
    document.getElementById('zip-sort-btn').textContent = '📍 ZIP Sort';
    renderList();
  } else {
    applyZipSort();
  }
}

// buildStoreCoords / validateStores are admin-only — use /admin/build-coords and /admin/validate-stores
function buildStoreCoords() {}
function validateStores() {}

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

  // Sort: ZIP mode → nearest first; favorites mode with search → favs first; else A-Z (allStores already sorted)
  if (window._zipSortMode && window._userLat) {
    filtered = [...filtered].sort((a, b) => _storeDistance(a) - _storeDistance(b));
  } else if (favsOnly && q) {
    const favSet = new Set(favorites);
    filtered.sort((a, b) => (favSet.has(b) ? 1 : 0) - (favSet.has(a) ? 1 : 0));
  }

  el.innerHTML = '';
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const dist  = (window._zipSortMode && window._userLat) ? _storeDistance(name) : null;
    // Distance suffix embedded in label: "Austin (7 mi)" — only in ZIP sort mode
    const distSuffix = dist !== null && dist !== Infinity
      ? ` <span class="store-dist-inline">(${dist.toLocaleString()} mi)</span>`
      : (window._zipSortMode ? ' <span class="store-dist-inline">(?)</span>' : '');
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/[^a-zA-Z0-9]/g,'_');
    const isChecked = checked.has(name);
    div.innerHTML =
      `<input type="checkbox" id="${id}" value="${name}" ${isChecked ? 'checked' : ''}>` +
      `<label for="${id}">${name}${distSuffix}</label>` +
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
let _priceDropFilterActive = false;
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
window._sortCol = null;   // null (not undefined) so user_sorted=false on fresh load
window._sortDir = 1;
let _srvTotalCount = 0;
let _srvTotalUnfiltered = 0;
let _srvTotalPages = 1;
let _srvLoading = false;
let _baseItemCount = 0;   // full catalog count (set on load, reset target when filters clear)
let _baseStoreCount = 0;  // full store count (set on load)

function _getBrowseFilters() {
  return {
    filter_q:              document.getElementById('res-search').value.trim(),
    filter_brands:         window._selectedBrands || [],
    filter_conditions:     window._selectedConds || [],
    filter_categories:     window._selectedCats || [],
    filter_subcategories:  window._selectedSubs || [],
    filter_watched:         _watchFilterActive,
    filter_price_drop_only: _priceDropFilterActive,
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
    user_last_scan: window._lastRunISO || '',
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
        '<div class="no-res">Select stores on the left, then click <b>Check Now</b> to scan for inventory.</div>';
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

    // Live item count near filter buttons — always reflects current view
    const countEl2 = document.getElementById('filter-item-count');
    if (countEl2) countEl2.textContent = _srvTotalCount.toLocaleString() + ' items';

    // Update header
    const hasFilters = filters.filter_q || (filters.filter_brands && filters.filter_brands.length) || (filters.filter_conditions && filters.filter_conditions.length) || (filters.filter_categories && filters.filter_categories.length) || (filters.filter_subcategories && filters.filter_subcategories.length) || filters.filter_watched;
    const newCount = d.new_count || 0;
    if (_wantListSearchActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} Want List matches nationwide`
        : 'No Want List matches found';
    } else if (_priceDropFilterActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `↓ Price Drops — ${_srvTotalCount.toLocaleString()} item${_srvTotalCount !== 1 ? 's' : ''}`
        : 'No price drops found in selected stores';
    } else if (_watchFilterActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `Watch List — ${_srvTotalCount.toLocaleString()} item${_srvTotalCount !== 1 ? 's' : ''} found`
        : 'Watch List — no matches in selected stores';
      document.getElementById('res-badge').textContent = '';
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

    // Cache items for mobile view toggle re-render
    window._lastBrowseItems = d.items;

    // Render table + paginator
    _renderServerTable(d.items);

    // Scroll results to top on page change (use #res-body on mobile where .results is overflow:hidden)
    (document.getElementById('res-body') || document.querySelector('.results'))?.scrollTo(0, 0);

    // Update want list count badge in toolbar (only when not already filtering by want list)
    if (page === 1 && !_watchFilterActive && !_wantListSearchActive) {
      _updateWantListCount();
    }

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
  const soldBadge = isSold ? ' <span class="tag-sold">Sold</span>' : '';
  const isNew = item.isNew || (item.id && window._newIds && window._newIds.has(item.id));
  const rowClass = [isSold ? 'sold-row' : '', item.isFav ? 'fav-row' : ''].filter(Boolean).join(' ');
  const brandCell = `<td>${item.brand ? esc(item.brand) : ''}</td>`;
  const hasDrop = item.price_drop > 0;
  const dropSinceLabel = hasDrop && item.price_drop_since
    ? ` · dropped ${_fmtDropDate(item.price_drop_since)}`
    : '';
  const priceCell = hasDrop
    ? `<td><span class="price-drop-val" title="Price drop! Down $${item.price_drop.toFixed(2)}${dropSinceLabel}">` +
      (item.list_price_raw > item.price_raw ? `<span class="price-orig">$${item.list_price_raw.toFixed(2)}</span> ` : '') +
      `↓ ${item.price||''}</span></td>`
    : `<td>${item.price||''}</td>`;
  return `<tr class="${rowClass}" data-name="${esc(item.name)}" data-brand="${esc(item.brand)}" data-price="${priceNum}" data-store="${esc(item.store)}" data-location="${esc(item.location)}" data-condition="${esc(item.condition)}" data-category="${esc(item.category)}" data-subcategory="${esc(item.subcategory)}" data-image-id="${esc(item.image_id)}">` +
    `<td>${isNew ? '<span class="tag">NEW</span>' : ''}</td>` +
    `<td>${item.kwMatch ? '<span class="tag-kw">WANT</span>' : ''}</td>` +
    `<td>${watchStar}</td>` +
    `<td>${nameCell}${soldBadge}</td>` +
    (_isMobile() ? priceCell + brandCell : brandCell + priceCell) +
    `<td>${esc(item.condition)}</td>` +
    `<td>${esc(item.category)}</td>` +
    `<td>${esc(item.subcategory)}</td>` +
    `<td>${esc(item.date||'')}</td>` +
    `<td>${esc(item.store||item.location)}</td>` +
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
  const mob = _isMobile();

  // On mobile, dispatch to card or compact-list renderer
  if (mob) {
    _updateViewToggleBtn();
    const view = localStorage.getItem('gt_mobile_view') || 'cards';
    if (view === 'list') {
      _renderMobileList(items);
    } else {
      _renderMobileCards(items);
    }
    return;
  }

  const hasNew  = items.some(i => i.isNew);
  const hasWant = items.some(i => i.kwMatch);
  const tblCls  = [!hasNew ? 'no-new' : '', !hasWant ? 'no-want' : ''].filter(Boolean).join(' ');
  let html = `<table id="res-table"${tblCls ? ` class="${tblCls}"` : ''}><thead><tr>
    <th data-col="0"></th>
    <th data-col="kw"></th>
    <th data-col="watch"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Brand</th>
    <th data-col="3">Price</th>
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
  // Scroll to top after content renders — desktop scrolls .results, mobile scrolls #res-body
  document.getElementById('res-panel')?.scrollTo(0, 0);
  document.getElementById('res-body')?.scrollTo(0, 0);

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
}

// ── Mobile view toggle ────────────────────────────────────────────────────────
function _updateViewToggleBtn() {
  const view = localStorage.getItem('gt_mobile_view') || 'cards';
  const btn  = document.getElementById('view-toggle-btn');
  const icon = document.getElementById('view-toggle-icon');
  if (!btn) return;
  // ⊞ = grid/card view icon, ☰ = list view icon
  // Show the icon for the OTHER view (what you'll switch TO)
  if (view === 'list') {
    icon.textContent = '⊞';   // tap to go back to cards
    btn.title = 'Switch to card view';
    btn.classList.add('active');
  } else {
    icon.textContent = '☰';   // tap to go to compact list
    btn.title = 'Switch to compact list view';
    btn.classList.remove('active');
  }
}

function toggleMobileView() {
  const cur  = localStorage.getItem('gt_mobile_view') || 'cards';
  const next = cur === 'cards' ? 'list' : 'cards';
  localStorage.setItem('gt_mobile_view', next);
  // Re-render with same items already fetched
  if (window._lastBrowseItems) {
    _updateViewToggleBtn();
    if (next === 'list') {
      _renderMobileList(window._lastBrowseItems);
    } else {
      _renderMobileCards(window._lastBrowseItems);
    }
  }
}

// ── Mobile card renderer ──────────────────────────────────────────────────────
function _renderMobileCards(items) {
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '<div class="card-grid">';
  items.forEach(item => {
    const isNew    = item.isNew  ? ' is-new'  : '';
    const isWant   = item.kwMatch ? ' is-want' : '';
    const isSold   = item.sold   ? ' is-sold'  : '';
    const watched  = window._watchlist && window._watchlist[item.id];
    const watchCls = watched ? ' wl-on' : '';

    const imgId = item.image_id || '';
    const imgUrl = imgId
      ? `https://media.guitarcenter.com/is/image/MMGS7/${imgId}-00-200x200.jpg`
      : '';

    const newBadge  = item.isNew   ? '<span class="tag">NEW</span>'   : '';
    const wantBadge = item.kwMatch  ? '<span class="tag-kw">WANT</span>' : '';
    const soldBadge = item.sold     ? '<span class="tag-sold">SOLD</span>' : '';

    const price   = item.price || '—';
    const store   = esc(item.store_name || item.store || '');
    const loc     = esc(item.location   || '');
    const cond    = esc(item.condition  || '');
    const name    = esc(item.name       || '');
    const url     = item.url || '#';

    html += `<div class="item-card${isNew}${isWant}${isSold}">`;

    // Thumbnail
    html += '<div class="card-thumb-wrap">';
    if (imgUrl) {
      html += `<a href="${url}" target="_blank" rel="noopener"><img class="card-thumb" src="${imgUrl}" alt="" loading="lazy" onerror="this.style.display='none'"></a>`;
    } else {
      html += '<div class="card-thumb" style="background:#1a1a1a;display:flex;align-items:center;justify-content:center;color:#444;font-size:.7rem">No img</div>';
    }
    html += '</div>';

    // Body
    html += '<div class="card-body">';
    html += `<div class="card-badges">${newBadge}${wantBadge}${soldBadge}</div>`;
    html += `<div class="card-name"><a href="${url}" target="_blank" rel="noopener">${name}</a></div>`;
    html += `<div class="card-price">${price}</div>`;
    html += `<div class="card-meta">${store}${item.date ? ' · ' + esc(item.date) : ''}</div>`;
    html += `<div class="card-actions">`;
    html += `<button class="card-watch-btn${watchCls}" onclick="toggleWatch('${item.id}',this)" data-id="${item.id}">${watched ? '★' : '☆'}</button>`;
    html += `</div>`;
    html += '</div>'; // card-body

    html += '</div>'; // item-card
  });
  html += '</div>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;
}

// ── Mobile compact list renderer ─────────────────────────────────────────────
function _renderMobileList(items) {
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '<div class="compact-list">';
  items.forEach(item => {
    const isNew    = item.isNew   ? ' is-new'  : '';
    const isWant   = item.kwMatch  ? ' is-want' : '';
    const watched  = window._watchlist && window._watchlist[item.id];
    const watchCls = watched ? ' wl-on' : '';
    const newBadge = item.isNew ? '<span class="tag">NEW</span>' : '';
    const price    = item.price || '—';
    const url      = item.url || '#';
    const name     = esc(item.name || '');

    html += `<div class="compact-row${isNew}${isWant}">`;
    html += `<div class="compact-row-left">`;
    html += `<span class="compact-row-name">${newBadge}<a href="${url}" target="_blank" rel="noopener">${name}</a></span>`;
    html += `</div>`;
    html += `<span class="compact-row-price">${price}</span>`;
    html += `<button class="compact-row-watch${watchCls}" onclick="toggleWatch('${item.id}',this)" data-id="${item.id}">${watched ? '★' : '☆'}</button>`;
    html += `</div>`;
  });
  html += '</div>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;
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
    _priceDropFilterActive = false;
    document.getElementById('price-drop-toggle').classList.remove('wl-active');
    _wantListSearchActive = false;
    document.getElementById('want-list-toggle').classList.remove('wl-active');
    _srvLoading = false;  // Cancel any in-flight request so store changes always land
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
    // Try table row first, fall back to cached browse items (mobile card/list view)
    const row = btn.closest('tr');
    let name = '', store = '', location = '';
    if (row) {
      name     = row.dataset.name     || '';
      store    = row.dataset.store    || '';
      location = row.dataset.location || '';
    } else {
      const item = (window._lastBrowseItems || []).find(i => i.id === id);
      if (item) {
        name     = item.name          || '';
        store    = item.store_name    || item.store || '';
        location = item.location      || '';
      }
    }
    window._watchlist[id] = { name, store, location, date_added: new Date().toISOString().slice(0,10) };
  }
  _lsSet('watchlist', window._watchlist);
  btn.classList.toggle('active', !isWatched);
  btn.classList.toggle('wl-on',  !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
}

function toggleWatchFilter() {
  _watchFilterActive = !_watchFilterActive;
  const btn = document.getElementById('watchlist-toggle');
  btn.classList.toggle('wl-active', _watchFilterActive);
  _updateFilterDot();

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

function togglePriceDropFilter() {
  _priceDropFilterActive = !_priceDropFilterActive;
  const btn = document.getElementById('price-drop-toggle');
  btn.classList.toggle('wl-active', _priceDropFilterActive);
  if (_priceDropFilterActive) {
    // Price Drop mode: sort by drop date descending (most recent first)
    _srvSortField = 'price_drop_since';
    _srvSortDir   = 'desc';
    window._sortCol = null;  // not a user-clicked sort
    // Deactivate other exclusive filters
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
    _wantListSearchActive = false;
  }
  _updateFilterDot();
  _srvPage = 1;
  _fetchBrowsePage(1);
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
  // Sort alphabetically (case-insensitive), preserving original indices for safe removal
  const sorted = window._keywords
    .map((kw, i) => ({kw, i}))
    .sort((a, b) => a.kw.toLowerCase().localeCompare(b.kw.toLowerCase()));
  el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:7px;padding:10px 0">` +
    sorted.map(({kw, i}) => {
      const safe = kw.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `<span style="display:inline-flex;align-items:center;gap:5px;background:#0a5c2a;color:#4ade80;border:1px solid #2d6a2d;border-radius:14px;padding:4px 7px 4px 11px;font-size:.78rem;font-weight:600;white-space:nowrap">` +
        `${safe}` +
        `<button onclick="removeKeywordAt(${i})" style="background:none;border:none;color:#4ade80;opacity:.6;font-size:.75rem;cursor:pointer;padding:0 0 0 2px;line-height:1" title="Remove" onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=.6">&#10005;</button>` +
        `</span>`;
    }).join('') +
  `</div>`;
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

function removeKeywordAt(i) {
  // Index-based removal — safe for keywords containing any characters (quotes, etc.)
  window._keywords.splice(i, 1);
  _lsSet('keywords', window._keywords);
  renderKeywordList();
}

function clearAllKeywords() {
  if (!window._keywords.length) return;
  if (!confirm(`Clear all ${window._keywords.length} want list item${window._keywords.length !== 1 ? 's' : ''}? This cannot be undone.`)) return;
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
  _priceDropFilterActive = false;
  document.getElementById('price-drop-toggle').classList.remove('wl-active');
  _wantListSearchActive = false;
  document.getElementById('want-list-toggle').classList.remove('wl-active');
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
  // Go back to whatever stores are selected — bypass browseCache debounce
  // and force-clear any stuck loading flag so the fetch always fires
  const stores = getSelected();
  if (!stores.length) {
    document.getElementById('res-panel').style.display = 'none';
    return;
  }
  _srvStores = stores;
  _srvPage = 1;
  _srvLoading = false;
  _fetchBrowsePage(1);
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
  _priceDropFilterActive = false;
  document.getElementById('price-drop-toggle').classList.remove('wl-active');
  document.getElementById('want-list-toggle').classList.add('wl-active');
  _fetchBrowsePage(1);
}

function _resetWantListLink() {
  const btn = document.getElementById('want-list-toggle');
  if (btn) btn.classList.remove('wl-active');
}

// Show/hide Edit Want List link based on whether keywords exist
let _wlCountTimer = null;
function _updateWantListCount() {
  const editLink = document.getElementById('search-wl-link');
  if (!editLink) return;
  const hasKeywords = window._keywords && window._keywords.length;
  editLink.style.display = hasKeywords ? 'inline' : 'none';
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

  // Include this device's last-run time so the server gives per-device NEW results
  const runPayload = Object.assign({}, payload, {
    device_last_run: window._lastRunISO || ''
  });
  const resp = await fetch('/api/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(runPayload)
  });
  if (!resp.ok) {
    const e = await resp.json();
    running = false; stopBtn.style.display = 'none'; updateCount();
    if (resp.status === 409) {
      appendLog('⏳ Another scan is already in progress — if this is stale, use /admin/clear-lock?pw=… to reset.', 'log-dim');
    } else {
      appendLog('Error: ' + (e.error || resp.statusText), 'log-err');
    }
    return;
  }
  // Get run_id for per-session message stream (prevents cross-user contamination)
  const runData = await resp.json();
  const runId = runData.run_id || '';

  const es = new EventSource('/api/progress' + (runId ? '?run_id=' + encodeURIComponent(runId) : ''));
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
      // The scan likely completed on the server even if our SSE stream dropped
      // (common on mobile when screen locks or network blips). Update _lastRunISO
      // to now so browse gating doesn't hide items the scan found.
      window._lastRunISO = new Date().toISOString();
      _lsSet('last_run', window._lastRunISO);
      _updateRelativeTime();
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

  // New-item detection is date-based, computed server-side.
  // The server compares each item's date_listed (Algolia startDate) against this device's
  // previous scan time. Items listed after that scan are "new".
  const newIdSet = new Set(msg.new_ids || []);
  const isFirstRun = msg.baseline;
  const freshNewCount = newIdSet.size;

  // Always replace the NEW set with exactly what this scan found.
  // 0 new = all NEW tags clear. Each scan is the source of truth.
  if (!isFirstRun) {
    window._newIds = newIdSet;
    _lsSet('new_ids', [...newIdSet]);
    // Immediately remove stale NEW badges from the DOM — don't wait for async browse re-render
    if (freshNewCount === 0) {
      document.querySelectorAll('.tag').forEach(el => { if (el.textContent.trim() === 'NEW') el.remove(); });
      document.querySelectorAll('.is-new').forEach(el => el.classList.remove('is-new'));
    }
  }

  appendLog(`\\n✓ Done${stoppedNote} — ${isFirstRun ? 'initial database built' : freshNewCount.toLocaleString() + ' new this scan'}.`, 'log-dim');

  window._lastRunISO = msg.scan_time || new Date().toISOString();
  _lsSet('last_run', window._lastRunISO);
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  // Check if any new items match the want list and show notification
  const wantMatchEl = document.getElementById('s-want-match');
  if (freshNewCount > 0 && window._keywords && window._keywords.length) {
    // We need item details to check want list — fetch from server cache
    fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page:1, per_page:1000, all_stores:true, new_ids:[...newIdSet], keywords:window._keywords, filter_want_list_only:true})
    }).then(r => r.json()).then(d => {
      const wantNewCount = d.new_want_count ?? d.total_count ?? 0;
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
  document.getElementById('res-title').textContent = `${msg.scanned.toLocaleString()} Items`;
  document.getElementById('res-badge').textContent = freshNewCount > 0 ? freshNewCount + ' NEW' : '';

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
    _priceDropFilterActive = false;
    document.getElementById('price-drop-toggle').classList.remove('wl-active');
    _wantListSearchActive = false;
    document.getElementById('want-list-toggle').classList.remove('wl-active');
    if (!_srvStores.length) {
      _globalSearchActive = false; _wantListSearchActive = false;
      _globalSearchQuery = '';
    }
    _srvLoading = false;  // Reset guard — same defensive pattern as browseCache/clearGlobalSearch
    _fetchBrowsePage(1);
    return;
  }

  // Small scan: render items client-side, marking isNew per-user
  // Use the accumulated window._newIds (which may carry over from prior scan if this one found 0)
  _browseMode = 'local';
  const effectiveNewIds = window._newIds instanceof Set ? window._newIds : new Set();
  window._tableData = (msg.items || []).map(item => ({
    ...item,
    isNew: effectiveNewIds.has(item.id),
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
    _fetchBrowsePage(page);  // scroll handled inside _fetchBrowsePage after innerHTML
    return;
  }
  // Local mode
  window._localPage = page;
  renderTable();
  document.getElementById('res-panel')?.scrollTo(0, 0);
  document.getElementById('res-body')?.scrollTo(0, 0);
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
  const colW = Math.min(Math.ceil(maxW) + 32, 260); // cap at 260px
  const th = document.querySelector('#res-table th[data-col="1"]');
  if (th) th.style.width = colW + 'px';
  // Also set td widths via col group or direct style on first td of each row
  document.querySelectorAll('#res-table tbody tr td:nth-child(4)').forEach(td => {
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
    if (_isMobile()) return;  // No hover thumbnails on mobile
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
  // Clear keyword search box too
  const resSearch = document.getElementById('res-search');
  if (resSearch) { resSearch.value = ''; }
  document.getElementById('res-search-count').textContent = '';
  _updateResSearchClear();
  // Also turn off watch/price-drop filters if active
  if (_watchFilterActive) {
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
  }
  if (_priceDropFilterActive) {
    _priceDropFilterActive = false;
    document.getElementById('price-drop-toggle').classList.remove('wl-active');
  }
  if (_wantListSearchActive) {
    _wantListSearchActive = false;
    document.getElementById('want-list-toggle').classList.remove('wl-active');
  }
  // Bypass debounce — force-clear loading flag and re-fetch immediately
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function _updateResSearchClear() {
  const btn = document.getElementById('res-search-clear');
  if (!btn) return;
  const val = (document.getElementById('res-search').value || '').trim();
  btn.style.display = val ? '' : 'none';
}

function clearResSearch() {
  const el = document.getElementById('res-search');
  if (el) el.value = '';
  document.getElementById('res-search-count').textContent = '';
  _updateResSearchClear();
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function filterResults() {
  _updateFilterDot();
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
// populateStoreData is admin-only
function populateStoreData() {}

// validateStores / startValidate are admin-only — use /admin/validate-stores
function cancelValidate() {}
function startValidate() {}

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
  // Clear per-user inventory tracking state (preserves favorites, watchlist, want list)
  window._newIds = new Set();
  _lsSet('new_ids', []);
  window._lastRunISO = null;
  _lsSet('last_run', null);
  // Clean up any legacy keys
  try { localStorage.removeItem('gt_prev_snapshot'); localStorage.removeItem('gt_prev_fp_set'); } catch(e) {}
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline'; // Show so user can kick off a new scan
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

  const cols = _clCols;
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

const _clCols = ['title','price','location','date','relevance'];
function clSort(col) {
  const isRelevance = _clCols[col] === 'relevance';
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

async function clSearchWantList() {
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
  // Actually search CL for each want list keyword across all cities
  const btn = document.getElementById('cl-search-wl-link');
  const status = document.getElementById('cl-status');
  btn.textContent = 'Searching…';
  btn.style.color = '#ffbb33';
  status.textContent = 'Searching want list across all markets…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching want list…</div>';
  try {
    const allResults = [];
    const seenKeys = new Set();
    for (const kw of window._keywords) {
      // Strip quotes from keyword for search
      let q = kw.trim();
      if (q.startsWith('"') && q.endsWith('"') && q.length > 2) q = q.slice(1, -1);
      if (!q) continue;
      try {
        const r = await fetch('/api/cl-search?q=' + encodeURIComponent(q) + '&title_only=1');
        if (r.ok) {
          const d = await r.json();
          const results = d.results || [];
          for (const item of results) {
            const key = (item.title || '').toLowerCase().trim() + '|' + (item.price || '') + '|' + (item.cityId || '');
            if (!seenKeys.has(key)) {
              seenKeys.add(key);
              allResults.push(item);
            }
          }
        }
      } catch(e) { /* skip failed keyword */ }
      status.textContent = 'Searched "' + q + '"… (' + allResults.length + ' results so far)';
    }
    _clData = allResults;
    _clData.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    _clWantListFilterActive = true;
    btn.textContent = 'Clear Want List Search';
    btn.style.color = '#f88';
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Want list search failed: ' + e.message + '</div>';
    btn.textContent = 'Search Want List';
    btn.style.color = '#4ade80';
    status.textContent = '';
  }
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

APP_VERSION = "2.4.4"
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
        _q.put({"type": "done", "scanned": 0, "new_ids": [],
                "items": [], "baseline": False, "stopped": False,
                "update_success": success})
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started"})




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
