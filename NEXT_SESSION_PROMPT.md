# Next Session Prompt — Postgres Migration: exercise Phase C, then plan Phase D
Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first for full architecture/version history,
then `POSTGRES_MIGRATION_PLAN.md` (repo root) §3 and §7 for the full read-path/cutover plan.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud
sandbox filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-02, v2.16.20)

Phase A (schema + backfill) and Phase B (dual-write) are done and verified against live
production (436,343 == 436,343, 0 mismatches, as of the 2026-09-02 parity check). Phase C (SQL
Tier 1 shadow read path) shipped this session: `_pg_tier1_browse()` + connection pooling, gated
behind `?pg_shadow=1` + an active admin session — **not reachable by real users**, the served
`/api/browse` response is unchanged for everyone. Verified offline against a 436K-row synthetic
dataset at real production scale: a 37-case A/B diff harness passes byte-for-byte identical
(after fixing two real tiebreak-determinism bugs found by the harness — see HANDOFF.md's
v2.16.20 entry), the dominant no-filter/all-stores case is ~9.5x faster, and an 8-thread
concurrent load test against the connection pool had zero errors. Full detail in HANDOFF.md's
v2.16.20 entry — read it before doing anything below, it explains the two tiebreak fixes and
exactly what `_pg_tier1_eligible()` does and doesn't cover.

**v2.16.20 has not been pushed or deployed yet** — that's Chuck's call, via his own Mac terminal
per the standing rule (`cd ~/Desktop/gc_tracker`, `rm -f .git/index.lock`, then the normal
add/commit/push). Two throwaway files, `_to_delete/_tmp_handoff_entry.md` and
`_to_delete/_tmp_handoff_prompt_entry.md`, are sitting in the repo root's `_to_delete/`
subfolder — device_bash couldn't delete them directly (no delete permission granted this
session); Chuck can delete that folder himself, or grant delete permission next session.

## The task, in order

1. **Once v2.16.20 is deployed**: as admin, hit `POST /api/browse?pg_shadow=1` against real
   production a handful of times (e.g. via the browser's dev console `fetch()`, or curl with an
   admin session cookie) with a few different filter/sort combinations, and sanity-check the
   `_pg_shadow: true` / `_pg_shadow_ms` fields come back and the `items`/facet counts look
   right. This is a real-data spot check on top of the synthetic-scale offline diff — cheap,
   worth doing before anything else.

2. **Decide, with Chuck**: run a production shadow-mode burn-in period first (both paths run for
   real requests, only JSON's result is served, mismatches logged — POSTGRES_MIGRATION_PLAN.md
   §7 Phase C's optional last step, not yet built), or skip straight to planning Phase D (the
   actual `/api/browse` cutover for the Tier 1 case, plus `/api/state`'s `total_items` and the
   `has_store_data` check per the plan). If Chuck wants the burn-in, that's this session's first
   build; if not, move straight to Phase D planning/implementation.

3. **Phase D, when ready** (POSTGRES_MIGRATION_PLAN.md §7): flip `/api/browse` to
   `_pg_tier1_browse()` for real traffic when `_pg_tier1_eligible()` is true, for every user, not
   just admins with `?pg_shadow=1`. Tier 2 (keyword/`filter_q` requests) keeps falling through to
   the existing JSON-based path unchanged, exactly as Phase C's fallback does today — Phase D is
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
  path specifically — that's what this session did (see HANDOFF.md's v2.16.20 entry).
- If you rebuild or extend the A/B diff harness: the device-bridge shell has no network path to
  Railway's Postgres, so scale-testing against real-shaped data means a scratch Postgres +
  synthetic dataset in the **cloud sandbox** (this session's `gen_data.py`/`load_pg.py`/
  `diff_harness.py` pattern, not committed to the repo — recreate them fresh rather than looking
  for leftover files, the cloud sandbox doesn't persist across sessions), same as the v2.16.18
  session's backfill scale test.
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
  `_pg_parity_check`) onto the new connection pool — deliberately skipped in v2.16.20, they're
  rare and not on any hot path.
- The NEW-item anchor scope bug (`bug_new_item_anchor_scope.md`) — separate, deferred thread,
  do not touch it.

Current version: **v2.16.20**. Last updated: 2026-09-02.
