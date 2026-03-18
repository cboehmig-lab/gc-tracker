#!/usr/bin/env python3
"""
Guitar Center Used Inventory Tracker — Web App
------------------------------------------------
Run with:  python3 gc_tracker_app.py
Then open: http://localhost:5050
"""

import json, os, re, sys, time, threading, queue, webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from pathlib import Path

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

PORT        = int(os.environ.get("PORT", 5050))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# ── HTTP session ──────────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_http = http.Session()
_http.headers.update(_HEADERS)

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

FALLBACK_STORES = [
    # Alabama
    "Birmingham","Huntsville","Mobile",
    # Arizona
    "Phoenix","Scottsdale","Tempe","Tucson","Mesa","Chandler","Peoria AZ",
    # Arkansas
    "Little Rock",
    # California
    "Anaheim","Bakersfield","Brea","Burbank","Canoga Park","Cerritos",
    "Chico","Chula Vista","Clovis","Concord","El Cajon","Escondido",
    "Fresno","Hollywood","Lancaster","Long Beach","Modesto","Moreno Valley",
    "Murrieta","Northridge","Oakland","Ontario CA","Orange","Oxnard",
    "Pasadena","Rancho Cucamonga","Redding","Riverside","Roseville",
    "Sacramento","San Bernardino","San Diego","San Francisco","San Jose",
    "San Marcos","Santa Ana","Santa Barbara","Santa Rosa","Stockton",
    "Temecula","Torrance","Ventura","Victorville","Visalia","West Los Angeles",
    # Colorado
    "Arvada","Colorado Springs","Denver","Englewood","Fort Collins",
    # Connecticut
    "Fairfield","Manchester CT","North Haven",
    # Florida
    "Altamonte Springs","Boca Raton","Brandon","Clearwater","Davie",
    "Doral","Fort Lauderdale","Fort Myers","Gainesville","Hialeah",
    "Jacksonville","Jupiter","Kendale Lakes","Kissimmee","Melbourne FL",
    "Miami","Naples FL","North Miami","Orlando","Palm Beach Gardens",
    "Pensacola","Pinecrest","Sarasota","St Petersburg","Tallahassee",
    "Tampa","West Palm Beach",
    # Georgia
    "Alpharetta","Atlanta","Columbus GA","Kennesaw","Macon","Marietta","Savannah",
    # Idaho
    "Boise",
    # Illinois
    "Bloomington IL","Chicago","Downers Grove","Elgin","Orland Park",
    "Rockford","Schaumburg","Springfield IL",
    # Indiana
    "Evansville","Fort Wayne","Indianapolis","Merrillville",
    # Iowa
    "Cedar Rapids","Des Moines",
    # Kansas
    "Wichita",
    # Kentucky
    "Lexington","Louisville",
    # Louisiana
    "Baton Rouge","Metairie","New Orleans","Shreveport",
    # Maryland
    "Baltimore","Beltsville","Rockville","Towson",
    # Massachusetts
    "Boston","Braintree","Burlington MA","Cambridge","Springfield MA","Worcester",
    # Michigan
    "Ann Arbor","Detroit","Flint","Grand Rapids","Kalamazoo","Lansing",
    "Sterling Heights","Troy MI",
    # Minnesota
    "Bloomington MN","Duluth","Minneapolis","St Paul",
    # Mississippi
    "Jackson MS",
    # Missouri
    "Kansas City","Springfield MO","St Louis",
    # Nebraska
    "Omaha",
    # Nevada
    "Henderson","Las Vegas","North Las Vegas","Reno",
    # New Hampshire
    "Manchester NH","Nashua",
    # New Jersey
    "Eatontown","Edison","Linden","Paramus","Princeton",
    # New Mexico
    "Albuquerque",
    # New York
    "Albany NY","Brooklyn","Buffalo","Long Island","Manhattan",
    "Queens","Rochester NY","Staten Island","Syracuse","Yonkers",
    # North Carolina
    "Asheville","Charlotte","Durham","Fayetteville","Greensboro",
    "Raleigh","Wilmington NC","Winston-Salem",
    # Ohio
    "Akron","Canton OH","Cincinnati","Cleveland","Columbus OH",
    "Dayton","Toledo","Youngstown",
    # Oklahoma
    "Oklahoma City","Tulsa",
    # Oregon
    "Beaverton","Eugene","Portland OR","Salem OR",
    # Pennsylvania
    "Allentown","Erie PA","Philadelphia","Pittsburgh","Reading PA","Scranton",
    # Rhode Island
    "Providence",
    # South Carolina
    "Charleston SC","Columbia SC","Greenville SC",
    # Tennessee
    "Chattanooga","Knoxville","Memphis","Nashville",
    # Texas
    "Amarillo","Arlington TX","Austin","Cedar Park","Corpus Christi",
    "Dallas","El Paso","Fort Worth","Frisco","Garland","Houston",
    "Houston Willowbrook","Humble","Lubbock","McAllen","Mesquite",
    "North Austin","Plano","Round Rock","San Antonio","South Austin",
    "Sugar Land","Tyler TX","Waco",
    # Utah
    "Murray UT","Orem","Salt Lake City",
    # Virginia
    "Chesapeake","Hampton VA","Lynchburg","Newport News","Norfolk",
    "Richmond VA","Roanoke","Virginia Beach",
    # Washington
    "Bellevue WA","Lynnwood","Seattle","Spokane","Tacoma",
    # Washington DC
    "Washington DC",
    # Wisconsin
    "Green Bay","Madison WI","Milwaukee",
]


