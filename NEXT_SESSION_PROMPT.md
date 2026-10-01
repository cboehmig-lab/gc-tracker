# Next Session Prompt — v2.17.6 built (silent background sweep): push

**2026-10-01**: v2.17.5 + its docs live (the docs deploy first failed on a transient GitHub 500 while Railway's builder
downloaded `mise`; redeployed OK). Chuck: remove the sold/price-drop status line → v2.17.6 (also fixes the literal
"\\n✓ Done" log text), written to the Mac, NOT pushed. Push:
`cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
`git add gc_tracker_app.py static/gc.js HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
After: reload the site (footer v2.17.6), scan → no status line, log ends "✓ Done — N new this scan.".
Still open from v2.17.5: check Railway logs for `[sweep] WARNING` (expect none), then delete /api/quick-window-check.
Then the browse-side list (skip facet recompute on page flips/sorts, trim the brand payload, first-load waterfall).

---

# Next Session Prompt — v2.17.5 LIVE (quick-pass window fix verified: 1,995 = 1,995); confirm a real scan, then browse-side work

**Live 2026-09-30 14:35 CDT**: `/api/quick-window-check?hours=48` → nb_hits 1995 = db_count 1995 (all startDate 0).
First real scan 19:36Z: quick 984 found in 774 ms (Chuck saw 900+ NEW, "took like a second"); sweep complete in
20.4 s, 23 sold. Still: grep Railway logs for `[sweep] WARNING` over the next day (should be none), then delete
/api/quick-window-check. Commit the doc updates (HANDOFF.md / HANDOFF_PROMPT.md / NEXT_SESSION_PROMPT.md).


**2026-09-30 (latest)**: v2.17.4 pushed + live (9b88647). Live logs: quick passes 0.2-0.4 s, sweeps ~14 s complete
(115,301 found, 81 sold on the first), BUT every quick pass returned 0 hits — GC's recent listings have startDate 0
(dated from creationDate). No NEW items missed yet (nothing listed after the threshold since 09-29 09:12Z). v2.17.5
built + locally verified (v2.17.4 reproduced the miss: 50 of 150 NEW; v2.17.5 150 of 150, identical to a full scan),
written to the Mac, NOT pushed. See HANDOFF.md v2.17.5.
1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. Admin: `https://gcgeartracker.com/api/quick-window-check?hours=48` → nb_hits > 0 and ≈ db_count (proves Algolia
   honors the creationDate filter). Then a scan: quick coverage nbHits > 0 when the window has listings; no
   `[sweep] WARNING` lines. Then delete /api/quick-window-check in a later version.
3. Then back to the browse-side list.

---

# Next Session Prompt — v2.17.4 built (Phase G S1: two-phase scan): push + verify live

**2026-09-30 (latest)**: Chuck liked S1 and chose: sweep after each click; quiet + status line when it finishes.
v2.17.4 built + locally verified (identical NEW ids / anchor / final catalog vs v2.17.3's full scan; browser test
OK), written to the Mac, NOT pushed. See HANDOFF.md v2.17.4.
1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git add gc_tracker_app.py static/gc.js HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. Live check after a scan: Railway logs `[timing] scan done: quick …` (total ~1-2 s), then `[timing] sweep done: …
   complete True` (~12-15 s later), coverage lines complete; the site shows NEW items right away and the ✓ status
   line after; `/api/sweep-status` JSON; Railway HTTP `/api/progress` durations (were ~15-30 s). Watch for
   `SCAN WRITE FAILED` and `lean pages NOT used`.
3. Then back to the browse-side list (skip facet recompute on page flips, trim brand payload, first-load waterfall).

---

# Next Session Prompt — v2.17.3 LIVE (scans 29.3 s → 14.5 s): next S1 (two-phase scan)

**Live 2026-09-30 12:49 CDT**: v2.17.3 pushed + live. First scan: total 14.5 s (fetch 12.3, save 2.1) vs 29.3 s
baseline; lean pages used (299 KB vs 630 KB/page), prefetch used (read 57 ms vs 2,252), coverage complete, 69 sold.
Next: S1 — ask Chuck the three design questions (below, item 3) before building. Also re-check Railway memory over
the day and that no `[scan] lean pages NOT used` lines appear.


