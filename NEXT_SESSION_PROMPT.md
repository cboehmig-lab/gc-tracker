# Next Session Prompt — Phase E cutover pushed, needs post-deploy health check

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — the **2026-09-15** entry at the top
("Phase E CUTOVER — v2.16.26") — then project memory files
`postgres_migration_plan_2026-08-31.md` and `postgres_phase_e_v2.16.25_live_timing_2026-09-08.md`
for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Chuck decided to cut Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) over to real
traffic, accepting the ~8% per-request latency cost (measured 2026-09-08, v2.16.25) for the
Railway memory-pressure benefit — a business tradeoff decision, not an engineering conclusion that
Postgres won on speed. v2.16.26 dropped the `_pg_shadow_requested and _is_admin()` gate in
`api_browse()`'s Tier 2 block. Verified this session (py_compile, node --check, disposable-venv
route-table build, and a mocked-Postgres routing/fallback/diagnostics test — no live Postgres was
reachable in the device shell, see HANDOFF.md for exactly what that test did and didn't cover).
Both Postgres Phases D (Tier 1, v2.16.22) and E (Tier 2, v2.16.26) are now cut over to real
traffic — no admin/flag gates left in `/api/browse`'s Postgres call sites.

**Not yet done as of end of last session**: v2.16.26 was NOT pushed — Chuck was given the push
commands but push/deploy confirmation hadn't happened yet when the session ended.

## The task, in order

1. **Confirm with Chuck whether v2.16.26 has been pushed and deployed.** If not, no further action
   needed yet — just wait, or re-give the push commands if asked (cd into
   `~/Desktop/gc_tracker` first, then `rm -f .git/index.lock`, then the usual add/commit/push —
   never run `git push` yourself, sandbox or device bridge).
2. **If pushed and deployed**, do the same post-cutover health check Phase D got:
   - Confirm the `v2.16.26` version string is live on gcgeartracker.com.
   - Check Railway's deploy logs for the current deployment for `[pg] tier2 narrow failed,
     falling back to full cache` lines — should be zero or rare. Also check for any
     `@level:error` entries.
   - Note (don't over-promise): Railway's memory graph should be watched over the following
     days/weeks to see whether the sawtooth (last measured ~3.5-4.7GB active, per
     `postgres_migration_plan_2026-08-31.md`) settles further now that Tier 2 traffic also avoids
     building the old large temporary Python structures. Tier 2 traffic is a smaller share of
     total traffic than Tier 1's dominant pattern, so don't expect as dramatic an immediate change
     as Phase D showed — this is a "watch and report back" item, not a same-session verification.
   - Update `postgres_migration_plan_2026-08-31.md` in project memory with the health-check
     result (clean / found issues), same pattern as `postgres_phase_d_healthcheck_2026-09-04.md`.
3. **Phase F (retiring JSON entirely)** is a separate, much bigger project — not this session, and
   only after Phase E has run clean in production for a while (Chuck's call). Not yet designed
   (keyword search's dependency on the full in-memory list is the open question).

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox or the device bridge.
  Give him the exact commands; do not attempt `git push` yourself anywhere.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.
- Railway project is `serene-determination` / service `web` — see `reference_railway.md` in
  project memory for how to find metrics/logs, and its caution about the project canvas view
  registering accidental "Apply changes" state from mere navigation (never click Deploy/Apply
  there without meaning to).
