# GC Tracker — Handoff Document
*Last updated: 2026-04-30 · Current version: v2.7.4 · Status: deployed on Railway · Branch: main*

---

## What This Is

A Flask web app deployed on Railway that tracks Guitar Center used inventory. Users create accounts (username + password) and see items flagged NEW since their last scan. Watch list, want list, and favorites sync across all devices via server-side user accounts. Supports a Craigslist (CL) search tab, watchlist, want list (keyword alerts), server-side browsing, and mobile card/list views.

---

## Deployment

| Thing | Detail |
|---|---|
| Platform | Railway (`cboehmig-lab/gc-tracker` GitHub repo) |
| Auto-deploy | Every push to `main` triggers a Railway redeploy |
| Branch protection | Force-pushes blocked on `main` (set in GitHub → Settings → Branches) |
| Data dir | Set via `DATA_DIR` env var on Railway — **must be a persistent volume**, not ephemeral storage |
| Python entry | `gc_tracker_app.py` (single file, ~6900+ lines) |

### Critical env vars
| Var | Purpose |
|---|---|
| `DATA_DIR` | Where data files live — set to mounted volume path |
| `SECRET_KEY` | Flask session secret — **must be set** for user login sessions to survive restarts |
| `RESET_PASSWORD` | Password for admin pages and `/api/reset` — default `Beatle909!` |
| `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` | GC inventory API |

### Git push auth
- The default `origin` remote may authenticate as the wrong GitHub account (`charlesboehmig-boop` instead of `cboehmig-lab`)
- Fix: `git remote set-url origin https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git`
- Or push explicitly: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`
- Uses a GitHub PAT (personal access token) for auth — generate at https://github.com/settings/tokens with `repo` scope

---

## Key Files (on the server at DATA_DIR)

| File | Purpose |
|---|---|
| `gc_users.db` | SQLite user database — accounts, passwords (hashed), per-user watch/want/favorites/scan state |
| `gc_category_cache.json` | Main inventory store — all scanned items keyed by SKU |
| `gc_last_scan.txt` | Global last-scan timestamp (ISO, UTC) — fallback for guest users |
| `gc_device_log.jsonl` | Unique device access log (append-only, one line per device per day) |
| `gc_state.json` | Legacy — no longer used |
| `gc_invalid_stores.json` | Blocklisted store names (auto-managed) |

---

## Architecture

### User accounts
- SQLite database (`gc_users.db`) with two tables:
  - `users`: id, username (unique), email (optional, for password recovery), password_hash (bcrypt via Werkzeug), created_at
  - `user_data`: user_id, watchlist (JSON), keywords (JSON), favorites (JSON), last_run (ISO), new_ids (JSON)
- `_init_user_db()` runs at startup and creates tables if missing
- Passwords are hashed with PBKDF2/SHA-256 — never stored in plaintext, not visible even to admin
- Sessions use Flask's signed cookie (`SECRET_KEY`) — permanent sessions survive browser restarts
- Guest mode: users can dismiss the welcome modal and use the app without an account (data stays in localStorage only)

### Auth endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/register` | POST | Create account — username, password, optional email |
| `/api/login` | POST | Login with username + password |
| `/api/logout` | POST | Clear session |
| `/api/me` | GET | Check session state; returns username + full user data |
| `/api/sync` | POST | Save watchlist/keywords/favorites/last_run/new_ids to user record |

### Scan flow
1. Client POST `/api/run` with `{stores, baseline}`
2. If user is logged in, server uses their stored `last_run` as the comparison window (not localStorage)
3. Server acquires `_lock` (rejects concurrent scans with 409)
4. Scan runs in background thread, streams progress via SSE (`/api/progress?run_id=...`)
5. On completion, client receives `{new_ids, scan_time}` via SSE, syncs to server via `/api/sync`
6. New users get a baseline scan triggered automatically on first login (no prior `last_run`)

