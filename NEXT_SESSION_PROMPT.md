# Next Session Prompt — v2.16.48 built (Phase F 5b-ii cutover): push after shadow sees store scans, then burn-in → 5c

**Update 2026-09-28 (later)**: v2.16.47 pushed + live. Pre-check: column parity PASS (503,119 = 503,119, all
20 columns), new_ids PASS; 18 watchlist SKUs missing from Postgres but ALSO missing from JSON → Chuck said
treat as passing. Shadow: 2 real nationwide scans clean. **v2.16.48 (5b-ii cutover) built + locally
verified, NOT yet pushed** — see HANDOFF.md v2.16.48.
1. Before pushing: read the v2.16.47 shadow (`_pg_scan_shadow` via `POST /api/browse?pg_shadow=1`) — want a
   few STORE scans and at least one scan with `sold_json > 0`, all `clean`. (Only 2 nationwide scans with
   0 sold so far.)
2. Push: `cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`,
   `git add gc_tracker_app.py static/gc.js HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
3. After deploy: footer v2.16.48; `_pg_scan_writes` (same `?pg_shadow=1` call) — `ok` rising with scans,
   `failed` 0; Railway log `[pg] scan saved: …` per scan; nationwide scan "done" now arrives ~10-20s later
   than before (the DB write moved before "done"); `/api/pg-parity-check` still 0 diffs; `/api/fill-gaps`
   and `/api/populate-store-data` → 404. Optionally re-run `/api/pg-precheck-5b` to see the 18 missing
   watchlist entries' name/store/date_added.
4. Burn in a few days (`failed` stays 0), then **5c = v2.17.0**: stop JSON writes; delete `_cat_cache`,
   `_load_cat_cache`, `_save_cat_cache`, `/api/pg-parity-check`, `/api/pg-full-backfill`, `/api/pg-precheck-5b`,
   `migrate_cat_cache_to_pg.py`; rework `/api/reset` + `/api/export-data` / `/api/import-data`; measure
   Railway memory (the ~500K-entry in-memory JSON dict goes away).

---

# Next Session Prompt — v2.16.47 built (Phase F step 5b-i shadow): push, pre-check, watch shadow, then 5b-ii

**Update 2026-09-28 (5b session)**: v2.16.47 built + locally verified, NOT yet pushed. No user-visible
change: every scan also computes its prior state from Postgres in shadow and diffs it against the JSON
path; plus a temporary pre-cutover check endpoint. See HANDOFF.md v2.16.47 (includes the full map of
`_cat_cache` in the scan path). Chuck approved: the 5b-i/5b-ii split, deleting the legacy
`gc_watchlist.json` upkeep, and deleting `_fill_gaps` / `_populate_store_data` in 5b-ii.

1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. Footer v2.16.47; deploy log clean (no schema change).
3. Pre-check (browser console on the site, admin):
   `await (await fetch('/api/pg-precheck-5b',{method:'POST'})).json()` then poll
   `await (await fetch('/api/pg-precheck-5b')).json()` until `status` is `done`.
   Need `result.user_skus.PASS` true (0 missing watchlist/new_ids SKUs — zero tolerance; if not,
   investigate each sample before anything else) and `result.column_parity.PASS` true (or only diffs
   explained by a scan that ran during the check — see `scan_running_at_start/end`; re-run if so).
   Note `legacy_watchlist_file` and `dead_tools` for the 5b-ii deletions.
4. After a few real scans (ideally one nationwide + several store scans):
   `(await (await fetch('/api/browse?pg_shadow=1',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all_stores:true})})).json())._pg_scan_shadow`
   Expect `clean == scans`, `errors` 0. Look at `recent[*].samples` for any diff; `waited_for_sync`
   counts scans that started while the previous background Postgres write was still running.
5. Then **5b-ii (v2.16.48)**: `_run` merges from Postgres prior (`_pg_scan_prior_fetch`), writes via a
   synchronous `_pg_write_scan` (upsert + sold `UPDATE … RETURNING sku`, one transaction, 3 attempts)
   BEFORE "done"; on failure: done-with-error, no JSON write, user anchor/last_run not advanced,
   `_pg_scan_writes` counters + `[pg] SCAN WRITE FAILED` log; JSON backup = merged rows + returned sold
   SKUs applied to `_cat_cache`, then saved (nothing reads it). Delete the shadow, `gc_watchlist.json`
   upkeep in `_run`, `_fill_gaps`/`/api/fill-gaps`, `_populate_store_data`/`/api/populate-store-data`,
   `populateStoreData` stub in gc.js. Test locally with the scan simulator approach (mock
   fetch_page/parse_products/scrape_store; see HANDOFF.md v2.16.47). Then burn-in, then 5c (v2.17.0).

---

# Next Session Prompt — v2.16.46 LIVE (Phase F step 5a done): next is 5b

**Update 2026-09-28 (later)**: v2.16.46 pushed + live-verified — store pages match browse totals
(Austin 526 / Emeryville 396 / Danvers 772, ~90ms), `/api/state` equals Postgres available count
(114,548; it lags up to 60s after a scan by design), saved-search counts, `/admin/users` (0.3s) and
`/admin/listing-patterns` (2.3s — reads every row's date; admin-only) all fine; 0 browse errors.
Step 3 below (5b) is next.


**Update 2026-09-28**: weekend burn-in clean (v2.16.45 `_pg_browse_errors` all 0). Step 5 split into
5a / 5b / 5c (Chuck approved). **v2.16.46 = 5a**, built + locally verified, NOT yet pushed: every
read-only user of the JSON catalog now reads Postgres (`/api/state`, store pages, admin users,
listing patterns, saved-search counts). See HANDOFF.md v2.16.46.
1. Push (`cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`, add gc_tracker_app.py static/gc.js
   HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md, commit, push).
2. Live checks: footer v2.16.46; `/api/state` total_items == `/api/browse` (all_stores) total_unfiltered;
   `/store/austin` title count == Austin browse total; saved-search badges fill; `/admin/users` and
   `/admin/listing-patterns` load.
3. **5b** (own session): scan write path Postgres-primary. `_run`/`_fill_gaps`/`_populate_store_data`
   read prior state (NEW detection, price drops, first_seen, sold flags) from Postgres instead of
   `_cat_cache`; `_pg_sync_scan` becomes a required synchronous write with retry + failure surfacing;
   JSON still written as a write-only backup. Pre-cutover check: every SKU in real users' watchlists
   and new_ids exists in Postgres (zero tolerance). Then burn-in, then **5c**: stop JSON writes,
   delete `_cat_cache`/`_load_cat_cache`/`_save_cat_cache`, `/api/pg-parity-check`, backfill tooling,
   rework `/api/reset` + import/export, and measure Railway memory.

---

# Next Session Prompt — v2.16.45 built (search-box prefix match): push, verify, then counters + step 5

**Update 2026-09-25 (latest)**: v2.16.45 built + locally verified, NOT yet pushed — the search box's
last word now prefix-matches (`sm81` → "SM81LC"); Want List unchanged. After pushing: footer
v2.16.45, search `sm81` nationwide should return the 5 SM81LCs (Emeryville ×2, Danvers ×2,
N. Fort Worth), `fender -combo` should still behave. Then the error-counter check and step 5 below.

---

# Next Session Prompt — v2.16.44 LIVE (Phase F step 4c done): check error counters, then step 5

**Update 2026-09-25 (later)**: v2.16.44 PUSHED + LIVE (footer confirmed). Live spot-check, all 200 with
0 errors/retries: plain all-stores 114,616 items (~230ms server), one store (~25ms), search box
`fender deluxe` (~108ms), `*50s*` 391 (~100ms), want-list-only with mixed shapes 4,595 (~200ms),
bogus sort_field → date sort, facets+price+sort page 2 (~410ms). `/api/pg-browse-diff-check` → 404.
Saved-search counts match browse totals (391 / 508). Remaining: steps 3-4 below.


**Update 2026-09-25**: 4b burn-in clean (live counters on v2.16.43: `error 0, ineligible 0`, no
fallback ever recorded since deploy). v2.16.44 = **step 4c**, built + locally verified, NOT yet
pushed: `/api/browse` is SQL-only; legacy Tier 1/Tier 2/Python matcher path, `_build_base_item_list`,
and all 4a tooling deleted (`/api/pg-browse-diff-check` is gone — counters now at
`POST /api/browse?pg_shadow=1` → `_pg_browse_errors`). DB error → 503 + "Couldn't Load Inventory"
message; one retry on a dead pooled connection. See HANDOFF.md v2.16.44.
1. Chuck pushes (`cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, commit, push).
2. Confirm deploy log clean + footer v2.16.44; spot-check plain browse, Want List, search box,
   a saved search, a `*50s*` search; `/api/pg-browse-diff-check` should 404.