**2026-09-30 (later)**: v2.17.2 pushed (1fd5ca7). Chuck: "do it to it" → v2.17.3 = S2 (lean Algolia pages +
continuous fetch pool) + S3 (prior read during the fetch), built + locally verified (identical results to v2.17.2
in every scenario), written to the Mac, NOT pushed. See HANDOFF.md v2.17.3.
1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. After the first nationwide scan, Railway deploy log: `[timing] scan fetch: mode lean …` (a
   `[scan] lean pages NOT used` line means the page-1 check refused — read its reason), kb_per_page vs 630,
   wall_s vs 24.6; `[pg] scan saved … read …ms (prefetched …)` vs 2,252 ms; `[timing] scan done` total vs 29.3 s;
   coverage `complete True`; no Algolia errors. Also Railway HTTP logs `/api/progress` durations vs ~28 s, and
   memory during scans.
3. Then **S1** (two-phase scan: NEW items in seconds, full sweep in the background) — first ask Chuck: what the
   page shows while the sweep runs; what a second Scan click during a sweep does; whether the full sweep should
   run on a server timer instead of on clicks.

---

# Next Session Prompt — v2.17.2 built (desktop button "Scan for New Listings"): push; v2.17.1 LIVE

**Update 2026-09-30 (later)**: v2.17.1 pushed + live (ab639c4). Chuck asked for the desktop scan button to read
"Scan for New Listings" (all gear is used) → v2.17.2, text only, NOT pushed. Push:
`cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
`git add gc_tracker_app.py static/gc.js HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
**Baseline scan on live v2.17.1 (12:25 CDT)**: `[timing] scan done: nationwide, 114,731 found, total 29,283ms
(fetch 24,700, save 4,538, finish 44)`; `[timing] scan fetch: pages 479, batches 32, wall 24.6 s, req_ms p50 486 /
p90 625 / max 1,222, **630 KB per page, 295 MB per scan**, json_decode 2.96 s, parse 1.23 s, batch_ms p50 722 / max
1,241`. So each page is huge (attributesToRetrieve * + facets *), requests are ~0.5 s each, and each batch of 15
waits ~0.24 s longer than a typical page for its slowest one. Save: read 2,252 ms, write 826 ms.
Chuck also approved doing all three scan speedups: S2 + S3 next as v2.17.3 (after reading a baseline
`[timing] scan fetch` line from v2.17.1), then S1 as its own version after Chuck answers its design questions.

---

# Next Session Prompt — v2.17.1 built (Phase G step 1: request timing): push, read the numbers, Chuck picks

