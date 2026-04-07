#!/usr/bin/env python3
"""
Guitar Center Used Inventory Tracker — Web App
------------------------------------------------
Run with:  python3 gc_tracker_app.py
Then open: http://localhost:5050
"""

import json, os, re, sys, time, threading, queue, webbrowser, uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

try:
    from flask import (Flask, request, jsonify, Response, stream_with_context,
                       session, redirect, send_file, make_response)
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
# On Railway, set DATA_DIR=/data (persistent volume). Locally defaults to script folder.
SCRIPT_DIR     = Path(__file__).parent
DATA_DIR       = Path(os.environ.get("DATA_DIR", SCRIPT_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE     = DATA_DIR / "gc_state.json"
OUTPUT_FILE    = DATA_DIR / "gc_new_inventory.xlsx"
STORES_CACHE   = DATA_DIR / "gc_stores_cache.json"
FAVORITES_FILE = DATA_DIR / "gc_favorites.json"

PORT        = int(os.environ.get("PORT", 5050))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")   # set this in Railway env vars

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

# ── Store list ────────────────────────────────────────────────────────────────

FALLBACK_STORES = [
    # Alabama
    "Birmingham","Huntsville","Mobile",
    # Arizona
    "Phoenix","Scottsdale","Tempe","Tucson","Mesa","Chandler","Peoria AZ",
    # Arkansas
    "Little Rock",
    # California
    "Anaheim","Bakersfield","Burbank","Canoga Park","Chico","Clovis",
    "Concord","El Cajon","Escondido","Fresno","Hollywood","Long Beach",
    "Modesto","Moreno Valley","Northridge","Oakland","Ontario CA",
    "Orange","Oxnard","Pasadena","Rancho Cucamonga","Redding","Riverside",
    "Sacramento","San Bernardino","San Diego","San Francisco","San Jose",
    "San Marcos","Santa Ana","Santa Barbara","Santa Rosa","Stockton",
    "Torrance","Ventura","Victorville","Visalia","West Los Angeles",
    # Colorado
    "Aurora","Colorado Springs","Denver","Lakewood","Thornton",
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
    "Atlanta","Columbus GA","Kennesaw","Macon","Savannah",
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
    if STORES_CACHE.exists():
        try:
            return sorted(json.loads(STORES_CACHE.read_text()).get("stores", []))
        except Exception:
            pass
    return sorted(set(FALLBACK_STORES))


def refresh_store_list() -> list[str]:
    try:
        r = _http.get("https://www.guitarcenter.com/Stores/", timeout=15)
        r.raise_for_status()
        names = re.findall(r'storeName["\s:]+["\'](.*?)["\']', r.text)
        if not names:
            names = re.findall(r'/store/[^"]+">([^<]+)</a>', r.text)
        names = [n.strip() for n in names if len(n.strip()) > 2]
        if len(names) < 20:
            names = FALLBACK_STORES
    except Exception:
        names = FALLBACK_STORES
    stores = sorted(set(names))
    STORES_CACHE.write_text(json.dumps({"stores": stores, "updated": datetime.now().isoformat()}))
    return stores


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

def fetch_page(store_name: str, page: int) -> str:
    query = f"filters=stores:{store_name.replace(' ', '%20')}&Ns=cD"
    url   = f"https://www.guitarcenter.com/Used/?{query}&page={page}"
    r = _http.get(url, timeout=20)
    r.raise_for_status()
    return r.text


def parse_products(html: str, store_name: str) -> list[dict]:
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
            name = item.get("name", "").strip()
            sku  = item.get("sku",  "").strip()
            url  = item.get("url",  "").strip()
            raw  = item.get("offers", {}).get("price", "")
            try:    price = float(raw) if raw else None
            except: price = None
            if name and sku:
                products.append({"id": sku, "name": name, "price": price,
                                  "store": store_name, "url": url})
        return products
    return []


def scrape_store(store_name: str, seen_ids: set, send) -> tuple[list[dict], set]:
    """Returns (all_products_found, ids_seen_this_store)."""
    all_products, ids_seen = [], set()
    page = 1
    while page <= 50:
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
        if seen_ids and all(p["id"] in seen_ids for p in products):
            send({"type": "progress", "msg": f"  [{store_name}] up to date ✓"})
            break
        if len(products) < PAGE_SIZE:
            break
        page += 1
        time.sleep(1.5)
    return all_products, ids_seen


# ── State ─────────────────────────────────────────────────────────────────────

RECENCY_HOURS = 96   # default "New" window; extends automatically if gap > 96 h


def load_state() -> dict:
    if STATE_FILE.exists():
        raw = json.loads(STATE_FILE.read_text())
        # ── Migrate old flat format → new per-device format ───────────────────
        if "seen_ids" in raw and "items" not in raw:
            last_run = raw.get("last_run") or datetime.now().isoformat()
            raw = {
                "last_global_scan": last_run,
                "items":   {sid: last_run for sid in raw.get("seen_ids", [])},
                "devices": {},
            }
            STATE_FILE.write_text(json.dumps(raw, indent=2))
        return raw
    return {"last_global_scan": None, "items": {}, "devices": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_device(state: dict, device_id: str) -> dict:
    """Return per-device state dict (never None)."""
    return state.get("devices", {}).get(device_id, {"last_run": None, "seen_ids": []})


def _new_items_for_device(all_products: list, state: dict, device_id: str) -> list:
    """Items that count as 'new' for a specific device.

    An item is new to this device if:
      1. Its ID is not in this device's seen_ids, AND
      2. Its global first_seen timestamp falls within the recency window:
           window = max(RECENCY_HOURS, hours since this device's last run)
         This means if a device hasn't run in >96 h, the window expands to
         cover the full gap — so nothing slips through the cracks.
    """
    device      = _get_device(state, device_id)
    device_seen = set(device.get("seen_ids", []))
    last_run    = device.get("last_run")
    item_ts     = state.get("items", {})
    now         = datetime.now()

    if last_run:
        last_run_dt = datetime.fromisoformat(last_run)
        # cutoff = the earlier of (RECENCY_HOURS ago) and (device's last run)
        # i.e. window = max(RECENCY_HOURS, gap since last run)
        cutoff = min(now - timedelta(hours=RECENCY_HOURS), last_run_dt)
    else:
        # First run on this device: only show items from the last RECENCY_HOURS
        cutoff = now - timedelta(hours=RECENCY_HOURS)

    new = []
    for p in all_products:
        if p["id"] in device_seen:
            continue
        ts = item_ts.get(p["id"])
        if ts is None:
            # Brand-new discovery this scan — always include
            new.append(p)
        elif datetime.fromisoformat(ts) >= cutoff:
            new.append(p)
    return new


# ── Excel ─────────────────────────────────────────────────────────────────────

_COLS    = ["Status", "Date Found", "Item Name", "Price", "Store", "Link"]
_WIDTHS  = [8, 18, 58, 12, 16, 70]
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
    if OUTPUT_FILE.exists():
        wb = load_workbook(OUTPUT_FILE)
        ws = wb.active
        ws.insert_rows(2, amount=n)
        for i, item in enumerate(new_items):
            r = 2 + i
            ws.cell(r, 1, "New"); ws.cell(r, 2, ts); ws.cell(r, 3, item["name"])
            pc = ws.cell(r, 4, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 5, item["store"])
            lc = ws.cell(r, 6, item["url"] or "")
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
            pc = ws.cell(r, 4, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 5, item["store"])
            lc = ws.cell(r, 6, item["url"] or "")
            if item["url"]: lc.hyperlink = item["url"]; lc.style = "Hyperlink"
            _fmt_row(ws, r); ws.cell(r, 1).font = _NEW_FONT
    wb.save(OUTPUT_FILE)


# ── Flask ─────────────────────────────────────────────────────────────────────

app             = Flask(__name__)
app.secret_key  = os.environ.get("SECRET_KEY", os.urandom(24))
_q              = queue.Queue()
_lock           = threading.Lock()


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

@app.route("/")
@login_required
def index():
    resp = make_response(HTML_TEMPLATE)
    if not request.cookies.get("device_id"):
        resp.set_cookie("device_id", str(uuid.uuid4()),
                        max_age=365 * 24 * 3600, samesite="Lax")
    return resp

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
    return jsonify({"stores": get_store_list(), "favorites": load_favorites()})

@app.route("/api/stores/refresh", methods=["POST"])
@login_required
def api_stores_refresh():
    stores = refresh_store_list()
    return jsonify({"stores": stores, "favorites": load_favorites(), "count": len(stores)})

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
    state     = load_state()
    device_id = request.cookies.get("device_id", "default")
    device    = _get_device(state, device_id)
    return jsonify({
        "last_run":    device.get("last_run"),
        "known_items": len(device.get("seen_ids", [])),
        "excel_exists": OUTPUT_FILE.exists(),
        "is_first_run": not device.get("last_run"),
    })

@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    data     = request.json
    selected = data.get("stores", [])
    baseline = data.get("baseline", False)   # True = full nationwide baseline scan
    if not selected and not baseline:
        _lock.release()
        return jsonify({"error": "No stores selected."}), 400
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    device_id = request.cookies.get("device_id", "default")
    t = threading.Thread(target=_run, args=(selected, baseline, device_id), daemon=True)
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


def _run(selected_stores: list[str], baseline: bool, device_id: str):
    def send(msg): _q.put(msg)
    try:
        state       = load_state()
        device      = _get_device(state, device_id)
        device_seen = set(device.get("seen_ids", []))
        run_time    = datetime.now().isoformat()

        stores_to_scan = get_store_list() if baseline else selected_stores
        label = "baseline scan" if baseline else f"{len(stores_to_scan)} store(s)"
        send({"type":"progress","msg":f"Starting {label} — {len(stores_to_scan)} stores total…"})
        if baseline:
            send({"type":"progress","msg":"⏳ This may take 30–60 min. Feel free to leave it running!"})

        all_products, ids_this_run = [], set()
        for i, store in enumerate(stores_to_scan, 1):
            send({"type":"progress","msg":f"\n[{i}/{len(stores_to_scan)}] {store}"})
            # Use THIS device's seen_ids for early termination so each device
            # independently discovers what's new to it, regardless of other devices.
            products, ids = scrape_store(store, device_seen, send)
            for p in products:
                if p["id"] not in ids_this_run:
                    all_products.append(p)
            ids_this_run |= ids

        # Capture globally new items (not yet timestamped) before mutating state
        global_known = set(state.get("items", {}).keys())
        globally_new = [p for p in all_products if p["id"] not in global_known]

        # Stamp newly discovered items with a first_seen timestamp
        now_iso = datetime.now().isoformat()
        for p in all_products:
            if p["id"] not in state["items"]:
                state["items"][p["id"]] = now_iso

        # Determine what's 'new' to THIS device using the recency window
        new_items = _new_items_for_device(all_products, state, device_id)

        # Persist per-device state
        state.setdefault("devices", {}).setdefault(
            device_id, {"last_run": None, "seen_ids": []})
        updated_seen = set(state["devices"][device_id]["seen_ids"]) | ids_this_run
        state["devices"][device_id]["seen_ids"] = list(updated_seen)
        state["devices"][device_id]["last_run"]  = run_time
        state["last_global_scan"] = run_time

        save_state(state)

        # Excel logs only globally new items (new to the world, not just this device)
        if globally_new:
            write_excel(globally_new)

        def fmt(p):
            return {"name":p["name"], "price":f"${p['price']:,.2f}" if p["price"] else "",
                    "store":p["store"], "url":p["url"]}

        new_ids = {p["id"] for p in new_items}
        send({
            "type":       "done",
            "baseline":   baseline,
            "scanned":    len(all_products),
            "new_count":  len(new_items),
            "new_items":  [fmt(p) for p in new_items],
            # all items found this run (new first, then existing) — omit on baseline
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

header{background:#c00;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-shrink:0}
header h1{font-size:1.25rem;font-weight:700;color:#fff}
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

.status-bar{padding:8px 20px;background:#161616;border-bottom:1px solid #2e2e2e;font-size:.78rem;color:#666;display:flex;gap:20px;flex-shrink:0}
.status-bar b{color:#bbb}

#log{height:52px;overflow-y:auto;padding:6px 20px;font-family:monospace;font-size:.78rem;color:#6dba8d;line-height:1.75;flex-shrink:0;border-bottom:1px solid #2e2e2e}
.log-dim{color:#555}
.log-err{color:#f88}

.results{flex:1;overflow-y:auto}
.results-hdr{padding:8px 20px;font-size:.88rem;font-weight:600;color:#ccc;background:#111;position:sticky;top:0;z-index:1;border-bottom:1px solid #1e1e1e;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{background:#c00;color:#fff;font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:10px}
#res-search-wrap{margin-left:auto;display:flex;align-items:center;gap:6px}
#res-search{padding:5px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #3a3a3a;color:#eee;font-size:.8rem;width:200px;outline:none}
#res-search:focus{border-color:#c00}
#res-search-count{font-size:.75rem;color:#555;white-space:nowrap}

table{width:100%;border-collapse:collapse;font-size:.83rem}
th{background:#161616;color:#666;font-weight:600;text-align:left;padding:7px 16px;font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:40px}
td{padding:8px 16px;border-bottom:1px solid #1c1c1c;color:#ddd}
tr:hover td{background:#161616}
td a{color:#6ab0f5;text-decoration:none}
td a:hover{text-decoration:underline}
.tag{background:#c00;color:#fff;font-size:.65rem;font-weight:700;padding:1px 5px;border-radius:3px}
.no-res{padding:24px 20px;color:#555;font-size:.85rem}
</style>
</head>
<body>

<header>
  <h1>🎸 GC Used Inventory Tracker</h1>
  <span id="hdr-status">Loading…</span>
</header>

<div class="layout">

  <div class="left">
    <!-- Mode tabs -->
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-find" onclick="setMode('find')">Select Stores</button>
      <button class="mode-tab"        id="tab-favs" onclick="setMode('favs')">★ Favorites</button>
    </div>

    <div class="search-wrap" id="search-wrap">
      <input id="search" type="text" placeholder="Search stores…" autocomplete="off">
      <div class="sel-btns">
        <button class="sel-btn" onclick="selectAll()">Select All</button>
        <button class="sel-btn" onclick="clearAll()">Clear All</button>
      </div>
    </div>

    <div id="store-list"></div>

    <div class="left-footer">
      <div id="sel-count">0 stores selected</div>
      <div class="btn-row">
        <button id="run-btn"      onclick="runTracker()" disabled>Run</button>
        <button id="baseline-btn" onclick="runBaseline()" title="Scan every GC store nationwide to build a complete baseline">🌐 Build Baseline</button>
      </div>
    </div>
  </div>

  <div class="right">
    <div class="status-bar">
      <span>Last run: <b id="s-last">—</b></span>
      <span>Known items: <b id="s-known">—</b></span>
      <span id="s-excel" style="display:none"><a style="color:#6ab0f5" href="/download/excel">Download Excel ↗</a></span>
    </div>
    <div id="log"><span class="log-dim">Ready — click Run to check your favorites, or switch to Select Stores to choose others.</span></div>
    <div class="results" id="res-panel" style="display:none">
      <div class="results-hdr">
        <span id="res-title">New Items</span>
        <span class="badge" id="res-badge"></span>
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
  document.getElementById('hdr-status').textContent = allStores.length + ' stores available';
}

async function loadState() {
  const r = await fetch('/api/state');
  const s = await r.json();
  document.getElementById('s-last').textContent  = s.last_run ? s.last_run.replace('T',' ').slice(0,16) : 'Never';
  document.getElementById('s-known').textContent = s.known_items.toLocaleString();
  if (s.excel_exists) document.getElementById('s-excel').style.display = 'inline';
  if (s.is_first_run) {
    appendLog('💡 New device — select stores and click Run to see items added in the last 96 hrs (window extends automatically if you\'ve been away longer).', 'log-dim');
  }
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

// ── Render store list ─────────────────────────────────────────────────────────
function renderList() {
  const el    = document.getElementById('store-list');
  const q     = document.getElementById('search').value.toLowerCase();
  let stores;
  if (mode === 'favs') {
    stores = favorites.length ? favorites : null;
  } else {
    stores = allStores;
  }

  if (!stores) {
    el.innerHTML = '<div class="empty-msg">No favorites yet.<br>Click ★ next to any store to add it.</div>';
    updateCount(); return;
  }

  const filtered = mode === 'find' && q ? stores.filter(s => s.toLowerCase().includes(q)) : stores;

  el.innerHTML = '';
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/\W/g,'_');
    // Pre-check stores that are favorites so Run works immediately on load
    const autoCheck = isFav;
    div.innerHTML =
      `<input type="checkbox" id="${id}" value="${name}"${autoCheck ? ' checked' : ''}>` +
      `<label for="${id}">${name}</label>` +
      `<button class="fav-btn ${isFav?'active':''}" title="${isFav?'Remove from':'Add to'} favorites" onclick="toggleFav(event,'${name.replace(/'/g,"\\'")}',this)">★</button>`;
    div.querySelector('input').addEventListener('change', updateCount);
    el.appendChild(div);
  });

  // In "all" mode check all by default if list was just rendered
  updateCount();
}

function filterList() { if (mode==='find') renderList(); }

// ── Favorites ─────────────────────────────────────────────────────────────────
async function toggleFav(e, name, btn) {
  e.stopPropagation();
  const adding  = !favorites.includes(name);
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

// ── Run ───────────────────────────────────────────────────────────────────────
async function runTracker() {
  const stores = getSelected();
  if (!stores.length) return;
  await startRun({stores}, false);
}

async function runBaseline() {
  if (!confirm('This will scan every Guitar Center store nationwide (~300 stores) to build a complete inventory baseline.\\n\\nIt will take 30–60 minutes. Continue?')) return;
  await startRun({stores:[], baseline:true}, true);
}

async function startRun(payload, isBaseline) {
  running = true; updateCount();
  document.getElementById('res-panel').style.display = 'none';
  document.getElementById('log').innerHTML = '';

  const resp = await fetch('/api/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  if (!resp.ok) {
    const e = await resp.json();
    appendLog(e.error, 'log-err');
    running = false; updateCount(); return;
  }

  const es = new EventSource('/api/progress');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false; updateCount(); loadState(); showResults(msg, isBaseline);
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
  appendLog(`\\n✓ Done — ${msg.scanned.toLocaleString()} items scanned, ${n} new.`, 'log-dim');

  if (isBaseline && n === 0) {
    document.getElementById('res-title').textContent = 'Baseline Complete';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML =
      `<div class="no-res">Full inventory baseline saved (${msg.scanned.toLocaleString()} items). Run again any time to see what's new!</div>`;
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
    return;
  }

  let html = `<table><thead><tr><th></th><th>Item</th><th>Price</th><th>Store</th><th>Link</th></tr></thead><tbody>`;
  (msg.new_items || []).forEach(item => {
    html += `<tr><td><span class="tag">NEW</span></td><td>${item.name}</td><td>${item.price}</td><td>${item.store}</td><td><a href="${item.url}" target="_blank">View ↗</a></td></tr>`;
  });
  (msg.all_items || []).forEach(item => {
    html += `<tr><td></td><td>${item.name}</td><td>${item.price}</td><td>${item.store}</td><td><a href="${item.url}" target="_blank">View ↗</a></td></tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('res-body').innerHTML = html;
}

// ── Results filter ────────────────────────────────────────────────────────────
function filterResults() {
  const q     = document.getElementById('res-search').value.toLowerCase().trim();
  const rows  = document.querySelectorAll('#res-body tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const text  = row.textContent.toLowerCase();
    const show  = !q || text.includes(q);
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  const countEl = document.getElementById('res-search-count');
  countEl.textContent = q ? `${visible} of ${rows.length}` : '';
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
    if not STORES_CACHE.exists():
        print("Building store list…")
        refresh_store_list()
    url = f"http://localhost:{PORT}"
    print(f"\n  Guitar Center Tracker is running!")
    print(f"  Open: {url}")
    print(f"  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
