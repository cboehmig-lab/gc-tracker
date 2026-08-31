# GC Gear Tracker — Session Handoff Prompt
*Generated: 2026-08-31 · Version: v2.16.15 (Postgres migration Phase B: scan dual-write, no read-path change; not yet pushed) · Live at: gcgeartracker.com — v2.16.13 pushed and confirmed running gunicorn*

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
3. **Git pushes must come from the Mac terminal** — the Cowork sandbox gets a proxy 403 on GitHub pushes. As of v2.13.3, `origin` points at the SSH URL (`git@github.com:cboehmig-lab/gc-tracker.git`), so the normal `git push origin main` works AND keeps the ahead/behind count accurate. (Previously pushes went to the raw SSH URL while `origin` was HTTPS — pushes landed but `origin/main` tracking never updated, causing a phantom "ahead N" forever.)
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

## Current State: v2.16.15 — Postgres migration Phase B: scan dual-write (2026-08-31, not yet pushed)

**Postgres migration in progress** — see `POSTGRES_MIGRATION_PLAN.md` for the full 6-phase plan.
Phase A is done and VERIFIED against real data (Chuck ran the backfill: 91,686 items, exact
match). Phase B (this version) makes `_run()` mirror every scan into Postgres in a background
thread — additive only, no read path touches Postgres yet. Still silently a no-op in production
until `DATABASE_URL` is actually deployed to `web` (it's referenced but pending — deploying it
restarts `web` once; that hasn't happened yet, waiting on Chuck's timing preference). Do not
build Phase C (the diff harness) or touch any read path until dual-write has been running against
real production scans for the period Chuck decides on.

## Current State: v2.16.14 — Postgres migration Phase A: schema + backfill script (2026-08-31, not yet pushed)

**Postgres migration in progress** — see `POSTGRES_MIGRATION_PLAN.md` (repo root) for the full 6-phase plan. Railway Postgres is provisioned (project `serene-determination`) and `DATABASE_URL` is referenced into the `web` service's variables but left undeployed. `gc_tracker_app.py` now creates the `items` table schema at startup IF `DATABASE_URL` is set (it isn't yet, in production) — no request path reads/writes Postgres. The backfill script (`migrate_cat_cache_to_pg.py`) is written but **not yet run** — must run from Chuck's own Mac terminal (no network path to Railway from the Cowork sandbox). Do not build Phase B (dual-write) or touch `/api/browse`/`_run()` until Chuck has reviewed Phase A and the backfill has actually run successfully.

## Current State: v2.16.12 — NEW-detection anchor/sold-marking coverage-gap fix + /api/browse perf (2026-08-31)

**⚠️ Railway gotcha found during rollout:** Settings → Deploy → Custom Start Command in the Railway dashboard overrides `Procfile` silently — it was hardcoded to `python gc_tracker_app.py` and had to be updated by hand to match. See HANDOFF.md's top section. Check that field too if you ever change the start command again.

**⚠️ Read before touching `--workers` in Procfile:** it's `--workers=1` on purpose. This app coordinates scans and SSE fan-out via in-process global state (`_cat_cache`, `_run_queues`, `_current_run_id`, locks). Multiple gunicorn worker *processes* don't share memory — bumping `--workers` would reintroduce a worse version of the scan-hang bug just fixed (a client's `/api/run` and `/api/progress` could land on different processes that have never heard of each other's run_id). Concurrency comes from `--threads=8` instead. See HANDOFF.md's v2.16.10 entry for the full reasoning.

### Recent changes (this session)

- **v2.16.15** — **Postgres migration Phase B: scan dual-write.** `_run()` now mirrors every
  scan's results into Postgres in a fire-and-forget background thread (started right after
  `_save_cat_cache()`, never blocks the scan's "done" message) — bulk upsert of every SKU this
  run touched, plus a sold-marking anti-join `UPDATE` that replays the JSON path's IDENTICAL
  v2.16.11 coverage-gap-safe condition/scope. Purely additive, best-effort (any Postgres failure
  is caught and logged, never surfaced), and still a no-op end-to-end until `DATABASE_URL` is
  deployed to `web` (not yet — pending Chuck's timing call on the one-time restart that deploying
  it causes). Sold-marking SQL validated against a scratch Postgres covering nationwide,
  store-scoped, and empty-scan-result cases. Known gap: `/api/populate-store-data` and
  `/api/fill-gaps` (rare admin endpoints) aren't mirrored — scope was `_run()` only. `py_compile`/
  `node --check` clean, no JS touched.
