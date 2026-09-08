# Next Session Prompt — Phase E cutover decision (v2.16.25 is live and timed)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — especially the **2026-09-08** entries at
the top (there are three, newest first: "Phase E v2.16.25 live-timed on Railway", "Phase E
cursor-overhead fix — v2.16.25", and "Phase E live-spot-check pass completed") — then
`POSTGRES_MIGRATION_PLAN.md` §7 for Phase E's design, then project memory files
`postgres_phase_e_v2.16.25_live_timing_2026-09-08.md`,
`postgres_phase_e_cursor_investigation_2026-09-08.md`,
`postgres_phase_e_verification_2026-09-08.md`, `postgres_phase_e_2026-09-04.md`, and
`postgres_migration_plan_2026-08-31.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover, v2.16.22) is live and healthy — nothing to
revisit there.

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) is built, deployed as **v2.16.25**,
and still **shadow-mode only** (admin + `?pg_shadow=1` gate — zero real-user impact either way).
Full timeline:

1. **v2.16.24** (2026-09-04, live): Tier 2 narrowing shipped, shadow-mode only.
2. **2026-09-08 live-spot-check pass** (on v2.16.24): correctness and Railway deploy logs clean,
   but production timing on a narrowing-eligible request (5-store subset + category filter +
   keyword, 111K rows → 2,015 candidates) was **~12% slower** under Postgres shadow narrowing than
   legacy (128.4ms vs 114.1ms mean, 8 reps). Cutover not recommended as-is; Chuck asked to
   investigate the round-trip cost before deciding.
3. **v2.16.25 cursor-overhead fix, same day**: root-caused most of the ~40ms Postgres round-trip
   to `_pg_tier2_narrow_items()`'s use of `RealDictCursor`. Local sandbox benchmark showed a
   plain-cursor rewrite ~2.2-2.5x faster (36-44ms → 14-20ms) at production-matching row counts.
   Rewrote to use a plain cursor + `cur.description`-based column lookup. Offline-verified
   byte-identical across 8 diff-harness cases; `py_compile` / `node --check` / disposable-venv
   route-table build all clean.
4. **v2.16.25 pushed and live-timed, same day (2026-09-08)**: Chuck pushed from his Mac terminal;
   confirmed live via the version string on gcgeartracker.com. Re-ran the exact same live timing
   methodology (16 alternating `fetch()` calls via Claude in Chrome against Chuck's authenticated
   admin session, same 5-store-subset + category + keyword body, ~2,019 candidates) — **result:
   shadow mean 113.3ms vs legacy mean 104.7ms, ~8.2% slower (down from ~12.5% pre-fix), shadow lost
   6 of 8 reps.** The internal Postgres round-trip (`_pg_tier2_shadow_ms`) dropped from ~40ms to
   ~31ms — a real ~20-25% improvement, but well short of the ~15-20ms the local benchmark
   predicted (Railway's real hardware/network apparently carries more per-call overhead than the
   cursor-type difference alone explains). **The fix is real and worth keeping, but it narrowed the
   gap rather than closing or reversing it.**

## The task, in order

This session is a **decision session, not an investigation session** — the round-trip cost has
now been investigated twice (root-caused locally, then confirmed live) and the honest answer is
"meaningfully better, still not a net win." Bring this to Chuck plainly, same as both prior
sessions did, and get an explicit decision rather than picking one:

1. **Summarize where things stand** (the four-point timeline above) if Chuck wants the refresher.
2. **Lay out the options, unchanged in shape from before, but with better numbers**:
   - **Cut over anyway** given the memory-pressure benefit ([[perf_railway_memory_growth_2026-08-31]])
     — the latency cost is now ~8-9ms per narrowing-eligible request instead of ~12-14ms, which may
     change Chuck's calculus even though it's technically still a net loss on this benchmark.
   - **Hold in shadow mode indefinitely** — zero risk, zero benefit, status quo. Always available,
     never expires.
   - **Investigate further** — the internal round-trip is still ~31ms vs. what should plausibly be
     a single-digit-ms query against a ~2,000-row indexed table. Candidates not yet tried: measuring
     connection-pool checkout time specifically on Railway (ruled out as ~0.00ms only in the *local*
     benchmark, never isolated on Railway itself), checking whether `_pg_conn()`'s `conn.commit()`
     after a read-only SELECT is skippable in production the way it looked locally, or a lower-level
     look at psycopg2 connection setup / TLS handshake overhead on Railway's private network path.
     No specific fix is queued — this is speculative, not a lead the way the cursor fix was.
3. **If Chuck decides to cut over**: drop the `_pg_shadow_requested and _is_admin()` gate at the
   Tier 2 call site in `api_browse()` (the `_pg_tier2_would_narrow` guard stays as-is, it's
   orthogonal). Bump `APP_VERSION`, verify (`py_compile` + `node --check`, and since this touches
   the Flask route dispatch path, a disposable-venv module import + route-table build), update
   `HANDOFF.md`/`HANDOFF_PROMPT.md`, give Chuck the push commands (cd first, `rm -f
   .git/index.lock`, then add/commit/push — never run `git push` from the sandbox or device bridge).
   Then watch Railway's memory graph over the following days (last checked 2026-09-04: active
   ~3.5-4.7GB sawtooth) to see whether it settles further.
4. **If Chuck decides to hold or investigate further**: no code changes needed. Update
   `NEXT_SESSION_PROMPT.md` to reflect whichever path was chosen and stop there.
5. **Do not silently proceed with a cutover without Chuck weighing in** — this has been the rule
   for two sessions running and stays the rule.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes (a cutover would), actually import the module in a disposable
  venv and confirm the route table builds.
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
- Phase F (retire JSON entirely) — not until Phase D and Phase E have both run clean in
  production for a while, and Phase E's own cutover is still undecided; not next session.
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.
- Reimplementing the want-list/keyword matcher in SQL — Phase E deliberately keeps it as
  unmodified Python; there's no proven need to change that.
- Splitting `gc_tracker_app.py` into multiple files — discussed 2026-09-04, concluded single-file
  is fine at current scale; not blocking anything here.
- Applying the same RealDictCursor-to-plain-cursor treatment to Tier 1's `_pg_tier1_browse()` —
  not investigated, likely not worth it given its small per-request result sets, no evidence of a
  problem there.

**Current version: v2.16.25, live in production, confirmed via live timing.** Cutover decision is
the only open item. Last updated: 2026-09-08.
