# GC Gear Tracker — Session Handoff Prompt
*Generated: 2026-05-22 · Version: v2.12.20 · Live at: gcgeartracker.com*

Use this at the start of a new session to bring Claude up to speed instantly.

---

## The Project

**GC Used Inventory Tracker** (`gcgeartracker.com`) is a Flask web app that tracks Guitar Center used inventory. Users create accounts (username/password or Google Sign-In) and get items flagged NEW since their last scan. Watch list, want list, favorites, and saved searches all sync across devices via server-side user accounts. A separate `/cl` page does Craigslist used gear search.

- **Repo**: `cboehmig-lab/gc-tracker` on GitHub → auto-deploys to Railway on every push to `main`
- **Python entry**: `gc_tracker_app.py` (single file, ~8000+ lines)
- **Static assets**: `static/gc.js`, `static/gc.css`, `static/admin.js`, `static/og-image.svg`
- **Local workspace**: `~/Desktop/gc_tracker/`
- **Full technical reference**: read `~/Desktop/gc_tracker/HANDOFF.md` before making any changes

---

## Critical Rules (read before touching anything)

1. **Never write inline JS** — all JS lives in `static/gc.js`. CSP blocks inline scripts.
2. **No inline onclick attributes** — use `data-*` attributes + `addEventListener`.
3. **Git pushes must come from the Mac terminal** — the Cowork sandbox gets a proxy 403 on GitHub pushes. Always use: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`
4. **Sandbox git lock files**: if `git commit` fails with "cannot lock ref HEAD", the Mac owns the lock. Tell the user: `rm ~/Desktop/gc_tracker/.git/HEAD.lock && git add ... && git commit ... && git push`.
5. **Version bump**: only change `APP_VERSION` in `gc_tracker_app.py` — the `<!-- __VER__ -->` placeholder in `HTML_TEMPLATE` auto-propagates it everywhere.
6. **The Python/JS backslash gotcha is resolved** — JS is in `.js` files so Python no longer mangles `\\`. Don't re-introduce inline scripts.

---

## Architecture in 60 Seconds

**Desktop filter bar**: two-row layout inside `#results-top-bar` (`flex-direction:column`). Row 1 = `.quick-filter-bar` (chips). Row 2 = `.results-hdr` (dropdowns + search + Save/Clear buttons). Mobile: `#results-top-bar{display:contents}` — wrapper is invisible, children flow directly in `#res-panel`.

**Dropdowns that escape overflow clipping**: use `position:fixed` + JS `getBoundingClientRect()` — NOT `position:absolute`. `.right` has `overflow:hidden` which clips absolute children. `#ss-dropdown` and `#price-dd-panel` both use this pattern.

**Sticky table headers (v2.12.16)**: `border-collapse:separate; border-spacing:0` on `table`. `th{position:sticky; top:var(--tbl-hdr-top,88px)}`. The CSS variable is set by `_applyFrozenHeaderOffset()` in JS, called after each render and on resize.

**Server-side browse**: `POST /api/browse` — reads `gc_category_cache.json`, applies all filters in `_apply_base()`, returns 50 items/page. Space-separated search terms are AND'd (v2.12.13).

**NEW detection**: anchor-based per-user. `threshold = _norm_anchor` (user's "top of table" high-water mark). Wall-clock time never contaminates the threshold (v2.12.2). Anchor persisted server-side in `user_data.last_anchor`.

**Sync merge strategy**: server-wins for watchlist, keywords, saved_searches (so deletions propagate cross-device). Falls back to local only if server record is empty (first sync). `last_run`/`new_ids`: most-recent-wins.

**Mobile**: `_isMobile()` = `window.innerWidth <= 820px`. Bottom sheet pattern for store panel + filter panel. Swipe-to-dismiss on both. Mobile paginator is `position:static` (inline at end of list).

---

## Current State: v2.12.20 (deployed ✅)

### Recent changes

- **v2.12.17** — Watchlist cross-device deletion fix. `_loadAndMergeServerData()` now uses server-wins for watchlist (same as keywords). Local localStorage no longer overrides server deletions.
- **v2.12.18** — SEO fundamentals: `<title>` optimized, `<meta name="description">`, Open Graph tags, canonical URL, SVG favicon, `GET /robots.txt`, `GET /sitemap.xml`. Google Search Console + Bing Webmaster Tools verified, sitemaps submitted.
- **v2.12.19** — Google Search Console HTML verification route (`/google73eeaa5f083d2e84.html`).
- **v2.12.20** — OG image (`static/og-image.svg`, 1200×630): dark-themed stats bar + sample results table. `og:image` + `twitter:image` meta tags wired up. `twitter:card` = `summary_large_image`.
- **v2.12.16** — Sticky table column headers working. `_applyFrozenHeaderOffset()` sets `--tbl-hdr-top` CSS variable to `#results-top-bar.offsetHeight` after each render.
- **v2.12.13** — In-page search: space-split AND matching (`_apply_base()` pre-splits `filter_q` by spaces before `_compile_query()`).
- **v2.12.12** — Multi-category selection race condition fix (`_populateFiltersFromServer()` reads live `window._selectedXxx` vars, not stale response).
- **v2.12.11** — Two-row filter bar + `overflow-x:hidden` on `.results`.

### Traffic / scale
- ~854 unique visitors/week (Google Analytics)
- 132 registered accounts
- Organic + Reddit-driven (r/guitar post drove a batch of signups)
- Google Search Console + Bing Webmaster Tools both active as of 2026-05-22

---

## Nothing Currently Broken

No known issues. v2.12.17 fixed the watchlist cross-device sync bug. v2.12.16 fixed sticky table headers.

---

## Next Steps

- **Product Hunt listing** — ready to submit. Good fit for indie tool launch. Prep: screenshots, short description, pick a launch day with a few upvoters ready.
- **Reddit posts** — proven to convert. Next targets: `r/guitarpedals`, `r/WeAreTheMusicMakers`, `r/Bass`, `r/drums`. Lead with category-specific deal-finding angle.
- **Android app** — WebView wrapper is the fastest path to Play Store. Needs 14-day closed test with 12+ opted-in testers before production.
- **Monetization** — Reverb affiliate program (ShareASale) is the cleanest fit. Direct contextual affiliate links on item rows. AdSense viable at current traffic scale (~3,400 uniques/month).

---

## Where to Go for More

- Full architecture, all routes, auth flow, security hardening history, mobile layout details: **`~/Desktop/gc_tracker/HANDOFF.md`**
- Version history back to v2.8.0 is in HANDOFF.md under "Recent Changes" sections