def get_store_list() -> list[str]:
    cached = []
    if STORES_CACHE.exists():
        try:
            cached = json.loads(STORES_CACHE.read_text()).get("stores", [])
        except Exception:
            pass
    # Always merge with fallback so newly added stores are never missing
    return sorted(set(cached) | set(FALLBACK_STORES))


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


def refresh_store_list(send_progress=None) -> list[str]:
    """Fetch live store list from GC state-by-state and merge with fallback."""
    live_names = []

    # Strategy 1: scrape stores.guitarcenter.com state by state in parallel
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

    # Strategy 2: fall back to main stores page if state scrape yielded nothing useful
    if len(live_names) < 20:
        try:
            r = _http.get("https://www.guitarcenter.com/Stores/", timeout=15)
            r.raise_for_status()
            html = r.text
            # Only trust explicit store page URLs — same pattern as above
            for slug in re.findall(r'href="https?://stores\.guitarcenter\.com/([a-z]{2})/([a-z][a-z0-9\-]+)/(\d+)"', html):
                _, city_slug, _ = slug
                name = " ".join(w.capitalize() for w in city_slug.split("-"))
                live_names.append(name)
        except Exception:
            pass

    # Always merge with fallback to guarantee known stores are never dropped
    # Strip any non-city strings that could have slipped through scraping
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
    merged = sorted(set(live_names) | set(FALLBACK_STORES))
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


# ── GC scraping ───────────────────────────────────────────────────────────────

PAGE_SIZE = 24

def _clean_name(name: str) -> str:
    """Strip redundant 'Used ' prefix from item names."""
    name = name.strip()
    if name.lower().startswith("used "):
        name = name[5:].strip()
    return name


