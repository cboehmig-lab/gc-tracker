# Next Session Prompt — push v2.16.25, live-time it, then the Phase E cutover decision

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — especially the **2026-09-08** entries at
the top (there are two: "Phase E cursor-overhead fix — v2.16.25" and, right below it, "Phase E
live-spot-check pass completed") — then `POSTGRES_MIGRATION_PLAN.md` §7 for Phase E's design, then
project memory files `postgres_phase_e_cursor_investigation_2026-09-08.md`,
`postgres_phase_e_verification_2026-09-08.md`, `postgres_phase_e_2026-09-04.md`, and
`postgres_migration_plan_2026-08-31.md` for full status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover, v2.16.22) is live and healthy — nothing to
revisit there.

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) is built, still **shadow-mode
only** (admin + `?pg_shadow=1` gate — zero real-user impact either way). Timeline this project has
been through on it:

1. **v2.16.24** (2026-09-04, live in production): Tier 2 narrowing shipped, shadow-mode only.
2. **2026-09-08 live-spot-check pass** (on v2.16.24): correctness and Railway deploy logs both
   clean, but production timing on a narrowing-eligible request (5-store subset + category filter
   + keyword, narrowing 111K rows → 2,015 candidates) was **~12% slower** under Postgres shadow
   narrowing than legacy (128.4ms vs 114.1ms mean, 8 reps) — cutover NOT recommended as-is. Chuck
   asked to investigate the round-trip cost before deciding, rather than accept or shelve the
   trade-off blind.
3. **v2.16.25 cursor-overhead fix, same day (built, NOT pushed, NOT live-timed)**: found that
   `_pg_tier2_narrow_items()`'s `RealDictCursor` usage — not network latency, pool checkout, or the
   read-only `conn.commit()` — was the likely dominant cost. A local benchmark (this sandbox's own
   Postgres, NOT Railway's — same schema, row count matching production almost exactly) showed the
   RealDictCursor pattern taking ~36-44ms vs. ~14-20ms for a plain-cursor rewrite doing the exact
   same query and Python projection — a ~2.2-2.5x difference, closely matching the ~40ms
   `_pg_tier2_shadow_ms` seen live. Rewrote `_pg_tier2_narrow_items()` to use a plain cursor +
   `cur.description`-based column lookup instead of `RealDictCursor`. Verified offline: `py_compile`
   + `node --check` clean, module import + Flask route-table build clean in a disposable venv (60
   routes), and an 8-case offline diff harness (local Postgres, varying store subsets/search_all/
   user_last_scan, including a 108,785-row all-stores case and a zero-row edge case) found the OLD
   and NEW implementations **byte-identical** on every case. Tier 1's `_pg_tier1_browse()` also uses
   `RealDictCursor` but wasn't touched — its result sets are tiny per request (summary rows, facet
   rows, one page of ≤200 items), so the overhead this fix addresses doesn't apply there.

**What's NOT done yet**: v2.16.25 has not been pushed to git, deployed, or live-timed against
Railway's real Postgres instance — only verified locally. The local benchmark used different
hardware/network than Railway, so the ~2.2-2.5x local speedup is a strong lead, not a proven
production result.

## The task, in order

1. **Push and deploy v2.16.25 first** (it's still shadow-mode only — zero real-user risk either
   way, same as every prior phase). Give Chuck the exact git commands (cd into
   `~/Desktop/gc_tracker` first, `rm -f .git/index.lock`, then the usual add/commit/push — do not
   run `git push` yourself anywhere). Confirm live via the version string on `gcgeartracker.com`.

2. **Re-run the same live timing methodology** [[postgres_phase_e_verification_2026-09-08]] used:
   admin session, `?pg_shadow=1` vs. no flag, same 5-store-subset + category + keyword request
   body, several alternating reps (`fetch()` in Chuck's own authenticated browser session, via
   Claude in Chrome or however he sets it up). Compare shadow vs. legacy mean/median timing. This
   answers the real open question: does the plain-cursor rewrite actually close (or reverse) the
   ~12-14ms gap on Railway's real hardware, the way it did locally?

3. **Then, and only then, revisit the cutover decision** — this is still Chuck's call, not
   something to decide unilaterally:
   - If shadow timing is now at or below legacy: Phase E is a confirmed win. Chuck may want to cut
     over — drop the `_pg_shadow_requested and _is_admin()` gate at the Tier 2 call site in
     `api_browse()` (the `_pg_tier2_would_narrow` guard stays as-is, it's orthogonal). Bump
     `APP_VERSION`, verify, update `HANDOFF.md`/`HANDOFF_PROMPT.md`, give Chuck the push commands.
     Then watch Railway's memory graph over the following days (last checked 2026-09-04: active
     ~3.5-4.7GB sawtooth) to see whether it settles further.
   - If shadow timing is still slower even after the fix: bring that back to Chuck plainly, same
     as 2026-09-08 did — don't paper over it. At that point the honest options are hold in shadow
     mode (status quo, zero risk) or accept a smaller-but-real latency cost for the memory-pressure
     benefit ([[perf_railway_memory_growth_2026-08-31]]) — Chuck's call either way.
   - Do not silently proceed with a cutover without Chuck weighing in.

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
- Applying the same RealDictCursor-to-plain-cursor treatment to Tier 1's `_pg_tier1_browse()` —
  not investigated, likely not worth it given its small per-request result sets, no evidence of a
  problem there.

Current version: **v2.16.24 still live in production**; **v2.16.25 built and offline-verified on
the device but NOT pushed/deployed yet** — that's next session's first step. Last updated:
2026-09-08.