### NEW detection (per-user) — date_listed only (v2.6.3+)
- Each **user account** has its own `last_run` and `new_ids` stored server-side
- Guest users fall back to localStorage
- On scan: item is NEW if `date_listed > prev_scan_time` (Algolia's `creationDate` in ISO UTC)
- Simplified from hybrid first_seen approach (v2.5.0–v2.6.2) — was pulling in too many false positives
- `new_ids` are sent from client to server on every browse request; server marks `isNew` accordingly
- Each scan replaces `_newIds` entirely — 0 new = clean slate

### Data sync flow
- On page load: `/api/me` is called; if logged in, server data is merged with localStorage
- Merge strategy: union for watchlist/keywords/favorites; most-recent-wins for last_run/new_ids
- Auto-sync triggers: watchlist toggle, keyword add/remove, favorites toggle, scan completion
- Sync is debounced (600ms) for rapid changes like keyword edits

### Algolia date fields (investigated 2026-04-28)
- Only two top-level date fields exist: `startDate` (Unix seconds, can be 0) and `creationDate` (Unix ms, always set)
- `startDate = 0` is the norm on fresh used items; fallback to `creationDate / 1000` kicks in
- **Critical finding**: `creationDate` reflects when GC internally creates the listing record, NOT when the item becomes searchable in Algolia. There is a **6-12+ hour indexing pipeline delay**.
- Our comparison is apples-to-apples: both sides are `YYYY-MM-DDTHH:MM:SSZ` UTC strings
- GC lists items in real-time (item-by-item), peak volume 1–4am UTC = store closing times across US time zones

### Browse flow (server-side pagination)
- Client POST `/api/browse` with filters, sort, page, `new_ids`, `user_last_scan`
- Server reads `gc_category_cache.json`, applies filters, returns 50 items/page
- `user_last_scan` gates visibility: items with `first_seen > user_last_scan` are hidden

### Concurrency
- Single global `threading.Lock()` — only one scan at a time (the cache is shared, parallel scans would race)
- **Scan subscription** (`feature/scan-subscription`, not yet merged): second user hitting Run joins the in-progress scan instead of getting a 409 — they see "Scan already in progress — joining…" and receive the same SSE stream. Fan-out implemented via `_run_queues: dict[str, list[queue.Queue]]`; each subscriber gets their own queue. `_broadcast(run_id, msg)` sends to all of them.
- `_stop_event` is global — if someone hits Stop it cancels the scan for all subscribers
- `/admin/clear-lock` force-releases a stuck lock without a restart

---

## Auth

- Site shows a **Welcome modal** on page load for non-logged-in users (login / create account / use as guest)
- Logged-in state shown in header: username + green sync dot + "Sign out" button
- "Sign in" button in header also opens auth modal for users who dismissed the welcome screen
- `login_required` decorator is still a pass-through — all pages are accessible; auth is opt-in
- `/api/reset` requires `RESET_PASSWORD` in POST body
- `/admin/*` pages require `?pw=RESET_PASSWORD` query param

---

## Admin Pages

| URL | Purpose |
|---|---|
| `/admin/users?pw=Beatle909!` | User account list — username, email, join date, last scan, watch/want/fav counts |
| `/admin/devices?pw=Beatle909!` | Device access log — unique devices, platform, first/last seen, daily active chart |
| `/admin/clear-lock?pw=Beatle909!` | Force-release stuck scan lock without restarting |
| `/admin/listing-patterns?pw=Beatle909!` | GC listing timestamp analysis |
| `/admin/build-coords?pw=Beatle909!` | Re-geocode store locations |

---

## Mobile

- `_isMobile()` = `window.innerWidth <= 820px`
- On mobile, `_renderServerTable()` dispatches to either `_renderMobileCards()` (default) or `_renderMobileList()`
- View preference saved in `localStorage` key `gt_mobile_view` (`'cards'` or `'list'`)
- Sidebars auto-collapse on mobile load; desktop layout is unchanged
- Pinch zoom / rotation disabled via `<meta name="viewport" content="...,maximum-scale=1,user-scalable=no">`

### Mobile bottom action bar (v2.7.x)
Fixed bar at bottom of screen (`position:fixed; bottom:0; z-index:150; height:56px`). Four buttons left-to-right:

| Button | Action |
|---|---|
| **Scan For New** (▶) | Starts/stops scan — same as desktop "Scan For New" button |
| **Filters** (🎛) | Opens filter bottom sheet; red dot when any filter is active |
| **Stores** (🏪) | Opens store-picker bottom sheet |
| **Sign In / Sign Out** (👤) | Opens auth modal if guest; signs out if logged in |

### Bottom sheet pattern
Both the store panel (`.left`) and filter panel (`#gc-filter-collapsible`) use the same pattern:
- `position:fixed; bottom:calc(56px + env(safe-area-inset-bottom)); transform:translateY(150%)` when closed
- `.sheet-open { transform:translateY(0) }` slides up
- Shared backdrop (`#store-sheet-backdrop / .store-sheet-backdrop`) dims everything behind — `z-index:119`
- `_closeAllSheets()` removes `sheet-open` from both panels and clears backdrop
- **Stacking context gotcha**: `.right { z-index:auto }` on mobile — if it were `z-index:1` it would trap fixed children below the backdrop

### Filter sheet — 3-zone layout (v2.7.4)
```
┌─────────────────────────────────┐
│  ── handle ──                   │  ← .filter-sheet-header (flex-shrink:0)
│  Filters            [Clear All] │
├─────────────────────────────────┤
│  ↓ Price Drops  ★ Watch  🎯 Want│  ← .filter-scroll-body (flex:1, overflow-y:auto)
│  [All Brands ▾]                 │
│  [All Conditions ▾]             │
│  [All Categories ▾]             │
│  [Search results…]              │
├─────────────────────────────────┤
│         Show Results            │  ← .filter-done-btn (flex-shrink:0, pinned)
└─────────────────────────────────┘
```
- Sheet container is `overflow:hidden` — only the scroll-body scrolls
- On desktop: `.filter-sheet-header{display:none}`, `.filter-scroll-body{display:contents}`, `.filter-chip-row{display:contents}` — filter controls flow inline as before
- **No auto-close on chip taps** — Watch List, Price Drops, Want List do NOT call `_closeAllSheets()`; user hits "Show Results" explicitly
- **Mutually exclusive dropdowns** — `_closeAllDropdowns()` runs before any dropdown opens; calls `_closeBrandDropdown / _closeCondDropdown / _closeCatDropdown / _closeSubDropdown` (note: subcat close fn is `_closeSubDropdown`, NOT `_closeSubcatDropdown`)

### Paginator (mobile)
`position:fixed!important; bottom:calc(56px + env(safe-area-inset-bottom))` — `position:sticky` is unreliable in iOS Safari overflow:auto containers.

### Sign-out state clearing
`_authLogout()` clears all JS state explicitly: `_authUser`, `_watchlist`, `_keywords`, `_newIds`, `_lastRunISO`, `favorites`, plus corresponding localStorage keys. Prevents stale want/watch lists persisting after sign-out.

---

## Version Numbering

**Semantic versioning: `MAJOR.MINOR.PATCH`**

- `PATCH` bump — bug fixes (most pushes)
- `MINOR` bump — new feature ships (e.g. `2.6.0`)
- `MAJOR` bump — Chuck says so (e.g. `3.0.0`)

Update both places when bumping:
1. `APP_VERSION = "x.y.z"` near the bottom of `gc_tracker_app.py`
2. The `v{x.y.z}` span in the `<h1>` tag in the HTML

---

## Device Tracking

- Every device gets a `gt_device_id` UUID cookie (2-year lifetime, set via `@app.after_request`)
- First visit each day appends one line to `gc_device_log.jsonl`
- **Admin dashboard**: `/admin/devices?pw=Beatle909!`

---

## Want List Architecture

- Keywords stored in `window._keywords` (array of strings), synced server-side for logged-in users
- `renderKeywordList()` renders them as a **sorted A–Z pill cloud**; each pill has an embedded ✕ button
- `removeKeywordAt(i)` — index-based removal (safe for keywords with quotes or special chars)
- **Toolbar count badge** (`#wl-count-badge`): shows "X want list items available" in bold green. Clickable — triggers `searchWantList()`.
- `_watchFilterActive` — filters current store browse to watched (★) items
- `_wantListSearchActive` — filters browse to keyword matches
- `filter_want_list_only: true` in `/api/browse` body triggers keyword filtering server-side

---

## Filter Buttons Behavior

- **Price Drops**: stackable filter — layers on top of watch list, want list, or any other active filter. Does NOT reset other filters when toggled.
- **Watch List**: stackable filter — toggles `_watchFilterActive`, layers on other filters.
- **Want List**: exclusive search — activates global search mode (`_globalSearchActive`), resets brand/condition/category filters and deactivates watch list and price drops. This is because want list searches across all stores.

---

## Key JS State (client-side)

| Variable | Where | Purpose |
|---|---|---|
| `window._authUser` | JS var | `null` = guest, `{username}` = logged in |
| `window._lastRunISO` | localStorage + server | Last scan time (ISO UTC) — server is authoritative for logged-in users |
| `window._newIds` | localStorage + server | Set of SKUs flagged NEW on last scan |
| `window._watchlist` | localStorage + server | Watched items `{id: {name, store, ...}}` |
| `window._keywords` | localStorage + server | Want list keywords |
| `favorites` | localStorage + server | Favorited store names |
| `gt_mobile_view` | localStorage | `'cards'` or `'list'` |
| `_browseMode` | JS var | `'server'` or `'local'` |
| `_srvLoading` | JS var | Prevents concurrent browse fetches |
| `_skipBrowse` | JS var | Set after scan to prevent overwrite of scan results |
| `_watchFilterActive` | JS var | True when filtering browse to watchlist items |
| `_wantListSearchActive` | JS var | True when filtering browse to want list keywords |
| `_priceDropFilterActive` | JS var | True when filtering to price-dropped items |
| `_syncTimer` | JS var | Debounce timer for `_syncToServer()` |

---

## Common Debugging

**User can't log in**
- Check username spelling (case-insensitive) and password
- If they forgot their password and have no email on file, account must be reset manually via SQLite on the server

**Items not showing as NEW after login on a new device**
- Check `/api/me` response — `data.last_run` should be set
- If `last_run` is empty on server, the scan will use `gc_last_scan.txt` as fallback
- Guest users: `window._lastRunISO` comes from localStorage — may be empty if Safari cleared it

**Sync not working**
- Open browser DevTools → Network tab → look for `/api/sync` calls after watchlist/keyword changes
- If 401: session cookie expired — user needs to log in again
- `SECRET_KEY` env var must be set on Railway, or sessions reset on every deploy

**Sandbox git lock files**
- The Cowork sandbox sometimes leaves stale `.git/*.lock` files
- Fix: `rm ~/Desktop/gc_tracker/.git/index.lock 2>/dev/null; true` then retry
- The sandbox cannot push to GitHub (proxy 403) — always push from Mac terminal

**Scan hangs / 409 forever**
- Hit `/admin/clear-lock?pw=Beatle909!` to force-release without a Railway restart

**No data after redeploy**
- Likely ephemeral storage — Railway wipes files on redeploy unless a volume is mounted
- Fix: attach a Railway volume, set `DATA_DIR` to its mount path
- `gc_users.db` lives in `DATA_DIR` — **must be on persistent volume** or all accounts are lost on redeploy

**Nominatim geocoding failures**
- Must use a clean `requests.Session()` (not the shared `_http` session which has browser-impersonation headers)
- Fixed in v2.1.3: `nom_session = http.Session()` with clean User-Agent and Accept headers

---

## Recent Changes (v2.6.3 → v2.7.4)

### v2.7.4
- **Filter sheet 3-zone layout**: `#gc-filter-collapsible` restructured into sticky header / scrollable body / pinned "Show Results" button. Container is `overflow:hidden`; only the scroll body scrolls.
- **Mutually exclusive dropdowns**: `_closeAllDropdowns()` helper closes all four dropdowns before opening any one. Prevents visual stacking glitches when switching between Brand/Condition/Category/Subcategory.
- **No auto-close on chip taps**: `toggleWatchFilter`, `togglePriceDropFilter`, `searchWantList` no longer call `_closeAllSheets()` — sheet stays open so multiple filters can be stacked before hitting "Show Results".
- **Desktop passthrough**: `.filter-sheet-header{display:none}`, `.filter-scroll-body / .filter-chip-row{display:contents}` — desktop filter bar unchanged.
- **Bug**: `_closeAllDropdowns` initially called `_closeSubcatDropdown()` (non-existent) instead of `_closeSubDropdown()` — fixed in same version, broke all dropdowns briefly.

### v2.7.3
- **Filter sheet as bottom sheet**: `#gc-filter-collapsible` now uses the same `position:fixed; transform:translateY(150%)` pattern as the store panel. Opened via Filters button in bottom bar.
- **Sign In / Sign Out as 4th bottom bar button**: `_mobileAuthToggle()` opens auth modal if guest, signs out if logged in. Icon updates to 🔓 when logged in.
- **Bottom bar button order**: Scan For New | Filters | Stores | Sign In
- **Sign-out clears client state**: `_authLogout()` now explicitly clears `_authUser`, `_watchlist`, `_keywords`, `_newIds`, `_lastRunISO`, `favorites`, and matching localStorage keys.
- **Filter sheet backdrop**: shares the same `#store-sheet-backdrop` — `_closeAllSheets()` handles both panels.
- **Stacking context fix**: `.right { z-index:auto }` on mobile prevents filter sheet from being trapped below backdrop.

### v2.7.2
- **Store panel as bottom sheet**: `.left` panel uses `position:fixed; bottom:calc(56px + env(safe-area-inset-bottom)); transform:translateY(150%)`. `.sheet-open` slides it up. Backdrop at z-index:119.
- **Paginator fixed**: `position:fixed!important` instead of `position:sticky` — sticky unreliable in iOS Safari overflow containers. `#res-body` gets `padding-bottom` to compensate.
- **Results background**: dark grey (`#161616`) instead of pure black.
- **"Scan For New"**: renamed from "Check Now" on both desktop and mobile.

### v2.7.1
- **Mobile bottom action bar**: `position:fixed; bottom:0; z-index:150; height:56px`. Three buttons: Scan For New | Filters | Stores.
- **`100dvh` body height**: prevents iOS Safari tab bar from pushing content off-screen.
- **Pinch zoom disabled**: `maximum-scale=1,user-scalable=no` in viewport meta.

### v2.6.3
- **NEW detection simplified**: dropped hybrid `first_seen` rule. Item is NEW only if `date_listed > prev_scan_time`. Reduces false positives from the Algolia indexing delay window.

---

## Recent Changes (v2.6.1 → v2.6.2)

### v2.6.2
- **Dropdown counts: italic + comma-formatted**: All filter dropdowns (brand, condition, category, subcategory) now show item counts in italic, non-bold, with comma separators (e.g. `1,234`). CSS class `.bcount` updated; JS uses `.toLocaleString()`.
- **Dev footer links**: Fixed bottom-right footer (desktop only — hidden on mobile via `@media(max-width:820px)`) with PayPal (`paypal.me/smurfco`), Venmo, and `animalsintrees.com` links. Dim by default, brightens on hover. Inline SVG icons.
- **Session lifetime**: Flask `PERMANENT_SESSION_LIFETIME` set to 31 days (default) — users stay logged in across browser restarts without re-authenticating.
- **HANDOFF platform fix**: corrected platform from Render → Railway throughout.

### feature/scan-subscription (branch, not yet merged)
- **Scan fan-out**: second user hitting Run now joins the in-progress scan instead of getting a 409 error. Shows "Scan already in progress — joining…" and receives the live SSE stream.
- `_run_queues` changed from `dict[str, Queue]` to `dict[str, list[Queue]]` — each subscriber gets their own queue.
- `_broadcast(run_id, msg)` replaces direct `run_q.put(msg)` in `_run()`.
- `_subscribe_to_run(run_id)` / `_cleanup_subscriber(run_id, q)` manage per-subscriber lifecycle.
- `_current_run_id` / `_current_run_time` globals track the active scan so late joiners get the right timestamps.
- `api_run()` returns `{"status": "joined", "run_id": ..., "run_time": ...}` instead of 409 when locked.
- `api_progress()` uses `_subscribe_to_run()` instead of `_get_run_queue()` — cleans up just the individual subscriber on disconnect.

---

## Recent Changes (v2.6.0 → v2.6.1)

### v2.6.1
- **Username-only auth**: removed email as a required field. Login is username + password. Email is now optional (stored if provided, for future password recovery).
- **Welcome modal on page load**: replaces the old "Run Initial Scan" first-run popup. Shows login / create account / use as guest with a note about cross-device sync. "Use as guest" dismisses modal and continues with localStorage-only mode.
- **Auto baseline scan**: new accounts (or logins with no prior scan history) automatically trigger a nationwide baseline scan — no button to click.
- **"Initial scan complete"** log message for baseline scans instead of "0 new this scan".
- **Admin `/admin/users` page**: shows all accounts with username, email, join date, last scan, watch/want/favorites counts.
- **Optional email field**: registration forms include an email field with note: "Optional — only used for password recovery. Never shared."
- **Modal text colors**: all helper text in the welcome modal brought up to `#aaa` minimum — nothing darker than the explainer text.

### v2.6.0
- **User accounts**: SQLite-backed username + password auth. Watch list, want list, favorites, last_run, and new_ids all sync server-side per user. Logged-in users get consistent NEW detection across all devices.
- **Server-side last_run**: `api_run` uses the user's stored `last_run` when logged in, so scan windows are consistent regardless of which device ran the last scan.
- **`/api/sync`**: client syncs all personal state to server after any change (debounced 600ms). Called immediately after scan completion.
- **`/api/me`**: called on every page load to restore session state silently.
- **Data merge on login**: server data merged with localStorage on login — union for sets, most-recent-wins for timestamps.
- **Header auth widget**: username + green sync dot when logged in; "Sign in" button when not.

---

## Recent Changes (v2.5.0 → v2.5.2)

### v2.5.2
- **Desktop table layout overhaul**: Switched from `table-layout:fixed` to `table-layout:auto`. Data columns auto-size to content. Item column capped at 420px max-width.

### v2.5.0
- **NEW detection rewritten — hybrid date + first_seen**: dual detection — item is NEW if `date_listed > prev_scan_time` OR (`first_seen > prev_scan_time` AND `date_listed > prev_scan_time - 24h`).
- **Price drop display reordered**: New price shows first (`↓ $84.99 $139.99`).
- **Price column widened**: 90px → 140px.

### v2.5.1
- **Price Drops is now a stackable filter**.
- **Price column left-justified**.
- **Debug logging removed**.

---

## Recent Changes (v2.3.3 → v2.4.8)

*(See previous HANDOFF for full details on v2.3.x–v2.4.x)*

Key highlights:
- v2.4.8: SSE disconnect NEW-detection fix; filters preserved on store change
- v2.4.7: Contextual (faceted) filter counts
- v2.4.2: Fixed-layout desktop table
- v2.3.x: Various NEW detection, geocoding, and UI fixes
