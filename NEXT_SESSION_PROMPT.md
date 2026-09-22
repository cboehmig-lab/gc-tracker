# Next Session Prompt — v2.16.37 built (not yet pushed): Phase F search-parity sub-project fully closed out

## Where things stand

This session closed out BOTH remaining open items from the Phase F search-engine rebuild:

1. **v2.16.36** — fixed a newly-discovered Postgres tokenization gap: `'simple'`'s parser fuses a
   bare `word/word` pattern (e.g. `Mesa/Boogie`) into one lexeme instead of splitting it, so plain
   searches like `mesa` never matched Mesa/Boogie-branded items. Same fix shape as the v2.16.35
   hyphen fix, extended to slashes. Pushed and confirmed live: deploy logs clean, site footer
   showed v2.16.36, and a live `/api/tsquery-diff-check` re-run (488,113-row catalog) came back
   clean on this specifically — 0 errors, no `/`-containing entry in either mismatch sample.
2. **v2.16.37** — with that live confirmation in hand, deleted the two temporary diagnostic
   endpoints (`/api/search-syntax-stats`, `/api/tsquery-diff-check`) and their ~487 lines of
   supporting code. The actual Stage 2 tsquery translator functions were explicitly kept — they're
   the real deliverable, not diagnostic tooling, just not wired into a live route yet (that's step
   4, below, not started).

Full detail: HANDOFF.md's 2026-09-22 v2.16.36 and v2.16.37 entries, and
POSTGRES_PHASE_F_DESIGN.md's Addenda 12 and 13.

**Verified locally, NOT yet verified against live production for v2.16.37 specifically** (v2.16.36
already was, per above): `py_compile` clean, `node --check` clean (JS untouched both versions),
`grep` across the whole file/JS/templates confirms no dangling references to anything deleted.

## Concrete next steps

1. Chuck pushes v2.16.37 from his Mac terminal (`cd ~/Desktop/gc_tracker`, `rm -f
   .git/index.lock`, commit, push — exact commands given at end of session).
2. Confirm the deploy is healthy — this one is a pure code removal, no schema/migration involved,
   so it should just be a clean gunicorn restart. Quick deploy-log check is enough (no `[pg]
   migrated...`/index-build sequence to wait for this time).
3. With that, **Phase F's search-parity sub-project (steps 1-3 of 5) is fully done, build and
   cleanup both.** Step 4 (unify Tier 1/Tier 2 browse into one `_pg_browse()` around the kept
   translator functions) and step 5 (shadow-mode-then-cutover, retire the JSON catalog) are next —
   not started, not blocking on anything, Chuck's call on when to pick this up. See
   POSTGRES_PHASE_F_DESIGN.md's Design section for the already-locked-in plan (read path: full
   tsquery translation as the sole search mechanism, no Python fallback in steady state; write
   path: promote `_pg_sync_scan` from best-effort mirror to primary write; rollback: write-only
   JSON snapshot during a burn-in window before fully retiring it).

## Standing rules (unchanged, worth repeating)

- Bump `APP_VERSION` for every logical change; verify with `python3 -m py_compile
  gc_tracker_app.py` and `node --check static/gc.js` before considering anything done.
- Update HANDOFF.md/HANDOFF_PROMPT.md with a changelog entry for every version bump.
- Git pushes happen from Chuck's Mac terminal only — `cd ~/Desktop/gc_tracker` FIRST, then
  `rm -f .git/index.lock` (that order matters).
- Investigate before proposing fixes — every real fix across this whole Phase F effort came from
  reproducing the exact mismatch against real Postgres execution in a local throwaway cluster,
  never from guessing at root causes from aggregate counts alone.
