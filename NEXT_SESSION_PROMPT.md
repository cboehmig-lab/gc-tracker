# Next Session Prompt — push v2.16.24, finish live-spot-checking Phase E, then decide on the cutover

Copy and paste this to start the next Cowork session (Monday).

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first (the v2.16.24 entry, then v2.16.23 above
it), then `POSTGRES_MIGRATION_PLAN.md` §7 for Phase E's design, then project memory files
`postgres_phase_e_2026-09-04.md`, `postgres_phase_d_healthcheck_2026-09-04.md`, and
`postgres_migration_plan_2026-08-31.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover, v2.16.22) is live and healthy — confirmed
2026-09-04, nothing to revisit there.

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) was built in shadow mode
(v2.16.23, 2026-09-04) and is now live in production **still shadow-mode only** — real
(non-admin/unflagged) users are 100% unaffected; only an admin session with `?pg_shadow=1` runs
the new code at all. Same session, Chuck asked Claude to live-spot-check `?pg_shadow=1` using his
own logged-in admin browser (Claude in Chrome). Output matched byte-for-byte on every shape
tried, but a real timing regression turned up: Chuck's own most common request shape — all
stores + keyword/want-list search, no other filter — was ~2.5x SLOWER under the shadow path than
legacy, because with no store subset and no `user_last_scan`, Postgres had nothing to narrow by
and ended up fetching the whole ~110K-row catalog over the network instead. Fixed same-day as
v2.16.24: a new `_pg_tier2_would_narrow` guard skips the Postgres narrowing attempt entirely
whenever neither a store subset nor `user_last_scan` would actually narrow anything, falling
through to the unchanged in-memory path instead. Re-verified: the 60-case offline diff harness
still passes 60/60 byte-identical (with its assertions updated for which cases now correctly skip
narrowing), a real gap in the forced-exception fallback test was caught and fixed (its old test
body no longer exercised the code path after the new guard — it was passing without testing
anything), and timing was re-confirmed to no longer regress for the no-predicate case. **v2.16.24
has NOT been pushed to git yet** — that's step 1 below.

**Open question v2.16.24 surfaced but did NOT resolve**: on the small 44,300-row *synthetic*
sandbox dataset, a still-narrowing-eligible case (3-store subset + keyword) timed SLOWER under
Postgres narrowing than legacy (~21-24ms vs ~8-11ms) despite narrowing 41,800 candidate rows down
to ~1,066 — plausibly just connection/round-trip overhead that doesn't show up at production's
much larger scale (110K+ active / 436K historical rows), but this was never checked against real
production timing. Step 2 below needs to specifically time a narrowing-eligible case live, not
just confirm correctness — don't let Phase E get called a proven latency win on the strength of
the synthetic-dataset numbers alone.

## The task, in order

1. **Give Chuck the exact `git add`/`git commit`/`git push` commands for v2.16.24** (from his Mac
   terminal — never `git push` from the sandbox or the device bridge) and confirm the deploy went
   out and shows `v2.16.24` in the page footer.

2. **Finish live-spot-checking Phase E's shadow path against real production data** (admin
   session, `?pg_shadow=1` vs. no flag on the SAME request, deep-compare after stripping
   `_pg_tier2_shadow*` diagnostic keys — same pattern used for Tier 1 in v2.16.20/21/22). Still
   outstanding from the 2026-09-04 session:
   - category and subcategory facet combos with a keyword active (only brand/condition were
     checked live so far)
   - the `fq-multitoken` filter_q case (`filter_q: 'fender strat'`) — got redacted by an
     unexplained tooling artifact last time (`"[BLOCKED: ...]"` in the tool output), not a real
     failure, but never got a clean retry
   - **timing**, not just correctness, on a narrowing-eligible request (store subset and/or
     `user_last_scan` active) against real production data — this is the open question above.
     If shadow timing is at or below legacy there, Phase E's SQL narrowing is a confirmed win; if
     it's still slower even when it does narrow, that's a real finding to bring back to Chuck
     before considering cutover, not something to paper over.

3. **Only if step 2 passes clean** (correctness AND timing), decide with Chuck whether to cut
   Phase E over to real traffic (drop the `_pg_shadow_requested and _is_admin()` gate in the Tier
   2 call site in `api_browse()` — the guard added in v2.16.24 stays as-is either way, since it's
   independent of the admin/flag gate). Same bar Phase D was held to before its own cutover. If
   anything looks off in step 2, stop, investigate, and do not cut over.

4. **After a Phase E cutover (if it happens), watch Railway's memory graph** over the following
   days — the last-24h graph checked on 2026-09-04 (post-Phase-D, pre-Phase-E) still showed an
   active ~3.5-4.7GB sawtooth from Tier 2 traffic; the real test of Phase E's value is whether that
   settles further once Tier 2 requests are also narrowed. Don't overclaim before that data exists.

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

- The NEW-item anchor/tagging bug — dismissed by Chuck, not a live task, don't revisit unless he
  raises it again.
- Phase F (retire JSON entirely) — not until Phase D and Phase E have both run clean in production
  for a while; not next session either.
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.
- Reimplementing the want-list/keyword matcher in SQL — Phase E deliberately keeps it as unmodified
  Python; there's no proven need to change that.
- Splitting `gc_tracker_app.py` into multiple files — discussed 2026-09-04 (Chuck asked whether
  single-file is still the right call); conclusion was single-file is fine at current scale
  (~7,350 lines, one Flask service, no team-merge pain), and if/when it's worth revisiting, the
  natural split is Flask blueprints (e.g. `pg_layer.py`, `browse.py`, `scan.py`) peeled off
  incrementally — not a project to start now, and not blocking anything in the migration.

Current version: **v2.16.24** (built and verified in the sandbox/device 2026-09-04, NOT yet
pushed or deployed — v2.16.23 is still what's live in production). Last updated: 2026-09-04.
