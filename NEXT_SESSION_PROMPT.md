# Next Session Prompt — v2.16.36 built (not yet pushed): Mesa/Boogie slash-tokenization fix

## Where things stand

v2.16.35 (hyphen tokenization + quoted-phrase punctuation/plural fixes) shipped, was confirmed
live and healthy, and a production diff-check re-run confirmed both fixes work: most residual
"mismatches" turned out to be the diff-check's own exact-match metric flagging cases where Postgres
now finds a strict SUPERSET of old's results (a real improvement, not a bug) — see
POSTGRES_PHASE_F_DESIGN.md's Addendum 11 status note and HANDOFF.md's v2.16.35 entry.

That same re-run surfaced one new, previously undocumented gap: Postgres's `'simple'` parser fuses
a bare `word/word` pattern (e.g. `Mesa/Boogie`) into ONE compound lexeme instead of splitting it,
so a plain `mesa` search never matched Mesa/Boogie-branded items. Chuck's explicit instruction this
session: "why not get those two things cleaned up now" (this fix, plus retiring the diagnostic
endpoints). This session built and verified the fix — not yet the endpoint cleanup, deliberately
sequenced after (see below).

**Fix, same shape as v2.16.35's hyphen fix**: `/` is now normalized to a space alongside `-`, in
both `search_vector`'s generated column expression and every query-side
`to_tsquery`/`phraseto_tsquery` call. No wildcard-path code change needed (the lexeme regex already
excludes `/`). The self-migrating schema's staleness check was updated from `'regexp_replace' not
in expr` (which would have wrongly treated a live v2.16.35 database as current) to `"'/'" not in
expr`. Full writeup: HANDOFF.md's 2026-09-22 v2.16.36 entry and POSTGRES_PHASE_F_DESIGN.md's
Addendum 12.

**Verified locally, NOT yet verified against live production**: (1) the full schema-migration flow
simulated against four local Postgres states — fresh deploy, pre-v2.16.35, upgrade-from-live-v2.16.35
(the critical new case), and idempotent re-run. (2) The real translator module run against real
Postgres with new Mesa/Boogie-style catalog items and test cases — 9/9 new cases pass exactly, no
regressions (52/55 total including the pre-existing suite, 3 already-documented tradeoffs, 0 new
failures). `py_compile` and `node --check` clean.

## Concrete next steps

1. Chuck pushes v2.16.36 from his Mac terminal (`cd ~/Desktop/gc_tracker`, `rm -f
   .git/index.lock`, commit, push — exact commands given at end of session).
2. Confirm the deploy is ACTIVE/healthy — another real schema change (full-table
   `ALTER TABLE ... DROP/ADD COLUMN` on `items`, plus rebuilding two concurrent indexes). Check
   Railway deploy logs for `[pg] migrated search_vector to hyphen+slash-normalized generated
   expression` → `[pg] items table ready` → `[pg] concurrent indexes ready` with no errors.
3. Re-run `POST /api/tsquery-diff-check` against real production data. Expect the Mesa/Boogie
   (and any other slash-joined brand) mismatches to disappear from the keyword/saved-search
   mismatch counts.
4. **Only once step 3 confirms the fix works live**: delete the two temporary diagnostic endpoints
   — `/api/search-syntax-stats` and `/api/tsquery-diff-check` — and their supporting code
   (`_tsquery_diff_build_catalog_index`, `_tsquery_diff_candidates`, `_plain_req_tokens`,
   `_tsquery_diff_old_want_list_matches`, `_tsquery_diff_check_group`, `_TSQUERY_DIFF_SAMPLE_CAP`,
   `_TSQUERY_DIFF_LOCK`, `_TSQUERY_DIFF_STATE`, and whatever `/api/search-syntax-stats`'s own
   stats-computation code consists of — re-examine the file for the exact deletion scope, it
   hasn't been re-checked since these were built). This was deliberately sequenced AFTER live
   verification rather than bundled into v2.16.36, so the diff-check tool is still available to
   confirm the fix in production before removing it. Ship as its own commit (v2.16.37).
5. Once that's clean, step 4 of the original Phase F plan (unifying Tier 1/Tier 2 browse around the
   translator) becomes the next real work — not started, not blocking on anything else.

## Standing rules (unchanged, worth repeating)

- Bump `APP_VERSION` for every logical change; verify with `python3 -m py_compile
  gc_tracker_app.py` and `node --check static/gc.js` before considering anything done.
- Update HANDOFF.md/HANDOFF_PROMPT.md with a changelog entry for every version bump.
- Git pushes happen from Chuck's Mac terminal only — `cd ~/Desktop/gc_tracker` FIRST, then
  `rm -f .git/index.lock` (that order matters).
- Investigate before proposing fixes — every real fix across this whole Phase F effort came from
  reproducing the exact mismatch against real Postgres execution in a local throwaway cluster,
  never from guessing at root causes from aggregate counts alone.
