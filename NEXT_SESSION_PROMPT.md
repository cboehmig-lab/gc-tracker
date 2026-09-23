# Next Session Prompt — v2.16.39 built (not yet pushed): Phase F step 4a follow-up

**Update 2026-09-23**: v2.16.38 is LIVE; its first production diff check (10 accounts) was 97/100
exact, `_pg_browse` ~5x faster. v2.16.39 (punctuation-normalized `search_vector` — self-migrating
column rebuild, ~20s on boot — plus full-set mismatch explanations with a `mismatch_plumbing_suspect`
counter) is built and verified locally, not yet pushed. After pushing: confirm the `[pg] migrated
search_vector to punctuation-normalized...` deploy log sequence, then rerun the diff check (10 accounts,
then all). 4b gate: zero `mismatch_plumbing_suspect`, and every search-semantics class accepted.
See HANDOFF.md v2.16.39. The v2.16.38 notes below are still accurate otherwise.

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
