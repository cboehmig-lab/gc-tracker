# GC Gear Tracker — Session Handoff Prompt
*Generated: 2026-05-19 · Version: v2.12.3 · Live at: gcgeartracker.com*

Use this at the start of a new session to bring Claude up to speed instantly.

---

## The Project

**GC Gear Tracker** (`gcgeartracker.com`) is a Flask web app that tracks Guitar Center used inventory. Users create accounts (username/password or Google Sign-In) and get items flagged NEW since their last scan. Watch list, want list, favorites, and saved searches all sync across devices via server-side user accounts. A separate `/cl` page does Craigslist used gear search.

- **Repo**: `cboehmig-lab/gc-tracker` on GitHub → auto-deploys to Railway on every push to `main`
- **Python entry**: `gc_tracker_app.py` (single file, ~8000+ lines)
- **Static assets**: `static/gc.css`, `static/gc.js` (no inline JS/CSS — CSP enforced)
- **Local workspace**: `~/Desktop/gc_tracker/`
- **Full technical reference**: read `~/Desktop/gc_tracker/HANDOFF.md` before making any changes

---

## Critical Rules (read before touching anything)

1. **Never write inline JS** — all JS lives in `static/gc.js`. CSP blocks inline scripts.
2. **The Python/JS backslash gotcha is gone** — JS is now in `.js` files so Python no longer mangles `\\` sequences. But don't re-introduce inline scripts.
3. **No inline onclick attributes** — use `data-*` attributes + `addEventListener` (Python triple-quote strings + inline onclick + regex = syntax bomb).
4. **Git pushes must come from the Mac terminal** — the Cowork sandbox gets a proxy 403 on GitHub pushes.
5. **Sandbox git lock files**: if `git commit` fails with "cannot lock ref HEAD", the Mac owns the lock. Tell the user: `rm ~/Desktop/gc_tracker/.git/HEAD.lock && git add ... && git commit ... && git push`.
6. **Always bump `APP_VERSION`** when shipping user-facing changes — the `<!-- __VER__ -->` placeholder in `HTML_TEMPLATE` auto-propagates it everywhere.

---

## Architecture in 60 Seconds

**Desktop filter bar**: `.filter-scroll-body{display:contents}` makes all children direct flex items in `.results-hdr`. Dropdowns are `display:none` in HTML, revealed by JS (`element.style.display = ''`) via `_setBrandList()` when data is available.

**Mobile filter**: bottom sheet (`position:fixed; bottom:calc(56px + env(safe-area-inset-bottom))`). `.filter-scroll-body{display:flex;flex-direction:column}` in `@media(max-width:820px)`. Desktop dropdowns hidden via `display:none!important` in mobile media. Mobile accordions (`#acc-brand`, `#acc-cond`, etc.) replace them.

**Dropdowns that escape overflow clipping**: use `position:fixed` + JS `getBoundingClientRect()` to position — NOT `position:absolute`. The `.right` panel has `overflow:hidden` which clips absolutely-positioned children. `#ss-dropdown` and `#price-dd-panel` both use this pattern.

**Server-side browse**: `POST /api/browse` — reads `gc_category_cache.json`, applies all filters in `_apply_base()`, returns 50 items/page. Filter params: `filter_q`, `filter_brands`, `filter_conditions`, `filter_categories`, `filter_subcategories`, `filter_watched`, `filter_price_drop_only`, `filter_price_min`, `filter_price_max`.

**Saved searches**: stored in `user_data.saved_searches` (SQLite JSON). `_getBrowseFilters()` captures state; `_applySavedSearch()` restores it. Price min/max included.

**`_closeAllDropdowns()`**: called by every dropdown toggle before opening — closes brand, condition, category, subcategory, AND price dropdown.

---

## Current State: v2.12.3 (ready to push)

### v2.12.3 — CJ Affiliate approval prep: Privacy Policy, disclosure, About modal
- **Privacy Policy**: new `/privacy` route + `PRIVACY_TEMPLATE` (standalone dark-themed page). Covers data collection, cookies, third-party services, affiliate links, contact email (cboehmig@gmail.com). Includes non-affiliation disclaimer.
- **Affiliate disclosure**: `#affiliate-disclosure` div — `position:fixed; bottom:10px; left:12px` on desktop, `display:none` on mobile. Text: *"This site may earn a commission on purchases made through links to Guitar Center."*
- **About modal**: added app description, non-affiliation disclaimer (*"Independent tool — not affiliated with or endorsed by Guitar Center, Inc."*), affiliate disclosure, and Privacy Policy link. All mobile-accessible.
- **Files changed**: `gc_tracker_app.py` (`PRIVACY_TEMPLATE`, `/privacy` route, `#affiliate-disclosure` div, About modal content, `APP_VERSION = "2.12.3"`), `static/gc.css` (`#affiliate-disclosure` styles + mobile hide)

