# GC Tracker — Handoff Document
*Last updated: 2026-04-17 · Current version: v2.2.7 · Status: CLEAN — no in-flight work*

---

## What This Is

A Flask web app deployed on Railway that tracks Guitar Center used inventory. Users select stores, run a scan, and see items flagged NEW since their last scan. Supports a Craigslist (CL) search tab, watchlist, want list (keyword alerts), server-side browsing, and mobile card/list views.

---

## Deployment

| Thing | Detail |
|---|---|
| Platform | Railway (`cboehmig-lab/gc-tracker` GitHub repo) |
| Auto-deploy | Every push to `main` triggers a Railway redeploy |
| Branch protection | Force-pushes blocked on `main` (set in GitHub → Settings → Branches) |
| Data dir | Set via `DATA_DIR` env var on Railway — **must be a persistent volume**, not ephemeral storage |
| Python entry | `gc_tracker_app.py` (single file, ~6300+ lines) |

### Critical env vars
| Var | Purpose |
|---|---|
| `DATA_DIR` | Where data files live — set to mounted volume path |
| `SECRET_KEY` | Flask session secret |
| `APP_PASSWORD` | Site-wide login (currently bypassed — see Auth section) |
| `RESET_PASSWORD` | Password for `/api/reset` and `/admin/devices` — default `Beatle909!` |
| `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` | GC inventory API |

---

## Key Files (on the server at DATA_DIR)

| File | Purpose |
|---|---|
| `gc_category_cache.json` | Main inventory store — all scanned items keyed by SKU |
| `gc_last_scan.txt` | Global last-scan timestamp (ISO, UTC) — fallback for new devices |
| `gc_device_log.jsonl` | Unique device access log (append-only, one line per device per day) |
| `gc_state.json` | Legacy — no longer used by advanced app |
| `gc_invalid_stores.json` | Blocklisted store names (auto-managed) |

---

## Architecture

### Scan flow
1. Client POST `/api/run` with `{stores, baseline, device_last_run}`
2. Server acquires `_lock` (rejects concurrent scans with 409)
3. Scan runs in background thread, streams progress via SSE (`/api/progress?run_id=...`)
4. On completion, server returns `{new_ids, scan_time}` — client saves to localStorage

### NEW detection (per-device)
- Each device has its own `last_run` and `new_ids` in **localStorage**
- On scan: server compares each item's `date_listed` (Algolia `startDate`, UTC ISO) against `device_last_run` sent from client
- Items where `date_listed > device_last_run` → NEW for that device
- Phone scan **does not** affect desktop's NEW tags — fully independent per localStorage
- `new_ids` are sent from client to server on every browse request; server marks `isNew` accordingly
- **NEW tags are preserved on 0-new scans**: if fresh scan finds 0 new items, existing `_newIds` are kept (not cleared)

### Algolia date fields (investigated 2026-04-15)
- Only two top-level date fields exist: `startDate` (Unix seconds, can be 0) and `creationDate` (Unix ms, always set)
- `startDate = 0` is common on fresh used items; fallback to `creationDate / 1000` kicks in
- Our comparison is apples-to-apples: both sides are `YYYY-MM-DDTHH:MM:SSZ` UTC strings
- GC lists items in real-time (item-by-item), peak volume 1–4am UTC = store closing times across US time zones
- No "went live" field exists separately — `startDate`/`creationDate` is the best available signal

### Browse flow (server-side pagination)
- Client POST `/api/browse` with filters, sort, page, `new_ids`, `user_last_scan`
- Server reads `gc_category_cache.json`, applies filters, returns 50 items/page
- `user_last_scan` gates visibility: items with `first_seen > user_last_scan` are hidden (not yet "seen" by this device)

### Concurrency
- Single global `threading.Lock()` — only one scan at a time
- Second user hitting Run gets HTTP 409 → friendly UI message: "Another scan is already in progress — try again in a moment"
- `_stop_event` is global — if someone hits Stop it cancels whoever is scanning

---

## Auth

- Site is **open** — `login_required` decorator is a pass-through (no password to enter the site)
- `/api/reset` requires `RESET_PASSWORD` in POST body
- `/admin/devices` requires `?pw=RESET_PASSWORD` query param
- `/admin/listing-patterns` requires `?pw=RESET_PASSWORD` — shows GC listing timestamp analysis
- `/login` and `/logout` routes exist but are inert

---

## Mobile

- `_isMobile()` = `window.innerWidth <= 820px`
- On mobile, `_renderServerTable()` dispatches to either `_renderMobileCards()` (default) or `_renderMobileList()`
- View preference saved in `localStorage` key `gt_mobile_view` (`'cards'` or `'list'`)
- Toggle button (⊞/☰) lives in the status bar next to "Check Now"
- Sidebars auto-collapse on mobile load
- Desktop layout is unchanged

