# Next Session Prompt — Deploy v2.16.23, then live-spot-check Phase E, then decide on the cutover

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first (especially the v2.16.23 entry), then
`POSTGRES_MIGRATION_PLAN.md` §7 for Phase E's design, then project memory files
`postgres_migration_plan_2026-08-31.md` and `postgres_phase_d_healthcheck_2026-09-04.md` for full
status.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand

Postgres Phase D (the `/api/browse` Tier 1 cutover, v2.16.22) was health-checked clean on
2026-09-04 — live spot-checks against production, zero `[pg]` fallback errors in Railway's deploy
logs, and Chuck confirmed nothing looked off. The Railway project was also finally located this
session: it's **`serene-determination`** (service `web`), NOT either of the two project names a
prior session guessed — see `reference_railway.md` in project memory before trying to check Railway
again.

Postgres Phase E (Tier 2 keyword/free-text candidate narrowing) was **built and verified in shadow
mode only** this session (v2.16.23) — NOT yet deployed, NOT yet cut over to real traffic. It adds
two new functions (`_pg_tier2_eligible`, `_pg_tier2_narrow_items`) that narrow the candidate row
set for keyword/`filter_q` requests via Postgres (store selection + scan-date gating only — NOT
facets or `_apply_base`'s own filters, see HANDOFF.md's v2.16.23 entry for exactly why), then hand
that narrowed set to the completely unmodified existing Python keyword matcher / facet-counting /
sort / paginate code. Gated behind admin + `?pg_shadow=1`, so it changes nothing for real users yet.
Verified via a 60-case offline diff harness (fresh 44,300-row synthetic dataset with sparse/
malformed rows) — one real bug found and fixed along the way (an earlier version corrupted
`total_unfiltered` by pushing `_apply_base()`'s own filters into the narrowing WHERE clause; see
the HANDOFF entry) — plus a forced-exception fallback test and a real-gunicorn+curl anonymous-
request smoke test. **v2.16.23 has NOT been pushed to git or deployed** — that's step 1 below.

## The task, in order

1. **Give Chuck the exact `git add`/`git commit`/`git push` commands for v2.16.23** (from his Mac
   terminal — never `git push` from the sandbox) and confirm the deploy went out and shows
   `v2.16.23` in the page footer.

2. **Live-spot-check Phase E's shadow path against real production data** — the same
   browser-console fetch-and-diff pattern used for Tier 1 in v2.16.20/21/22 (admin session,
   `?pg_shadow=1` vs. no flag on the SAME request, deep-compare after stripping
   `_pg_tier2_shadow*` diagnostic keys). Cover a real spread: a few real want-list keyword shapes
   Chuck actually uses, a couple of `filter_q` free-text searches, keyword + store-selection
   combos (the case Phase E actually narrows), keyword + facet combos (to catch any facet-context
   regression that the synthetic 44K-row dataset might not expose at real ~110K-row production
   scale/cardinality), and at least one every-sort-field spot-check. This is the offline-harness-
   passed-but-live-not-yet-checked gap the plan explicitly calls "necessary but not sufficient" —
   don't skip it just because the offline harness was thorough (that was also true going into
   v2.16.21's real store_count bug, which only live spot-checking caught).

3. **Only if step 2 passes clean**, decide with Chuck whether to cut Phase E over to real traffic
   (drop the `_pg_shadow_requested and _is_admin()` gate in the Tier 2 call site in `api_browse()`,
   mirroring exactly how the Phase C → Phase D cutover was a one-condition change) — same bar
   Phase D was held to before its own cutover. If anything looks off in step 2, stop, investigate,
   and do not cut over.

4. **After a Phase E cutover (if it happens), watch Railway's memory graph** over the following
   days — the last-24h graph checked on 2026-09-04 (post-Phase-D, pre-Phase-E) still showed an
   active ~3.5-4.7GB sawtooth from Tier 2 traffic; the real test of Phase E's value is whether that
   settles further once Tier 2 requests are also narrowed. Don't overclaim before that data exists.

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

Current version: **v2.16.23** (built and verified in the sandbox/device this session, NOT yet
pushed or deployed — v2.16.22 is still what's live in production). Last updated: 2026-09-04.