### v2.12.2 — Fix NEW-item detection: anchor-only threshold (bug fix)
- **Bug fixed**: Items appearing in Algolia for the first time during a "0 new" scan (due to Algolia's 6–12h indexing delay) were never flagged NEW even though the user had never seen them. They silently appeared between genuinely-new and old items, without NEW badges.
- **Root cause**: `threshold = max(anchor_date, prev_scan_time)`. Wall-clock UTC timestamps like `"2026-05-18T08:00:00Z"` always beat date-only strings like `"2026-05-17"` in a string compare, inflating the threshold above items that were genuinely new.
- **Fix**: threshold is now `anchor_date` only (user's "top of table" high-water mark). Falls back to `prev_scan_time` only if no anchor exists yet. New anchor is persisted as `max(this_scan_dates, old_anchor)` — wall-clock time never written into the anchor.
- **Files changed**: `gc_tracker_app.py` only (2 lines in `_run()`, `APP_VERSION = "2.12.2"`)

### v2.12.1 — Price filter layout fixes (bug fixes)
- **Desktop popover**: was clipped by `.right{overflow:hidden}`. Fixed by changing `#price-dd-panel` from `position:absolute` to `position:fixed` (z-index:500) and positioning via `getBoundingClientRect()` in `togglePriceDropdown()`.
- **Mobile equal-width inputs**: `.price-inp` changed from `flex:1` to `flex:1 1 0; width:0` so both inputs grow equally from a 0 basis.
- **Desktop spinners removed**: `#price-min-dd`/`#price-max-dd` use `-webkit-appearance:none` on spin buttons and `-moz-appearance:textfield` on the inputs.
- **Panel text brightened**: label `#666→#aaa`, `$` signs `#777→#bbb`, separator `#555→#999`, clear button `#c66→#f88`.

---

## Key HTML Elements

| Element | Location | Purpose |
|---|---|---|
| `#price-dropdown` | Desktop filter bar | Wrapper div (display:none until brand data loads) |
| `#price-dd-btn` | Inside `#price-dropdown` | Toggle button — label updates when filter active |
| `#price-dd-panel` | Inside `#price-dropdown` | Popover (position:fixed, positioned by JS) |
| `#price-min-dd` / `#price-max-dd` | Inside `#price-dd-panel` | Desktop number inputs |
| `.price-range-mobile` | Mobile filter sheet | Always-visible price row on mobile |
| `#price-min` / `#price-max` | Inside `.price-range-mobile` | Mobile number inputs |
| `#price-dd-clear` | Inside `#price-dd-panel` | "Clear price filter" link (hidden when inactive) |

---

## Files Changed Most Recently

- **`gc_tracker_app.py`** — `PRIVACY_TEMPLATE` added, `/privacy` route added, `#affiliate-disclosure` div added, About modal updated with description/disclaimer/disclosure/privacy link, `APP_VERSION = "2.12.3"`. Also contains v2.12.2 `_run()` anchor-only threshold fix.
- **`static/gc.css`** — `#affiliate-disclosure` styles added (fixed position left, mobile hide)
- **`static/gc.js`** — no changes in v2.12.3

---

## Git State

- All v2.12.3 changes are **uncommitted** — ready to push
- Push command: `cd ~/Desktop/gc_tracker && git add gc_tracker_app.py static/gc.css && git commit -m "v2.12.3 — Privacy Policy, affiliate disclosure, About modal description + disclaimer" && git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`

---

## Nothing Currently Broken

v2.12.2 fixed the NEW-detection gap. v2.12.3 adds CJ Affiliate approval prerequisites (privacy policy, disclosure, about modal). No known remaining issues.

---

## Affiliate Program Next Steps

- **Apply**: `cj.com` → Publishers → apply to Guitar Center program (after v2.12.3 is live)
- **CJ deep link format**: `https://www.anrdoezrs.net/click-[YOUR-SID]-[GC-AID]?url=[encoded-gc-url]`
- **Future**: implement `_gcUrl(url)` helper in `gc.js` to wrap all outgoing GC links once SID + GC's AID are known
- **Future**: set up `@gcgeartracker.com` email and update Privacy Policy contact address

---

## Where to Go for More

- Full architecture, all routes, auth flow, security hardening history, mobile layout details: **`~/Desktop/gc_tracker/HANDOFF.md`**
- Version history back to v2.8.0 is in HANDOFF.md under "Recent Changes" sections
