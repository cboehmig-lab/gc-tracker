# Next Session Prompt — Phase E closed out; store-filter bug fully resolved; Phase F not started

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — then project memory file
`postgres_migration_plan_2026-08-31.md` for full Postgres-migration status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-21)

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) cut over 2026-09-15 (v2.16.26) and
its post-cutover health check (2026-09-21, 6 days out) came back clean. **Phase E is closed out —
no further watching needed.**

Two bugs were found and fixed this session, both deployed as v2.16.27 and v2.16.28:

- **v2.16.27**: (1) a `RuntimeError: release unlocked lock` scan-lock race, found during the
  Phase E health check — fixed by wrapping the unguarded `_lock.release()` calls in
  try/except. (2) The Favorite/selected-stores filter wasn't applying to Watch List or Want List
  (reported via Discord) — fixed so both now send the current store selection like every other
  filter.
- **v2.16.28**: live-browser testing of the v2.16.27 store-filter fix (clicking through both
  "Favorites → Want List" and "Want List → Favorites" orderings on the production site) surfaced
  a follow-on bug: toggling store selection while Want List was already open didn't refresh the
  displayed results (stale display), because Want List's `_globalSearchActive` flag made
  `updateCount()`'s auto-refresh guard treat it like a nationwide keyword search. Watch List was
  unaffected. Fixed in `updateCount()`. **Needs a quick post-deploy live spot-check** (open Want
  List, then toggle Favorites, confirm results narrow instead of staying stale) — see HANDOFF.md's
  v2.16.28 entry for the exact repro steps used pre-fix.

See project memory `feature_store_filter_watch_want_2026-09-21.md` and
`bug_scan_lock_race_2026-09-21.md` for full investigation detail on both.

## The task

Nothing urgent is queued beyond the v2.16.28 post-deploy spot-check above. Options for next
session, in rough priority order if Chuck wants to keep moving:

1. **Phase F (retire JSON entirely)** — the big remaining lever on the original memory-pressure
   problem, since `_cat_cache` still stays resident in memory at all times regardless of Phases D/E.
   Not started, not designed yet — the open design question is keyword search's dependency on the
   full in-memory list. This is a much bigger project than D/E, worth a dedicated design session
   before any code.
2. Otherwise, check in with Chuck on what he wants to prioritize — Android app groundwork, other
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
