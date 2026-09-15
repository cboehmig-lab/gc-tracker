# Next Session Prompt — Phase E is live; watch the memory graph

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — the **2026-09-15** entry at the top
("Phase E CUTOVER — v2.16.26") — then project memory file
`postgres_phase_e_cutover_2026-09-15.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) is cut over and LIVE as of
2026-09-15 (v2.16.26) — Chuck's decision to accept the ~8% per-request latency cost (measured
2026-09-08) for the Railway memory-pressure benefit. Both Postgres Phases D (Tier 1, v2.16.22)
and E (Tier 2, v2.16.26) now serve every real user — no admin/flag gates left in `/api/browse`'s
Postgres call sites.

**Post-deploy health check already done same day (2026-09-15, ~10 min after deploy)**: version
string confirmed live, Railway deploy logs clean (zero `[pg] tier2 narrow failed, falling back to
full cache` lines; the only `@level:error` hits are the standard benign gunicorn-boot false
positives seen on every prior deploy). That check was necessarily early — it doesn't yet reflect
a full day of real Tier-2-eligible traffic.

## The task, in order

1. **Check in on how Phase E has held up** now that some real time has passed:
   - Railway deploy logs (project `serene-determination`, service `web` — see
     `reference_railway.md` in project memory) for the current deployment: search for
     `falling back to full cache` (should still be zero or rare) and any `@level:error` entries
     beyond the known benign gunicorn-boot lines.
   - Railway's memory graph (Metrics tab, `web` service): compare against the pre-cutover baseline
     (~3.5-4.7GB active sawtooth, last measured 2026-09-04) to see whether it's settled further.
     Don't expect a dramatic change the way Phase D showed — Tier 2 traffic is a smaller share of
     total traffic than Tier 1's dominant pattern — so report what's actually there rather than
     what's expected.
   - Update `postgres_migration_plan_2026-08-31.md` in project memory with the result.
2. **Phase F (retiring JSON entirely)** is a separate, much bigger project — not this session, and
   only after Phase E has run clean in production for a while (Chuck's call). Not yet designed
   (keyword search's dependency on the full in-memory list is the open question).

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox or the device bridge.
  Give him the exact commands; do not attempt `git push` yourself anywhere. If `git commit` fails
  with "cannot lock ref HEAD", a stale lock file is the cause (this happened 2026-09-15) — tell
  Chuck to `rm -f .git/HEAD.lock .git/refs/heads/main.lock` in `~/Desktop/gc_tracker` and retry.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.
- Railway project is `serene-determination` / service `web` — see `reference_railway.md` in
  project memory for how to find metrics/logs, and its caution about the project canvas view
  registering accidental "Apply changes" state from mere navigation (never click Deploy/Apply
  there without meaning to).
