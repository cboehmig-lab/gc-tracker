# Next Session Prompt — Phase E closed out; Phase F not started

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — then project memory file
`postgres_migration_plan_2026-08-31.md` for full Postgres-migration status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-21)

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) cut over 2026-09-15 (v2.16.26) and
its post-cutover health check (done 2026-09-21, 6 days out) came back clean: zero
`falling back to full cache` lines in deploy logs, memory graph settled around the same ~4-5GB
baseline as before cutover (no dramatic change — expected, since Tier 2 is a smaller share of
traffic than Tier 1's dominant pattern). **Phase E is closed out — no further watching needed.**

One unrelated, low-severity bug was found during that check: a `RuntimeError: release unlocked
lock` race between the scan thread's own lock release and `/api/stop`'s 5-second force-unlock
watchdog. Confirmed unrelated to Phase E, no user-facing impact (fires after the scan's SSE "done"
event is already sent). Not fixed, not queued — see project memory
`bug_scan_lock_race_2026-09-21.md` for full root-cause detail and a suggested fix if it's ever
worth doing.

## The task

Nothing urgent is queued. Options for next session, in rough priority order if Chuck wants to keep
moving on this:

1. **Phase F (retire JSON entirely)** — the big remaining lever on the original memory-pressure
   problem, since `_cat_cache` still stays resident in memory at all times regardless of Phases D/E.
   Not started, not designed yet — the open design question is keyword search's dependency on the
   full in-memory list. This is a much bigger project than D/E, worth a dedicated design session
   before any code.
2. **Scan-lock race fix** (optional, cosmetic) — wrap the unguarded `_lock.release()` calls at
   gc_tracker_app.py lines 6638/5906/6151/6221 in `try/except RuntimeError: pass`, matching the
   pattern already used at lines 3092/5824. Low priority, only worth doing opportunistically.
3. Otherwise, check in with Chuck on what he wants to prioritize — Android app groundwork, other
   feature ideas in `HANDOFF.md`'s "Future Ideas" section, etc.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox or the device bridge.
  Give him the exact commands; do not attempt `git push` yourself anywhere. If `git commit` fails
  with "cannot lock ref HEAD", a stale lock file is the cause — tell Chuck to
  `rm -f .git/HEAD.lock .git/refs/heads/main.lock` in `~/Desktop/gc_tracker` and retry.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.
- Railway project is `serene-determination` / service `web` — see `reference_railway.md` in
  project memory for how to find metrics/logs, and its caution about the project canvas view
  registering accidental "Apply changes" state from mere navigation (never click Deploy/Apply
  there without meaning to).
