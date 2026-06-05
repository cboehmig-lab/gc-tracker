# GC Tracker — Handoff Document
*Last updated: 2026-05-22 · Current version: v2.12.20 · Status: deployed ✅ · Domain: gcgeartracker.com*

> **Search syntax note (v2.10.5+):** `filter_strict: true` now means **fuzzy/contains mode** (old behavior). The default (`filter_strict: false`) is whole-word matching. This is the opposite of what v2.10.4 sent — saved searches stored before v2.10.5 that had `filter_strict: true` will behave differently (they'll use fuzzy mode, not strict, which is the safer fallback).

---

## AI Behavior Rules

These rules apply to any AI assistant working on this project. Read them before making any changes.

- **Be blunt and skeptical.** Don't validate bad ideas to be nice. Push back when something is over-engineered for the team's scale (solo developer, ~61 users, hobby project).
- **Prefer simple, maintainable solutions.** Clever is a liability. If a fix requires understanding 3 layers of abstraction to maintain, it's the wrong fix.
- **Always think production-first.** Consider how a change behaves under real conditions: concurrent users, Railway restart wiping in-memory state, a scan running while an admin page loads, etc.
- **Always use full name + acronym** when first referencing AWS (Amazon Web Services) services or other technology acronyms (e.g. "Content Security Policy (CSP)", "Server-Sent Events (SSE)", "Google Cloud Platform (GCP)").
- **Git pushes must come from the Mac terminal.** The Cowork sandbox gets a proxy 403 on GitHub pushes.
- **Never write inline JS.** All JS lives in `static/gc.js`. Content Security Policy (CSP) blocks inline scripts.
- **No inline onclick attributes.** Use `data-*` attributes + `addEventListener`.

---

## What This Is

A Flask web app deployed on Railway that tracks Guitar Center used inventory. Users create accounts (username + password, or Google Sign-In) and see items flagged NEW since their last scan. Watch list, want list, and favorites sync across all devices via server-side user accounts. A separate standalone `/cl` page provides Craigslist used gear search.

---

## Deployment

| Thing | Detail |
|---|---|
| Domain | `gcgeartracker.com` (primary) · `gctracker.animalsintrees.com` redirects here |
| Platform | Railway (`cboehmig-lab/gc-tracker` GitHub repo) |
| Auto-deploy | Every push to `main` triggers a Railway redeploy |
| Branch protection | Force-pushes blocked on `main` (GitHub → Settings → Branches) |
| Data dir | Set via `DATA_DIR` env var on Railway — **must be a persistent volume** |
| Python entry | `gc_tracker_app.py` (single file, ~8000+ lines) |
| Static assets | `static/gc.css`, `static/gc.js`, `static/cl.css`, `static/cl.js` |

### Critical env vars
| Var | Purpose |
|---|---|
| `DATA_DIR` | Where data files live — set to mounted volume path |
| `SECRET_KEY` | Flask session secret — **must be set** for sessions to survive restarts |
| `APP_PASSWORD` | Password for admin pages and `/api/reset` — **must be set**; no default |
| `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` | GC inventory API |
| `GOOGLE_CLIENT_ID` | Google OAuth — from Google Cloud Console credentials |
| `GOOGLE_CLIENT_SECRET` | Google OAuth — from Google Cloud Console credentials |
| `ADMIN_EMAIL` | Email address that gets admin privileges when logged in via Google (e.g. `cboehmig@gmail.com`) |

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
  - `users`: id, username (unique), email (optional), password_hash (nullable — NULL for Google-only users), google_id (unique, nullable), created_at
  - `user_data`: user_id, watchlist (JSON), keywords (JSON), favorites (JSON), last_run (ISO), new_ids (JSON), saved_searches (JSON)
- `_init_user_db()` runs at startup and creates tables if missing; both `saved_searches` and `google_id` columns are added via `ALTER TABLE` for existing databases (migration-safe)
- Sessions use Flask's signed cookie (`SECRET_KEY`) — permanent sessions survive browser restarts
- Guest mode: users can dismiss the welcome modal and use the app without an account (data stays in localStorage only)
- `optional_user_context` decorator (renamed from `login_required` in v2.10.17) is a no-op pass-through — all user-facing pages are publicly accessible; auth is opt-in

### Google OAuth (v2.10.13+)
- Requires `authlib` pip package + `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` env vars
- `_GOOGLE_OAUTH_ENABLED` flag: True only if both env vars are set AND `authlib` is installed
- **ProxyFix**: enabled on Railway (`RAILWAY_ENVIRONMENT` is set) so `url_for(..., _external=True)` generates `https://` URLs
- **Auto-link**: if Google email matches an existing username/password account → link `google_id` and log in
- **New Google users**: username auto-generated from Google display name via `_gen_google_username()`; `password_hash = ""` (empty string, not NULL — satisfies `NOT NULL` DB constraint and naturally fails `check_password_hash()`)
- **Existing password accounts**: unchanged; if a Google-only user tries password login, they get a helpful error
- **OAuth state**: stored server-side in `_oauth_pending` dict (not Flask session) — avoids Railway proxy CSRF state mismatch
- Frontend: Google buttons are hidden by default; an IIFE fetches `/api/auth/config` on load and reveals them if `google_oauth: true`; stores `window._googleOauthEnabled` globally
- `?next=` parameter stores where to redirect after OAuth callback (e.g. `/cl` from the CL page)
- **Re-triggering welcome modal**: navigate to `/?google_new=1` while logged in to re-open the username setup modal (useful if skipped on first login)
- **Username setup / account import** (`POST /api/setup-google-account`): lets new Google users set a username; if username is taken and correct password is provided, merges old account's data into the Google account and deletes the old account
- **Link banner** (v2.10.14): shows for password-only users who haven't linked Google; dismissible (stored in `localStorage: gt_google_link_dismissed`); calls `_maybeShowLinkBanner(googleLinked, hasEmail, googleOauthEnabled)` from both `_onAuthSuccess` and the DOMContentLoaded `/api/me` handler
- `/api/login` and `/api/register` responses now include `google_linked: bool`

### Auth endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/register` | POST | Create account — username, password, optional email |
| `/api/login` | POST | Login with username + password |
| `/api/logout` | POST | Clear session |
| `/api/me` | GET | Check session state; returns username + full user data |
| `/api/sync` | POST | Save watchlist/keywords/favorites/last_run/new_ids to user record |
| `/api/auth/google` | GET | Redirect to Google OAuth (accepts `?next=` param) |
| `/api/auth/google/callback` | GET | Handle Google OAuth callback; create/find/link user; redirect |
| `/api/auth/config` | GET | Returns `{"google_oauth": bool}` — used by frontend to show/hide Google buttons |
| `/api/setup-google-account` | POST | Set/change username after Google sign-in; optionally import (merge + delete) an existing password account |

### Scan flow
1. Client POST `/api/run` with `{stores, baseline}`
2. If user is logged in, server uses their stored `last_run` as the comparison window
3. Server acquires `_lock` (rejects concurrent scans with 409)
4. Scan runs in background thread, streams progress via SSE (`/api/progress?run_id=...`)
5. On completion, client receives `{new_ids, scan_time}` via SSE, syncs to server via `/api/sync`

### NEW detection (per-user, anchor-based — v2.12.2)
- Item is NEW if `date_listed > threshold`, where `threshold = _norm_anchor` (the max `date_listed` the user was actually exposed to at their last scan — their "top of table" high-water mark). Falls back to `prev_scan_time` only if no anchor exists yet (e.g. first scan after baseline).
- **Critical**: `prev_scan_time` (wall-clock UTC scan time) is NOT mixed into the threshold. Before v2.12.2, `threshold = max(anchor, prev_scan_time)`. Wall-clock timestamps like `"2026-05-18T08:00:00Z"` are lexicographically larger than date-only strings like `"2026-05-17"`, so `prev_scan_time` silently won the `max()` and blocked items that were genuinely new to the user (the "5 items between the known item and the 10 new ones, none flagged" bug).
- The new anchor persisted after each scan is `max(max_date_listed_this_scan, old_anchor)` — no wall-clock time included.
- Each user account has its own `last_run`, `last_anchor`, and `new_ids` stored server-side in `user_data`
- Guest users fall back to localStorage (`last_anchor` round-tripped in SSE `done` payload as `scan_anchor`)
- Each scan replaces `_newIds` entirely — 0 new = clean slate

### Browse flow (server-side pagination)
- Client POST `/api/browse` with filters, sort, page, `new_ids`, `user_last_scan`
- Server reads `gc_category_cache.json`, applies filters, returns 50 items/page
- Filters: `filter_q` (keyword), `filter_brands`, `filter_conditions`, `filter_categories`, `filter_subcategories`, `filter_watched`, `filter_price_drop_only`, `filter_price_min`, `filter_price_max` (numeric, applied against `price_raw`)

### Algolia date fields
- Only two top-level date fields: `startDate` (Unix seconds, can be 0) and `creationDate` (Unix ms, always set)
- `startDate = 0` is the norm on fresh used items; fallback to `creationDate / 1000` kicks in
- **Critical**: `creationDate` has a 6-12+ hour indexing pipeline delay vs when item becomes searchable
- GC lists items in real-time, peak volume 1–4am UTC = store closing times across US time zones

---

## Admin Pages

**Normal access**: log in to the main app via Google (must be the `ADMIN_EMAIL` account) → small "Admin" link appears in the page footer → click through to `/admin/users`.

**Break-glass**: navigate directly to `/admin/login` and enter `APP_PASSWORD` if Google auth is unavailable.

The old `?pw=<APP_PASSWORD>` query-string pattern was removed in v2.10.16. **Do not reintroduce it.**

All admin pages share a top nav bar (`_admin_nav(current)` helper) with links to all pages and a ← App return link.

| URL | Purpose |
|---|---|
| `/admin/login` | Password-based admin login (break-glass only — normal path is Google + footer link) |
| `/admin/users` | User account list with soft-delete (10-day scheduled deletion, Undo, Delete Now) |
| `/admin/devices` | Device access log (4411+ unique devices, platform breakdown) |
| `/admin/clear-lock` | Force-release stuck scan lock |
| `/admin/listing-patterns` | GC listing timestamp analysis |
| `/admin/build-coords` | Re-geocode store locations |
| `/admin/validate-stores` | Validate and clean up store list |

### Soft-delete flow (v2.11.3+)
- ✕ Delete button → marks user with `deleted_at = now + 10 days` (row dims, shows "Deletes May 25")
- **Undo** → clears `deleted_at`, restores user to normal
- **Delete Now** → immediately removes user from `users` and `user_data` tables
- On every `/admin/users` page load, any user whose `deleted_at` is in the past is auto-purged
- `deleted_at TEXT` column added via migration in `_init_user_db()`

### Admin CSRF protection
- Admin login form: CSRF token in `session["_admin_csrf"]`, validated with `hmac.compare_digest`
- Admin POST actions (delete-user): CSRF token embedded as hidden `<input name="_csrf">` in each form, validated in the endpoint via `hmac.compare_digest`
- `_admin_page_csrf()` — generates/reuses session token for post-login pages
- `_check_admin_csrf_header()` — validates `X-CSRF-Token` header (used if switching back to JSON endpoints)

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
| `window._authUser` | JS var | `null` = guest, `{username, googleLinked}` = logged in |
| `window._googleOauthEnabled` | JS var | Set by `_initGoogleOAuth` IIFE on page load; used by `_maybeShowLinkBanner` |
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
| `window._priceMin` | JS var | Minimum price filter (`null` = no filter) |
| `window._priceMax` | JS var | Maximum price filter (`null` = no filter) |
| `_globalSearchActive` | JS var | True when a global search (all stores) is active |
| `_syncTimer` | JS var | Debounce timer for `_syncToServer()` |

---

## Version Numbering

**Semantic versioning: `MAJOR.MINOR.PATCH`**

When bumping version, update ONE thing only:
1. `APP_VERSION = "x.y.z"` near the bottom of `gc_tracker_app.py`

The desktop `<h1>` span and mobile `.mtb-ver` span both use `<!-- __VER__ -->` placeholders that are replaced at startup via `HTML_TEMPLATE.replace('<!-- __VER__ -->', f'v{APP_VERSION}')`. No manual HTML edits needed. `CL_TEMPLATE` has no version display.

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
- Log in at `/admin/login`, then hit `/admin/clear-lock` to force-release without a Railway restart

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

- **Additional OAuth providers** (Facebook, Apple): same direct Authorization Code flow as Google — each needs its own `CLIENT_ID`/`CLIENT_SECRET` env vars, a new `/api/auth/<provider>` + callback route, and a `<provider>_id` column in `users`. Authlib is already a dependency.
- **Account settings page/modal**: allow users to change username or link Google at any time (not just via `/?google_new=1`).

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

### v2.10.18 — Security hardening (auth guards, timing fix, rate limiting)

**Admin guards on previously unprotected endpoints**:
- `POST /api/set-cookies` — now requires admin session. Was publicly accessible; injecting cookies corrupted the shared `_http` session for all concurrent users/scans.
- `GET /api/export-data` — now requires admin session. Was publicly accessible; returned full server state bundle.
- `GET /api/cl-debug` — now requires admin session + validates `city` against `_CL_CITIES` allowlist (SSRF fix).
- `GET /api/cl-parse-test` — now requires admin session.
- `GET /api/debug-fetch` — now requires admin session.
- `GET /api/debug-condition` — now requires admin session.
- `GET/POST /api/debug-condition/reset` — now requires admin session.
- `GET /api/debug-condition/diag` — now requires admin session.
- None of these endpoints are called from the frontend UI — they are dev/debug tools only.

**Timing oracle fix**:
- Admin login: `pw != admin_pw` replaced with `not hmac.compare_digest(pw, admin_pw)`.
- `/login` form: `password == APP_PASSWORD` replaced with `hmac.compare_digest(...)`.
- `import hmac` added to top-level imports.

**Rate limiting**:
- `POST /login` form: now applies `_check_login_rate()` / `_record_login_failure()` (same logic as `/api/login` and `/admin/login`). Was previously unrate-limited.
- `POST /api/run`: unauthenticated (guest) callers are now limited to one scan per 60 seconds per IP (`_SCAN_COOLDOWN = 60`, tracked in `_scan_last` dict). Logged-in users (`user_id` in session) are exempt from this limit.

**No behavior change** for normal users — all user-facing endpoints work identically.

---

### v2.10.17 — Security hardening (CSP, cookies, OAuth, rate limiting, rename)

**CSP / headers**:
- `default-src` changed from `'self'` to `'none'` with explicit allowlists
- `frame-ancestors 'none'` added (fixes Observatory clickjacking finding)
- `connect-src` adds `https://api.zippopotam.us` (fixes ZIP sort "check connection" error broken since v2.10.16 CSP addition)
- `Permissions-Policy`: `geolocation=()` (was `geolocation=(self)` — app doesn't use browser geolocation)

**Cookies**:
- `gt_device_id` cookie: `secure=True` when `RAILWAY_ENVIRONMENT` is set (fixes Observatory finding)

**Google OAuth**:
- OAuth callback now checks `email_verified` before auto-linking an existing account by email match

**Rate limiting**:
- `_client_ip()` helper added — uses `request.remote_addr` (normalized by ProxyFix) instead of raw `X-Forwarded-For`
- Admin login rate limiting added (uses same `_check_login_rate()` / `_record_login_failure()` as `/api/login`)

**Rename**:
- `login_required` decorator renamed to `optional_user_context` (33 occurrences) to avoid false sense of protection

**HANDOFF / docs**:
- Admin URL examples using `?pw=` removed from HANDOFF.md
- Version bumped, header and mobile title bar updated

---

### v2.10.16 — Security hardening (credentials, admin auth, headers)

**Credential cleanup**:
- Algolia `APP_ID` / `API_KEY` moved from hardcoded values to `ALGOLIA_APP_ID` / `ALGOLIA_API_KEY` env vars (must be set on Railway)
- `SECRET_KEY` fallback removed — app now crashes on startup if env var is missing (was falling back to a publicly visible default)
- Auto-updater removed entirely (`GITHUB_RAW`, `GITHUB_REPO`, `_check_for_update`, `_do_update`, `/api/version`, `/api/update`, JS `installUpdate()`, update banner HTML) — redundant with Railway auto-deploy and leaked repo identity
- Utility scripts (`analyze_listings.py`, `probe_geoloc.py`) updated to read Algolia creds from env vars
- `.gitignore` expanded: `.env`, `__pycache__`, `.DS_Store`, IDE files, utility scripts, runtime data files

**Admin auth overhaul**:
- All admin endpoints moved from `?pw=<password>` query string to session-based auth via `/admin/login`
- New routes: `GET/POST /admin/login`, `POST /admin/logout`
- `_is_admin()`, `_require_admin()`, `_require_admin_api()` helpers replace inline password checks
- Admin password no longer appears in URLs, browser history, or server/proxy logs
- `_admin_task_page()` helper no longer embeds password in JS POST bodies
- `/api/reset` changed from password-in-body to admin session check; old password modal removed, replaced with `confirm()` dialog
- Same session auth applied to: `/api/clear-blocklist`, `/api/validate-stores`, `/api/build-store-coords`, `/api/import-data`

**Security headers**:
- `Content-Security-Policy`: restricts script/style/img/connect sources; allows inline (required for single-file app) + Google OAuth domains
- `Strict-Transport-Security`: enabled on Railway (HTTPS only), `max-age=31536000`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy`: camera/microphone denied, geolocation self-only

**CSRF protection**:
- `@app.before_request` Origin/Referer check on all state-changing requests (POST/PUT/DELETE/PATCH)
- Cross-origin requests blocked with 403; same-origin and no-origin (curl, server-to-server) allowed

**Other fixes**:
- Open redirect in OAuth `?next=` parameter: now validated to only allow relative paths starting with `/`
- XSS in admin pages: all user-supplied data (usernames, IPs, user agents, dates) passed through `html.escape()` before HTML insertion
- OAuth error redirects no longer leak exception details in URL `?debug=` params — errors logged server-side only
- `import html as _html` added to imports

**Admin access after deploy**: navigate to `/admin/login`, enter admin password once — session persists. Old `?pw=` URLs no longer work.

### v2.10.15 — Per-user anchor for NEW detection (multi-user bug fix)

**Bug**: After v2.10.11 added `anchor_date = max(date_listed across _cat_cache)` to protect against Algolia's indexing-delay reordering, NEW-item detection silently broke in the multi-user case. `_cat_cache` is the **global shared inventory** (one `gc_category_cache.json` written by every user's scan). So when User A finished a scan minutes before User B started one, B's `anchor_date` was computed from a cache already containing A's freshest items. With `threshold = max(anchor_date, prev_scan_time)`, B's older `prev_scan_time` lost to A's recent anchor, threshold became "minutes ago," and almost nothing in B's results could satisfy `date_listed > threshold` → 0 new items, even though items genuinely new to B were sitting at the top of the table.

**Fix**: anchor is now **per-user**. New `last_anchor` column on `user_data` (migration-safe `ALTER TABLE`). At scan start, threshold = `max(this_user.last_anchor, this_user.prev_scan_time)`. At scan completion, server computes `new_anchor = max(date_listed in _cat_cache after scan)` and persists it to the scanning user's record (atomic with the scan via `_set_user_data`). Other users' scans no longer touch this user's anchor.

**Flow**:
- `_run()` signature gained `device_last_anchor` + `user_id` params
- `api_run` loads per-user `last_anchor` from `_get_user_data(user_id)` for logged-in users; accepts `device_last_anchor` in the request payload for guests
- `_run` uses `device_last_anchor` (not the global cache max) as the anchor for threshold
- Post-scan, `_run` computes `new_anchor = max(date_listed in all_products)` (THIS scan's results only — NOT `_cat_cache`, which is global; see v2.10.19 fix) and persists it to the user's record server-side
- SSE `done` payload now includes `scan_anchor` so guests can roundtrip via localStorage
- `/api/sync` accepts `last_anchor`; `_get_user_data` returns it; client merges with server-wins-when-newer (ISO string compare)
- Client: `window._lastAnchorISO` initialized from `localStorage.last_anchor`, sent as `device_last_anchor` in `/api/run`, updated from `msg.scan_anchor` in the done SSE
- Google account import merge picks the newer of the two `last_anchor` values

**Edge cases**:
- First scan after deploy: existing logged-in users have `last_anchor = ""` → threshold falls back to `prev_scan_time` alone for that one scan, then the anchor is established. (Same as pre-v2.10.11 behavior — one scan of being "unprotected.")
- Baseline scans still persist `last_anchor` so subsequent non-baseline scans have a starting point.
- Guests: round-trip via localStorage. No multi-device sync but single-device anchor protection works.
- Logged-in users on multiple devices: each device sends its own `last_anchor` via `/api/sync`, server takes the newer value; the next scan from any device gets the freshest anchor.

**Not changed**: the original Algolia-indexing-delay protection. If an item appears in today's search results with `date_listed` from a week ago (well below this user's anchor), it still won't be flagged NEW — that's the intended behavior.

### v2.10.14 — Google welcome modal + link banner

**Welcome modal** (`#google-welcome-modal`): appears for new Google users on first sign-in (`?google_new=1`). Pre-fills auto-generated username from Google display name. Users can:
- Change their username (3–30 chars, alphanumeric + `_-`)
- Import an existing account: enter old username's password → data merges (watchlist, keywords, favorites, saved searches, last_run, new_ids merged), old account deleted, Google account takes over old username
- Skip for now (username stays as auto-generated)

Navigate to `/?google_new=1` to re-trigger the modal at any time (e.g. to change username later).

**Link banner** (`#google-link-banner`): nudge for existing password-only users to link Google. Shows after login/page-load if: Google OAuth is enabled AND user does not have `google_id` AND they haven't dismissed it. Dismiss stored in `localStorage` key `gt_google_link_dismissed`. Clicking "Link Google Account" navigates to `/api/auth/google?next=<current_path>`.

**Account deletion on import**: when a Google user imports an existing password account, both `user_data` and `users` rows for the old account are hard-deleted. This is intentional — leaves one login path (Google) and eliminates the weaker password-only account.

**Both auth paths remain open**: username/password registration and login are still fully functional. Google is additive, not a replacement.

---

### v2.10.13 — Google Sign-In

**Google OAuth integration** — users can now sign in with their Google account alongside the existing username/password flow.

**Backend (Python)**
- Added `authlib` to `requirements.txt`
- `_GOOGLE_OAUTH_ENABLED` flag: True only when `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `authlib` are all present
- ProxyFix enabled when `RAILWAY_ENVIRONMENT` is set (so `url_for(..., _external=True)` generates `https://`)
- `users` table: `google_id TEXT UNIQUE` column added; `password_hash` constraint loosened to allow NULL for Google-only accounts; migration-safe `ALTER TABLE` on startup
- New helpers: `_user_by_email()`, `_user_by_google_id()`, `_gen_google_username()`
- New routes: `GET /api/auth/google`, `GET /api/auth/google/callback`, `GET /api/auth/config`
- Callback logic: (1) existing `google_id` → log in; (2) matching email → link account + log in; (3) new user → create account with auto-generated username
- Password login now returns a helpful error if a Google-only user tries to log in with a password

**Frontend**
- Google button (white, Google logo SVG) hidden by default; revealed on load if `/api/auth/config` returns `google_oauth: true`
- Buttons added to: header `#auth-modal` (login + register tabs), welcome `#first-run-modal` (login + register tabs), CL page `#auth-modal`
- `_googleSignIn(next)` navigates to `/api/auth/google?next=...`
- `?google_error=1` on callback failure: error shown in auth modal login tab
- CL page Google button uses `?next=/cl` so users land back on the CL page after auth

### v2.10.12 — Desktop thumb icon, favorites selection fix, mobile button rename

**Desktop thumbnail button**: Changed from "⊞ Thumbnails" pill to an icon-only toggle (⊞ in list mode → click to switch to grid; ☰ in grid mode → click to switch back to list). Matches the mobile ☰/⊞ pattern.

**Favorites selection behavior fix**: Switching TO favorites now selects *only* your favorites (previously merged with current all-stores selection). Pre-favorites selection is saved in `_preFavsSelection`; switching back to All Stores restores that snapshot exactly. Persistence across filter-text changes remains, but is scoped to whichever mode is active.

**Mobile bottom bar label**: "Search & Filter" → "Filter & Sort".

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

---

## Recent Changes (v2.12.3 → v2.12.8)

> **Deploy note**: Railway experienced a platform-wide outage on 2026-05-19 (Google Cloud Platform (GCP) suspended their account without warning for ~8 hours). v2.12.4–v2.12.9 were held back during recovery and pushed together once deploys resumed.

### v2.12.4 — Admin: last_login column, sortable table, prominent user count

**`last_login` tracking**
- New `last_login TEXT` column added to `users` table via migration-safe `ALTER TABLE` in `_init_user_db()`
- `_touch_last_login(user_id)` helper writes current UTC timestamp
- Called at every login path: username/password login (`api_login`), and all three Google OAuth (Open Authorization) callback paths (existing user by google_id, existing user by email link, new Google user)

**`/admin/users` page improvements**
- SQL query now selects `u.last_login`
- User count displayed prominently as `"N users"` stat above the table (was buried in muted subtitle)
- New **Last login** column between Last scan and Watch
- All column headers have `data-col="N"` attributes; date `<td>` cells have `data-value="ISO timestamp"` for correct chronological sorting
- `static/admin.js` — new file; lightweight click-to-sort for admin tables. Uses `Number()` (not `parseFloat()`) to distinguish ISO date strings from integers. `"—"` always sorts to bottom. Asc/desc toggle with ↑/↓ indicator.
- Sort bug note: `parseFloat("2025-03-15T...")` returns `2025` (strips after first non-digit) — makes all dates look equal. `Number("2025-03-15T...")` returns `NaN`, so dates sort as strings (ISO format sorts correctly as strings).

### v2.12.5 — Admin: watch count fix (sort bug fix)

- `Number()` fix applied in `static/admin.js` (was shipping broken in v2.12.4 — `parseFloat` bug described above)

### v2.12.6 — Single-page results: paginator always renders

- `_buildPaginatorHtml()` previously returned `''` when `totalPages <= 1`, leaving the bottom of the results area visually transparent (no `#111` background anchor)
- Now renders a minimal `<div class="paginator">` with item count and a disabled "1" button — the sticky `bottom:0; background:#111` always seals the bottom regardless of result count

### v2.12.7 — Admin watch count: cross-reference live catalog

- Previous fix (filtering `sold: True`) was wrong — the per-user SQLite watchlist synced from the client doesn't carry a `sold` field; that flag only exists in the global `gc_watchlist.json` server file
- Root cause: items that sell stay in `window._watchlist` (localStorage) and therefore in `user_data.watchlist` (SQLite) — they're simply absent from `_cat_cache` (the live inventory). The Watch List tab shows only items whose IDs are in the catalog; the admin count should match
- Fix: `_load_cat_cache()` called before the count loop; `wl_count = sum(1 for sku in _wl if sku in _cat_cache)` — matches exactly what the Watch List tab shows

### v2.12.8 — Want List modal: Done button always visible on mobile

- **Bug**: on mobile, a long want list caused the modal to grow taller than the viewport, pushing "Clear Want List" and "Done" off the bottom of the screen with no way to reach them
- **Fix**: modal inner div restructured as a flex column (`display:flex; flex-direction:column; overflow:hidden; max-height:80vh`)
  - Title, instructions, and Add input: pinned header section (`flex-shrink:0`)
  - Keyword chips (`#kw-list`): scrollable middle section (`overflow-y:auto; flex:1; min-height:0`)
  - Clear / Done buttons: pinned footer (`flex-shrink:0`) — always visible regardless of keyword count
- `max-width:calc(100vw - 32px)` added to prevent modal from overflowing narrow phone screens

### v2.12.9 — Want List modal: shorten instructions to fix Done button visibility

The pinned header section was tall enough on small phones to fill the entire viewport even after the flex restructure in v2.12.8, keeping the footer out of view.

- Combined two wildcard lines into one: `OD*` and `*drive*` on a single line with `·` separator — saves one full line
- Shortened all descriptions (e.g. "phrase match (words in that exact order)" → "exact phrase")
- "How to write keywords:" → "Keyword syntax:"
- Tip shortened: "Same syntax works in the search bar — click ⓘ for reference."
- `line-height` 1.6 → 1.45, `margin-bottom` 16px → 12px on `<p>` and input row
- Top padding 24px → 16px

**Files changed (v2.12.4–v2.12.9):** `gc_tracker_app.py`, `static/gc.js`, `static/admin.js` (new file)

---

## Recent Changes (v2.12.9 → v2.12.16)

### v2.12.10 — Filter bar overflow: wrong fix (immediately superseded)
- Added `order:-1` to `#save-search-btn,#clear-filters-btn` in CSS — this moved the buttons to the left of the filter bar, which was wrong. Superseded immediately by v2.12.11.

### v2.12.11 — Two-row filter bar + table overflow guard

**Problem**: Save Search / Clear All buttons overflowed off the right edge of the filter bar on narrower desktops when all four dropdowns were visible.

**Fix — two-row layout for `#results-top-bar`**:
- Changed `#results-top-bar` from `flex-direction:row` to `flex-direction:column` so the quick-filter chip row (Watch/Want/Price Drops/Saved Searches chips) sits on row 1 and the filter/search/action bar (dropdowns, search, Save/Clear buttons) sits on row 2.
- CSS:
  ```css
  #results-top-bar{display:flex;flex-direction:column;align-items:stretch;flex-shrink:0;position:sticky;top:0;z-index:2;background:#111}
  #results-top-bar .quick-filter-bar{border-bottom:1px solid #2e2e2e;background:none;padding:4px 8px 4px 16px;flex-shrink:0}
  #results-top-bar .results-hdr{flex:none;border-bottom:none;background:none;box-shadow:none}
  ```
- Mobile: `#results-top-bar` already uses `display:contents` inside the mobile `@media` block (no-op wrapper — children flow as direct children of `#res-panel`). **Zero mobile change.**

**Fix — table horizontal overflow guard**:
- Added `overflow-x:hidden` to `.results` as a backstop — clips horizontal overflow within the results scroll container rather than causing a page-level horizontal scrollbar.

### v2.12.12 — Multi-category selection race condition fix

**Bug**: Selecting a second category (e.g. Bass then Guitars) sometimes showed only the first selection. Root cause: `_populateFiltersFromServer()` received a stale server response (from request #1, triggered by the first category click) AFTER the user had already clicked the second category, and it overwrote `window._selectedCats` using `currentFilters.filter_cats` (the stale request's filter state) instead of the live selection.

**Fix** (`static/gc.js`, `_populateFiltersFromServer()`): replace all `currentFilters.filter_xxx` reads with the live `window._selectedXxx` variables so stale responses never overwrite a more recent user action:
```javascript
function _populateFiltersFromServer(brands, conditions, categories, subcategories, currentFilters) {
  _setBrandList(brands);
  window._selectedBrands = (window._selectedBrands || []).filter(b => brands.some(br => br.name === b));
  _updateBrandBtn();
  _setCondList(conditions);
  window._selectedConds = (window._selectedConds || []).filter(c => conditions.some(x => (x.name !== undefined ? x.name : x) === c));
  _updateCondBtn();
  _setCatList(categories);
  window._selectedCats = (window._selectedCats || []).filter(c => categories.some(x => (x.name !== undefined ? x.name : x) === c));
  _updateCatBtn();
  if (subcategories.length && window._selectedCats.length) {
    _setSubList(subcategories);
    window._selectedSubs = (window._selectedSubs || []).filter(s => subcategories.some(x => (x.name !== undefined ? x.name : x) === s));
  } else {
    _setSubList([]);
    window._selectedSubs = [];
  }
  _updateSubcatBtn();
}
```

### v2.12.13 — In-page search: space-split AND matching (not exact phrase)

**Bug**: The in-page search bar sent `filter_q` to `/api/browse`. The server passed the whole query string to `_compile_query()`, which splits by commas (want-list syntax). So "fender jaguar vintera" became a single term compiled to `\bfender jaguar vintera\b` — an exact phrase match. Typing "fender jaguar" would not match "Jaguar Vintera by Fender".

**Fix** (`gc_tracker_app.py`, `_apply_base()`): pre-split `filter_q` by spaces (respecting quoted phrases) before calling `_compile_query()` on each token:
```python
if fq:
    # Split by spaces (respecting quoted phrases) so each word is an independent
    # AND term — "fender jaguar vintera" means fender AND jaguar AND vintera in
    # any order, not an exact phrase.
    fq_tokens = _re.findall(r'"[^"]+"|[^\s]+', fq)
    fq_terms = []
    for tok in fq_tokens:
        fq_terms.extend(_compile_query(tok, fuzzy=f_strict))
    r = [i for i in r if fq_terms and _matches_all(
        ((i["name"] or "") + " " + (i["brand"] or "")).lower(), fq_terms)]
```
Quoted phrases (e.g. `"Jam Pedals"`) are preserved as a single token and still do exact-phrase matching.

### v2.12.14 — Sticky table column headers: initial attempt (insufficient)

- Changed `th` color to `#aaa` (lighter — easier to read against dark `#161616` background)
- Added `position:sticky; top:0; z-index:1` to `th`
- This was insufficient: `#results-top-bar` is INSIDE `.results` (the scroll container), also sticky at `top:0` with `z-index:2`. Column headers were sticking but immediately hidden behind the filter bar when scrolled.

### v2.12.15 — Sticky table headers: `border-collapse:separate` fix

- `border-collapse:collapse` is a documented browser bug that prevents `position:sticky` from working on `th` elements.
- Changed table CSS to `border-collapse:separate; border-spacing:0`. This enables stickiness but `top:0` was still wrong (headers hide behind the filter bar).

### v2.12.16 — Sticky table headers: correct offset via CSS variable (working)

**Key architectural insight**: `#results-top-bar` and the `th` elements are BOTH sticky within the same scroll container (`.results{overflow-y:auto}`). `#results-top-bar` is `position:sticky; top:0; z-index:2`. For `th` to appear below the filter bar (not hidden behind it), `th`'s `top` must equal `#results-top-bar.offsetHeight`.

**Implementation**:

New JS function `_applyFrozenHeaderOffset()` (added after `_isMobile()` in `static/gc.js`):
```javascript
function _applyFrozenHeaderOffset() {
  if (_isMobile()) return;
  const topBar = document.getElementById('results-top-bar');
  const h = topBar ? topBar.offsetHeight : 88;
  document.documentElement.style.setProperty('--tbl-hdr-top', h + 'px');
}
window.addEventListener('resize', _applyFrozenHeaderOffset);
```
- Called after each `_renderServerTable()` sets `innerHTML` (table height may vary after render)
- Called on `window resize` (filter bar height can change)

CSS variable `--tbl-hdr-top` used in `th` rule (`static/gc.css`):
```css
th{background:#161616;color:#aaa;font-weight:600;text-align:left;padding:7px 10px;
   font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;
   position:sticky;top:var(--tbl-hdr-top,88px);z-index:1;
   cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
```
Default fallback of `88px` covers the initial render before JS runs.

**Files changed (v2.12.10–v2.12.16):** `gc_tracker_app.py`, `static/gc.js`, `static/gc.css`

---

## Recent Changes (v2.12.27 → v2.12.28)

### v2.12.28 — Security audit: three unprotected endpoints hardened

**Context**: Reddit commenter said the app "still isn't secure." A full audit was run.

**Issues found and fixed:**

- **`/api/stop` — DoS via unauthenticated scan-stopping** (Critical): Any internet user could send `POST /api/stop` to abort an active scan. Fixed by requiring the caller to echo back the `run_id` returned by `/api/run`. Non-admins with a wrong or missing `run_id` get 403. Admin sessions remain exempt (needed for `/admin/clear-lock`).
  - `gc.js`: added `window._activeRunId = runId` when scan starts; `stopRun()` now sends `{run_id: window._activeRunId}` in the POST body; clears `_activeRunId` when scan completes.
  - `gc_tracker_app.py`: `api_stop()` validates `run_id` against `_current_run_id`.

- **`/api/populate-store-data` — no admin guard** (High): Any user could trigger hundreds of parallel Algolia API calls (quota exhaustion/resource abuse). Added `_require_admin_api()` guard.

- **`/api/fill-gaps` — no admin guard** (High): Same issue — expensive per-store Algolia scraping callable by any user. Added `_require_admin_api()` guard.

- **`/api/favorites` — unauthenticated write to global server file** (Medium): Endpoint writes to `gc_favorites.json` on disk with no auth check. This is dead code (not called from `gc.js` — per-user favorites live in SQLite via `/api/sync`). Added `user_id` session check; returns 401 if not logged in.

**Everything else audited and confirmed OK:**
- Rate limiting on `/api/login`, `/api/register`, `/api/setup-google-account`, `/admin/login`, `/login` ✅
- `SECRET_KEY` crashes app on startup if not set ✅
- Session cookies: HttpOnly, SameSite=Lax, Secure on Railway ✅
- CSRF: Origin/Referer check on all state-changing requests + form tokens on admin pages ✅
- Admin endpoints: all properly gated with `_require_admin_api()` ✅
- Password hashing (werkzeug), timing-safe compares (hmac.compare_digest) ✅
- XSS: admin pages use `html.escape()` on all user data ✅
- CSP, HSTS, X-Frame-Options, X-Content-Type-Options ✅
- SSRF in `/api/cl-search` fixed (city allowlist) ✅

**Files changed:** `gc_tracker_app.py`, `static/gc.js`

---

## Recent Changes (v2.12.21 → v2.12.27)

### v2.12.22 — Filter bypass + state restore for Watch List, Want List, Saved Searches

**Feature**: Activating Watch List, Want List, or a Saved Search now clears all current filters and searches nationwide. Toggling any of them off restores the exact prior state.

**Implementation** (`static/gc.js`):
- `_preSpecialViewState` — module-level variable, holds captured state or `null`
- `_captureFilterState()` — snapshots all filter state: stores, brands, conds, cats, subs, price min/max, search query, strict search, active special views, sort field/dir
- `_restoreFilterState()` — restores all captured state, fires all UI update functions, triggers a fresh fetch
- `toggleWatchFilter()` rewrote: on activation, captures state, clears all filters, sets `all_stores: true` via `_watchFilterActive`; on deactivation, calls `_restoreFilterState()` if state is saved
- `searchWantList()` same pattern: captures state on activation, restores on toggle-off; clears price filter too
- `_applySavedSearch()`: captures state before applying each saved search
- `_renderSavedSearchesDropdown()`: renders a "← Back" button when `_preSpecialViewState` is non-null; click calls `_restoreFilterState()`
- `_fetchBrowsePage()`: sends `all_stores: true` when `_globalSearchActive || _watchFilterActive`

**CSS** (`static/gc.css`): `.ss-back-btn` — styled as a small blue-tinted chip inside the saved searches dropdown.

### v2.12.23 — /newdeals admin page

**Feature**: Private admin-only page at `/newdeals` for browsing new GC inventory discounted from MSRP. Manual refresh scans all new inventory via Algolia.

**Backend** (`gc_tracker_app.py`):
- `NEW_DEALS_CACHE_FILE = DATA_DIR / "gc_new_deals_cache.json"`
- `_fetch_new_page(page)` — fetches `condition.lvl0:New` from Algolia, 240 hits/page
- `_load_new_deals_cache()` / `_save_new_deals_cache()` — in-memory + disk cache
- `GET /newdeals` — `_require_admin()` gate; returns `NEWDEALS_TEMPLATE`
- `POST /api/new-scan` — `_require_admin_api()` gate; parallel fetch all pages via `ThreadPoolExecutor(max_workers=12)`, dedupes by SKU, saves cache
- `POST /api/new-browse` — `_require_admin_api()` gate; filters by include_software, keyword search, brand, category, pct_off, price range, want list keywords; returns paginated results + brand/category facets

**Frontend**: `static/newdeals.js`, `static/newdeals.css` — self-contained, dark-themed, matching main app aesthetic. Column sorting, sticky headers, paginator, want list chip, "Include Software" checkbox.

**Note**: `_require_admin()` / `_require_admin_api()` are NOT decorator factories. Call inline: `denied = _require_admin(); if denied: return denied`.

### v2.12.24 — Fix software/plugin filtering on /newdeals

**Bug**: `_is_software_cat(category)` always returned False because `categoryPageIds` for new Algolia items only contains `["New"]` — no category hierarchy. Category column was also empty for all rows.

**Fix**:
- `_SOFTWARE_KEYWORDS` (renamed from `_SOFTWARE_CATS`) — extended set including name-based keywords: "plug-in", "plugin", "software", "virtual instrument", "ilok", "(download)", "sample pack", "sample library", "expansion pack", "loop library"
- `_is_software_item(name, category)` — new function checking both name AND category against keywords
- `api_new_scan`: stores `"is_software": _is_software_item(name, category)` on each cached item at scan time
- `api_new_scan`: category now uses `hit.get("categories")[0].get("lvl0")` (same as `parse_products` for used gear). Falls back to `categoryPageIds` excluding bare "New"/"Used" values.
- `api_new_browse`: filter changed to `items = [i for i in items if not i.get("is_software", False)]`

**⚠️ Admin must click "↻ Refresh Data" on /newdeals after deploy** to rebuild cache with `is_software` and `category` fields.

### v2.12.25–v2.12.27 — SEO improvements

**Context**: Google Search Console data showed 40+ location-specific queries ("guitar center [city] inventory") with page-1 rankings (positions 6–10) but zero clicks — a CTR problem. Store names weren't in indexable HTML (all loaded via JS).

**Changes**:
- **Title**: "Guitar Center Used Gear Tracker — Browse Inventory by Store Location" (was: "GC Used Inventory Tracker — Search Guitar Center Used Gear")
- **Meta description**: "Browse used gear at any Guitar Center location. Search guitars, amps, pedals, drums, and more by store, city, condition, and price — updated in real time. Free watch list and want list."
- **OG + Twitter tags**: updated to match
- **JSON-LD** (`<script type="application/ld+json">`): `WebSite` schema with `SearchAction`. Not blocked by CSP `script-src 'self'` (data block, not executable JS).
- **Visible footer** (`.seo-footer`): "Privacy Policy · Not affiliated with Guitar Center, Inc." — color `#555`, links `#666`. Compact, on-brand.
- **`_build_stores_noscript()`**: generates `<noscript>` block listing all stores from `STORES_CACHE`. Called at request time in `index()` (not startup) so it always reflects the live cache. `<!-- __STORES_NOSCRIPT__ -->` placeholder stays in `HTML_TEMPLATE` string; replaced per-request.

**Files changed**: `gc_tracker_app.py`, `static/gc.css`

---

## Recent Changes (v2.12.16 → v2.12.20)

### v2.12.17 — Watchlist deletion cross-device sync fix

**Bug**: Removing items from the watch list on one device (e.g. desktop) did not propagate to other devices (e.g. mobile). The server was updated correctly, but the other device's stale localStorage re-added the deleted items on the next `/api/me` merge.

**Root cause** (`static/gc.js`, `_loadAndMergeServerData()`): watchlist was merged with `Object.assign({}, sWl, window._watchlist)` — local storage spread last, so it always won over the server copy. Keywords and saved searches had already been fixed to server-wins in v2.9.0 and v2.10.0 respectively, but watchlist was left on the old union strategy.

**Fix**: Same server-wins pattern as keywords and saved_searches:
```javascript
const mergedWl = Object.keys(sWl).length > 0
  ? Object.assign({}, sWl)
  : Object.assign({}, window._watchlist);
```
Server is authoritative when non-empty; falls back to local only on first-ever sync (empty server record). Also updated the stale comment that still said "union for sets".

**Files changed:** `static/gc.js` only

### v2.12.18 — SEO fundamentals

Added all missing SEO and social-sharing infrastructure:

- **`<title>`**: changed from bare "GC Used Inventory Tracker" to "GC Used Inventory Tracker — Search Guitar Center Used Gear"
- **`<meta name="description">`**: 155-char description targeting used gear search intent
- **`<link rel="canonical">`**: `https://gcgeartracker.com/`
- **Open Graph tags**: `og:type`, `og:url`, `og:site_name`, `og:title`, `og:description`
- **Twitter card**: `summary` card with title + description
- **SVG favicon** (data URI): dark red `#7a0000` rounded square with "GC" text in `#ffcccc` — no more blank browser tab
- **`GET /robots.txt`**: allows `/`, disallows `/admin/` and `/api/`, links to sitemap
- **`GET /sitemap.xml`**: lists `/` (priority 1.0, daily) and `/privacy` (priority 0.3, monthly)

Google Search Console and Bing Webmaster Tools both verified and sitemaps submitted same day.

**Files changed:** `gc_tracker_app.py`

### v2.12.19 — Google Search Console verification route

- Added `GET /google73eeaa5f083d2e84.html` serving the Google-provided verification string
- Serves `Content-Type: text/html`
- Google Search Console ownership verified via this route

**Files changed:** `gc_tracker_app.py`

### v2.12.20 — OG image

- `static/og-image.svg`: 1200×630 dark-themed social preview image
  - Top bar with app title + `gcgeartracker.com` domain
  - Three stats cards: 92,000+ items · 300+ stores · Free
  - Faux results table (5 sample rows with NEW badges, prices in green)
  - Footer strip: "Browse and track GC's used inventory across all stores." + subtitle
- `og:image` and `twitter:image` meta tags added pointing to `/static/og-image.svg`
- `twitter:card` upgraded from `summary` to `summary_large_image` for full-width preview on Twitter/X

**Note**: SVG OG images work on most modern platforms. If a platform requires PNG/JPG in future, convert with `cairosvg static/og-image.svg -o static/og-image.png`.

**Files changed:** `gc_tracker_app.py`, `static/og-image.svg` (new file)

---

## Recent Changes (v2.12.2 → v2.12.3)

### v2.12.3 — Privacy Policy, About modal, UX fixes, affiliate removal

**Privacy Policy (`/privacy`)**
- New standalone route `GET /privacy` serving `PRIVACY_TEMPLATE` — dark-themed page matching the app's aesthetic
- Covers: account data, preferences/scan history, cookies (session, `gt_device_id`, GA4), third-party services (Google Analytics, Google OAuth, Railway), data retention, children's privacy, contact info
- Intro paragraph explicitly states the site is independent and not affiliated with Guitar Center, Inc.
- Contact email: `cboehmig@gmail.com`

**About modal**
- Added two-sentence description of what the tracker does (scan, watch list, want list, saved searches, cross-device sync)
- Added non-affiliation disclaimer: *"Independent tool — not affiliated with or endorsed by Guitar Center, Inc."*
- Added Privacy Policy link — visible on both desktop and mobile
- "About" link added to `#dev-footer` (desktop-accessible for the first time)

**Footer "About" link** (`static/gc.js`)
- `[data-action="open-about"]` link in `#dev-footer` — `addEventListener` in DOMContentLoaded calls `_openAboutModal()`

**Paginator / footer overlap fix** (`static/gc.css`)
- Paginator uses `position:sticky; bottom:0` inside `.results{overflow-y:auto}` scroll container
- Increased paginator `padding-bottom` to `36px` so its `#111` background visually covers the fixed `#dev-footer` zone — no gap, no clipping

**Footer link colors** (`static/gc.css`)
- `#dev-footer a { color:#bbb }` — all footer links now clearly readable against the dark background
- `#dev-footer span { color:#888 }` — separator dots and static text dimmer than links

**Item name ellipsis** (`static/gc.css`, desktop only)
- `td:nth-child(4)` in `@media(min-width:821px)`: `white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:1px`
- `max-width:1px` trick forces ellipsis in `table-layout:auto` tables

**Instant row removal on unwatch** (`static/gc.js`)
- In Watch List view: when user unstars an item, the row is immediately removed from the DOM before the server sync
- Uses `btn.closest('tr') || btn.closest('.item-card') || btn.closest('.compact-row')` to handle all three view modes

**Filter item count — only when active** (`static/gc.js`)
- `#filter-item-count` element now only shows filtered count text when at least one filter is active
- When no filters active, the element is blank — catalog total in the status bar (`s-known`) is the only count shown
- Avoids confusion between status bar count (~92,390 total catalog) and browse count (~92,295 visible — items hidden until next scan for NEW detection)

**Affiliate language removed** (all files)
- Removed `#affiliate-disclosure` div from `gc_tracker_app.py` and all related CSS from `gc.css`
- Removed Sovrn "GC Used Homepage" affiliate link from `#dev-footer`
- Removed affiliate disclosure paragraph from About modal
- Removed "Affiliate Links" section from `PRIVACY_TEMPLATE`
- Reason: applied to Sovrn affiliate program, was denied ("quality, security, or transparency"); GC is not on CJ Affiliate network either; all affiliate language stripped

**Files changed:** `gc_tracker_app.py`, `static/gc.css`, `static/gc.js`

---

## Affiliate Program Research (2026-05-19) — CLOSED

Applied to Sovrn (the only available network carrying GC) — was denied. Guitar Center is **not** currently on CJ Affiliate either (confirmed by search). Removed all affiliate language from the site. Not pursuing affiliate links further unless GC joins a network in the future.

---

## Recent Changes (v2.12.1 → v2.12.2)

### v2.12.2 — Fix NEW-item detection: anchor-only threshold (no wall-clock contamination)

**Bug**: Items that appeared in Algolia for the first time during a "0 new items" scan (due to Algolia's 6–12h indexing delay) would never be flagged NEW, even though the user had never seen them. They would silently appear in the table sorted between genuinely-new items and the previous top item, without NEW badges. Root cause: `threshold = max(anchor_date, prev_scan_time)`. Wall-clock UTC timestamps (e.g. `"2026-05-18T08:00:00Z"`) are lexicographically larger than date-only strings (e.g. `"2026-05-17"`), so `prev_scan_time` always won the `max()`, inflating the threshold above the date-only anchor and blocking items that had dates between the user's last "seen" item and the wall-clock scan time.

**Fix** (two lines in `_run()`, `gc_tracker_app.py`):

1. **Threshold**: changed from `max(_norm_anchor, prev_scan_time)` → `_norm_anchor if _norm_anchor else prev_scan_time`. The threshold is now purely item-date-based (the user's "top of table" high-water mark). Wall-clock time is only used as a fallback on the very first scan when no anchor exists yet.

2. **New anchor persistence**: changed from `max(new_anchor, anchor_date or "", prev_scan_time or "")` → `max(new_anchor, anchor_date or "")`. The saved anchor never gets inflated by wall-clock scan times.

**Effect**: Any item with `date_listed` newer than the top item in the user's table will be flagged NEW at the first scan where it appears — regardless of when that scan's wall clock reads.

**Caveat**: Items already silently in the cache from before this fix cannot be retroactively flagged (their `first_seen` timestamp matches the previous scan). Going forward, this scenario won't recur.

**Files changed:** `gc_tracker_app.py` only (`_run()` threshold and anchor lines, `APP_VERSION = "2.12.2"`)

---

## Recent Changes (v2.12.0 → v2.12.1)

### v2.12.1 — Price filter layout fixes

**Desktop popover clipping fixed**
- `#price-dd-panel` changed from `position:absolute` to `position:fixed`. Root cause: `.right{overflow:hidden}` clips any absolutely-positioned child that escapes the `.results-hdr` bar. Changing to `fixed` lifts the panel to the viewport stacking context so it can never be clipped.
- `togglePriceDropdown()` updated to position the panel via `getBoundingClientRect()` on the button (same pattern as `#ss-dropdown`). Sets `top`/`left` in `style`, then clamps to viewport right edge in a `requestAnimationFrame` callback.
- `#price-dropdown` wrapper no longer needs `position:relative` (removed from HTML). The `z-index` on the panel raised to `500` to match `#ss-dropdown`.

**Mobile equal-width inputs fixed**
- `.price-inp` changed from `flex:1` to `flex:1 1 0` with explicit `width:0`. Without `width:0`, browsers use the input's intrinsic content width as the flex-basis, which caused Min to be noticeably wider than Max. Setting `width:0` forces the flex algorithm to start both inputs from 0 and grow them equally from there.

**Desktop number spinners removed**
- Browser-native up/down spinner arrows were showing on the desktop dropdown inputs. Fixed via CSS pseudo-elements:
  ```css
  #price-min-dd,#price-max-dd{-moz-appearance:textfield}
  #price-min-dd::-webkit-inner-spin-button,#price-min-dd::-webkit-outer-spin-button,
  #price-max-dd::-webkit-inner-spin-button,#price-max-dd::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
  ```

**Desktop panel text brightened**
- "PRICE RANGE" label: `#666 → #aaa`
- `$` symbols: `#777 → #bbb`
- `–` separator: `#555 → #999`
- "✕ Clear price filter" button: `#c66 → #f88`

**Files changed:** `gc_tracker_app.py` (HTML inline styles on panel, `position:fixed`), `static/gc.js` (`togglePriceDropdown`), `static/gc.css` (spinner hiding, `.price-inp` flex fix)

---

## Recent Changes (v2.11.5 → v2.12.0)

### v2.12.0 — Price min/max filter

**Desktop:** `Price ▾` button added to the filter bar, consistent with Brand/Condition/Category/Subcategory dropdown pattern. When a price range is active, the button label changes to show the range inline (e.g. `$200–$500 ▾`) so it's visible without clicking. Clicking opens a popover with two number inputs (`$Min – $Max`, accepts decimals). A "✕ Clear price filter" link appears inside the popover when active.

**Mobile:** Always-visible `Price` row in the filter bottom sheet, placed between keyword search and the Brand accordion. Two side-by-side `$Min – $Max` inputs, same debounce/fetch pattern as all other filters.

**Both inputs stay in sync:** changing one set (desktop or mobile) updates the other automatically.

**Server-side (`/api/browse`):** `filter_price_min` and `filter_price_max` applied in `_apply_base()` using `price_raw` — works with all other filters, sort, pagination, and NEW detection.

**Saved searches:** price range is included in saved search data (`filter_price_min`, `filter_price_max`) and fully restored by `_applySavedSearch()`.

**Clear All / filter dot / Save Search:** all updated to include price state.

**Files changed:** `gc_tracker_app.py` (HTML_TEMPLATE, `/api/browse`, `/api/saved-search-counts`), `static/gc.js`, `static/gc.css`

## Recent Changes (v2.11.0 → v2.11.4)

### v2.11.5 — Browse-anchor advancement fix
- `_fetchBrowsePage` now advances `window._lastAnchorISO` to the max `date_raw` of page 1 results on every browse
- Persists to localStorage via `_lsSet('last_anchor', ...)` and syncs to server via `_syncToServer()`
- Ensures "Scan For New" only flags items genuinely newer than what was already visible at the top of the table — items you could already see won't get incorrectly flagged as NEW

### v2.11.4 — Admin nav bar on all admin pages
- `_admin_nav(current)` helper renders a shared top nav bar across all admin pages
- Links: ← App | 👤 Users · 📡 Devices · 📊 Listing Patterns · 🗺 Build Coords · ✓ Validate Stores
- Current page highlighted white; others are dimmed links
- Injected into devices, users, listing-patterns pages and `_admin_task_page()` shared template
- `_admin_task_page()` accepts new `nav_current` param; build-coords and validate-stores pass their own paths

### v2.11.3 — Fix delete button; soft-delete with 10-day window; list/grid button moved
- **Delete button fix**: replaced JS fetch approach (silently failing) with plain HTML form POST per row — no JS required, no CSRF header complexity, 100% reliable
- **Soft-delete**: ✕ Delete schedules deletion 10 days out; row dims and shows "Deletes [date]" with Undo + Delete Now buttons; auto-purge of past-due accounts on page load
- **`deleted_at TEXT` column**: added to `users` table via migration in `_init_user_db()`
- **List/grid toggle buttons** moved to first position in the quick-filter chip bar (before ↓ Price Drops) on both desktop and mobile

### v2.11.2 — Version display automated; delete button rewrite attempt
- `HTML_TEMPLATE` now uses `<!-- __VER__ -->` placeholder (replaced at startup via `.replace()`) — `APP_VERSION` is the single source of truth; no more manual HTML edits when bumping version
- `deleted_at` DB migration added

### v2.11.1 — Remove admin password login from normal flow
- `_require_admin()` now redirects unauthenticated users to `/` (main app) and shows a clean 403 to logged-in non-admins
- `/admin/login` password page still exists as break-glass but is no longer reached automatically
- Normal admin flow: log in with Google → admin footer link appears → click through

### v2.11.0 — Admin overhaul (minor version bump)
- CSRF token on admin login form
- `ADMIN_EMAIL` env var: any Google-logged-in user matching this email gets admin privileges via `_is_admin()`
- `POST /admin/delete-user` endpoint (at the time: JSON-based; replaced in v2.11.3 with form POST)
- `/api/me` now returns `is_admin: bool`
- Admin footer link (`#admin-footer-link`) in `#dev-footer`: hidden by default, revealed by JS if `/api/me` returns `is_admin: true`

---

## Recent Changes (v2.10.20 → v2.11.0)

### v2.11.0 — Admin overhaul + security hardening (minor version bump)

This release collects several admin-panel improvements and security fixes added across v2.10.21–v2.10.22, and marks them as a minor version bump because they represent new user-facing functionality rather than pure bug fixes.

**Admin login CSRF protection**
- `_ADMIN_LOGIN_HTML` now includes a `{csrf}` hidden field
- `admin_login()` generates a `session["_admin_csrf"]` token on GET, validates it with `hmac.compare_digest` on POST, rotates it on every response, and pops it on successful login

**Google-based admin access (`ADMIN_EMAIL` env var)**
- New `ADMIN_EMAIL` env var (set to `cboehmig@gmail.com` on Railway)
- `_is_admin()` now grants admin privileges to any logged-in user whose account email matches `ADMIN_EMAIL` — no separate password session required

**Delete users on admin panel**
- `POST /admin/delete-user` endpoint: removes the user's rows from both `users` and `user_data` tables
- Admin users page now shows a ✕ Delete button per row with a confirmation dialog
- CSRF-protected: the admin users page embeds a `<meta name="csrf-token">` tag; the delete JS reads it and sends it as an `X-CSRF-Token` header; the endpoint validates with `_check_admin_csrf_header()` before touching the DB

**Admin footer link (visible to admins only)**
- A small "Admin" link added to the `#dev-footer` div, hidden (`display:none`) by default
- On page load, `/api/me` now returns `is_admin: true/false`; if true, JS reveals the link and a separator dot
- Regular users never see it; server-side protection is unchanged

**New helpers added to `gc_tracker_app.py`**
- `_admin_page_csrf()` — generates/reuses a session CSRF token for post-login admin pages
- `_check_admin_csrf_header()` — timing-safe validation of the `X-CSRF-Token` request header

---

## Recent Changes (v2.10.19 → v2.10.20)

### v2.10.20 — Search reliability fixes (two bugs)

**Bug 1 — `_srvLoading` race condition (search silently dropped)**
- `_fetchBrowsePage` guards against concurrent requests with `if (_srvLoading) return` — if any background browse (page load, filter change, store toggle) was still in-flight when the user typed, the search call was silently dropped. Results stayed showing the old state. User clears box and retypes → works, because `_srvLoading` was false by then. This caused intermittent "search doesn't work" reports.
- **Fix**: `_globalKeywordSearch`'s debounce callback now sets `_srvLoading = false` and `_srvStores = getSelected()` before calling `_fetchBrowsePage`. Matches the pattern already used by every other deliberate user action (`clearFilters`, `clearGlobalSearch`, `toggleWatchFilter`, scan done). Also refreshes `_srvStores` from current selection so stale store state can't cause searches to query wrong stores.

**Bug 2 — Want-list mode silences search box text**
- Two related issues: (a) `clearResSearch()` (the ✕ button) cleared the input but didn't reset `_globalSearchActive` / `_wantListSearchActive`, so clicking ✕ after a want-list search left those flags true and subsequent fetches continued sending `filter_want_list_only: true` invisibly; (b) typing in the search box while want-list mode was active caused `_fetchBrowsePage` to override `filter_q` with `_globalSearchQuery = ''` instead of the typed text — results came back un-filtered or as all want-list matches.
- **Fix**: `clearResSearch()` now resets `_globalSearchActive`, `_wantListSearchActive`, `_globalSearchQuery`, and calls `_resetWantListLink()`. `_globalKeywordSearch` does the same at the top so typing always exits want-list mode cleanly.

---

## Recent Changes (v2.10.18 → v2.10.19)

### v2.10.19 — Domain migration, anchor persistence fix, GA CSP fix

**Domain migration to gcgeartracker.com**
- DNS moved from Squarespace (blocks CNAME on apex) to Cloudflare (supports CNAME flattening)
- Railway custom domain `gcgeartracker.com` added, SSL auto-provisioned
- `_redirect_old_domain()` `@app.before_request` hook: 301-redirects `gctracker.animalsintrees.com` → `gcgeartracker.com` preserving path + query string
- Google Cloud Console OAuth credentials updated: new domain added to Authorized JavaScript Origins and Redirect URIs

**New-item anchor contamination fix (0-new / reordered bug, second part)**
- v2.10.15/v2.10.18 fixed the anchor used as the *threshold for the current scan* (using `device_last_anchor` instead of global cache max). But `new_anchor` — the value *persisted for next time* — was still computed as `max(date_listed in _cat_cache)`. Since `_cat_cache` is global and written by every user's scan, another user's scan could push this user's stored anchor forward, causing their next scan to see 0 new items.
- **Fix**: `new_anchor` now computed from `all_products` (this scan's results only), not `_cat_cache`. Anchor only advances as far as items this user actually saw.

**GA4 CSP fix**
- Phase 5 CSP hardening (v2.10.18) added `https://www.googletagmanager.com` to `script-src` (allows gtag.js to load) but omitted `https://www.google-analytics.com` from `connect-src`. GA4 sends event beacons via `fetch()` to `https://www.google-analytics.com/g/collect` — blocked silently by the browser since that commit.
- **Fix**: `https://www.google-analytics.com` added to `connect-src`. GA4 tracking restored.

---

### Key env vars (Railway)
| Var | Status |
|---|---|
| `SECRET_KEY` | ✅ Set (long random string) |
| `APP_PASSWORD` | ✅ Set (custom password) |
| `DATA_DIR` | ✅ Points to persistent volume |
| `GOOGLE_CLIENT_ID` | ✅ Set (from Google Cloud Console) |
| `GOOGLE_CLIENT_SECRET` | ✅ Set (from Google Cloud Console) |

---

## ✅ Completed: Static File Extraction (v2.10.18, merged 2026-05-14)

All JS and CSS extracted from inline Python template strings into static files:

```
static/
  gc.css    ← all CSS from HTML_TEMPLATE
  gc.js     ← all JS from HTML_TEMPLATE
  cl.css    ← all CSS from CL_TEMPLATE
  cl.js     ← all JS from CL_TEMPLATE
```

- `script-src 'unsafe-inline'` **removed** from CSP — static JS files only
- `style-src 'unsafe-inline'` **retained** — required for inline `style="..."` HTML attributes throughout both templates (hundreds of occurrences; not feasible to migrate)
- All 124 inline `onclick="..."` event handlers replaced with `data-action` / `data-*` attributes + global event delegation
- The Python/JS backslash escape gotcha is now permanently resolved — `.js` files are not processed by Python string handling
- Affiliate link (Sovrn) removed — application was rejected; removed from HTML template, About modal, and CSS
- "Download Excel" removed from the status bar

---

## ✅ Completed: Domain Migration (v2.10.19, 2026-05-15)

`gcgeartracker.com` is now the primary domain. `gctracker.animalsintrees.com` 301-redirects to it.

### What was done
1. **DNS**: Registered `gcgeartracker.com` on Squarespace. Squarespace blocks CNAME on apex (`@`) — worked around by moving DNS management to Cloudflare (free). Added CNAME `@` → `gaeuti41.up.railway.app` (DNS only, no proxy) and TXT `_railway-verify` record. Updated Squarespace nameservers to `kipp.ns.cloudflare.com` / `nova.ns.cloudflare.com`.
2. **Railway**: Added `gcgeartracker.com` as a custom domain on the web service (port 8080). SSL provisioned automatically.
3. **Flask redirect**: `_redirect_old_domain()` `@app.before_request` hook 301-redirects any request with `Host: gctracker.animalsintrees.com` to `https://gcgeartracker.com`.
4. **Google OAuth**: Added `https://gcgeartracker.com` to Authorized JavaScript Origins and `https://gcgeartracker.com/api/auth/google/callback` to Authorized Redirect URIs. Old `animalsintrees.com` entries kept in place.

### Key notes
- Keep both Google OAuth URIs registered — remove `animalsintrees.com` only after confirming no active users rely on it
- Session cookies are domain-scoped; users 301'd from the old domain log in once on the new domain (expected)
- `RAILWAY_ENVIRONMENT` ProxyFix and `Secure` cookie flags work correctly on the new domain with no code changes

---

## 🎯 Future Ideas

### Android App (next priority)
Goal is Play Store discoverability. Approach: WebView wrapper (fastest path) with enough native touches to pass Google Play review.

**Google Play requirements (as of 2026):**
- $25 one-time developer account fee at play.google.com/console
- Personal accounts created after 2023-11-13 must complete 14 consecutive days of closed testing with ≥12 opted-in testers before publishing to production. Testers need to be opted in (clicked the invite link) but don't need to actively use the app daily — they just can't opt out. Recruit 15–16 to have a buffer.
- Testers must have Android devices (cannot test on iPhone)
- Google Play rejects bare WebView wrappers — need at least a couple native touches: push notifications (Firebase Cloud Messaging (FCM)), offline error screen, custom back/loading behavior
- Recommended build tool: Android Studio (free, Mac-compatible) with built-in Android Virtual Device (AVD) emulator — no physical Android device needed for development

**Steps:**
1. Create Play Console account (starts the clock on account verification)
2. Build Android Studio project: WebView + Firebase Cloud Messaging (FCM) push notifications
3. Recruit 15+ Android-owning testers (r/guitar, r/guitarpedals are good sources)
4. Run 14-day closed test, then flip to production
5. iOS second — App Store is $99/year, stricter WebView policy, longer review times

### Other Future Ideas
- **Additional OAuth providers** (Facebook, Apple): same Authorization Code flow as Google — each needs `CLIENT_ID`/`CLIENT_SECRET` env vars + a new callback route + a `<provider>_id` column in `users`.
- **Account settings page**: allow users to change username or link Google without the `/?google_new=1` URL trick.
- **Reddit outreach**: r/guitar, r/guitarpedals, r/geartalk, r/WeAreTheMusicMakers — site is ready. First post got ~200 visitors and 61 signups (strong conversion). Repost with better framing: lead with the deal-finding angle, not "I built a tool."
- **Domain email**: set up `@gcgeartracker.com` address and update Privacy Policy contact from `cboehmig@gmail.com`