3. After a day: in the browser console on the site (admin),
   `await (await fetch('/api/browse?pg_shadow=1',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all_stores:true})})).json()`
   → `_pg_browse_errors` (expect `error` 0; a few `retried` after a Postgres restart are fine).
4. Then **step 5**: retire the JSON catalog (scan writes → Postgres primary, `/api/saved-search-counts`
   JSON fallback, `/api/state` totals, scan NEW detection, everything that calls `_load_cat_cache`;
   short write-only JSON backup burn-in, then gone). Then Phase G.

---

# Next Session Prompt — v2.16.43 built (all wildcards in SQL): push, burn in, then 4c

**Update 2026-09-24**: burn-in check of the v2.16.40 cutover (counters since 2026-09-23T22:51Z):
`error: 0`, `ineligible: 119` (last: `'*50s*'`). Users still got results (legacy fallback), but 4c
removes the fallback. v2.16.43 (built + locally verified, NOT yet pushed) translates every wildcard
shape to SQL (leading/infix → ILIKE `%a%b%`, same as legacy) and tallies all fallback reasons in
`_pg_browse_fallbacks.reasons`. Steps:
1. Chuck pushes v2.16.43 (`cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, commit, push).
2. Confirm deploy + footer v2.16.43; spot-check a `*50s*` search in the search box.
3. After 1-2 days: `await (await fetch('/api/pg-browse-diff-check')).json()` → `fallbacks`.
   Expect `ineligible` ~0 and `error` 0; read `reasons` for anything left.
4. Then **4c** (delete legacy path + 4a tooling), then **step 5** (retire JSON).

---

# Next Session Prompt — v2.16.40 LIVE (Phase F step 4b cutover): burn-in, then 4c

**Update 2026-09-23 (latest)**: v2.16.40 = step 4b, PUSHED + LIVE and spot-checked (all request types served by `_pg_browse`, 0 fallbacks; Want List ~1.3s → ~0.15s, search box ~0.35s → ~0.12s, plain all-stores browse unchanged ~1s). Every `/api/browse` request now tries the unified
SQL path (`_pg_browse`) first, falling back per-request to the legacy Tier 1/Tier 2/Python path only
for an ineligible search or an exception (counted in `_PG_BROWSE_FALLBACKS`: admin-visible via
`?pg_shadow=1` → `_pg_browse_fallbacks`, and in `GET /api/pg-browse-diff-check` → `fallbacks`; resets
on deploy). Also supports trailing wildcards on multi-word/punctuated terms (`Takamine TSP*`). Chuck
approved accepting start-of-word-only wildcards. Rollback = default engine `"legacy"` in `api_browse()`.

After pushing: confirm deploy (no schema change), spot-check the site (plain browse, want list,
search box, a saved search), check `?pg_shadow=1` shows `_pg_browse: true`, re-run the diff check once
(now diffs legacy vs pg; expect the same results as v2.16.39 minus the 6 Takamine ineligibles). Then
burn in for a few days and check `fallbacks` (`error` should stay 0) before **4c**: delete the legacy
path, `_pg_tier1_browse`, `_pg_tier2_narrow_items`, and all 4a comparison tooling. Then **step 5**:
retire the JSON catalog (scan writes, `/api/saved-search-counts`, `/api/state` totals, scan NEW
detection → Postgres; short write-only JSON backup burn-in, then gone). Chuck confirmed that's the
end state: no JSON, no old search code.

## Phase G (proposed by Chuck, 2026-09-23): make the site faster and better for users

Starts after Phase F is finished (4c + step 5). Chuck's idea; scope not locked yet. Candidate list:
1. **Measure first**: per-request server timing logs for the main endpoints plus real browser page-load
   numbers, so we fix what users actually wait on instead of guessing.
2. **Plain all-stores browse (~0.8-1.2s)**: most of the time is the brand/category facet counts over
   ~114K rows. Options: short-lived cache of counts per store scope, fewer/combined queries.
3. **More than one server worker**: gunicorn is `--workers=1` because scan coordination + SSE state
   live in process memory; moving that state into Postgres would let the site serve several people at
   once without one slow request making everyone wait.
4. **Memory sawtooth / restarts**: largely caused by the in-memory JSON catalog, so step 5 should
   already help; re-measure after it.
5. **Page load + feel**: size of static/gc.js, image lazy-loading/sizing, cache headers, keeping old
   results visible while the next page loads, prefetching the next page, search-box debounce.
6. **User-facing improvements**: collect ideas from Chuck / user feedback (e.g. Discord).
7. **Search-box autocomplete** (Chuck, 2026-09-25): typing `stra` shows a dropdown of real inventory
   words ("Stratocaster (1,240)", "Strat"…). Design sketched: (a) `search_terms(term, count)` table
   built from `ts_stat` over `search_vector` (available items), rebuilt at the end of each scan
   (hook `_pg_sync_scan`), btree/`text_pattern_ops` index for prefix lookup; (b) `GET /api/suggest?q=`
   → top ~8 by count, >= 2 chars, completes only the LAST word of the query, rate-limited;
   (c) gc.js dropdown under `#res-search` — ~150ms debounce + abort stale requests, up/down/Enter/Esc,
   click/tap, `position:fixed` like `#ss-dropdown`, mobile sheet support. Est. one session
   (~50-80 lines Python, ~150-200 JS/CSS). Optional later: brand+model phrase suggestions,
   pg_trgm typo tolerance, store-scoped suggestions. Independent of step 5.

**Update 2026-09-23 (later)**: v2.16.39 PUSHED and LIVE. Full production diff check (123 accounts,
485 scenarios, ~11 min): 463 exact, 16 search-semantics mismatches, **0 plumbing suspects**, 6
ineligible, 0 errors. Avg current path ~1,040ms vs `_pg_browse` ~195ms. Dr.scientist gap confirmed fixed.
Every SQL-only difference is SQL being more forgiving in the user's favor (`K-Line`→"K Line",
`Gibson ES 335`→"ES-335", `Dr z`/`Dr. Z`/"Dr.z", double spaces, `WA‑84` with a non-ASCII hyphen, `Casio, CZ*`
which Python treats as a literal "casio, cz" regex). Python-only: only mid-word wildcards (`69*`→"ST69",
36 items in one saved search; `beyer* M*`). All 6 ineligible = ONE account's multi-word wildcard entry
`Takamine TSP*` (every scenario for that account falls back). Open decision before 4b: support a
trailing wildcard on a multi-word phrase (`takamine <-> tsp:*`) so that account doesn't need the
fallback, and accept the mid-word-wildcard tradeoff (already accepted in v2.16.32).

