# GC Tracker — Handoff Document
*Last updated: 2026-05-11 · Current version: v2.10.11 · Status: deployed on Railway · Branch: main*

> **Search syntax note (v2.10.5+):** `filter_strict: true` now means **fuzzy/contains mode** (old behavior). The default (`filter_strict: false`) is whole-word matching. This is the opposite of what v2.10.4 sent — saved searches stored before v2.10.5 that had `filter_strict: true` will behave differently (they'll use fuzzy mode, not strict, which is the safer fallback).

---

## What This Is

A Flask web app deployed on Railway that tracks Guitar Center used inventory. Users create accounts (username + password) and see items flagged NEW since their last scan. Watch list, want list, and favorites sync across all devices via server-side user accounts. A separate standalone `/cl` page provides Craigslist used gear search.

---

## Deployment

| Thing | Detail |
|---|---|
| Platform | Railway (`cboehmig-lab/gc-tracker` GitHub repo) |
| Auto-deploy | Every push to `main` triggers a Railway redeploy |
| Branch protection | Force-pushes blocked on `main` (GitHub → Settings → Branches) |
| Data dir | Set via `DATA_DIR` env var on Railway — **must be a persistent volume** |
| Python entry | `gc_tracker_app.py` (single file, ~8000+ lines) |

### Critical env vars
| Var | Purpose |
|---|---|
| `DATA_DIR` | Where data files live — set to mounted volume path |
| `SECRET_KEY` | Flask session secret — **must be set** for sessions to survive restarts |
| `APP_PASSWORD` | Password for admin pages and `/api/reset` — **must be set**; no default |
| `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` | GC inventory API |

### Git push auth
- The default `origin` remote may authenticate as the wrong GitHub account (`charlesboehmig-boop` instead of `cboehmig-lab`)
- Always push explicitly: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`
- The Cowork sandbox **cannot push to GitHub** (proxy 403) — always push from Mac terminal
- Sandbox sometimes leaves stale `.git/*.lock` files — fix: `rm ~/Desktop/gc_tracker/.git/index.lock 2>/dev/null; true`

---

## Key Files (on the server at DATA_DIR)

| File | Purpose |
|---|---|
| `gc_users.db` | SQLite user database — accounts, passwords (hashed), per-user watch/want/favorites/scan state |
| `gc_category_cache.json` | Main inventory store — all scanned items keyed by SKU |
| `gc_last_scan.txt` | Global last-scan timestamp (ISO, UTC) — fallback for guest users |
| `gc_device_log.jsonl` | Unique device access log (append-only, one line per device per day) |
| `gc_invalid_stores.json` | Blocklisted store names (auto-managed) |

---

## Architecture

### User accounts
- SQLite database (`gc_users.db`) with two tables:
  - `users`: id, username (unique), email (optional), password_hash (PBKDF2/SHA-256 via Werkzeug), created_at
  - `user_data`: user_id, watchlist (JSON), keywords (JSON), favorites (JSON), last_run (ISO), new_ids (JSON)
- `_init_user_db()` runs at startup and creates tables if missing
- Sessions use Flask's signed cookie (`SECRET_KEY`) — permanent sessions survive browser restarts
- Guest mode: users can dismiss the welcome modal and use the app without an account (data stays in localStorage only)
- `login_required` decorator is a no-op pass-through — all pages publicly accessible; auth is opt-in

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
2. If user is logged in, server uses their stored `last_run` as the comparison window
3. Server acquires `_lock` (rejects concurrent scans with 409)
4. Scan runs in background thread, streams progress via SSE (`/api/progress?run_id=...`)
5. On completion, client receives `{new_ids, scan_time}` via SSE, syncs to server via `/api/sync`

### NEW detection (per-user, v2.6.3+)
- Item is NEW if `date_listed > prev_scan_time` (Algolia's `creationDate` in ISO UTC)
- Each user account has its own `last_run` and `new_ids` stored server-side
- Guest users fall back to localStorage
- Each scan replaces `_newIds` entirely — 0 new = clean slate

### Browse flow (server-side pagination)
- Client POST `/api/browse` with filters, sort, page, `new_ids`, `user_last_scan`
- Server reads `gc_category_cache.json`, applies filters, returns 50 items/page
- Filters: `filter_q` (keyword), `filter_brands`, `filter_conditions`, `filter_categories`, `filter_subcategories`, `filter_watched`, `filter_price_drop_only`

### Algolia date fields
- Only two top-level date fields: `startDate` (Unix seconds, can be 0) and `creationDate` (Unix ms, always set)
- `startDate = 0` is the norm on fresh used items; fallback to `creationDate / 1000` kicks in
- **Critical**: `creationDate` has a 6-12+ hour indexing pipeline delay vs when item becomes searchable
- GC lists items in real-time, peak volume 1–4am UTC = store closing times across US time zones

---

## Admin Pages

| URL | Purpose |
|---|---|
| `/admin/users?pw=<APP_PASSWORD>` | User account list |
| `/admin/devices?pw=<APP_PASSWORD>` | Device access log |
| `/admin/clear-lock?pw=<APP_PASSWORD>` | Force-release stuck scan lock |
| `/admin/listing-patterns?pw=<APP_PASSWORD>` | GC listing timestamp analysis |
| `/admin/build-coords?pw=<APP_PASSWORD>` | Re-geocode store locations |

---

## Routes

| Route | Purpose |
|---|---|
| `/` | Main GC Used Inventory Tracker app |
| `/cl` | Standalone Craigslist used gear search page |
| `/download/excel` | Download inventory as Excel file |
| `/api/*` | All API endpoints |
| `/admin/*` | Admin pages (password protected) |

---

## Mobile (v2.7.x → v2.8.0)

- `_isMobile()` = `window.innerWidth <= 820px`
- On mobile, `_renderServerTable()` dispatches to `_renderMobileCards()` (default) or `_renderMobileList()`
- View preference saved in `localStorage` key `gt_mobile_view` (`'cards'` or `'list'`)
- `100dvh` body height prevents iOS Safari tab bar from pushing content off-screen
- Pinch zoom / rotation disabled via `<meta name="viewport" content="...,maximum-scale=1,user-scalable=no">` **AND** via JS event listeners (iOS 10+ ignores `user-scalable=no` in the meta tag):
  ```javascript
  document.addEventListener('gesturestart', e => e.preventDefault(), { passive: false });
  document.addEventListener('touchmove', e => { if (e.touches && e.touches.length > 1) e.preventDefault(); }, { passive: false });
  ```

### Mobile title bar (v2.8.0)
Dark maroon gradient header at very top of screen (desktop header is hidden on mobile):
```css
background: linear-gradient(135deg, #4a0000, #7a0000);
color: #ffcccc;
```
3-column CSS grid layout: `grid-template-columns: 1fr auto 1fr`
- Left column: empty spacer (keeps title truly centered)
- Center: "GC Used Inventory Tracker" title
- Right: version number (e.g. `v2.8.0`)

When bumping version, update the `<span class="mtb-ver">` in the `.mobile-title-bar` div AND `APP_VERSION` AND the `<h1>` version span.

### Mobile bottom action bar
Fixed bar at bottom of screen (`position:fixed; bottom:0; z-index:150; height:56px`). Four buttons:

| Button | Action |
|---|---|
| **Scan For New** (▶) | Starts/stops scan |
| **Search & Filter** (🔍) | Opens filter bottom sheet; red dot when any filter is active |
| **Stores** (🏪) | Opens store-picker bottom sheet |
| **Sign In / Sign Out** (👤) | Opens auth modal if guest; signs out if logged in |

`.mobile-bottom-bar{display:none}` globally — only shown inside `@media(max-width:820px)`.

### Bottom sheet pattern
Both the store panel (`.left` / `#gc-left`) and filter panel (`#gc-filter-collapsible`) use the same pattern:
- `position:fixed; bottom:calc(56px + env(safe-area-inset-bottom)); transform:translateY(150%)` when closed
- `.sheet-open { transform:translateY(0) }` slides up
- Shared backdrop (`#store-sheet-backdrop`) dims behind — `z-index:119`
- `_closeAllSheets()` removes `sheet-open` from both panels and clears backdrop
- **Stacking context**: `.right { z-index:auto }` on mobile — if it were `z-index:1` it would trap fixed children

### Swipe-to-dismiss (v2.8.0)
`_initSwipeDismiss(sheetEl, closeFn, scrollBodySel)` wires touch events on each sheet:
- Only triggers when swiping **downward** AND scroll body is at the top (`scrollTop === 0`)
- Temporarily disables CSS transition during drag so sheet follows finger
- If `dy > 90px` on release: calls `closeFn()` (dismiss); otherwise snaps back to `translateY(0)`
- Called in DOMContentLoaded for both `#gc-left` (scroll body: `#store-list`) and `#gc-filter-collapsible` (scroll body: `.filter-scroll-body`)

### Filter sheet layout
```
┌─────────────────────────────────┐
│  ── handle ──                   │  ← .filter-sheet-header (flex-shrink:0, mobile only)
│  Filters            [Clear All] │
├─────────────────────────────────┤
│  [🔍 Search all stores…]        │  ← .filter-scroll-body (flex:1, overflow-y:auto)
│  [Brand accordion ▾]            │
│  [Condition accordion ▾]        │
│  (Category/Subcat, hidden until │
│   relevant selection is made)   │
│  ── desktop-only dropdowns ──   │
├─────────────────────────────────┤
│         Show Results            │  ← .filter-done-btn (flex-shrink:0, pinned)
└─────────────────────────────────┘
```

On desktop: `.filter-sheet-header{display:none}`, `.filter-scroll-body{display:contents}`, `.filter-accordion{display:none}` — desktop uses dropdown menus, not accordions.

**Accordion pattern (mobile)**: `_accToggle(id)`, `_accRenderBrand()`, `_accRenderCond()`, `_accRenderCat()`, `_accRenderSub()`. Items use `data-val` attributes + event delegation (NOT inline onclick strings — those cause Python string escape issues with regex in triple-quoted strings).

**Keyword search (`#res-search`)**: global server-side search, fires `_globalKeywordSearch()` on input with 400ms debounce. Sends `filter_q` to `/api/browse`. Has 🔍 icon on mobile (`.res-search-icon`).

**CRITICAL Python escape gotcha**: In Python triple-quoted strings, `\\/` → `\/` in JS output. Never use regex patterns in inline onclick attributes built by string concatenation inside `HTML_TEMPLATE`. Use `data-*` attributes + `addEventListener` instead.

### Quick-filter chip bar (v2.8.0)
Always visible above results (`.quick-filter-bar`):
- **↓ Price Drops**: stackable filter, `togglePriceDropFilter()`
- **★ Watch List**: stackable filter, `toggleWatchFilter()`
- **🎯 Want List**: exclusive global search across all stores, `searchWantList()`
- **☰ / ⊞ View toggle** (`#view-toggle-chip`): mobile only (`display:none` globally, `display:inline-flex!important` in mobile media). Calls `toggleMobileView()`. Shows ☰ (current: cards → switch to list) or ⊞ (current: list → switch to cards). Red highlight when list mode active.

Price Drops, Watch List, and Want List do NOT auto-close the filter sheet — user must tap "Show Results".

### Edit Want List (v2.9.0)
`#search-wl-link` lives in `.quick-filter-bar`, immediately after the Want List chip. On **desktop** it is always visible (`_isMobile()` check in `_updateWantListCount()`). On **mobile** it only shows when `_wantListSearchActive === true` (want list filter active) to conserve horizontal space. `_updateWantListCount()` enforces this. Called from `searchWantList()` (show) and `_resetWantListLink()` (hide).

### Paginator (v2.8.0)
Mobile paginator is now `position:static!important` — lives inline at the bottom of the results list. Only visible when user scrolls to the bottom. Desktop paginator remains `position:sticky; bottom:0`.

`#res-body` padding-bottom on mobile: `calc(64px + env(safe-area-inset-bottom))` — just enough to clear the bottom action bar.

### Price drops in mobile views (v2.8.0)
Both `_renderMobileCards()` and `_renderMobileList()` now show struck-through original price when `item.price_drop > 0`:
```javascript
const priceHtml = hasDrop
  ? `<span class="price-drop-val">` +
    (item.list_price_raw > item.price_raw ? `<span class="price-orig">$${item.list_price_raw.toFixed(2)}</span> ` : '') +
    `↓ ${item.price}</span>`
  : item.price;
```
CSS classes `.price-drop-val` (green) and `.price-orig` (grey, line-through) already existed.

---

## CL Page (`/cl`)

Standalone Craigslist used gear search. No link from the main GC tracker — navigate directly.

- **Template**: `CL_TEMPLATE` string, served by `@app.route("/cl")`
- **Auth**: shares the same Flask session as the main app. Shows login modal if not signed in.
- **Accent color**: indigo/purple (`#a5b4fc`, `#818cf8`, `#c7d2fe`) to visually distinguish from the red GC tracker
- **Backend**: reuses `/api/cl-search` endpoint (which scrapes Craigslist via `_cl_search()`)
- **Keywords**: reads `window._keywords` from localStorage (shared with main app's want list)
- **Watch list**: saves to `_lsSet('cl_watchlist', ...)` — same key as before
- **City favorites**: saved to `localStorage.getItem('cl_favs')`
- **Features**: city sidebar with search/favorites, search bar, sortable results table, watch list filter, want list search, city favorite stars

The CL panel stub still exists in the main `HTML_TEMPLATE` as `<div id="cl-panel" style="display:none">` — kept to avoid JS ReferenceErrors from the many existing JS references (`cl-left`, `cl-toggle-arrow`, etc.). Do NOT delete it.

---

## JS State (client-side, main app)

| Variable | Where | Purpose |
|---|---|---|
| `window._authUser` | JS var | `null` = guest, `{username}` = logged in |
| `window._lastRunISO` | localStorage + server | Last scan time (ISO UTC) |
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
| `_globalSearchActive` | JS var | True when a global search (all stores) is active |
| `_syncTimer` | JS var | Debounce timer for `_syncToServer()` |

---

## Version Numbering

**Semantic versioning: `MAJOR.MINOR.PATCH`**

When bumping version, update ALL FOUR of these:
1. `APP_VERSION = "x.y.z"` near the bottom of `gc_tracker_app.py`
2. `<h1>GC Used Inventory Tracker <span ...>vx.y.z</span></h1>` in the HTML
3. `<span class="mtb-ver">vx.y.z</span>` in the `.mobile-title-bar` div
4. `CL_TEMPLATE` doesn't have a version display — skip

---

## Common Debugging

**Table not loading on page load**
- Most likely a JS crash in `browseCache()` or a related function referencing a removed DOM element
- Check browser console for TypeError on `getElementById(...).value`
- `browseCache()` previously crashed on `#global-search` (removed in v2.8.0) — fixed

**Bottom bar showing on desktop**
- `.mobile-bottom-bar` must have `display:none` in the global (non-media-query) CSS section
- The mobile `@media` block sets `display:flex` — without the global default it shows everywhere

**Git push fails with 403**
- Wrong GitHub account — use explicit URL: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`

**Sandbox git lock files**
- `rm ~/Desktop/gc_tracker/.git/index.lock 2>/dev/null; true` then retry commit/push

**Scan hangs / 409 forever**
- Hit `/admin/clear-lock?pw=<APP_PASSWORD>` to force-release without a Railway restart

**No data after redeploy**
- Railway wipes ephemeral storage on redeploy — attach a volume, set `DATA_DIR` to its mount path

**User can't log in**
- If forgotten password with no email on file: reset manually via SQLite on the server

**Sync not working**
- Check `/api/sync` calls in DevTools Network tab
- If 401: session cookie expired — user must log in again
- `SECRET_KEY` env var must be set on Railway, or sessions reset on every deploy

---

## Recent Changes (v2.7.4 → v2.8.0)

### v2.8.0 — Major UI overhaul

**Rebranding**
- Page title, header h1, welcome modal, and mobile title bar all updated to "GC Used Inventory Tracker" (was "Gear Tracker")
- Desktop header `<h1>` loses guitar emoji

**Tab/CL removal from main page**
- Removed `.app-tabs` div and tab buttons entirely (desktop + mobile)
- Removed `<div class="app-panel active" id="gc-panel">` wrapper — `.layout` is now the direct child of body
- CL panel collapsed to hidden stub `<div id="cl-panel" style="display:none">` — JS references to `cl-left`, `cl-toggle-arrow`, etc. still exist and would ReferenceError if stub were deleted

**Global search bar removed from status bar**
- `#global-search-wrap` removed from status bar HTML
- `globalSearch()` rewritten to use `#res-search` (the filter-sheet search input) — dead code but no longer crashes
- `clearGlobalSearch()` updated to clear `#res-search` instead of `#global-search`
- **Bug**: `browseCache()` crashed on `document.getElementById('global-search').value` (null) — lines removed

**Filter sheet search**
- `#res-search-wrap` moved to TOP of `.filter-scroll-body`
- Placeholder changed to "Search all stores…"
- 🔍 magnifying glass icon (`.res-search-icon`) added, visible only on mobile inside `.filter-scroll-body`
- Search fires `_globalKeywordSearch()` → 400ms debounce → `_fetchBrowsePage(1)` with `filter_q`

**Quick-filter chip bar**
- New `.quick-filter-bar` always visible above results
- Price Drops, Watch List, Want List chips — one-tap, not inside filter sheet
- View toggle chip `#view-toggle-chip` (☰/⊞) — mobile only, calls `toggleMobileView()`
- "Edit Want List" link removed from chip bar → moved to `.results-hdr`, shown only when want list active

**Mobile accordion filters**
- `#acc-brand`, `#acc-cond`, `#acc-cat`, `#acc-sub` replace dropdowns inside the mobile filter sheet
- `_accToggle(id)`, `_accRenderBrand/Cond/Cat/Sub()`, `_accBuildItems()` functions
- Items use `data-val` + event delegation (no inline onclick regex — avoids Python escape bug)

**Mobile title bar**
- `<div class="mobile-title-bar">` added above `.layout`
- 3-column CSS grid: empty spacer | centered title | right-aligned version
- Dark maroon gradient: `linear-gradient(135deg, #4a0000, #7a0000)`, text `#ffcccc`
- `display:none` globally; `display:grid` inside mobile `@media` block

**Swipe-to-dismiss**
- `_initSwipeDismiss(sheetEl, closeFn, scrollBodySel)` attached to both sheets in DOMContentLoaded
- Triggers only on downward swipe when scroll body is at top; 90px threshold for dismiss

**Mobile paginator**
- Changed from `position:fixed` (always visible above bottom bar) to `position:static` (inline at end of list)
- `#res-body` padding-bottom reduced from `calc(130px + ...)` to `calc(64px + ...)`

**Mobile price drops**
- `_renderMobileCards()` and `_renderMobileList()` now show struck-through original price for price-drop items

**Bottom bar desktop fix**
- Added `.mobile-bottom-bar{display:none}` to global CSS (before `@media` block) so it's hidden on desktop

**Standalone `/cl` page**
- New `@app.route("/cl")` serving `CL_TEMPLATE`
- Full CL search functionality: city sidebar, search, sortable results, watch list, want list, favorites
- Indigo/purple accent colors, shares Flask session + localStorage with main app
- No link from the main GC tracker to `/cl` — direct URL access only

---

## Recent Changes (v2.8.0 → v2.9.0)

### v2.9.0 — Strict search, per-filter clear, desktop layout

**Per-filter clear buttons**
- Each filter dropdown (Brand, Condition, Category, Subcategory) now has a small ✕ Clear button inside its panel header
- On mobile accordions: same clear buttons inside the accordion body header row
- Functions: `_clearBrandFilter()`, `_clearCondFilter()`, `_clearCatFilter()`, `_clearSubFilter()` — each resets the relevant state array, re-renders, and re-fetches
- The existing "Clear All" / "✕ Clear Filters" button remains unchanged

**Strict / whole-word search (= prefix convention)**

*Want list keywords* — per-keyword strict mode:
- Prefix a keyword with `=` to make it a whole-word-only match (e.g. `=Carr` matches "Carr" but not "Carruthers")
- No data model change — the `=` is stored as-is in the keywords array and syncs naturally
- **Server-side** (`_compile_keywords()`): detects `=` prefix, builds `re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)`, stored as `("word", pattern)`
- **Client-side** (`_itemMatchesKeyword()`): `text.toLowerCase().split(/\W+/).includes(kw.slice(1).toLowerCase())`
  - ⚠️ Uses **regex literal** `/\W+/` NOT a RegExp constructor — see Python escape gotcha below
- Chip rendering: strict chips are dark blue (`#0a3c6e` / `#93c5fd`) with a yellow `=` badge; normal chips remain green
- Keyword input help text: shows `=Carr` example for strict, `Wang Caster` for phrase match, `"exact phrase"` for quoted exact

*Main search bar* — global strict toggle:
- Small `≈` button (`#strict-search-btn`) next to the search input — click to toggle strict on/off (shows `=` when active)
- Sends `filter_strict: true` in the browse payload
- **Server-side** in `_apply_base()`: when `f_strict` is true and query isn't quoted, splits query into words and applies `\b...\b` regex to all searchable fields (name, brand, store, location, category, subcategory)

**Want list deletion sync fix**
- Changed merge strategy from union (additions only) to **server-wins**:
  ```javascript
  const mergedKw = sKw.length > 0 ? [...sKw].sort() : [...window._keywords].sort();
  ```
- Deletions on any device now propagate globally — server state overwrites local state on next `/api/me` sync

**Desktop single-row layout**
- `.quick-filter-bar` and `.results-hdr` (filter bar) are now wrapped in `<div id="results-top-bar">`
- Global CSS: `#results-top-bar{display:flex;flex-direction:row;align-items:stretch;flex-shrink:0}` — chips and filter bar sit side by side
- Mobile CSS: `#results-top-bar{display:contents}` — wrapper is invisible to the flex layout; its children flow as direct children of `#res-panel` exactly as before; **zero mobile change**
- `#search-wl-link` (Edit Want List) moved into `.quick-filter-bar` immediately after `#want-list-toggle`
- On desktop, Edit Want List is always visible; on mobile, only when want list filter is active

**CSS gotcha: `flex:1` + `display:contents`**
- `#results-top-bar .results-hdr{flex:1}` is correct on desktop (fills remaining horizontal space)
- On mobile with `display:contents`, `.results-hdr` becomes a column-flex child of `#res-panel` — `flex:1` then expands it to fill ALL remaining vertical space, creating a giant blank gap below the chips
- Fix: `#results-top-bar .results-hdr{flex:none}` in the mobile `@media` block
- Key insight: `display:contents` is visual-only — CSS parent selectors (`#results-top-bar .results-hdr`) still match because the DOM parent relationship is unchanged

**Mobile zoom re-locked**
- iOS 10+ ignores `user-scalable=no` in the viewport meta tag
- Fixed with two JS event listeners added in `DOMContentLoaded`:
  - `gesturestart` → `preventDefault()` (blocks pinch-to-zoom on iOS Safari)
  - `touchmove` with `touches.length > 1` → `preventDefault()` (blocks multi-touch zoom everywhere)
  - Both registered with `{ passive: false }` (required for `preventDefault()` to work on touch events)

---

## Recent Changes (v2.9.0 → v2.10.0)

### v2.10.0 — Saved Searches

**Feature overview**
- Logged-in users only (not guests). Save named combinations of filters + stores + search term for instant recall.
- Saves: `filter_q`, `filter_brands`, `filter_conditions`, `filter_categories`, `filter_subcategories`, `filter_strict`, `filter_price_drop_only`, `filter_watched`, and `_srvStores` (the selected stores)
- Syncs to server via `/api/sync` — persists across devices. Server wins on merge so deletions propagate everywhere.

**Data model**
- New `saved_searches TEXT DEFAULT '[]'` column in `user_data` SQLite table
- `_init_user_db()` adds the column via `ALTER TABLE` with try/except for existing databases (migration-safe)
- Schema per entry: `{id, name, filters: {...}, stores: [...], created_at}` — `id` = `"ss_" + Date.now()`
- `_get_user_data()`, `_set_user_data()`, `api_sync()` all updated to handle `saved_searches`

**New API endpoint**
- `POST /api/saved-search-counts` — takes `{"searches": [{filters, stores}, ...]}`, returns `{"counts": [n, ...]}`
- Loads `gc_category_cache.json` once, applies all filter combos, returns match counts in one batch
- Called when the Saved Searches dropdown opens; count badges (green when loaded) update in-place

**Chip button (`#saved-searches-btn`)**
- Lives in `.quick-filter-bar`, between ↓ Price Drops and ★ Watch List
- `#ss-wrap` wrapper is hidden when not logged in; `_setAuthUI()` controls visibility
- `#ss-dropdown` is a sibling of `#results-top-bar` (NOT inside `.quick-filter-bar`) — this is intentional: iOS Safari has a bug where `position:fixed` children inside `-webkit-overflow-scrolling:touch` containers don't behave correctly. Moving it outside the scroll container fixes mobile.
- JS computes `top`/`left` from `getBoundingClientRect()` when opening; clamps to right edge of viewport
- Outside-click listener on `document` closes it (with null-guard on the button ref)

**Dropdown contents**
- Header row: "SAVED SEARCHES" label + "Clear" button (`data-ss-clear`) — clears active filters via `clearFilters()`, does NOT delete saved searches
- Each item: name (white), description summary (`_ssDescription()` — shows query, brands, conditions, store count), match count badge, ✕ delete button
- Event delegation on `dd` handles all three: clear header, delete, apply — no inline onclick (avoids Python escape gotcha)
- Uses `data-ss-id` (apply search), `data-ss-del` (delete), `data-ss-clear` (clear filters) attributes
- `_ssEsc()` is a minimal HTML-escape helper for user-supplied strings

**Save Search button (`#save-search-btn`)**
- Lives inside `#filter-action-btns` wrapper in `.filter-scroll-body`
- On desktop: `#filter-action-btns` has `display:contents` (CSS) so buttons flow inline in the filter bar, next to ✕ Clear All
- On mobile: `#filter-action-btns` is set to `display:flex` by JS (`_updateSaveSearchBtn()`), showing both buttons side-by-side above the red Show Results bar
- Only shown when logged in AND at least one filter/search term is active
- `_updateSaveSearchBtn()` called from `_updateFilterDot()` AND end of `_fetchBrowsePage()` — fires on every filter change and every browse result
- Clicking opens `prompt()` for a name, pushes to `window._savedSearches`, syncs to server

**Clear All button (`#clear-filters-btn`)**
- Renamed from "✕ Clear Filters" to "✕ Clear All"
- Visibility condition expanded to include `filter_q`, `filter_strict`, `filter_price_drop_only`, `filter_watched` (previously only showed for dropdown filters)
- `clearFilters()` hides `#filter-action-btns` wrapper immediately, then re-fetches (which calls `_updateSaveSearchBtn()` to confirm)

**Delete a saved search**
- `_deleteSavedSearch(id)` — `confirm()` dialog before removing; syncs + re-renders dropdown
- `_clearAllSavedSearches()` exists but is not exposed in UI (kept for future use)

**Applying a saved search**
- `_applySavedSearch(id)` restores all filter state variables, updates button labels, accordion summaries, strict button, watch/price-drop chip states, store checkboxes via `renderList(new Set(savedStores))`, then `_fetchBrowsePage(1)`

**JS state**
- `window._savedSearches` — array of saved search objects
- Initialized to `[]` at startup; merged from server on login (server wins, like `keywords`)
- Reset to `[]` on logout

---

## ⚠️ Critical Python/JS Template Gotchas

**THE MOST DANGEROUS BUG IN THIS CODEBASE — READ THIS BEFORE WRITING ANY JS**

All JavaScript in this app lives inside Python triple-quoted strings. Python processes backslash escape sequences in those strings before they ever reach the browser. This has burned us repeatedly and is the reason v2.10.5–v2.10.8 all had to be reverted.

**Rule 1: `\\` in Python source → single `\` in browser output**
- `'\\\\'` in Python source → `'\\'` in browser → `\` character in JS string. Fine.
- `'\\'` in Python source → `'\'` in browser → **JS syntax error** (backslash escapes the closing quote)
- This means any JS string literal containing a lone backslash, like `'\\' + s[i]`, becomes `'\' + s[i]` in the browser — a parse error that kills all JS on the page.

**Rule 2: Regex literals with `\` near `/` are lethal**
- `/[.*+?^${}()|\[\]\\]/g` in Python source: the `\\` becomes `\`, so browser gets `/[.*+?^${}()|\[\]\]/g`
- That trailing `\` escapes the closing `/` — browser never finds the end of the regex — throws `SyntaxError: Invalid regular expression: missing /` — **kills all JS on the page**
- Symptom: page loads HTML, stores list empty, "Loading…" never resolves, console shows `SyntaxError: Invalid regular expression: missing /`

**Rule 3: Safe patterns to use instead**

When you need to build a regex from a user string (to escape special chars, etc.) and that code lives in the Python template:
- ✅ Use `new RegExp(...)` constructor — string arguments go through normal JS string rules, not regex literal parsing
- ✅ Use `String.fromCharCode(92)` instead of `'\\'` to get a backslash without Python eating it
- ✅ Use char code comparisons (`s.charCodeAt(i) === 92`) instead of `s[i] === '\\'`
- ✅ Use a lookup table approach: build the output character by character using only code points, no backslash literals
- ❌ Never use regex literals (`/pattern/`) inside the Python template if the pattern contains `\\` — Python will collapse it
- ❌ Never use `'\\'` to represent a backslash in a JS string — Python collapses it to `'\'` which is a JS syntax error

**Rule 4: Test in the browser before committing**
After any JS change, open the page, open the browser console, and confirm there are no SyntaxErrors before pushing. A working page is the only real test.

**Inline onclick strings with regex**
- Don't build regex patterns inside `onclick="..."` attributes via Python string concatenation
- Use `data-*` attributes + `addEventListener` instead (see accordion items)

---

## 🎯 Planned Next Features

Collecting user feedback from soft launch (guitar groups). Check back here after feedback is in.

---

## Recent Changes (v2.10.0 → v2.10.5)

### v2.10.1 — (internal patch, no user-facing change)

### v2.10.2 — Date-only new-item detection fix
- **Bug**: items with `date_listed = "2026-05-05"` (date-only, no time component) were never flagged NEW on the same day because `"2026-05-05" < "2026-05-05T08:00:00Z"` in string comparison
- **Fix**: `_norm_item_date(d)` — if `len(d) == 10` (date-only), appends `T23:59:59Z` before comparison
- Applied in the scan loop when building `new_ids_list`

### v2.10.3 — Keyword search false-positive fix
- **Bug**: want-list keyword "Allen" was matching store names like "Allentown" and "McAllen" because the search included store name and location fields
- **Fix**: keyword matching now searches only `name + brand` fields, not store/location/category

### v2.10.4 — `=` prefix whole-word search, UI polish
- `=Allen` in the search bar forces whole-word match (`\bAllen\b`) — won't match "Allentown"
- Save Search / Clear All buttons narrowed (removed `flex:1` from inline HTML; `flex:1` added to mobile CSS only)
- Want list modal help text updated with real examples (replacing "Wangcaster")

### v2.10.5 — Unified search syntax (wildcard, whole-word default, AND/comma, ⓘ popup), filter bar nowrap

**The root cause of v2.10.5–v2.10.8 failures was solved:** `_escapeRegex` contained `'\\' + s[i]` which Python collapses to `'\' + s[i]` in the browser — a JS syntax error that killed all JS on page load. Similarly `'\\b'` became `'\b'` (backspace char, not word boundary). Fixed using `String.fromCharCode(92)` throughout.

**Search syntax (server + client, fully mirrored):**
- `_compile_query(query_str, fuzzy=False)` — unified Python function used by both `/api/browse` filter and want-list keyword compilation
- Plain term (e.g. `Allen`) → whole-word match by default (`\bAllen\b`) — won't match "Allentown" or "McAllen"
- `"Jam Pedals"` → exact phrase (contains match, case-insensitive)
- `OD*` → wildcard (`*` = any characters); `*drive*` = contains "drive"
- `Thorpy, Dane*` → comma = AND; each part supports full syntax independently
- `≈` button (`#strict-search-btn`) → when active, switches server to **fuzzy/contains mode** (old pre-v2.10.5 behavior). `filter_strict: true` now means fuzzy, not strict — semantics inverted from v2.10.4. Default (off) = whole-word.

**JS functions (safe backslash patterns):**
- `_escapeRegex(s)` — uses `var bs = String.fromCharCode(92)` for backslash; `specials = bs + '.^$*+?()[]{}|'`
- `_parseQueryTerms(queryStr, fuzzy)` — uses `var wb = String.fromCharCode(92) + 'b'` for `\b` word boundary passed to `new RegExp()`
- `_matchesAllTerms()` — handles `exact`, `contains`, and `regex`/`word` modes
- `_itemMatchesKeyword()` — strips leading `=` prefix (legacy v2.10.4 strict marker, now redundant)

**ⓘ search help popup:**
- `#search-info-btn` button next to `#res-search` — already in HTML since attempted v2.10.5
- `#search-info-popover` div — shows syntax examples on click, closes on outside click
- `_toggleSearchInfo(e)` — toggles `.open` class; global `document` click listener closes it
- CSS: `#search-info-popover{position:absolute;top:calc(100%+6px);right:0;z-index:200;...}`; mobile: `right:auto;left:0`

**`=` prefix on want-list keywords:**
- Server: `_kw_compiled` strips `=` via `.lstrip('=')` before calling `_compile_query` (whole-word is now default so prefix is redundant)
- Client: `_itemMatchesKeyword` strips `=` via `.replace(/^=/, '')` — safe (no backslash in this regex literal)

**Filter bar nowrap (desktop):**
- `.results-hdr{flex-wrap:nowrap}` — prevents Save/Clear buttons from wrapping to a new line when all 4 filter dropdowns are visible
- `.cat-sel{flex-shrink:1;min-width:70px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}` — dropdowns compress with ellipsis instead of pushing
- `#res-search-wrap{flex-shrink:1;min-width:90px}` + `#res-search{width:150px;min-width:0}` — search box compresses too
- `#res-search-count{flex-shrink:0}` — "N of M" count never gets squeezed
- Mobile `@media` block still sets `flex-wrap:wrap` — no mobile change

---

## Recent Changes (v2.10.5 → v2.10.10)

### v2.10.6 — Admin privacy, store UX fixes
- Email column removed from `/admin/users` entirely (no hash, no raw — just username/dates/counts)
- Favorites button in store panel now toggles label: "★ Favorites" → "All Stores" when active; toggling back reselects ALL stores (not just favorites)
- Favorites ★ button moved to left of checkbox in store rows (was on right, looked attached to item name)

### v2.10.7 — Security hardening, NEW badge column move
- Login rate limiting: 10 failed attempts per IP per 5-min rolling window → 429
- Session cookie flags: `HttpOnly`, `SameSite=Lax`, `Secure` (auto-enabled on Railway via `RAILWAY_ENVIRONMENT`)
- Security headers on every response: `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`
- `Beatle909!` removed from all Python defaults and JS — hardcoded password entirely gone from codebase
- JS `confirmReset()` no longer hardcodes old password — sends to server for validation
- All admin `os.environ.get()` calls made fail-closed: if `APP_PASSWORD` env var unset, access denied (not open)
- NEW badge column moved to sit immediately left of item title (was leftmost column, looked attached to store names)

### v2.10.8 — Additional security fixes
- SSRF fix in `/api/cl-search`: city parameter now whitelisted against `_CL_CITIES` before use in URLs
- `/api/import-data`: now requires `APP_PASSWORD` in request body (was completely unprotected — could wipe inventory cache for all users)
- `/api/clear-blocklist`: same password protection added

### v2.10.9 — Pre-launch UX + registration rate limiting
- Registration rate limiting added (same 10/5-min window as login)
- "Only used for password recovery" claim removed from email field (no recovery flow exists) — replaced with honest copy
- `Beatle909!` removed from HANDOFF.md; admin URL examples now show `<APP_PASSWORD>` placeholder
- Forgot-password note added then removed — no automated reset exists, no contact mechanism wired up

### v2.10.11 — Multi-store filter, hover contrast, mobile UX, desktop thumbnails, anchor NEW detection

**Store filter — preserve selections across filter changes**
- Added `let _selectedStores = new Set()` as the authoritative in-memory selection store
- `renderList()` no longer accepts a `preserveChecked` argument — always reads `_selectedStores`
- Checkbox `change` events update `_selectedStores` directly; `selectAll()`/`clearAll()` do the same
- `getSelected()` and `_getCheckedStores()` both read from `_selectedStores`, not the DOM
- Effect: typing "dallas" after checking Plano TX preserves the Plano selection — filter is navigation only

**Table row hover contrast (desktop)**
- Row hover color changed from `#1d1d1d` (near-black) to `#2e1e1e` (visible dark maroon)

**Mobile condition in subtitle**
- Card view `.card-meta` now reads: `Condition · Store · Date` (e.g. "Excellent · South Austin · 5/11/26")
- List view (`.compact-row`) gains a new `.compact-row-sub` line with the same format
- `.compact-row-left` changed from `align-items:center` to `flex-direction:column` to accommodate two lines

**Mobile sort by price**
- New `.mobile-sort-row` in the filter sheet (hidden on desktop) with four buttons: Newest / Oldest / Price ↑ / Price ↓
- `_updateMobileSortBtns()` keeps active state in sync
- Event delegation wired in DOMContentLoaded; updates `_srvSortField`, `_srvSortDir`, re-fetches

**Desktop thumbnail view toggle**
- `⊞ Thumbnails` button in `.quick-filter-bar` (hidden on mobile via `@media`)
- `_buildRowHtml` adds `<img class="row-thumb">` inside `.thumb-name-cell` wrapper in the item name cell
- Toggle adds `.thumb-mode` class to `#res-body`; CSS shows `.row-thumb` only in that mode
- State persisted in `localStorage` key `gt_desktop_thumb_view`; applied on DOMContentLoaded

**Anchor-based NEW item detection**
- Before the scan cache is overwritten, the server computes `anchor_date` = max `date_listed` across all cached items
- After scanning, threshold = `max(anchor_date, prev_scan_time)` — the most restrictive (most recent) of the two
- Fixes the "0 new items / table reordered" bug where Algolia's 6-12h indexing pipeline delay caused items to appear in search results with `date_listed` older than the last scan time, silently pushing existing items down the date-sorted table without being flagged NEW

### v2.10.10 — Store panel UX, env var fix
- Select All / Clear All merged into one toggle button (`#sel-all-btn`): shows "Select All" normally, switches to "Clear All" when all visible stores checked. Logic in `toggleSelectAll()`, label updated in `updateCount()`
- Store list scroll resets to top (`el.scrollTop = 0`) in `renderList()` — fixes mid-list positioning after favorites toggle on mobile
- `APP_PASSWORD` env var: code was reading `RESET_PASSWORD` but Railway var is named `APP_PASSWORD`. Fixed throughout. Top-level constant `APP_PASSWORD = (os.environ.get("APP_PASSWORD") or "").strip()` defined once at startup, used everywhere.

### Key env vars (Railway)
| Var | Status |
|---|---|
| `SECRET_KEY` | ✅ Set (long random string) |
| `APP_PASSWORD` | ✅ Set (custom password) |
| `DATA_DIR` | ✅ Points to persistent volume |