def fetch_page(store_name: str, page: int) -> str:
    query = f"filters=stores:{store_name.replace(' ', '%20')}&Ns=cD"
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
    GC's five condition grades are: Excellent, Great, Good, Fair, Poor.
    No whitelist — accept whatever label GC uses after 'Condition:'.
    Uses store-name-text spans as per-card anchors to avoid positional drift.
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

    # Match "Condition: Great" or "Condition: <!-- --> Good" etc.
    # No whitelist — accept any 2-20 char word GC puts here
    cond_re = re.compile(
        r'Condition:\s*(?:<!--[^>]*>\s*)*([A-Z][A-Za-z ]{1,19}?)(?:\s*[<\n\r])',
        re.DOTALL
    )

    # Use store-name-text as per-card anchor (one per card, right before condition)
    store_anchors = [m.start() for m in re.finditer(r'store-name-text', card_html)]
    conditions = []

    if len(store_anchors) >= len(urls):
        for anchor_pos in store_anchors[:len(urls)]:
            chunk = card_html[anchor_pos:anchor_pos + 600]
            m = cond_re.search(chunk)
            conditions.append(m.group(1).strip() if m else "")
    else:
        # Fallback: positional scan
        for m in cond_re.finditer(card_html):
            conditions.append(m.group(1).strip())
            if len(conditions) == len(urls):
                break

    # Diagnostics
    try:
        hits = []
        for m in cond_re.finditer(card_html):
            hits.append(card_html[m.start():m.start()+80].replace("\n", "\\n"))
            if len(hits) >= 5:
                break
        anchor_sample = ""
        if store_anchors:
            anchor_sample = card_html[store_anchors[0]:store_anchors[0]+300].replace("\n", "\\n")
        (DATA_DIR / "gc_condition_diag.json").write_text(json.dumps({
            "url_count": len(urls),
            "store_anchor_count": len(store_anchors),
            "conditions_found": sum(1 for c in conditions if c),
            "sample_urls": urls[:3],
            "sample_conditions": conditions[:5],
            "first_5_condition_hits": hits,
            "html_around_first_anchor": anchor_sample,
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
            send({"type": "progress", "msg": f"  [{store_name}] error: {e}"})
            break
        products = parse_products(html, store_name)
        if not products:
            break
        if all(p["id"] in ids_seen for p in products):   # loop guard
            break
        for p in products:
            if p["id"] not in ids_seen:
                all_products.append(p)
                ids_seen.add(p["id"])
        if len(products) < PAGE_SIZE:
            break
        page += 1
        time.sleep(1.5)
    return all_products, ids_seen


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
    for f in [STATE_FILE, CAT_CACHE_FILE, OUTPUT_FILE]:
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    # Also clear in-memory cache
    global _cat_cache
    _cat_cache = {}
    return jsonify({"deleted": deleted, "status": "Reset complete. Ready for a fresh baseline."})

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

@app.route("/api/debug-condition/reset", methods=["POST"])
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
            send({"type":"progress","msg":"⏳ This may take 30–60 min. Feel free to leave it running!"})

        all_products, ids_this_run = [], set()
        for i, store in enumerate(stores_to_scan, 1):
            if _stop_event.is_set():
                send({"type":"progress","msg":"⏹ Stopped by user."})
                break
            send({"type":"progress","msg":f"\n[{i}/{len(stores_to_scan)}] {store}"})
            products, ids = scrape_store(store, seen_ids, send, _stop_event)
            for p in products:
                if p["id"] not in ids_this_run:
                    all_products.append(p)
            ids_this_run |= ids

        # ── Classify categories (parallel) & use listing-page condition ─────────
        # Condition is already parsed from the listing page in parse_products — use it.
        # Category requires the individual product page (breadcrumb) — fetch in parallel.
        needs_cat = [p for p in all_products
                     if not baseline and p.get("url")
                     and not _cat_cache.get(p["id"], {}).get("category")]

        if needs_cat:
            send({"type": "progress", "msg": f"\nFetching categories for {len(needs_cat)} item(s) (parallel)…"})

            def _fetch_cat(p):
                try:
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

        # Apply all data to products
        for p in all_products:
            sku    = p["id"]
            cached = _cat_cache.get(sku, {})
            # Category: from cache (just fetched) or keyword fallback
            cat    = cached.get("category") or classify_by_name(p.get("name", ""))[0]
            subcat = cached.get("subcategory") or classify_by_name(p.get("name", ""))[1]
            # Condition: from the listing page (parse_products already got it) — never blank it out
            condition = p.get("condition") or cached.get("condition", "")
            _cat_cache[sku] = {"category": cat, "subcategory": subcat,
                               "condition": condition, "condition_fetched": True,
                               "name": p.get("name", ""), "url": p.get("url", "")}
            p["category"]    = cat
            p["subcategory"] = subcat
            p["condition"]   = condition
        _save_cat_cache()

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
                "name":       p["name"],
                "price":      f"${p['price']:,.2f}" if p["price"] else "",
                "store":      p["store"],
                "url":        p["url"],
                "category":   p.get("category", ""),
                "subcategory":p.get("subcategory", ""),
                "condition":  p.get("condition", ""),
                "date":       item_dates.get(p["id"], ""),
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
<title>GC Used Tracker</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;height:100vh;display:flex;flex-direction:column}

header{background:#c00;padding:12px 24px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:1.2rem;font-weight:700;color:#fff}
#stop-btn{display:none;padding:7px 14px;background:#fff;color:#c00;border:none;border-radius:5px;font-size:.82rem;font-weight:700;cursor:pointer;white-space:nowrap}
#stop-btn:hover{background:#ffe0e0}
#stop-btn:disabled{opacity:.6;cursor:not-allowed}
#hdr-status{font-size:.8rem;color:#ffbbbb;margin-left:auto}

.layout{display:flex;flex:1;overflow:hidden}

/* ── Left panel ── */
.left{width:300px;min-width:260px;background:#1a1a1a;border-right:1px solid #2e2e2e;display:flex;flex-direction:column;flex-shrink:0}

.mode-tabs{display:flex;border-bottom:1px solid #2e2e2e;flex-shrink:0}
.mode-tab{flex:1;padding:10px 4px;text-align:center;font-size:.78rem;font-weight:600;color:#777;cursor:pointer;border:none;background:none;letter-spacing:.3px;text-transform:uppercase;border-bottom:2px solid transparent;margin-bottom:-1px}
.mode-tab:hover{color:#ccc}
.mode-tab.active{color:#fff;border-bottom-color:#c00}
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
#run-btn{flex:1;padding:10px;background:#c00;color:#fff;border:none;border-radius:5px;font-size:.95rem;font-weight:700;cursor:pointer}
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

table{width:auto;min-width:100%;border-collapse:collapse;font-size:.83rem;table-layout:fixed}
th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 10px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:40px;cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden}
th:nth-child(1){width:46px}
th:nth-child(2){} /* Item width set dynamically */
th:nth-child(3){width:110px}
th:nth-child(4){width:110px}
th:nth-child(5){width:110px}
th:nth-child(6){width:72px}
th:nth-child(7){width:88px}
th:nth-child(8){width:128px}
th:hover{color:#ccc}
th.sort-asc::after{content:" ▲";color:#c00;font-size:.6rem}
th.sort-desc::after{content:" ▼";color:#c00;font-size:.6rem}
td{padding:7px 10px;border-bottom:1px solid #1c1c1c;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr:hover td{background:#161616}
td a{color:#6ab0f5;text-decoration:none}
td a:hover{text-decoration:underline}
.tag{background:#c00;color:#fff;font-size:.65rem;font-weight:700;padding:1px 5px;border-radius:3px}
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
</style>
</head>
<body>

<!-- Password modal -->
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
  <h1>🎸 GC Used Inventory Tracker</h1>
  <button id="stop-btn" onclick="stopRun()">⏹ Stop Running</button>
  <span id="hdr-status">Loading…</span>
</header>

<div class="layout">

  <div class="left">
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-find" onclick="setMode('find')">Select Stores</button>
      <button class="mode-tab"        id="tab-favs" onclick="setMode('favs')">★ Favorites</button>
    </div>

    <div class="search-wrap" id="search-wrap">
      <input id="search" type="text" placeholder="Search stores…" autocomplete="off">
      <div class="sel-btns">
        <button class="sel-btn" onclick="selectFavorites()">★ Favorites</button>
        <button class="sel-btn" onclick="selectAll()">Select All</button>
        <button class="sel-btn" onclick="clearAll()">Clear All</button>
      </div>
    </div>

    <div id="store-list"></div>

    <div class="left-footer">
      <div id="sel-count">0 stores selected</div>
      <div class="btn-row">
        <button id="run-btn"      onclick="runTracker()" disabled>Run</button>
        <button id="baseline-btn" onclick="runBaseline()" title="Scan every GC store nationwide">🌐 Build Baseline</button>
      </div>
      <button id="refresh-stores-btn" onclick="refreshStores()"
        style="margin-top:8px;width:100%;padding:7px;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:5px;color:#777;font-size:.75rem;cursor:pointer"
        title="Re-fetch the full store list from Guitar Center's website">
        🔄 Refresh Store List
      </button>
      <button id="fill-gaps-btn" onclick="fillGaps()"
        style="margin-top:6px;width:100%;padding:7px;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:5px;color:#777;font-size:.75rem;cursor:pointer"
        title="Re-scrape selected stores to fill in missing Condition and Category data">
        🔍 Fill Data Gaps
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
    </div>
    <div id="log"><span class="log-dim">Ready — select stores and click Run, or build a full baseline.</span></div>
    <div class="results" id="res-panel" style="display:none">
      <div class="results-hdr">
        <span id="res-title">New Items</span>
        <span class="badge" id="res-badge"></span>
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

<script>
let allStores = [], favorites = [], mode = 'find', running = false;
const BASELINE_PW = 'Beatle909!';

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('search').addEventListener('input', filterList);
  await loadData();
  await loadState();
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
}

// ── Refresh store list ────────────────────────────────────────────────────────
async function refreshStores() {
  const btn = document.getElementById('refresh-stores-btn');
  btn.textContent = '🔄 Fetching all stores…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/stores/refresh', {method:'POST'});
    const d = await r.json();
    allStores = d.stores; favorites = d.favorites;
    renderList();
    const info = d.info || {};
    const label = d.count + ' stores';
    document.getElementById('hdr-status').textContent = label + ' available';
    document.getElementById('s-stores').textContent = label + ' · refreshed just now';
    btn.textContent = `✓ ${d.count} stores loaded`;
  } catch(e) {
    btn.textContent = '🔄 Refresh Store List';
  }
  btn.disabled = false;
}

// ── Mode switching ────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  ['find','favs'].forEach(t => {
    document.getElementById('tab-'+t).classList.toggle('active', t===m);
  });
  document.getElementById('search-wrap').style.display = m === 'find' ? '' : 'none';
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
function selectFavorites() {
  document.querySelectorAll('.store-row input[type=checkbox]').forEach(cb => {
    cb.checked = favorites.includes(cb.value);
  });
  updateCount();
}

// ── Render store list ─────────────────────────────────────────────────────────
function renderList() {
  const el    = document.getElementById('store-list');
  const q     = document.getElementById('search').value.toLowerCase();
  let stores  = mode === 'favs' ? (favorites.length ? favorites : null) : allStores;

  if (!stores) {
    el.innerHTML = '<div class="empty-msg">No favorites yet.<br>Click ★ next to any store to add it.</div>';
    updateCount(); return;
  }

  const filtered = (mode === 'find' && q) ? stores.filter(s => s.toLowerCase().includes(q)) : stores;
  el.innerHTML = '';
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/\\W/g,'_');
    div.innerHTML =
      `<input type="checkbox" id="${id}" value="${name}">` +
      `<label for="${id}">${name}</label>` +
      `<button class="fav-btn ${isFav?'active':''}" title="${isFav?'Remove from':'Add to'} favorites"
        onclick="toggleFav(event,'${name.replace(/'/g,"\\\\'")}',this)">★</button>`;
    div.querySelector('input').addEventListener('change', updateCount);
    el.appendChild(div);
  });
  updateCount();
}

function filterList() { if (mode==='find') renderList(); }

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
  if (mode === 'favs') renderList();
}

// ── Selection ─────────────────────────────────────────────────────────────────
function updateCount() {
  const checked = [...document.querySelectorAll('.store-row input:checked')];
  const n = checked.length;
  document.getElementById('sel-count').textContent = n + ' store' + (n===1?'':'s') + ' selected';
  document.getElementById('run-btn').disabled = (n===0 || running);
  document.getElementById('baseline-btn').disabled = running;
}

function getSelected() {
  return [...document.querySelectorAll('.store-row input:checked')].map(c => c.value);
}

// ── Baseline password modal ───────────────────────────────────────────────────
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

async function fillGaps() {
  if (running) return;
  const stores = getSelected();
  if (!stores.length) {
    appendLog('Select at least one store before filling gaps.', 'log-err');
    return;
  }
  const btn = document.getElementById('fill-gaps-btn');
  const resp = await fetch('/api/fill-gaps', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stores})
  });
  if (!resp.ok) {
    const e = await resp.json();
    appendLog('Fill gaps error: ' + e.error, 'log-err');
    return;
  }
  running = true; updateCount();
  btn.textContent = '⏳ Filling gaps…';
  btn.disabled = true;
  document.getElementById('stop-btn').style.display = 'inline-block';
  document.getElementById('stop-btn').disabled = false;
  document.getElementById('stop-btn').textContent = '⏹ Stop Running';
  document.getElementById('log').innerHTML = '';
  appendLog(`🔍 Re-scanning ${stores.length} store(s) to fill missing Condition & Category data…`);

  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      document.getElementById('stop-btn').style.display = 'none';
      btn.textContent = '🔍 Fill Data Gaps';
      btn.disabled = false;
      updateCount();
      if (msg.gap_fill) {
        appendLog(`✓ Gap fill complete — ${msg.fixed || 0} items updated. Re-run to see refreshed data.`, 'log-dim');
      }
    }
  };
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
    ['cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
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
    ['cat-filter','subcat-filter'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  window._tableData = [];
  (msg.new_items || []).forEach(item => window._tableData.push({isNew:true,  ...item}));
  (msg.all_items  || []).forEach(item => window._tableData.push({isNew:false, ...item}));
  window._sortCol = null; window._sortDir = 1;

  populateCategoryFilter();
  renderTable();
}

// ── Category filters ──────────────────────────────────────────────────────────
function populateCategoryFilter() {
  const data = window._tableData || [];
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
const _SORT_COLS = [null, 'name', 'condition', 'category', 'subcategory', 'price', 'date', 'store'];

function renderTable() {
  const data = window._tableData || [];
  let html = `<table id="res-table"><thead><tr>
    <th data-col="0"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Condition</th>
    <th data-col="3">Category</th>
    <th data-col="4">Subcategory</th>
    <th data-col="5">Price</th>
    <th data-col="6">Date</th>
    <th data-col="7">Store</th>
  </tr></thead><tbody>`;
  data.forEach(item => {
    const priceNum = parseFloat((item.price||'').replace(/[^0-9.]/g,'')) || 0;
    const esc = s => (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    const nameCell = item.url
      ? `<a href="${item.url}" target="_blank">${esc(item.name)}</a>`
      : esc(item.name);
    html += `<tr data-name="${esc(item.name)}" data-price="${priceNum}" data-store="${esc(item.store)}" data-category="${esc(item.category)}" data-subcategory="${esc(item.subcategory)}">` +
      `<td>${item.isNew ? '<span class="tag">NEW</span>' : ''}</td>` +
      `<td>${nameCell}</td>` +
      `<td>${esc(item.condition)}</td>` +
      `<td>${esc(item.category)}</td>` +
      `<td>${esc(item.subcategory)}</td>` +
      `<td>${item.price||''}</td>` +
      `<td style="color:#888;font-size:.75rem">${esc(item.date||'')}</td>` +
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
  document.getElementById('cat-filter').value = '';
  const subEl = document.getElementById('subcat-filter');
  subEl.value = '';
  subEl.style.display = 'none';
  document.getElementById('clear-filters-btn').style.display = 'none';
  filterResults();
}

function filterResults() {
  const q      = document.getElementById('res-search').value.toLowerCase().trim();
  const cat    = document.getElementById('cat-filter').value;
  const subcat = document.getElementById('subcat-filter').value;
  const rows   = document.querySelectorAll('#res-body tbody tr');
  let visible  = 0;
  rows.forEach(row => {
    const show = (!q      || row.textContent.toLowerCase().includes(q)) &&
                 (!cat    || (row.dataset.category    || '') === cat) &&
                 (!subcat || (row.dataset.subcategory || '') === subcat);
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const countEl = document.getElementById('res-search-count');
  countEl.textContent = (q || cat || subcat) ? `${visible} of ${rows.length}` : '';
  // Show/hide Clear Filters button based on whether any filter is active
  const clearBtn = document.getElementById('clear-filters-btn');
  if (clearBtn) clearBtn.style.display = (cat || subcat) ? '' : 'none';
}

// ── Reset ─────────────────────────────────────────────────────────────────────
async function resetData() {
  if (running) { appendLog('Stop the current run before resetting.', 'log-err'); return; }
  const pw = prompt('Enter your app password to reset all data (state, cache, Excel):');
  if (pw === null) return;
  if (!confirm('This will delete gc_state.json, gc_category_cache.json, and gc_new_inventory.xlsx. Are you sure?')) return;
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
</script>
</body>
</html>"""


# ── Launch ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _load_cat_cache()
    if not STORES_CACHE.exists():
        print("Building store list…")
        refresh_store_list()
    url = f"http://localhost:{PORT}"
    print(f"\n  Guitar Center Tracker is running!")
    print(f"  Open: {url}")
    print(f"  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