---

# (previous) v2.16.38 notes

## Where things stand

Phase F step 4 ("unify Tier 1 + Tier 2") is split into 4a / 4b / 4c. **4a is built and verified
locally, not yet pushed.**

- `/api/browse` is now a thin wrapper around `_browse_compute(data, *, logged_in, pg_diag, engine)`.
  Real requests use `engine="current"` — the same Tier 1 / Tier 2 / Python path as before.
- New `_pg_browse()` serves ANY browse request entirely in SQL (Tier 1 logic + filter_q as a WHERE
  predicate + want-list keywords as a `kw_match` expression feeding want-only, NEW+want tiering,
  `new_want_count`, per-item `kwMatch`). Not live — reached only through the admin comparison tools:
  - `POST /api/browse?pg_shadow=2` → normal response + `_pg_browse_shadow` diff report.
  - `POST /api/pg-browse-diff-check` (optional `{"max_accounts": N}`), then `GET` the same URL to
    poll → replays every account's real want list / favorites / saved searches through both engines.
- Translator fix: an all-negative want-list entry (`-fender`) is now a no-op instead of matching
  `fender`.
- Locally: 222/222 (30K catalog) and 231/231 (150K catalog) exact matches on "clean" synthetic want
  lists; DoS caps identical; adversarial mismatches are only the known text-semantics classes
  (mid-word suffix wildcards like `TS*`→"Gretsch", punctuation variants like `dr z`/`'69`, a lone `-`
  filter_q, `*muff` → ineligible). Full detail: HANDOFF.md v2.16.38, POSTGRES_PHASE_F_DESIGN.md
  Addendum 14.

