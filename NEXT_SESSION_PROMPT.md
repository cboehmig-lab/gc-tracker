# Next Session Prompt — v2.16.31 built (not yet deployed); Phase F search-engine stage 1 shipped, stage 2 (translator) next

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — then project memory files
`postgres_migration_plan_2026-08-31.md` and `postgres_phase_f_design_2026-09-21.md` for full
Postgres-migration status. The actual Phase F design doc lives in the repo at
`POSTGRES_PHASE_F_DESIGN.md` — read it before writing any more Phase F code, especially the new
"Stage 1 — SHIPPED" addendum under §7 (what changed, the transaction-block bug it caught, and
what's NOT yet verified) and §7 step 2 (the translator, the actual next step).

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-22)

Postgres Phases A–E are all complete, deployed, and healthy — nothing left to watch on any of
them. Phase F (retire the JSON catalog, make Postgres sole source of truth) has a full written
design and has now shipped its first real code:

- **v2.16.31** (built this session, **NOT YET PUSHED/DEPLOYED** — confirm git status before
  assuming otherwise): `pg_schema.sql` gets a generated `search_vector tsvector` column
  (`'simple'` config, over `name`+`brand` — matches `_kw_match()` exactly) plus a GIN index built
  `CONCURRENTLY`. Along the way, found and fixed a real bug the design doc's step 1 would have
  shipped: `_init_pg_schema()` and `migrate_cat_cache_to_pg.py` both apply the whole schema file
  as one transactional `cur.execute()` call, and Postgres rejects `CREATE INDEX CONCURRENTLY`
  outright inside a transaction block. Fixed with a `-- ==CONCURRENT-INDEXES==` marker in
  `pg_schema.sql` that both callers split on, running the concurrent statement afterward on its
  own autocommit connection. Full detail in `POSTGRES_PHASE_F_DESIGN.md`'s Stage 1 addendum and
  `HANDOFF.md`'s v2.16.31 entry.
- **Verified**: py_compile (both `gc_tracker_app.py` and `migrate_cat_cache_to_pg.py`),
  `node --check`, a disposable-venv import + 61-route table build, and a standalone check that
  the marker-partition splits the schema file exactly as intended.
- **NOT verified**: no live Postgres was reachable from the device shell this session (no root,
  no Docker/Homebrew) — none of this DDL has run against a real server yet. First thing to do
  after Chuck pushes and Railway redeploys: check Railway's deploy logs for `[pg] items table
  ready` followed by `[pg] concurrent indexes ready` (or a `[pg] concurrent index build skipped:
  ...` line with the actual exception, which means it didn't work and needs a follow-up). Also
  worth Chuck double-checking Railway isn't running >1 replica of the `web` service before this
  deploys — the `ALTER TABLE ... ADD COLUMN ... STORED` is a full-table rewrite under an
  `ACCESS EXCLUSIVE` lock (~450K rows including historical sold/delisted items), and the safety
  reasoning for why that's fine assumed a single instance.

## The task

**Once v2.16.31 is confirmed live and healthy** (the log lines above, no new errors in Railway),
**next step: the `tsquery` translator**, `POSTGRES_PHASE_F_DESIGN.md` §7 step 2 — a new function
that turns the ALREADY-BUILT parsed query structure (`_compile_query`'s AND/phrase/suffix-
wildcard token classification, `_wl_bool_compile`'s OR-of-AND-with-NOT clause shape) into a
Postgres query, composed via parameterized `to_tsquery('simple', %s)` calls + SQL `&&`/`||`/`!!`
operators — never by string-concatenating a raw tsquery expression (avoids ever having to
hand-escape tsquery's own operator syntax against a user-typed term). Then step 3: extend
`/api/search-syntax-stats`'s query into a real diff harness against the 833 real keyword entries
+ 343 real saved searches (pull fresh numbers, don't reuse the 2026-09-21 sample) plus synthetic
edge cases (genuine mid-word wildcards, punctuation, apostrophes, empty/very-long input) — diff
old Python matcher vs. new translator, byte-for-byte on the resulting SKU sets, before touching
any live code path. Steps 4 (unify Tier 1/Tier 2 into one `_pg_browse()`) and 5 (shadow-mode
then cutover) come after that.

**Versioning**: Chuck is planning to bump to v2.17.0 when Phase F fully lands (JSON dual-write
retired, Postgres sole source of truth) — see `postgres_phase_f_design_2026-09-21` memory file's
versioning-note addendum. Keep bumping patch versions before that milestone lands.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds (a `SECRET_KEY=dummy DATABASE_URL= python3 -c "import gc_tracker_app"`
  style check works from a venv with `requirements.txt` installed).
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox or the device bridge.
  Give him the exact commands; do not attempt `git push` yourself anywhere. If `git commit`/`git
  status` fails with a lock error, a stale lock file is the cause — tell Chuck to
  `rm -f .git/index.lock .git/HEAD.lock .git/refs/heads/main.lock` in `~/Desktop/gc_tracker` and
  retry (the device bridge cannot remove these itself — permission denied by design).
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.
- Railway project is `serene-determination` / service `web` — see `reference_railway.md` in
  project memory for how to find metrics/logs, and its caution about the project canvas view
  registering accidental "Apply changes" state from mere navigation (never click Deploy/Apply
  there without meaning to).
