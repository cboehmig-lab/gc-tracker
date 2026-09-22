# Next Session Prompt — v2.16.34 built (not yet pushed): fixed a real tsquery translator bug found by running the diff-check against production; two more findings need a decision, not yet fixed

## Where things stand

v2.16.33's diff-check endpoint (`/api/tsquery-diff-check`, Phase F stage 3) was run against real
production want-list/saved-search data for the first time this session. It was NOT clean: 48/810
want-list keywords and 23/294 saved searches mismatched between the stage-2 tsquery translator and
the current Python matcher.

Every distinct mismatch pattern was root-caused using a local throwaway Postgres cluster spun up
in the sandbox (this environment has `postgresql-16` preinstalled — `pg_ctlcluster 16 main start`,
create a scratch DB, apply `pg_schema.sql` verbatim) with synthetic catalogs — this lets any
hypothesis get tested against REAL Postgres execution without ever touching production data, and
closes the "never verified against live Postgres" gap every session since stage 1 had flagged.

**Found and fixed (v2.16.34): a real bug in `_tsquery_bool_clauses`/`_tsquery_filter_q`** — SQL
params were appended in original left-to-right term order, but the SQL fragment string reorders
positive-then-negative before joining. Whenever a negative term preceded a later positive term in
the same clause, params landed in the wrong `%s` placeholders — silently testing the wrong words,
sometimes inverting a NOT into a requirement. Confirmed end to end against real Postgres with a
synthetic catalog (a want-list entry matched the one pedal it was supposed to EXCLUDE). Fixed by
keeping `pos_params`/`neg_params` parallel to `pos_frags`/`neg_frags`. Full writeup in HANDOFF.md's
v2.16.34 entry and `POSTGRES_PHASE_F_DESIGN.md`'s new diff-check-run addendum.

**Two more real, CONFIRMED findings — NOT fixed, need Chuck's decision:**

1. **Hyphen-adjacent numbers don't match** (`ES-335` tokenizes to lexeme `-335` in Postgres's
   `simple` config — a hyphen before digits is parsed as a negative-number sign — so plain
   searches for `335` never match it; the Python regex tokenizer has no such quirk). This is
   likely the single biggest remaining mismatch cluster by item count. A real fix means changing
   what the generated `search_vector` column indexes (e.g. replace `-` with a space before digits,
   consistently on both the stored column and the query side) — a schema change (another
   full-table rewrite like stage 1's), not a quick patch. Needs Chuck's go-ahead before building.
2. **Quoted-phrase narrowing (a stage-2 DELIBERATE, documented choice) is not invisible on real
   data** — `'"jam pedal"'` (singular) dropped from 90 matches to 3 in production, because
   `'simple'` config has no stemming and `phraseto_tsquery` requires an exact adjacent-lexeme
   match, while the old matcher's raw-substring check tolerated a trailing "s" for free. Also
   newly observed: punctuation is now insensitive (`"Mr. Black"` / `"mr black"` converge to the
   same result) — a real, probably-fine, but surprising behavior change. Worth Chuck knowing this
   explicitly rather than finding out later; no code change proposed here, just flagging it.

## Concrete next steps

1. Chuck pushes v2.16.34 from his Mac terminal (`cd ~/Desktop/gc_tracker`, `rm -f
   .git/index.lock`, commit, push — exact commands given at end of session).
2. Confirm the deploy is ACTIVE/healthy (Railway logs + `gcgeartracker.com` footer showing
   v2.16.34), same pattern as every prior version.
3. Re-run `POST /api/tsquery-diff-check` against real production data (admin session, same as
   this session used via the browser). Expect the mismatch count to drop noticeably (every
   negative-term-not-last want-list/filter_q entry should now match) but NOT reach zero — the two
   open findings above will still show up as mismatches until they're separately decided/fixed.
4. Bring the new counts back to Chuck and get a decision on finding #1 (worth a schema change?)
   and awareness of #2 (acceptable as-is, or worth a different quoted-phrase translation
   strategy?).
5. Only once the diff-check is clean (modulo whatever's explicitly decided to be acceptable) does
   step 4 of the original plan (unifying Tier 1/Tier 2 into one `_pg_browse()`) become the next
   real work — not started, not blocking on anything else right now.

## Standing rules (unchanged, worth repeating)

- Bump `APP_VERSION` for every logical change; verify with `python3 -m py_compile
  gc_tracker_app.py` and `node --check static/gc.js` before considering anything done.
- Update HANDOFF.md/HANDOFF_PROMPT.md with a changelog entry for every version bump.
- Git pushes happen from Chuck's Mac terminal only — `cd ~/Desktop/gc_tracker` FIRST, then
  `rm -f .git/index.lock` (that order matters).
- `python3 -m venv` fails with an `ensurepip` error inside the FUSE-mounted connected folder —
  build venvs in `/tmp` instead if one is needed (this session didn't need one: a preinstalled
  `postgresql-16` cluster in the CLOUD sandbox, not the device, was enough for live-Postgres
  verification against synthetic data).
- Investigate before proposing fixes — this session's whole value came from reproducing each
  mismatch against real Postgres execution rather than guessing at root causes from the aggregate
  counts alone.