---

## Version Numbering

**Semantic versioning: `MAJOR.MINOR.PATCH`**

- `PATCH` bump — bug fixes (most pushes)
- `MINOR` bump — new feature ships (e.g. `2.1.0`)
- `MAJOR` bump — Chuck says so (e.g. `3.0.0`)

Update both places when bumping:
1. `APP_VERSION = "x.y.z"` near the bottom of `gc_tracker_app.py`
2. The `v{x.y.z}` span in the `<h1>` tag in the HTML (~line 3468)

---

## Device Tracking

- Every device gets a `gt_device_id` UUID cookie (2-year lifetime, set via `@app.after_request`)
- First visit each day appends one line to `gc_device_log.jsonl`:
  ```json
  {"date":"2026-04-15","time":"14:32:11Z","device_id":"abc-123...","ua":"Mozilla/5.0...","ip":"98.1.2.3"}
  ```
- **Admin dashboard**: `https://your-app.railway.app/admin/devices?pw=Beatle909!`
  - Shows unique device count, platform guess, first/last seen, days active, daily active chart

---

## Want List Architecture

- Keywords stored in `localStorage` key `keywords` as `window._keywords` (array of strings)
- `renderKeywordList()` renders them as a **sorted A–Z pill cloud** in the modal; each pill has an embedded ✕ button
- `removeKeywordAt(i)` — index-based removal (safe for keywords with quotes or special chars)
- **Toolbar count badge** (`#wl-count-badge`): shows "X want list items available" in bold green between the 🎯 Want List button and All Brands dropdown. Clickable — triggers `searchWantList()`. Fetched in background via `_updateWantListCount()` after each page-1 browse.
- `_watchFilterActive` — filters current store browse to watched (★) items; separate from want list keywords
- `_wantListSearchActive` — filters browse to keyword matches; uses current `_srvStores` (not truly nationwide)
- `filter_want_list_only: true` in `/api/browse` body triggers keyword filtering server-side

---

## Key JS State (client-side)

| Variable | Where | Purpose |
|---|---|---|
| `window._lastRunISO` | localStorage `last_run` | This device's last scan time (ISO UTC) |
| `window._newIds` | localStorage `new_ids` | Set of SKUs flagged NEW on last scan |
| `window._watchlist` | localStorage `watchlist` | Watched items `{id: {name, store, ...}}` |
| `window._keywords` | localStorage `keywords` | Want list keywords |
| `favorites` | localStorage `favorites` | Favorited store names |
| `gt_mobile_view` | localStorage | `'cards'` or `'list'` |
| `_browseMode` | JS var | `'server'` or `'local'` |
| `_srvLoading` | JS var | Prevents concurrent browse fetches |
| `_skipBrowse` | JS var | Set after scan to prevent overwrite of scan results |
| `_watchFilterActive` | JS var | True when filtering browse to watchlist items |
| `_wantListSearchActive` | JS var | True when filtering browse to want list keywords |
| `_wlCountTimer` | JS var | Debounce timer for `_updateWantListCount()` |

---

## Common Debugging

**Items not showing as NEW**
- Check `date_listed` and `run_time` are both `YYYY-MM-DDTHH:MM:SSZ` (no microseconds) — confirmed correct
- Check `window._newIds` in browser console — should be a Set of SKU strings
- Check `window._lastRunISO` — should be the ISO time of last scan on this device
- NEW tags now preserved on 0-new scans (fixed v2.1.5) — if 0 new found, old tags stay

**Sandbox git lock files**
- The Cowork sandbox sometimes leaves stale `.git/*.lock` files after commits
- Fix: `rm ~/Desktop/gc_tracker/.git/index.lock ~/Desktop/gc_tracker/.git/HEAD.lock ~/Desktop/gc_tracker/.git/objects/maintenance.lock 2>/dev/null; true` then retry
- The sandbox cannot push to GitHub (proxy 403) — always push from Mac terminal

**Scan hangs / 409 forever**
- Something crashed while holding `_lock` without releasing it
- Hit `/admin/clear-lock?pw=Beatle909!` to force-release without a Railway restart
- As a last resort, restart the Railway service to reset the process

**No data after redeploy**
- Likely ephemeral storage — Railway wipes files on redeploy unless a volume is mounted
- Fix: attach a Railway volume, set `DATA_DIR` to its mount path

**Nominatim geocoding failures**
- Must use a clean `requests.Session()` (not the shared `_http` session which has browser-impersonation headers)
- Fixed in v2.1.3: `nom_session = http.Session()` with clean User-Agent and Accept headers