- **v2.16.14** — **Postgres migration Phase A: schema + backfill script.** No request-path
  behavior change. New `pg_schema.sql` (the `items` table DDL, `sku`-keyed, `date_listed` kept
  as TEXT deliberately) shared by the app's new guarded `_init_pg_schema()` (runs at startup only
  if `DATABASE_URL` is set — it isn't yet in prod) and the new `migrate_cat_cache_to_pg.py`
  backfill script (writes `gc_category_cache.json` into Postgres via bulk `ON CONFLICT` upsert,
  not yet run — needs Chuck's terminal, sandbox has no network path to Railway). Postgres
  provisioned on Railway, `DATABASE_URL` referenced into `web`'s variables but undeployed.
  `requirements.txt` +psycopg2-binary. `py_compile`/`node --check` clean; DDL and upsert SQL
  validated against a scratch Postgres in a sandbox first. Full plan: `POSTGRES_MIGRATION_PLAN.md`.
- **v2.16.13** — **Periodic malloc_trim to fix Railway memory growth.** Root-caused the
  production memory sawtooth (2GB→7-8GB+ between deploys, never decaying) via local load-test
  repro: confirmed glibc high-water-mark RSS under concurrent `/api/browse` traffic, not a Python
  leak — `malloc_trim(0)` reliably reclaimed it in testing (~1.2GB→~410MB). Shipped a daemon
  thread calling `gc.collect()` + `malloc_trim(0)` every 5 minutes (Linux-only, no-ops on
  macOS/local). Mitigation, not the structural fix — see v2.16.14 above for the Postgres
  migration addressing the actual mechanism. Also found, not fixed: `_cleanup_run_queue()` is
  dead code (a separate, smaller SSE-subscriber leak). `gc_tracker_app.py` only. Pushed and live.
- **v2.16.12** — **/api/browse: single-pass facet counts + single sort.** Followed up on the speed question with real measurement (scratch venv, real ~92K-item cache, cProfile). flask-compress confirmed still working (~79% reduction on /api/browse JSON) — not the bottleneck. Found the real cost: 4 separate facet-count passes (brand/condition/category/subcategory) plus TWO full-list sorts (date/price/etc, then a second pass just to bubble NEW items to the top) on every call. Merged the 4 facet-count passes into one combined pass; replaced the second sort with an O(N) stable partition into 3 tiers (new+want / new-only / rest) instead of a second O(N log N) sort. Verified byte-identical output across 8 request shapes (filters, sorts, pagination) via Flask test-client A/B diff against the real cache. ~278.6ms → ~244.5ms per warm call (~12% faster), measured over 15 calls each. `gc_tracker_app.py` only, `py_compile` clean, no JS changes. See HANDOFF.md for full detail.
- **v2.16.11** — **scan coverage gaps no longer corrupt the NEW-detection anchor or falsely mark items sold.** Root-caused Chuck's "genuinely new items sometimes don't get flagged NEW" report: `scrape_store()` silently swallows a page-1 fetch failure for any one store (routine given ~298 stores fanned out at once) — the scan completes "successfully" but is missing that store's data. Two silent consequences: (1) `_run()` still persists `new_anchor = max(date_listed across all_products)` as the user's GLOBAL anchor even though it's missing whatever the failed store's freshest items are — future scans then silently skip flagging genuinely-new items in that store; (2) the failed store's previously-cached items get incorrectly marked sold (treated the same as a store that legitimately returned zero items). Fix: `scrape_store()` now returns a `complete` flag (with one retry on a page-1 error), `_run()` tracks which stores/pages were incomplete, excludes them from sold-marking, and skips advancing the anchor entirely on any run with a coverage gap (safe trade-off — worst case an already-seen item gets re-flagged NEW later, never the reverse). Verified with a from-scratch test: `_run()` called directly with mocked Algolia responses, one store always failing — confirmed anchor doesn't advance, failed store's item isn't marked sold, and a fully clean scan still works exactly as before (no regression). Not confirmed against Chuck's real production account (no live data access this session) — Chuck confirmed he always scans with all stores selected, which rules out the "deliberate subset selection" variant of this bug as his trigger, but per-store fetch failures are independent of selection width. `gc_tracker_app.py` only, `py_compile`/`node --check` clean. See HANDOFF.md for full detail.
- **v2.16.10** — **gunicorn switch + scan-hang fix.** Two issues investigated after Chuck reported the site "didn't get faster, maybe got slower" plus a Reddit report of it being "buggy" (no detail available). (1) `Procfile` was still running Flask's dev server (`python gc_tracker_app.py`) — never actually switched to gunicorn despite it being discussed earlier in the project. Now `gunicorn gc_tracker_app:app --workers=1 --worker-class=gthread --threads=8 --timeout=0 --graceful-timeout=30 --bind=0.0.0.0:$PORT`; `gunicorn` added to `requirements.txt`. Also found and fixed a real bug in the process: startup bootstrap (`_load_cat_cache()`, `_load_cookies()`, store-list check) was inside `if __name__ == "__main__":`, which gunicorn never runs — moved to unconditional module-level code so it runs under any entry point. Verified locally: gunicorn boots with the exact Procfile flags, `/` and `/api/stores` both return 200, clean shutdown. (2) The previously-unresolved scan-hang bug (single-store scan pinging 10+ min, no error, no completion) — traced to `as_completed()` calls with no timeout in `_run()`'s two `ThreadPoolExecutor` blocks; if one worker thread never returns (a slow-trickle response can evade `requests`' own `timeout=`, which only bounds silent gaps, not total time), the loop blocks forever and the outer `try/except` is never reached. Added hard wall-clock ceilings (`STORE_SCAN_TIMEOUT = max(120, stores*12)`, `PAGE_BATCH_TIMEOUT = 90`/batch) via `as_completed(futures, timeout=...)`, switched pool cleanup to `shutdown(wait=False)` so a stuck thread can't block exit either. `gc_tracker_app.py`, `requirements.txt`, `Procfile`. `py_compile` clean. See HANDOFF.md for full detail.
- **v2.16.9** — **condition_note ⓘ: instant custom tooltip.** Replaced the native `title=` attribute (browser-controlled ~600ms-1s show delay, not adjustable) with a JS-driven tooltip: `.cond-info-icon` now carries `data-tooltip="..."`, a single shared `#cond-tooltip` node (added to `HTML_TEMPLATE` next to `#ss-dropdown`, same "escape the table's overflow:hidden via position:fixed" reasoning) is positioned via `getBoundingClientRect()` on delegated `mouseover`/`mouseout` listeners and shown synchronously — no delay. Desktop-only, unchanged elsewhere.
- **v2.16.8** — **condition_note ⓘ: normal cursor.** Chuck didn't want the OS help/question-mark cursor on hover — `.cond-info-icon` (`static/gc.css`) changed from `cursor:help` to `cursor:default`. One-line change.
- **v2.16.7** — **condition_note UI tweak: dedicated ⓘ icon.** Chuck asked for a distinct hoverable ⓘ icon next to the condition value instead of a dotted-underline hover-anywhere-on-the-text cue. `_buildRowHtml` (`static/gc.js`) now appends `<span class="cond-info-icon" title="...">ⓘ</span>` after the condition text; `static/gc.css` swapped `.cond-has-note` for `.cond-info-icon` (green `#4ade80`, bold, matches the Want List modal's existing ⓘ button styling). Backend/data plumbing unchanged from v2.16.6. Desktop table only, same as before.
- **v2.16.6** — **condition_note: "Condition & Details" hover tooltip.** Extracts the freeform staff note (e.g. "Includes Hardshell Case") that sometimes follows a "Condition & Details" marker in Algolia's `longDescription` field, via new `_extract_condition_note()`. Wired through `parse_products()` → `_cat_cache` → `_build_base_item_list()` → `/api/browse` → and the SSE small-scan `fmt(p)` path — same 5-location pattern as the `is_vintage` flag. Frontend: desktop table only (`_buildRowHtml` in `static/gc.js`); mobile card/list views untouched, per Chuck's request. Empty for existing cached items until their next scan (same rollout caveat as `is_vintage`). `gc_tracker_app.py` + `static/gc.js` + `static/gc.css`. See HANDOFF.md for full detail.
- **v2.16.5** — **(reverted)** temporary Algolia hit-shape probe used to investigate the above; superseded by v2.16.6.
- **v2.16.4** — **E2: /api/browse base-list memoization.** Chuck reported the page feeling slower — scan finds a few hundred items, table takes 10-15s to refresh. Root cause: he browses all 298 stores, so every scan lands on server-side browse, and `/api/browse` rebuilt its entire ~92K-item list from scratch on every call. New `_build_base_item_list()` memoizes the expensive per-item work (price/date formatting, lowercasing) by cache mtime; each request now does a cheap filter/annotate pass instead. Verified byte-identical output against the real 91,686-item production cache (0 mismatches) and benchmarked: ~281ms every call before → ~93ms (all-stores) / ~11ms (typical single-store) after cache warms. Registered users nearly doubled recently, so less GIL-hold-time per request should also ease queuing under concurrent load, likely explaining the gap between the audited per-call cost and the reported 10-15s. `gc_tracker_app.py` only. See HANDOFF.md for full detail.
- **v2.16.3** — **Colon-prefix OR syntax.** Chuck's friend reported that entries like `"Mesa", -combo, Angel; Blues; Electra; ...` "ignore Mesa and -combo, return everything" — confirmed as documented `;`-clause behavior (no cross-clause propagation), not a bug, but a real gap for the common "brand + OR'd models + exclusion" case. New syntax: `prefix : b1; b2; b3` applies the prefix to every OR branch — `Mesa, -combo: Angel; Blues; Trem`. Pure preprocessing expansion feeding the existing v2.16.0 clause compilers, no new matching logic. Works in both the want list and search box. No colon = zero behavior change (verified). `gc_tracker_app.py` + `static/gc.js`. See HANDOFF.md for full verification detail.
- **v2.16.2** — **Accessibility: Want List ⓘ button bigger + brighter.** Chuck has retinitis pigmentosa; the v2.16.1 ⓘ button was too small/low-contrast to spot. Green-accented border+text, bolder, larger padding; popover border/text also brightened and enlarged. `search-info-btn` (search bar) has the same subtle style, left as-is pending a decision on whether to match it.
- **v2.16.1** — **Want List modal syntax popover.** User-reported: v2.16.0's NOT/OR syntax block in the Want List modal's pinned header left only ~2 lines visible in the scrollable keyword list. Moved the syntax reference into a click-to-open ⓘ popover (`#kw-info-btn`/`#kw-info-popover`), mirroring the existing search-box pattern (`#search-info-btn`/`#search-info-popover`). Header now one short line; `#kw-list` gets the space back. `gc_tracker_app.py` + `static/gc.css` + `static/gc.js`. Verified: py_compile + node --check clean.
- **v2.16.0** — NOT (`-`) / OR (`;`) query operators in search box + want list, arrow-key pagination, saved-search-count parity fix. Pushed 2026-07-14.
- **v2.15.4** — **Compression + cache-busting (audit S7 closed).** Railway doesn't gzip (verified by curl). Added flask-compress (new dep) with two required non-defaults: `text/javascript` in `COMPRESS_MIMETYPES` (Flask 3.x mimetype for .js) and `gzip` in `COMPRESS_ALGORITHM_STREAMING` (static files are streamed; default excludes gzip). `_version_static()` adds `?v=APP_VERSION` to all static js/css refs in the three templates → `SEND_FILE_MAX_AGE_DEFAULT` = 1 year. gc.js 195KB → 46KB wire; SSE untouched.
- **v2.15.3** — **Audit "do now" items closed.** Daily `gc_users.db` backup: `_maybe_backup_users_db()` off the after_request hook — `VACUUM INTO DATA_DIR/backups/gc_users_YYYYMMDD.db` once per UTC day, keeps 7, lock-guarded, failure-tolerant. SQLite **WAL** enabled in `_init_user_db` (persistent). SSE **error-leak fix**: the three populate/fill-gaps handlers no longer broadcast `str(e)` over the progress stream (generic message + server log). The only remaining "do now" from the audit is the gzip check (`curl -sI -H 'Accept-Encoding: gzip' https://gcgeartracker.com/static/gc.js` from the Mac).
- **v2.15.2** — **Store page → app pre-filtered deep link.** Landing-page CTA now links `/?store=<slug>`; new `_applyStoreDeepLink()` in gc.js (runs after loadData/loadState) matches the slug against `allStores`, sets `_selectedStores` to that one store, `renderList()` auto-refetches. Param stripped via replaceState; unknown slugs ignored. Completes audit S6 for `?store=` (`?q=` still deferred).
- **v2.15.1** — **Store-page meta description double-escape fix.** All ~298 `/store/<slug>` pages rendered "&amp;" literally in meta/og descriptions (Google snippet shows it verbatim). Cause: v2.15.0 escaped category names when building `desc` AND at interpolation. Now built plain, escaped once. Verified on rendered attributes. Also: live store count is 298 (local repo cache stale at 240); GSC sitemap resubmitted (300 URLs processed); metro pages (/chicago) in design discussion, no code.
- **v2.15.0** — **July 2026 audit bundle + per-store SEO landing pages.** Audit Phases 1–3 (security, SEO, efficiency) are in `AUDIT_REPORT_2026-07.md`; the apply-now findings shipped as one bump. **NEW `GET /store/<slug>`** (e.g. `/store/boise`): server-rendered zero-JS city pages (live count, category counts, newest-50 table, ItemList+Breadcrumb JSON-LD), memoized per store by cat-cache mtime — the fix for the Search Console CTR problem (page-1 city queries, generic title, ~0 clicks). `sitemap.xml` now lists all ~240 store pages w/ `lastmod`; homepage noscript city list links to them. Security: `MAX_CONTENT_LENGTH=1MB` (unauth body DoS), `/api/run` stores cap `[:300]`, deleted dead `/login`+GET `/logout`+`LOGIN_PAGE`, Algolia-health refresh lock. Efficiency: `/api/saved-search-counts` now uses the memoized cache (was re-parsing 51MB from disk per call, ~400ms GIL-held), atomic new-deals cache write, deleted dead legacy watchlist/keywords/favorites endpoints + orphaned helpers. SEO: `og-image.png` (1200×630 — SVG og-images don't render on Reddit/Discord/FB/X), real `static/favicon.svg` (data-URI favicons get no SERP icon), dropped `user-scalable=no`. Repo: `git rm --cached` the 51MB cache + other ignored-but-tracked files, deleted dead `gc_inventory_tracker.py`. **Post-deploy: resubmit sitemap in Search Console.** Deferred items in the audit report's backlog (E2 browse memoization, E4 local-render consolidation, S6 deep links, S7 cache-busting, gunicorn, module split, DB backup).
- **v2.14.4** — **Want-list comma-AND fix** (user-reported). Multi-term entries using the comma-AND operator (e.g. `Whirlwind, box`) stopped matching once a want list had **>30 combined exotic terms** (wildcard+quoted+comma), because v2.13.1's anti-DoS cap `_EXOTIC_KW_CAP=30` was *shared* across all three; quoted terms sort first (start with `"`) and fill the slots, so comma terms drop — hence "quotes work, commas don't." **Not** from the v2.14.x work (cap dates to v2.13.1). Fix (`gc_tracker_app.py`, `/api/browse` matcher): comma-AND gets its own cap (`_AND_KW_CAP=200`) + a sound required-tokens pre-filter (`_req <= _toks` before the regexes) so it stays cheap on the unauthenticated endpoint; wildcard/quoted keep the tight 30-cap. Verified behavior-identical vs old across the full 91,686-item cache (0 diffs) and the repro now matches. Detail in HANDOFF.md "v2.14.3 → v2.14.4".
- **v2.14.3** — **Results-table horizontal scroll** (follow-up to v2.14.2 #3). Truncation alone still left the table wider than the panel on some rows, and `.results` was `overflow-x:hidden`, so Date Added + Location/Store were clipped with no scrollbar. `gc.css` (2 props): `.results` → `overflow-x:auto`; `#results-top-bar` → add `left:0` to its `position:sticky;top:0` so the filter bar stays pinned while the table scrolls. Sticky header untouched (vertical scroll still on `.results`); mobile unaffected (its media query overrides `.results` to `overflow:hidden` and scrolls `#res-body`). Truncation + `title=` tooltips from v2.14.2 kept. **Also folded in**: brandless rows now show a muted **"(none)"** in the Brand column (`_buildRowHtml` + `.brand-none`) instead of a blank cell, matching the filter label; `data-brand` stays `""` so sorting is unchanged. Detail in HANDOFF.md "v2.14.2 → v2.14.3".
- **v2.14.2** — **Three user-reported fixes** (Discord); no cache rebuild, no new endpoints; `gc_tracker_app.py` + `static/gc.js` + `static/gc.css`. (1) **Filter dropdowns no longer hidden by the paginator**: Brand/Cond/Cat/Subcat panels were `position:absolute;z-index:50` trapped in `#results-top-bar`'s stacking context (`z-index:2`), so the sticky opaque `.paginator` (`z-index:5`) painted over them when the result set was short (≲5 items). Moved the four panels to `position:fixed;z-index:500` + a new `_positionFixedPanel()` helper (the Price/Saved-Search pattern), which escapes the top-bar context. Desktop-only (mobile uses the accordion). (2) **"(none)" brand**: ~108 brandless items (`brand==""`) are now filterable — `NO_BRAND_LABEL` + `_brand_ok()` in `/api/browse` (facet count via `empty_label` + both filter passes) and matching logic in `/api/saved-search-counts`. Flows through the existing generic brand dropdown UI — no frontend change; plain equality, no regex/DoS surface. (3) **Long Category/Subcategory truncation**: capped col 8/9 `max-width` (160/200px) in `gc.css` so they truncate with `…` instead of pushing Date Added + Store off-screen (`.results` is `overflow-x:hidden`); full text shown via `title=` hover tooltip on both cells in `_buildRowHtml`. Detail in HANDOFF.md "v2.14.1 → v2.14.2".
- **v2.14.1** — **Algolia key health endpoint** (`GET /api/health/algolia`) + daily Cowork monitor (`gc-algolia-key-health`, 08:00 local). The GC Algolia search key is the single point of failure for scans (rotation → 401/403 → silent scan death). The endpoint runs the scanner's used query at `hitsPerPage:0`, returns `{ok, nbHits, http_status}`, cached ~15 min (≤ ~96 probes/day — no quota abuse, public, no secret leaked). Monitor alerts on key death / 0 results / unreachable. Pending push.
- **v2.14.0** — **Vintage filter** (new feature). "🎸 Vintage" quick-filter chip (right of Price Drops) showing only gear GC classifies as vintage. Uses GC's own authoritative signal — the raw Algolia hit's **`premiumGear == "Vintage"`** field (~2,127 used items) — captured at scan time as a per-item `is_vintage` flag (the `is_software` pattern). `/api/browse` takes a `vintage_only` boolean → `_apply_base()` keeps `is_vintage` items (plain boolean, no regex/DoS surface). The chip is a **composable** content filter like Price Drops: respects store selection, folded into `_captureFilterState`/`_restoreFilterState` + saved searches; it is NOT a Watch/Want-style nationwide takeover. Verified cleaner than a title heuristic — `premiumGear` excludes the modern "Fender American Vintage"/"American Vintage II" reissues, Vintage Reissue amps, and the modern "Vintage" brand (overlap 0–3 items) while keeping genuine vintage; the `name.startswith("Vintage")` heuristic carried ~128 false positives. ⚠️ The cache has no `is_vintage` until the **first scan after deploy** — the chip is empty until then (same caveat as the v2.12.24 software-flag rollout). Algolia findings documented by `probe_vintage*.py` (gitignored).
- **v2.13.3** — Mobile ZIP apply fix: iOS's `inputmode="numeric"` keypad has no Return/Go key, so the ZIP input now auto-applies ZIP Sort when 5 digits are present (covers AutoFill too), with `blur()` on mobile to dismiss the keypad; `enterkeyhint="go"` added for Android. Also fixed the phantom "ahead N" git state (see Critical Rule 3).
- **v2.13.2** — **ZIP distance filter** (the planned feature — now done). "Within [Any/5/10/25/50/100 mi]" select under the ZIP input, shown only in ZIP Sort mode; filters the store list AND narrows `_selectedStores` (snapshot/restore via `_preRadiusSelection`, same pattern as `_preFavsSelection`) so browse results actually filter. Un-geocoded "(?)" stores excluded by any finite radius; Watch/Want List unaffected (`all_stores:true`); Favorites toggle and saved-search apply reset radius to Any. Not persisted, like the ZIP. Full detail in HANDOFF.md "Recent Changes (v2.13.1 → v2.13.2)".
- **v2.13.1** — Full-site audit fixes: per-type want-list keyword caps (preserves large lists, bounds wildcard DoS); atomic 53MB cat-cache write (temp + `os.replace`, no more truncation-wipe). See HANDOFF.md + `AUDIT_REPORT_2026-06-12.md`.
- **v2.13.0** — Want-list fix + `/api/browse` performance overhaul (minor bump; `gc_tracker_app.py` only, no JS, no cache rebuild). (1) **Want lists >50 terms no longer drop matches** — the v2.12.31 `keywords[:50]` DoS cap was silently breaking power users (real cases: 220 and 73 terms). The matcher was rewritten (plain words → set membership; phrases/wildcards → one alternation regex), verified behavior-identical (32K fuzz cases, 0 mismatches), and the cap raised to **750 logged-in / 250 guest** after dedupe. (2) **`_load_cat_cache()` no longer re-parses the 53MB cache on every browse** — memoized by mtime (**~400ms → ~1µs/call**), which also un-serializes concurrent request threads (GIL was held during the parse). (3) Defense-in-depth: `filter_q` token cap (12) + `/api/saved-search-counts` clamp. Full writeup: HANDOFF.md "Recent Changes (v2.12.36 → v2.13.0)".
- **v2.12.36** — Security posture (not a vuln): added `Cross-Origin-Opener-Policy: same-origin-allow-popups` to every response (scanner credit + cross-window isolation; safe with redirect-based OAuth) and an RFC 9116 `/.well-known/security.txt` (private report channel). Deliberately did NOT add CORP (would break OG-image social previews). Documented the one remaining CSP weakness (`style-src 'unsafe-inline'`) as a future refactor — no longer an active hole after the v2.12.35 escaping.
- **v2.12.35** — Security: fixed a stored XSS in the Craigslist render path. CL listing fields (title/price/location/url/image) are scraped from a world-writable source and were concatenated into `innerHTML` unescaped in `static/cl.js` and `static/gc.js` (`clRenderResults`). Now HTML-escaped + URL-allowlisted. (CSP `script-src 'self'` blocked script execution, but inline-style overlay phishing was live — Medium.) `newdeals.js` was already escaped.
- **v2.12.34** — Security: `/api/cl-search` now requires login (was an unauthenticated outbound-amplification vector — one call fans out to ~75 Craigslist markets); stopped leaking raw exception text; added the `_CL_CITIES` allowlist to `/api/cl-parse-test` (admin SSRF primitive). No UX impact (CL is sign-in-only by design).
- **v2.12.33** — Security: fixed an open-redirect bypass in the `?next=` param on `/api/auth/google` and `/admin/login`. `/\evil.com` passed the old `startswith("/")` check but browsers normalize it to `//evil.com`. New `_safe_next()` helper rejects backslashes, `//`, and control chars.
- **v2.12.32** — Security: closed three unauthenticated "write to a global file" endpoints that the v2.12.28 favorites fix missed. `/api/stores/refresh` → admin-only (it scrapes GC and overwrites the shared store cache — anyone could wipe the store list). `/api/watchlist` + `/api/keywords` (GET & POST) → require login. All are dead code (not called by any frontend).
- **v2.12.31** — Security: `/api/browse` unauthenticated CPU-DoS fix — capped the client `keywords` array (≤50, ≤100 chars each) and `filter_q` (≤200 chars). Each keyword compiles to a regex run over the ~92K-item cache; the array was previously unbounded. Same class as the v2.12.30 saved-search-counts cap.
- **v2.12.30** — Security: `/api/saved-search-counts` DoS fix — added login check and 50-search hard cap. No UX impact.
- **v2.12.29** — Security: fixed admin privilege escalation. `_is_admin()` now requires `google_id` to be set before trusting the email match — blocks password-account users from claiming admin by self-reporting the admin email at registration.
- **v2.12.28** — Security audit: hardened three unprotected endpoints. `/api/stop` now requires `run_id` echo. `/api/populate-store-data` and `/api/fill-gaps` now require admin session. `/api/favorites` now requires logged-in session. Full audit log in HANDOFF.md.
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

v2.13.0–v2.13.3 all pushed and deployed. No known issues. Still-open recommendations from the 2026-06-12 audit (not bugs): memoize the browse base list by cache mtime, single gunicorn worker instead of Flask dev server, SQLite WAL, back up `gc_users.db` — see `AUDIT_REPORT_2026-06-12.md`. Run `/admin/build-coords` if store coords coverage looks thin (un-geocoded stores are excluded by any ZIP radius).

---

## ✅ Security Audit: TWO ROUNDS DONE (v2.12.28 + v2.12.31–34)

**Round 1 (v2.12.28–30)** — see HANDOFF.md:
- v2.12.28: `/api/stop` (run_id validation), `/api/populate-store-data` + `/api/fill-gaps` (admin guard), `/api/favorites` (require login).
- v2.12.29: `_is_admin()` privilege escalation via self-reported email — now requires `google_id`.
- v2.12.30: `/api/saved-search-counts` CPU DoS — login + 50-search cap.

**Round 2 (v2.12.31–36, this session)** — full adversarial re-review. Round 1's "all other surface clean" was overconfident; round 2 found a related family of bugs and fixed them. See the "Security Audit Round 2" section in HANDOFF.md for the complete log (attack vector + severity + fix for each):
- **v2.12.31 (High)**: `/api/browse` unauthenticated CPU-DoS — capped `keywords` (≤50) and `filter_q` (≤200). The single biggest remaining public-abuse surface.
- **v2.12.32 (High+Med)**: `/api/stores/refresh` → admin (unauth scrape + store-list wipe); `/api/watchlist` + `/api/keywords` → login (the favorites fix's two missed siblings).
- **v2.12.33 (Med)**: open-redirect via `/\evil.com` backslash bypass in `?next=` — new `_safe_next()`.
- **v2.12.34 (Med+Low)**: `/api/cl-search` → login (outbound amplification) + no more leaked exception text; `/api/cl-parse-test` city allowlist.
- **v2.12.35 (Med)**: stored XSS in the Craigslist render path — scraped (world-writable) listing fields went into `innerHTML` unescaped in `cl.js` + `gc.js`. Now escaped + URL-allowlisted. Script exec was already blocked by CSP, but inline-style overlay phishing was live. (`newdeals.js` was already escaped; admin pages escape server-side.)
- **v2.12.36 (posture)**: added COOP header + RFC 9116 `/.well-known/security.txt`; documented `style-src 'unsafe-inline'` as the one known CSP weakness (future refactor, not an active hole).
- **Confirmed clean (re-verified)**: SSRF (cl-search allowlist + quoting), SQLi (parameterized), ReDoS (`re.escape` + new caps), SSTI, CSRF "no-Origin" path (SameSite=Lax + JSON content-type + admin tokens make it non-exploitable), admin escalation, OAuth state/email_verified, SECRET_KEY/CSP/HSTS/cookies, client-side *manipulation* of server behavior (the render-path XSS was the one client-side gap — now fixed in v2.12.35).
- **Documented Low (deferred)**: L1 dead `/login` + GET `/logout` CSRF (recommend deleting both routes — `session["logged_in"]` is confirmed dead code); L3 SSE exception strings; L4 malformed-input 500s; L5 unbounded `/api/run` stores array. Full detail in HANDOFF.md.

**Reddit comment ("still isn't secure")**: round 2 closed the most likely candidates — an unauthenticated endpoint that wipes shared state (`/api/stores/refresh`), trivial unauthenticated CPU-DoS (`/api/browse`), an OAuth open-redirect, and a **stored XSS in the CL search results** (post a Craigslist listing with HTML in the title, search for it — it rendered). That XSS is probably the single most likely thing a security-minded redditor actually poked at. We still can't know for sure what they meant, but these are the things a casual prober finds first.

App is ready for Reddit posts and Product Hunt once v2.12.36 is deployed.

---

## Next Steps (after security)

- **Product Hunt listing** — hold until after security audit. Good fit for indie tool launch.
- **Reddit posts** — proven to convert. Targets: `r/guitarpedals`, `r/WeAreTheMusicMakers`, `r/Bass`, `r/drums`. Hold until security is buttoned up.
- **SEO — watch Search Console** — give it 4–6 weeks after v2.12.25–27. Expect CTR improvement on existing impressions first, then possible ranking lift on location queries. URL Inspection → Request Indexing already done.
- **Android app** — WebView wrapper is the fastest path to Play Store. Needs 14-day closed test with 12+ opted-in testers before production.
- **Monetization** — Reverb affiliate (ShareASale) is the cleanest fit — inline sponsored rows contextual to nearby items. Freemium (email/SMS alerts, more watch list slots) avoids ads entirely.

---

## Where to Go for More

- Full architecture, all routes, auth flow, security hardening history, mobile layout details: **`~/Desktop/gc_tracker/HANDOFF.md`**
- Version history back to v2.8.0 is in HANDOFF.md under "Recent Changes" sections
