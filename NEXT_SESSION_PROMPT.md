# Next Session Prompt — Postgres Migration: decide on burn-in, then plan Phase D
Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first for full architecture/version history,
then `POSTGRES_MIGRATION_PLAN.md` (repo root) §3 and §7 for the full read-path/cutover plan.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud
sandbox filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-02, v2.16.21, deployed and live)

Phase A (schema + backfill) and Phase B (dual-write) are done and verified against live
production. Phase C (SQL Tier 1 shadow read path) shipped in v2.16.20, deployed, and confirmed
working against real production data by Chuck spot-checking `?pg_shadow=1` from the browser
console as admin — exactly the step queued from the prior session. That spot-check actually
caught a real bug: `store_count` was off by one (Postgres counted an empty-string `store` value
as a real store; the JSON path's own `store_count` line explicitly excludes falsy values). Fixed
in v2.16.21, reproduced locally first, then verified against the 37-case offline diff harness
plus the live spot-check pattern again. **Read HANDOFF.md's v2.16.20 AND v2.16.21 entries before
touching any of this** — v2.16.20 explains `_pg_tier1_browse()`'s design and the two
tiebreak-determinism bugs found there; v2.16.21 explains the store_count bug and, importantly,
*why* the offline synthetic-data harness missed it (the generator never produced an empty-store
item) — that's a real, general lesson for any further Tier 1 SQL work, also saved in project
memory as `postgres-phase-c-2026-09-02`.

`?pg_shadow=1` is admin/dev-only and still does not affect what any real user is served —
`/api/browse`'s response is unconditionally the JSON path for everyone except an admin session
that explicitly appends `?pg_shadow=1` to a direct `/api/browse` API call (not reachable by
just visiting a URL — the frontend never sets this flag).

## The task, in order

1. **Do at least one more live spot-check first**, ideally with a few different filter/sort
   combinations (not just the no-filter/all-stores case v2.16.20/21 were checked against) — the
   store_count bug is proof the offline synthetic data has real blind spots relative to actual
   production data. Use the browser-console diff pattern from this session (fetch both
   `/api/browse` and `/api/browse?pg_shadow=1` with the same body, strip `_pg_shadow`/
   `_pg_shadow_ms`, deep-compare). If anything differs, treat it exactly like the store_count bug
   was treated: read the code, reproduce it locally against a scratch Postgres before proposing a
   fix, fix it in a way that's provably correct (not by tuning a test), and re-run the full diff
   harness after.

2. **Decide, with Chuck**: run a production shadow-mode burn-in period (both paths run for real
   requests, only JSON's result is served, mismatches logged automatically — this hasn't been
   built yet, it's POSTGRES_MIGRATION_PLAN.md §7 Phase C's optional last step), or go straight to
   planning Phase D now that a couple of rounds of live spot-checking have come back clean. If
   Chuck wants the burn-in, that's this session's first build; if not, move straight to Phase D.

3. **Phase D, when ready** (POSTGRES_MIGRATION_PLAN.md §7): flip `/api/browse` to
   `_pg_tier1_browse()` for real traffic when `_pg_tier1_eligible()` is true, for every user, not
   just admins with `?pg_shadow=1`. Tier 2 (keyword/`filter_q` requests) keeps falling through to
   the existing JSON-based path unchanged, exactly as the shadow fallback does today — Phase D is
   "make the fallback path unconditional for Tier 1 users too," not new logic. JSON dual-write
   (Phase B) keeps running throughout — that's the rollback lever, reverting is flipping back to
   the JSON path, not a data-recovery operation.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — but per
  the v2.16.16 incident, **also actually import the module** in a disposable venv (`python3 -m
  venv` + install the same deps as `requirements.txt` — the repo's own `.venv` is tied to this
  Mac's Homebrew Python) and confirm the route table builds, for any change touching Flask
  routes. Go one step further and boot gunicorn locally with the real `--workers=1
  --worker-class=gthread` config and curl the routes when touching anything on the `/api/browse`
  path specifically.
- **A synthetic-data offline diff is necessary but not sufficient** — v2.16.21 is direct proof.
  Any Tier 1 SQL change needs both the offline harness AND a live `?pg_shadow=1` spot-check
  against real production before being considered verified.
- If you rebuild or extend the A/B diff harness: the device-bridge shell has no network path to
  Railway's Postgres, so scale-testing against real-shaped data means a scratch Postgres +
  synthetic dataset in the **cloud sandbox** (this session's `gen_data.py`/`load_pg.py`/
  `diff_harness.py` pattern, not committed to the repo — recreate them fresh rather than looking
  for leftover files, the cloud sandbox doesn't persist across sessions). Consider seeding a few
  deliberately-malformed/sparse rows (empty store, empty brand/category, etc.) into the synthetic
  data by default now, given v2.16.21's lesson.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes. Use `data-*` attributes + `addEventListener`.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump,
  written in enough detail that a fresh session could pick up the reasoning.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.
- Test locally before proposing anything's done. For any change to the Tier 1 SQL path
  specifically, the A/B diff harness is the actual test — don't skip it, and fix any mismatch it
  finds rather than tuning the harness to pass.

## What's explicitly out of scope right now

- Tier 2 / keyword search in SQL (Phase E, and only if still needed after Tier 1 ships).
- Retiring `gc_category_cache.json` (Phase F).
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.
- Migrating the admin-only Postgres call sites (`_pg_sync_scan`/`_pg_full_backfill`/
  `_pg_parity_check`) onto the new connection pool — deliberately skipped, they're rare and not
  on any hot path.
- The NEW-item anchor scope bug (`bug_new_item_anchor_scope.md`) — separate, deferred thread,
  do not touch it.

Current version: **v2.16.21**. Last updated: 2026-09-02.