## Concrete next steps

1. Chuck pushes v2.16.38 from his Mac terminal (`cd ~/Desktop/gc_tracker`, then `rm -f
   .git/index.lock`, commit, push).
2. Confirm the deploy is healthy (no schema change → plain restart, no `[pg] migrated...` line), and
   the site footer shows v2.16.38.
3. Run the production diff check while logged in as admin, from the browser console on
   gcgeartracker.com:
   ```js
   await (await fetch('/api/pg-browse-diff-check', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({max_accounts: 10})})).json()
   // then poll until status is "done":
   await (await fetch('/api/pg-browse-diff-check')).json()
   ```
   Start with `max_accounts: 10` to see how long it takes and how much load it adds (1 gunicorn worker,
   and the current engine's Python matching holds the GIL), then run the full set at a quiet time.
4. Triage: mismatches should ONLY be the known classes above (check each sample's `kw_explain` /
   `filter_q`). Any facet/total/order diff with no keyword or filter_q explanation is a real bug —
   root-cause it against a local Postgres before 4b. Also compare `avg_current_ms` vs `avg_pg_ms` —
   production adds network round trips (`_pg_browse` makes 4-5 queries).
5. Then **4b**: `api_browse` calls `_pg_browse` for every request, with the current path kept as the
   exception/ineligible fallback. Then **4c**: delete `_pg_tier1_browse`, `_pg_tier2_narrow_items`,
   the Python filter/facet/sort loop, and the 4a comparison tooling (`_browse_diff`,
   `_browse_run_both`, `_browse_shadow_compare`, `_py_kw_entry_match`, `_explain_kw_mismatch`,
   `/api/pg-browse-diff-check`, `?pg_shadow=2`). Step 5 (retire the JSON catalog) comes after.

## Standing rules (unchanged)

- Bump `APP_VERSION` for every logical change; verify with `python3 -m py_compile
  gc_tracker_app.py` and `node --check static/gc.js`.
- Update HANDOFF.md/HANDOFF_PROMPT.md with a changelog entry for every version bump.
- Git pushes happen from Chuck's Mac terminal only — `cd ~/Desktop/gc_tracker` FIRST, then
  `rm -f .git/index.lock` (that order matters).
- Investigate before proposing fixes — reproduce mismatches against real Postgres execution in a local
  throwaway cluster (`pg_ctlcluster 16 main start` in the sandbox), never guess from aggregate counts.
