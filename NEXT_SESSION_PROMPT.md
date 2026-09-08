# Next Session Prompt — Phase E cutover decision (blocked on Chuck)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — especially the **2026-09-08** entry at
the top ("Phase E live-spot-check pass completed") — then `POSTGRES_MIGRATION_PLAN.md` §7 for
Phase E's design, then project memory files `postgres_phase_e_verification_2026-09-08.md`,
`postgres_phase_e_2026-09-04.md`, and `postgres_migration_plan_2026-08-31.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover, v2.16.22) is live and healthy — nothing to
revisit there.

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) is built and deployed, live in
production as **v2.16.24**, still **shadow-mode only** (admin + `?pg_shadow=1` gate — zero
real-user impact either way). The 2026-09-08 session finished the live-spot-check pass that was
queued:

- **Correctness: clean.** Category/subcategory facet + keyword, and the `filter_q: 'fender
  strat'` multitoken case, all byte-identical between shadow and legacy against real production
  data (tested against a 5-store subset so the narrowing path was actually engaged — an
  all-stores request never engages Postgres narrowing at all, by the `_pg_tier2_would_narrow`
  guard's design).
- **Railway deploy logs: clean.** Zero `[pg] tier2 narrow failed` / `[pg] tier1 ... falling back`
  lines anywhere in the v2.16.24 deployment's log history (2026-09-04 through 2026-09-08).
- **Timing: NOT clean — this is the open decision.** A narrowing-eligible request (5-store
  subset + category filter + keyword, narrowing 111K rows → 2,015 candidates) timed **~12%
  slower** under Postgres shadow narrowing than legacy on real production data (8 alternating
  reps: mean 128.4ms shadow vs. 114.1ms legacy; shadow lost 7 of 8 reps). Smaller relative
  regression than the ~2.5x seen on the small synthetic dataset in v2.16.24's own testing, but the
  same direction — Postgres narrowing has not been a demonstrated latency win at either scale
  tried so far. The ~40ms Postgres round-trip isn't paid back by the downstream Python savings at
  this candidate-set size.

**Per the standing bar for this decision** ("if shadow timing is at or below legacy, Phase E is a
confirmed win; if it's still slower even when it does narrow, bring it back to Chuck before
considering cutover, not something to paper over") — **the 2026-09-08 session did not cut Phase E
over**, and does not recommend it as-is.

## The task, in order

1. **This is a decision point for Chuck, not further investigation** — bring him the timing
   result above. Options, roughly:
   - **Cut over anyway** if the ~12% latency cost on keyword/facet searches is acceptable given
     the memory-pressure benefit ([[perf_railway_memory_growth_2026-08-31]] — Tier 2 traffic is
     the residual driver of Railway's active sawtooth even after Phase D). This is a real
     trade-off call, not obviously wrong — Chuck's to make.
   - **Hold Phase E in shadow mode** (status quo) — zero user impact, keep gathering data or
     revisit only if memory pressure becomes an actual operational problem.
   - **Investigate reducing the Postgres round-trip cost** before deciding (e.g., is there
     connection-pool warmup overhead being paid per-request that a persistent connection or a
     different query shape could avoid? Not investigated this session — the ~40ms
     `_pg_tier2_shadow_ms` itself wasn't broken down further). Worth a quick look ONLY if Chuck
     wants to keep pursuing the narrowing approach rather than accept the trade-off or shelve it.
   - **Do not silently proceed with the cutover** without Chuck weighing in — the whole point of
     shadow mode is that this decision costs nothing to defer.

2. **If Chuck decides to cut over**: drop the `_pg_shadow_requested and _is_admin()` gate at the
   Tier 2 call site in `api_browse()` (the `_pg_tier2_would_narrow` guard added in v2.16.24 stays
   as-is either way — it's orthogonal to the admin/flag gate). Bump `APP_VERSION`, verify
   (`py_compile` + `node --check` + route-table import), update `HANDOFF.md`/`HANDOFF_PROMPT.md`
   with the changelog entry, give Chuck the push commands. Then watch Railway's memory graph over
   the following days (last checked 2026-09-04: active ~3.5-4.7GB sawtooth) to see whether it
   settles further — don't overclaim before that data exists.

3. **If Chuck decides to hold**: no code change needed. Just note the decision in project memory
   so it doesn't get re-litigated from scratch next time, and this migration effort is
   effectively paused pending a reason to revisit (a memory incident, or Chuck changing his mind).

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

## What's explicitly out of scope next session

- The NEW-item anchor/tagging bug — dismissed by Chuck, not a live task.
- Phase F (retire JSON entirely) — not until Phase D and Phase E have both run clean in production
  for a while, and Phase E's own cutover is still undecided; not next session.
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.
- Reimplementing the want-list/keyword matcher in SQL — Phase E deliberately keeps it as unmodified
  Python; there's no proven need to change that.
- Splitting `gc_tracker_app.py` into multiple files — discussed 2026-09-04, concluded single-file
  is fine at current scale; not blocking anything here.

Current version: **v2.16.24** (deployed and confirmed live 2026-09-08; unchanged this session —
verification only). Last updated: 2026-09-08.
