# GC Gear Tracker — Session Handoff Prompt
*Generated: 2026-06-03 · Version: v2.12.27 · Live at: gcgeartracker.com*

Use this at the start of a new session to bring Claude up to speed instantly.

---

## The Project

**GC Used Inventory Tracker** (`gcgeartracker.com`) is a Flask web app that tracks Guitar Center used inventory. Users create accounts (username/password or Google Sign-In) and get items flagged NEW since their last scan. Watch list, want list, favorites, and saved searches all sync across devices via server-side user accounts. A separate `/cl` page does Craigslist used gear search. A private `/newdeals` admin page browses new GC inventory deals (items discounted from MSRP).

- **Repo**: `cboehmig-lab/gc-tracker` on GitHub → auto-deploys to Railway on every push to `main`
- **Python entry**: `gc_tracker_app.py` (single file, ~5600+ lines)
- **Static assets**: `static/gc.js`, `static/gc.css`, `static/newdeals.js`, `static/newdeals.css`, `static/admin.js`, `static/og-image.svg`
- **Local workspace**: `~/Desktop/gc_tracker/`
- **Full technical reference**: read `~/Desktop/gc_tracker/HANDOFF.md` before making any changes

---

## Critical Rules (read before touching anything)

1. **Never write inline JS** — all JS lives in `static/gc.js` (main app) or `static/newdeals.js` (/newdeals). CSP blocks inline scripts.
2. **No inline onclick attributes** — use `data-*` attributes + `addEventListener`.
3. **Git pushes must come from the Mac terminal** — the Cowork sandbox gets a proxy 403 on GitHub pushes. SSH is configured; always use: `git push git@github.com:cboehmig-lab/gc-tracker.git main`
4. **Sandbox git lock files**: if `git commit` fails with "cannot lock ref HEAD", the Mac owns the lock. Tell the user: `rm ~/Desktop/gc_tracker/.git/HEAD.lock && rm ~/Desktop/gc_tracker/.git/refs/heads/main.lock` then re-run.
5. **Version bump**: only change `APP_VERSION` in `gc_tracker_app.py` — the `<!-- __VER__ -->` placeholder in `HTML_TEMPLATE` auto-propagates it everywhere.
6. **`_require_admin()` / `_require_admin_api()` are NOT decorators** — they return None or a response. Call inline: `denied = _require_admin(); if denied: return denied`. Never use as `@_require_admin`.
7. **Template replacements at startup**: `HTML_TEMPLATE`, `CL_TEMPLATE`, and `NEWDEALS_TEMPLATE` all get `<!-- __GA__ -->` replaced at module load. `HTML_TEMPLATE` also gets `<!-- __VER__ -->`. The `<!-- __STORES_NOSCRIPT__ -->` placeholder stays in `HTML_TEMPLATE` and is replaced at *request time* in `index()` — do not bake it in at startup.

---

## Architecture in 60 Seconds

**Desktop filter bar**: two-row layout inside `#results-top-bar` (`flex-direction:column`). Row 1 = `.quick-filter-bar` (chips). Row 2 = `.results-hdr` (dropdowns + search + Save/Clear buttons). Mobile: `#results-top-bar{display:contents}` — wrapper is invisible, children flow directly in `#res-panel`.

**Dropdowns that escape overflow clipping**: use `position:fixed` + JS `getBoundingClientRect()`. `.right` has `overflow:hidden` which clips absolute children. `#ss-dropdown` and `#price-dd-panel` both use this pattern.

**Sticky table headers**: `border-collapse:separate; border-spacing:0` on `table`. `th{position:sticky; top:var(--tbl-hdr-top,88px)}`. CSS variable set by `_applyFrozenHeaderOffset()` in JS after each render and on resize.

**Server-side browse**: `POST /api/browse` — reads `gc_category_cache.json`, applies all filters in `_apply_base()`, returns 50 items/page. Space-separated search terms are AND'd.