---

## Recent Changes (v2.0.1 → v2.1.9)

### v2.1.3
- Nominatim geocoding fixed: was failing 298/298 because `_http` session carried `Sec-Fetch-*` headers that Nominatim rejected. Now uses a clean `nom_session`.

### v2.1.4
- `_srvLoading` guard added before `_fetchBrowsePage(1)` in large-scan `showResults` path (was getting stuck)
- Auto-build done handler: restores saved ZIP and sort preference after `_loadStoreCoords` completes

### v2.1.5
- Want list keyword X buttons: switched from `removeKeyword(kw)` (broke on quotes) to `removeKeywordAt(i)` (index-based, safe for all chars)
- NEW tags fix: `showResults` previously cleared `_newIds` even on 0-new scans. Now only replaces when `freshNewCount > 0`; preserves existing tags otherwise. Scan log shows carry-over note.
- Small-scan path: uses `effectiveNewIds` (checks if `_newIds` is a Set before using)

### v2.1.6
- `/admin/listing-patterns` endpoint: password-protected page showing GC listing timestamp analysis (by day/hour/minute, clustering signals, 40 most recent)
- `analyze_listings.py`: standalone CLI script to fetch ~2400 items from Algolia and analyze timestamp patterns

### v2.1.7
- Want list toolbar: count badge (`#wl-count-badge`) added between 🎯 Want List button and All Brands dropdown
- Badge fetches count in background via `_updateWantListCount()`, shows "X want list items available"
- Results header when want list filter active now shows "Want List — X items found"

### v2.1.8 (may not be pushed as separate commit — rolled into v2.1.9)
- "Filter to Want List" link hidden; count badge made bold + clickable (calls `searchWantList()`)
- `.badge:empty { display:none }` CSS to prevent empty badge rendering as artifact

### v2.1.9
- Want list modal: keywords now display as A–Z sorted pill cloud (flex-wrap), each pill has embedded ✕
- `res-badge` permanently hidden (`display:none!important`) — count badge in toolbar is the new NEW signal display
- `renderKeywordList()` fully rewritten: sorted copy with original indices preserved for safe `removeKeywordAt(i)` calls

### v2.2.x series — UI polish + admin tooling

### v2.2.4
- `.log-dim` color changed from `#555` (invisible dark grey) to `#6dba8d` (green) — all processing box text now readable
- Done message shortened: removed "X items scanned," — now reads `✓ Done — 0 new this scan (850 still marked NEW from previous scan).`
- `/admin/clear-lock?pw=…` endpoint added — force-releases stuck `_lock` without Railway restart
- 409 UI message updated to mention the clear-lock URL

### v2.2.5
- **Store geocoding rewritten** to use Algolia's `storeName` field (e.g. `"South Austin, TX"`) as the Nominatim query instead of `"Guitar Center {store}"`. Fixes distance sort missing multi-store-city locations (South Austin, North Austin, West LA, Long Island, etc.).
- `_build_store_coords` now: (1) pulls 1 Algolia hit per store to get storeName, (2) geocodes storeName via Nominatim, (3) for stores with no items (dead/closed), tries `"{store}, {state}"` as last-ditch. Each coords entry gets a `source` string for auditing.
- `force=True` flag on `_build_store_coords` re-geocodes everything even if cached. Exposed via `/api/build-store-coords` body param and a "Force re-geocode all" checkbox on `/admin/build-coords`.
- `_admin_task_page()` helper extended with `options_html` and `extra_body_js` params for per-page customisation (checkboxes etc).
- Per-store Algolia errors now logged. Progress messages more detailed.

### v2.2.7
- **Want List button/link swapped**: 🎯 Want List button now filters to want list (toggles green when active, same pattern as Watch List and Price Drops). "Edit Want List" text link next to it opens the keyword editor modal. Match count shows as grey `(N matches)` next to the link. All reset paths updated to clear the want-list-toggle active state.

### v2.2.6
- **NEW detection fixed**: logic is `date_listed > prev_scan_time → NEW`. `date_listed` comes from Algolia's `startDate` (seconds) or `creationDate/1000` (ms) for each used-item record — this IS the listing date for that specific used item. `prev_scan_time` = this device's last scan time from localStorage (`gt_last_run`). Both timestamps are UTC ISO strings so string comparison is valid and correct.
- Root cause of regression: an intermediate commit tried using `first_seen` (when our scanner first found the item) instead of `date_listed`. This caused old inventory to be flagged NEW if it was first-scanned recently, and new inventory (like 4/17 items) to be missed. Reverted.
- Key insight: each used item is a distinct Algolia record with its own `creationDate` = when it was listed as used. Not related to the product's catalog age.