**2026-09-30 (Phase G session 1)**:
- **v2.17.0 health check** (Chuck's Chrome, ~11:05 CDT): Railway web memory ~250-300 MB flat since the v2.17.0
  deploy (was ~1.5-1.7 GB flat, with spikes to 2.5-2.8 GB, all through the previous 24 h on v2.16.51). Only
  ~25 min of v2.17.0 data existed, so the "over a day" re-check is still open — look again (Metrics → 7 day).
  Deploy logs: 2 scans on v2.17.0 code (10:4x and 10:56), both `[pg] scan saved` (latest: 114,693 found, 53
  new/changed, 16 sold, total 4,703 ms), no `SCAN WRITE FAILED`, coverage complete. Still no STORE scan seen.
- **v2.17.1 built + locally verified, written to the Mac, NOT pushed**: request timing + nationwide scan fetch
  breakdown only (no behavior change) — see HANDOFF.md v2.17.1. Baseline live numbers are in that entry.

1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. After deploy: footer v2.17.1; in DevTools → Network any /api/browse response has a `Server-Timing` header;
   admin `https://gcgeartracker.com/api/timing` returns JSON. After ~15 min of traffic Railway logs show
   `[timing] summary …` blocks; each scan logs `[timing] scan fetch: …` (nationwide) and `[timing] scan done: …`.
3. After ~a day: read `/api/timing` → per-shape p50/p90 and the phase split (`q1` / `facets` / `q3` / `page` /
   `conn`), `inflight_at_arrival` (how often requests overlap on the single worker), and the scans list. Also
   grep Railway logs for `[timing] SLOW`. Also re-check memory over the day and watch for a store scan.
4. Chuck picks from the ranked list below, then build.

## Phase G ranked list (from the 2026-09-30 measurements — confirm with step 3's live phase split)

**The scan is the longest wait by far** (Chuck, confirmed from Railway HTTP logs): ~28 s per "Scan For New"
(22-33 s), always nationwide; ~23 s of it fetching 478 Algolia pages in lock-step batches of 15, ~4.7 s saving.
Read the first live `[timing] scan fetch` line (v2.17.1) before choosing between S1-S3:

S1. **Two-phase scan: show NEW items in ~2-3 s, finish the sweep in the background** (M-L, the big one).
   NEW detection only compares `date_listed` with the user's anchor, so a first pass can ask Algolia only for
   items listed since the anchor (minus a safety margin) — `numericFilters: startDate>=…` (startDate is already
   used as a numeric filter) — usually 1-3 pages. Save + send "done" with the NEW items, then keep sweeping all
   478 pages in the background for sold-marking and price drops (same coverage guard). Needs care: items with no
   startDate (creationDate fallback), per-user anchors, the scan lock / "joined" scans, and what the UI shows
   while the background pass runs. Could also let a server-side timer do the full sweep so users never wait on it.
S2. **Faster full sweep** (S): drop `facets:["*"]`, request only the ~18 attributes parse_products reads, and
   replace lock-step batches with a continuous pool (next page starts as soon as any finishes). Maybe 23 s →
   ~10-15 s; verify Algolia doesn't throttle a higher sustained rate.
S3. **Faster save** (S-M): start the Postgres prior-state read (2.4 s) in parallel with the fetch (e.g. read all
   available rows up front) instead of after it. ~2 s off every scan.

Where the time goes elsewhere (desktop, logged-in, live v2.17.0):
first results ≈ 1.9 s = ~0.45 s page + assets, ~0.4 s five API calls one after another, **0.3 s idle debounce**,
**~0.65 s database** for /api/browse, ~0.1 s moving a 200 KB JSON. Page flip / sort ≈ 0.8 s (the whole query,
facets included, re-runs). Search box ≈ 0.4 s debounce + 0.25-0.3 s. Want List ≈ 0.3 s. One store ≈ 0.14 s.

1. **Don't recompute facets on page flips / sorts** (effort S-M, gc.js + small server flag). Client sends
   `facets:false` when only page or sort changed and keeps its current facet lists/totals; server skips Q1,
   facets, Q3. Expected page flip ~0.8 s → ~0.2-0.3 s and 197 KB → ~30 KB. Biggest win per effort for anyone
   browsing past page 1 or sorting.
2. **Trim the brand facet payload** (S). 5,168 brands ≈ 170 KB of every browse response. Send the brands the
   dropdown actually needs (e.g. top ~200 + any selected), fetch the full list only when the brand dropdown's
   search is used. Cuts JSON work on both ends; biggest help on phones/slow connections.
3. **First-load waterfall** (M, gc.js only; careful — sync/merge ordering). Drop the 300 ms browseCache debounce
   for the initial load, run /api/stores + /api/state + /api/store-coords in parallel with /api/me→/api/sync,
   start the first browse as soon as stores + me are known. Expected ~0.5-0.7 s off the 1.9 s first load.
4. **Make the plain all-stores browse query itself cheaper** (M). ~0.65 s server on every first page. Options:
   cache facet counts/totals per (scope, filters) keyed by a "catalog generation" bumped by each scan write
   (per-user gate `user_last_scan` complicates reuse — measure how many requests share a key first), or a
   per-scan precomputed facet table. Needs step 3's phase split to pick the target (facets vs page sort vs q3).
5. **Search box debounce 400 → ~200 ms** (XS). Saves ~0.2 s per search; slightly more requests (aborts already exist).
6. **Search autocomplete** (M, one session — design in the Phase G section below). A feature, not a speed fix.
7. **More than one gunicorn worker** (L — scan coordination + SSE state must move to Postgres first). Only worth it
   if `inflight_at_arrival` shows real overlap; the local concurrency test was Postgres-bound, not GIL-bound, and
   Postgres releases the GIL while queries run. Defer unless the numbers say otherwise. (If `conn` phase shows
   non-trivial time in production, raising the pool's minconn from 2 is a one-line fix to consider first.)
8. Not worth doing now: gc.js size (52 KB on the wire, cached for a year per version), desktop images (not loaded),
   memory (fixed by v2.17.0 — confirm over a day).

---

# Next Session Prompt — v2.17.0 built (Phase F step 5c: JSON catalog retired): push + verify, then Phase G

**Update 2026-09-30**: burn-in check on live v2.16.51 clean (74 scan saves / 0 failed; parity 507,520 = 507,520,
0 diffs; 13 scans marked items sold via the Postgres write, up to 500; no store scan seen yet — all ~75 were
nationwide). Chuck: "build 5c". **v2.17.0 built + locally verified, written to the Mac, NOT pushed.** Postgres is
now the only catalog store — see HANDOFF.md v2.17.0 for what was deleted and reworked.
**Live verification (2026-09-30, ~10:40 CDT)**: v2.17.0 live (footer confirmed). Deploy log clean: boot straight to
`[pg] items table ready` → `concurrent indexes ready` → `connection pool ready`, no errors. First nationwide scan
after deploy: coverage complete (114,663 = nbHits, 0 retries), `[pg] scan saved: 114,663 found, 34 new/changed
written, 6 marked sold — total 4624ms`. `/api/pg-parity-check`, `/api/pg-precheck-5b`, `/admin/pg-backfill` → 404;
admin nav shows no "PG Backfill". Railway memory: ~1.7-2.0 GB on v2.16.51 → **~300 MB** after the restart, still
~300 MB after that first nationwide scan. Re-check memory over a day (the old sawtooth climbed between deploys).

1. Push: `cd ~/Desktop/gc_tracker`, then `rm -f .git/index.lock`, then
   `git rm migrate_cat_cache_to_pg.py`,
   `git add gc_tracker_app.py static/gc.js pg_schema.sql HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`,
   commit, `git push origin main`.
2. After deploy: footer v2.17.0; deploy log clean; after a scan or two, `_pg_scan_writes` (POST
   `/api/browse?pg_shadow=1`) `failed` 0 and Railway log `[pg] scan saved: …` per scan;
   `/api/pg-parity-check`, `/api/pg-precheck-5b`, `/admin/pg-backfill` → 404; admin nav has no "PG Backfill";
   Railway web → Metrics → memory after the restart vs the same hours on v2.16.51 (locally: 850 → 84 MB RSS
   at startup with a 300 MB JSON file present).
3. Still unconfirmed live: a STORE scan through the Postgres write (same code; tested locally). Watch for a
   `[pg] scan saved` line with a small "found" count.
4. **RESOLVED 2026-09-30 — not a bug**: Railway HTTP logs show the only `POST /api/stop` of that deploy at
   18:15:52 (200), 8s before the scan saved; 316 pages = page 1 + exactly 21 batches of 15, i.e. the loop exited
   cleanly on the stop flag between batches. Someone pressed Stop (gc.js has no unload/pagehide stop handler, so
   closing a phone alone doesn't stop a scan). Original note: the 2026-09-29 18:16 CDT nationwide scan got 75,840 of 115,423
   (`missing 39583`, `retried 0`, no empty/short pages) — looks like it stopped after ~316 of 481 pages.
   The coverage guard skipped sold-marking, so no data harm. Check how the parallel page loop can end early
   without marking pages as suspect (batch wall-clock ceiling? stop event?) — `_run()` nationwide branch.
5. Optional cleanup: delete the stale `gc_category_cache.json` on the Railway volume (nothing reads it).
6. Then **Phase G** (below) — start with "measure first".

---

# Next Session Prompt — v2.16.51 LIVE (nationwide scan coverage guard works): burn-in → 5c (v2.17.0)

**Live verification (2026-09-29, 16:45 CDT)**: v2.16.51 live. Scan 1 (nationwide): nbHits 115,580 / 482
pages; 480 items (2 pages) unaccounted for after the parallel pass → 2 pages re-fetched → recovered 480 →
unique 115,580 = nbHits, complete; "3,600 new/changed, 0 sold" (the items earlier scans had falsely marked sold
came back). Scan 2 a minute later: complete on the first pass, no retries, 0 new/changed, 0 sold — stable.
Save phase ~4.8-5.9s. So the lost pages are transient (a single re-fetch returns them); the guard + retry fixes
the flicker. The post-retry stats overwrite the first-pass page stats, so we don't yet know whether those pages
came back empty or duplicated — not needed for the fix.


**Update 2026-09-29 (latest)**: v2.16.50 live but a no-op (Algolia gives those 105 items no store AND no
location). Found instead: nationwide scans silently miss 5-6 whole pages (1,200-1,440 items) and mark them
sold → flicker. v2.16.51 (built + locally verified, NOT pushed) retries suspect pages and refuses to
sold-mark unless unique ≈ nbHits. See HANDOFF.md v2.16.51.
1. Push: `cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`,
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. After 1-2 nationwide scans: `(await (await fetch('/api/browse?pg_shadow=1',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all_stores:true})})).json())._scan_coverage`
   → which pages were empty/no-new/short, retried/recovered, complete? Decide the fetch-side fix if pages
   stay missing. Deploy log line: `[scan] nationwide coverage: …`.
3. Then burn-in → 5c (v2.17.0).

---

# Next Session Prompt — v2.16.50 built (missing stores from location): push + verify, then burn-in → 5c (v2.17.0)

**Update 2026-09-29 (later)**: investigated the storeless items (Chuck's pick, via Railway's Postgres Data tab in
Chuck's Chrome): 105 for sale, 74 with a location. v2.16.50 (built + locally verified, NOT yet pushed) fills the
store from the location + new `items.store_inferred` column so store scans don't flap them. See HANDOFF.md v2.16.50.
1. Push: `cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`,
   `git add gc_tracker_app.py pg_schema.sql HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. After deploy + one nationwide scan: `SELECT COUNT(*) FROM items WHERE available AND store=''` → ~31 (was 105);
   `SELECT COUNT(*) FROM items WHERE store_inferred` → ~74; `_pg_scan_writes.failed` 0; parity check 0 diffs.
3. Then the 5b burn-in → 5c (v2.17.0) as below. Tip: Railway checks work best through Claude in Chrome (Chuck's
   logged-in Chrome); the built-in browser pane doesn't keep the Railway login.

---

# Next Session Prompt — v2.16.49 LIVE (faster scan save): burn-in, then 5c (v2.17.0)

**Update 2026-09-29 (latest)**: v2.16.49 LIVE (deploy was delayed ~25 min by a Railway API incident — "slow or
stuck deployments" — not by anything in the app). Since deploy (15:49Z): `_pg_scan_writes` ok 23 / failed 0 /
retried 0. Last nationwide scan: 114,373 found, **0 new/changed written**, 0 sold — read 2.29s, write 0.69s,
**total 4.37s (was 9.4-11.2s on v2.16.48)**. The write's 0.69s with 0 rows is the 114K-SKU temp table used
by sold-marking (possible later micro-optimization). `/api/pg-parity-check` 505,284 = 505,284, 0 diffs (the
after-"done" JSON backup keeps up). v2.16.48 burn-in before this: 113 scans, 0 failures. Next: a couple more
days of burn-in, then 5c (v2.17.0).


**Update 2026-09-29**: Chuck: the "Saving items…" step was slow (~11s on nationwide scans). v2.16.49 (built +
locally verified, NOT yet pushed) writes only new/changed rows and moves the JSON backup after "done". See
HANDOFF.md v2.16.49.
1. Push: `cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`,
   `git add gc_tracker_app.py HANDOFF.md HANDOFF_PROMPT.md NEXT_SESSION_PROMPT.md`, commit, `git push origin main`.
2. After a nationwide scan: `_pg_scan_writes` (POST /api/browse?pg_shadow=1) → `last_changed` small,
   `last_ms` / `last_read_ms` / `last_write_ms` well under ~11s, `failed` 0; `/api/pg-parity-check` 0 diffs
   (run it a few seconds after the scan — the JSON backup now lands after "done").
3. Continue the 5b burn-in (a store scan + a scan with `last_sold` > 0), then 5c (v2.17.0) — prompt below.

---

# Next Session Prompt — v2.16.48 LIVE (Phase F 5b-ii cutover): burn-in, then 5c (v2.17.0)

**Update 2026-09-28 (latest)**: v2.16.48 PUSHED + LIVE (footer confirmed). First 2 real scans after deploy
(both nationwide) saved via the new path: `_pg_scan_writes` ok 2 / failed 0 / retried 0, DB phase ~11.2s,
114,453 rows. `/api/fill-gaps` → 404. `/api/pg-parity-check`: 503,199 = 503,199, 0 diffs (JSON backup
mirrors PG). `/api/pg-precheck-5b` again: 20-column parity PASS, new_ids 0 missing; the 18 missing watchlist
SKUs were all added 2026-03-21 → 2026-04-16 (pedals etc., users 4/13/2) — i.e. before the catalog's
late-April snapshot/restore era, long gone from both JSON and PG; harmless. Still to observe during burn-in:
a store scan and a scan that marks items sold (`last_sold` > 0) — check `_pg_scan_writes` + parity again
in a day or two, then 5c (v2.17.0).


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
