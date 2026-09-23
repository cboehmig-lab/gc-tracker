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