**NEW detection**: anchor-based per-user. `threshold = _norm_anchor` (user's "top of table" high-water mark). Anchor only advances when browsing fully unfiltered (`!hasFilters && !_globalSearchActive`). Persisted server-side in `user_data.last_anchor`.

**Special view state save/restore**: `_captureFilterState()` / `_restoreFilterState()` in `gc.js`. `_preSpecialViewState` is set when Watch List, Want List, or a Saved Search is activated — stores all filter state. Toggling any of these off calls `_restoreFilterState()` to return to the exact prior state. Watch and Want List also clear all filters (brand, cond, cat, price, search) and go nationwide (`all_stores: true` in browse body) on activation.

**Sync merge strategy**: server-wins for watchlist, keywords, saved_searches (so deletions propagate cross-device). Falls back to local only if server record is empty.

**Mobile**: `_isMobile()` = `window.innerWidth <= 820px`. Bottom sheet pattern for store panel + filter panel. Swipe-to-dismiss on both.

---

## /newdeals Admin Page

Private page (`_require_admin()` gate). New GC inventory (not used) discounted from MSRP.

- **`NEW_DEALS_CACHE_FILE`**: `gc_new_deals_cache.json` — stores all new inventory items
- **`/api/new-scan` (POST)**: fetches ALL new GC inventory via Algolia (`condition.lvl0:New`), dedupes by SKU, saves cache. Uses `ThreadPoolExecutor(max_workers=12)` for parallel page fetches.
- **`/api/new-browse` (POST)**: filters/sorts/paginates the cached items. Filters: `include_software`, `filter_q`, `filter_brands`, `filter_categories`, `filter_min_pct_off`, `filter_price_min/max`, `filter_want_list` + `keywords`.
- **Software detection**: `_is_software_item(name, category)` checks both fields against `_SOFTWARE_KEYWORDS`. `is_software` boolean stored on each item at scan time — browsing filters on `item.get("is_software", False)`.
- **Category extraction**: uses `hit.get("categories")[0].get("lvl0")` (same as used gear). Falls back to `categoryPageIds` (skips bare "New"/"Used" values).
- **Static files**: `static/newdeals.js`, `static/newdeals.css` — self-contained, no shared state with main app JS.
- **Want list**: loaded from `/api/me` on page load; "Want List" chip filters by whole-word keyword match (OR logic across keywords).
- **⚠️ After any deploy that changes scan logic**: admin must click "↻ Refresh Data" on `/newdeals` to rebuild cache with updated fields.

**`gc_new_deals.py`** — standalone terminal script (not part of the web app). Same Algolia credentials. Run: `python3 gc_new_deals.py [--threshold 0.5] [--category Guitars]`

---

## Algolia Details

- **Index**: `cD-guitarcenter`
- **Used inventory**: `facetFilters: ["condition.lvl0:Used"]` + `stores: [<store_name>]`
- **New inventory**: `facetFilters: ["condition.lvl0:New"]` — nationwide, no store filter
- **Price fields**: `hit.get("price")` = sale price, `hit.get("listPrice")` = MSRP
- **Category fields**: `hit.get("categories")[0].get("lvl0")` = top-level category (e.g. "Guitars")
- **Store fields**: `hit.get("stores")` array; `hit.get("storeName")` = "City, ST" format
- **`all_stores: true`** in `/api/browse` body bypasses store filter server-side

---

## SEO (v2.12.25–27)

- **Title**: "Guitar Center Used Gear Tracker — Browse Inventory by Store Location"
- **Meta description**: "Browse used gear at any Guitar Center location. Search guitars, amps, pedals, drums, and more by store, city, condition, and price — updated in real time. Free watch list and want list."
- **JSON-LD**: `WebSite` schema with `SearchAction` (`potentialAction`) — injected as `<script type="application/ld+json">` (not blocked by CSP `script-src 'self'`)
- **Noscript store list**: `_build_stores_noscript()` called in `index()` — reads `STORES_CACHE` fresh, generates `<noscript>` block listing all ~240+ store names. Invisible to JS users, crawlable by Google. Updates automatically when store list is refreshed.
- **Footer**: `.seo-footer` — visible "Privacy Policy · Not affiliated with Guitar Center, Inc." in `#555` gray. No hidden text.

---

## Current State: v2.12.27 (deployed ✅)

### Recent changes (this session)

- **v2.12.27** — SEO: `_build_stores_noscript()` moved to request-time (was startup-time) so it always reflects the live store cache. Clean footer with only Privacy Policy + affiliation notice.
- **v2.12.26** — SEO: visible footer simplified; store location content moved to `<noscript>`.
- **v2.12.25** — SEO: updated title/description/OG/Twitter tags to match location-inventory search intent; added JSON-LD `WebSite` schema with `SearchAction`.
- **v2.12.24** — Fixed software/plugin filtering on `/newdeals`. Name-based detection via `_is_software_item(name, category)`. `is_software` flag stored at scan time. Category extraction now uses `categories[0].lvl0` (same as used gear). **Admin must Refresh Data after this deploy.**
- **v2.12.23** — Built `/newdeals` admin page end-to-end: backend routes (`/newdeals`, `/api/new-scan`, `/api/new-browse`), `NEWDEALS_TEMPLATE`, `static/newdeals.js`, `static/newdeals.css`.
- **v2.12.22** — Watch List, Want List, and Saved Searches now bypass all current filters and search nationwide on activation. Toggle-off restores exact prior filter/store state. Added "← Back" button to Saved Searches dropdown. Implemented `_captureFilterState()` / `_restoreFilterState()` helper pattern in `gc.js`.

### Traffic / scale
- ~854 unique visitors/week (Google Analytics)
- 152 registered accounts
- Organic + Reddit-driven
- Google Search Console active — showing page 1 positions (avg 6–10) for location-specific queries like "guitar center [city] inventory" with zero clicks (CTR problem, not ranking problem — addressed in v2.12.25–27)

---

## Nothing Currently Broken

No known issues. Admin should hit "↻ Refresh Data" on `/newdeals` after v2.12.24 deploys to rebuild cache with `is_software` flags.

---

## Next Steps

- **Product Hunt listing** — ready to submit. Prep: screenshots, short description, pick a launch day with a few upvoters lined up.
- **Reddit posts** — proven to convert. Targets: `r/guitarpedals`, `r/WeAreTheMusicMakers`, `r/Bass`, `r/drums`. Lead with category-specific deal-finding angle.
- **SEO — watch Search Console** — give it 4–6 weeks after v2.12.25–27. Expect CTR improvement on existing impressions first (description change), then possible ranking lift on location queries (noscript store list). Request Indexing in Search Console to speed up recrawl.
- **Android app** — WebView wrapper is the fastest path to Play Store. Needs 14-day closed test with 12+ opted-in testers before production.
- **Monetization** — Reverb affiliate (ShareASale) is the cleanest fit — inline sponsored rows contextual to nearby items. Freemium (email/SMS alerts, more watch list slots) avoids ads entirely.

---

## Where to Go for More

- Full architecture, all routes, auth flow, security hardening history, mobile layout details: **`~/Desktop/gc_tracker/HANDOFF.md`**
- Version history back to v2.8.0 is in HANDOFF.md under "Recent Changes" sections
