# Next Session Prompt — v2.16.32 built (not yet pushed); Phase F stage 2 (tsquery translator) shipped, step 3 (live diff harness) next

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — then project memory files
`postgres_migration_plan_2026-08-31.md` and `postgres_phase_f_design_2026-09-21.md` for full
Postgres-migration status. The actual Phase F design doc lives in the repo at
`POSTGRES_PHASE_F_DESIGN.md` — read it before writing any more Phase F code, especially the
"Stage 2 — SHIPPED" addendum under §7 (what the translator does, the two deliberate semantic
narrowings, the want-list-vs-filter_q asymmetry it preserves, and the concrete design for step 3).

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-22)

Postgres Phases A–E are complete/deployed/healthy. Phase F stage 1 (schema) is live and confirmed
healthy (v2.16.31). Stage 2 (the tsquery translator) is built:

- **v2.16.32** (built this session, **NOT YET PUSHED** — confirm git status before assuming
  otherwise): five new functions in `gc_tracker_app.py` (`_tsquery_compile_term`,
  `_tsquery_compile_query`, `_tsquery_bool_clauses`, `_tsquery_want_list_entry`,
  `_tsquery_filter_q`, `_TsqueryUnsupported`) that translate want-list-keyword/`filter_q` parsed
  structure into parameterized Postgres tsquery SQL. **Not wired into any route** — every live
  request still uses the unmodified Python matcher. Verified structurally (py_compile, node
  --check, 61-route table build, a 22-case self-test covering every syntax branch) — all pass.
  **Not verified against live Postgres** — this sandbox has no DB access, so nothing confirms the
  generated SQL actually matches the same SKUs the Python matcher does. That's step 3.

## The task

**Step 3: build and ship the live diff harness**, per `POSTGRES_PHASE_F_DESIGN.md`'s Stage 2
addendum. Key finding from this session: this CANNOT be a Chuck's-Mac-terminal script like
`migrate_cat_cache_to_pg.py` — the real 833+343 keyword/saved-search strings live in
`gc_users.db` (`user_data.keywords`/`user_data.saved_searches`) on the Railway volume, not
locally, and there's no local copy the way `gc_category_cache.json` has one. Instead, build a new
admin-only, read-only endpoint (e.g. `/api/tsquery-diff-check`), following the exact precedent
`/api/search-syntax-stats` (v2.16.29) set: it runs server-side (where `_cat_cache`, `gc_users.db`,
and `_PG_POOL`/`_pg_conn()` all already live in the same process), and returns AGGREGATE mismatch
counts plus a small capped sample of just the mismatching entries' raw TEXT — never SKUs or item
names, matching that endpoint's existing privacy posture.

For each real keyword/filter_q string: (a) run it through the existing single-entry
`_compile_query`/`_wl_bool_compile` path against `_cat_cache` (the FULL catalog including
sold/delisted history, not just `available` items — apples-to-apples with Postgres's `items`
table) to get the OLD matching SKU set; (b) run it through the new translator and execute
`SELECT sku FROM items WHERE search_vector @@ (...)` via `_pg_conn()` to get the NEW set;
(c) diff; (d) aggregate. Catch `_TsqueryUnsupported` per entry (expected to basically never fire,
per the zero-real-usage finding in §3) and count it separately from an actual mismatch. Once this
runs clean on all real data, step 4 (unify Tier 1/Tier 2 into one `_pg_browse()`) and step 5
(shadow-mode then cutover) follow.

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
