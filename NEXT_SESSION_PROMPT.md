# Next Session Prompt — v2.16.35 built (not yet pushed): both open tsquery-parity findings fixed — hyphen tokenization + quoted-phrase punctuation/plural

## Where things stand

v2.16.34 (params/fragment-order bug fix) shipped and was confirmed live against real production
data: keyword mismatches dropped 48→43, saved-search mismatches held at 23 (expected — those two
findings below weren't fixed yet at that point).

This session, per Chuck's explicit instruction ("change those two to get as close to parity as
possible with old"), both remaining open findings from the v2.16.34 production diff-check run were
root-caused, fixed, and verified — not just documented:

1. **Hyphen-adjacent digits now tokenize correctly.** `search_vector`'s generated column and every
   query-side `to_tsquery`/`phraseto_tsquery` call normalize `-` to a space before tokenizing, so
   `ES-335` and a plain `335` search now match consistently. Needed a self-migrating schema
   (`_pg_migrate_search_vector_if_stale()`) since the generated column's expression can't be
   altered in place. New: the wildcard fast path now excludes hyphenated words (falls back to the
   dormant Python matcher) rather than risk a wrong match there — a separate quirk in how
   `to_tsquery` parses hyphens in the QUERY string, confirmed via direct testing.
2. **Quoted-phrase punctuation/plural narrowing fixed.** Quoted terms now use a `pg_trgm`-backed
   ILIKE substring match instead of `phraseto_tsquery`, restoring the old Python matcher's exact
   substring semantics (plural tolerance, punctuation sensitivity, mid-word matching). New trigram
   index backs it so it stays index-searchable.

This forced an architectural change: `_tsquery_compile_term` now returns a COMPLETE boolean SQL
predicate per term (ILIKE or `search_vector @@ (...)`) rather than a bare tsquery fragment, since
the two predicate types can't compose under tsquery's own `&&`/`||`/`!!` operators. All translator
functions now compose with plain SQL `AND`/`OR`/`NOT`. Full writeup: HANDOFF.md's 2026-09-22
v2.16.35 entry and `POSTGRES_PHASE_F_DESIGN.md`'s Addendum 11.

**Verified locally, NOT yet verified against live production**: (1) the full schema-migration flow
simulated end to end against a local Postgres cluster, covering fresh-deploy / upgrade-from-old /
idempotent-rerun states. (2) The rewritten translator run against real Postgres over an extended
25→42-case self-test — 39/42 pass exactly, the 3 differences all explained (2 pre-existing
documented wildcard tradeoffs unrelated to this session, 1 new intentional side effect of the
hyphen fix itself). `py_compile` clean; JS untouched.

## Concrete next steps

1. Chuck pushes v2.16.35 from his Mac terminal (`cd ~/Desktop/gc_tracker`, `rm -f
   .git/index.lock`, commit, push — exact commands given at end of session).
2. Confirm the deploy is ACTIVE/healthy. This one applies a REAL schema change (full-table
   `ALTER TABLE ... DROP/ADD COLUMN` on `items`, ~450K+ rows, plus building TWO concurrent
   indexes) — check Railway deploy logs for `[pg] migrated search_vector to hyphen-normalized
   generated expression` → `[pg] items table ready` → `[pg] concurrent indexes ready` with no
   errors, and sanity-check timing against stage 1's original ~10s index-build precedent (this one
   may take longer: two indexes, plus the DROP/rebuild).
3. Re-run `POST /api/tsquery-diff-check` against real production data (admin session, same pattern
   as prior sessions). Expect the keyword/saved-search mismatch counts to drop further from
   v2.16.34's 43/23 baseline — ideally close to zero, modulo ordinary catalog-churn drift during
   the run (already-documented, not a bug) and the two pre-existing wildcard-tradeoff cases (which
   are DESIGNED to still show as "mismatches" against the old matcher, not something to chase).
4. Bring the new counts back to Chuck. If clean (modulo the above), step 4 of the original plan
   (unifying Tier 1/Tier 2 browse around the translator) becomes the next real work — not started,
   not blocking on anything else right now.

## Standing rules (unchanged, worth repeating)

- Bump `APP_VERSION` for every logical change; verify with `python3 -m py_compile
  gc_tracker_app.py` and `node --check static/gc.js` before considering anything done.
- Update HANDOFF.md/HANDOFF_PROMPT.md with a changelog entry for every version bump.
- Git pushes happen from Chuck's Mac terminal only — `cd ~/Desktop/gc_tracker` FIRST, then
  `rm -f .git/index.lock` (that order matters).
- Investigate before proposing fixes — every real fix this session (and the last) came from
  reproducing the exact mismatch against real Postgres execution in a local throwaway cluster,
  never from guessing at root causes from aggregate counts alone.
