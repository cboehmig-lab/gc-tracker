# Next Session Prompt — Phase D health check, then Phase E (Tier 2 SQL cutover) if clean

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first (especially the v2.16.22 entry), then
`POSTGRES_MIGRATION_PLAN.md` §7 for Phase E's design, then project memory file
`postgres_migration_plan_2026-08-31.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover) shipped as v2.16.22 on 2026-09-02 and was
confirmed live in production the same day — see HANDOFF.md and
`postgres_migration_plan_2026-08-31.md` for the full verification writeup (16/16 live spot-checks,
39/39 offline diff cases, forced-exception fallback test, real gunicorn+curl smoke test, plus
post-deploy production checks).

Chuck's own call was to let Phase D run clean in production for a few days before starting
Phase E — so **step 1 below is a real gate, not a formality.**

The NEW-item anchor/tagging bug that was previously queued as "next up" after Phase D
(`bug_new_item_anchor_scope.md` in project memory) is **no longer active work** — Chuck confirmed
on 2026-09-04 that it's working correctly and he was mistaken earlier. Don't investigate it unless
Chuck explicitly raises it again.

## The task, in order

1. **Check Phase D's health first.** This is the actual "let it run a few days" checkpoint, so
   don't skip it even if it feels like a formality:
   - Ask Chuck (or check Railway's logs directly if dashboard access works this session) whether
     anything looked off since 2026-09-02 — page errors, slow loads, any `[pg] tier1 browse
     failed, falling back to JSON path: ...` lines in the logs (safe by design, but worth knowing
     the frequency).
   - Do a couple of live sanity requests against `https://gcgeartracker.com/api/browse` yourself
     (unflagged, no admin session — a real Tier 1 shape: no-filter/all-stores, one facet, one
     sort) and confirm sane `total_count` / fast response.
   - If anything looks wrong: stop here, investigate, and if needed use the rollback lever
     documented in HANDOFF.md's v2.16.22 entry (`_is_admin()` back in front of the Postgres
     routing condition in `api_browse()` — a one-condition revert, not a data-recovery operation).
   - If it's clean: proceed to Phase E.

2. **Phase E — Tier 2 (keyword/free-text) SQL cutover.** Read `POSTGRES_MIGRATION_PLAN.md` §7 for
   the design Chuck already approved: push every filter SQL can express into the WHERE clause to
   narrow the candidate row set, then run the EXISTING unmodified Python keyword matcher over just
   that narrowed set — do NOT reimplement keyword matching in SQL (it's complex, has its own bug
   history, not worth rebuilding without a proven need). Same discipline as Phases C/D:
   - Build behind a debug-only flag first (shadow mode), diff against the legacy path.
   - Build a fresh offline diff harness with deliberately sparse/malformed synthetic data — same
     lesson as Phase D's `store_count` bug: synthetic data alone isn't enough, live spot-checking
     against real production is required too.
   - Only cut it over to serve real traffic once both offline and live checks pass clean, the same
     bar Phase D was held to.
   - If, after reading the plan and current Tier 2 traffic patterns, Phase E doesn't look worth it
     yet (i.e. Tier 2's residual cost isn't actually significant in practice), it's fine to say so
     and hold off rather than build it for its own sake — check with Chuck before investing the
     session in it if that's what you find.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.

## What's explicitly out of scope this session

- The NEW-item anchor/tagging bug — dismissed by Chuck (see above), not a live task.
- Phase F (retire JSON entirely) — not until Phase D (and, once built, Phase E) have run clean in
  production for a while; not this session.
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.

Current version: **v2.16.22** (deployed, live, pushed and confirmed by Chuck). Last updated:
2026-09-04.
