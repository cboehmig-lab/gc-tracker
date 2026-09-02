# Next Session Prompt — Postgres Migration Phase D: post-deploy verification (or, if not yet deployed, deploy it)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — especially the v2.16.22 entry — then
`POSTGRES_MIGRATION_PLAN.md` §7 Phase D for the plan this cutover implements.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-02, v2.16.22 committed locally, NOT YET PUSHED)

Phase D — the real `/api/browse` cutover — is code-complete and thoroughly verified:

- **16/16 live spot-check request shapes** against real production came back byte-identical
  between the legacy JSON path and the Postgres Tier 1 path (facets alone/combined, watched-only,
  price-drop-only, vintage-only, price range, every sort field both directions, page 2/3, a
  fully-combined filter+sort+page case).
- **39/39 offline diff-harness cases** passed against a fresh 53K-row synthetic dataset with
  2,000 deliberately sparse/malformed rows (empty store/brand/category/subcategory/condition,
  zero price, empty date_listed) — the direct lesson from v2.16.21's real production
  `store_count` bug. Compared the actual cutover code path (no flag, `_PG_POOL` live) against the
  legacy path (`_PG_POOL` monkeypatched to `None`).
- **Forced-exception test** confirmed the `try/except` fallback around `_pg_tier1_browse()`
  protects real (non-admin, unflagged) traffic, not just admin diagnostic calls.
- **Real gunicorn boot**, exact Procfile flags, against a scratch Postgres — confirmed an
  anonymous `POST /api/browse` with no flag gets Postgres-backed data with no `_pg_shadow`
  diagnostic keys leaking, and that `?pg_shadow=1` without an admin session also adds nothing.
- `py_compile` + `node --check` clean; module imports cleanly in a disposable venv with the route
  table intact (60 routes, unchanged).

Full writeup: HANDOFF.md's `v2.16.21 → v2.16.22` entry.

## The task, in order

1. **Check whether v2.16.22 is already pushed and deployed.** `cd ~/Desktop/gc_tracker && git
   log --oneline -3` and check Railway's dashboard / `curl -s https://gcgeartracker.com/api/state
   -H 'Cache-Control: no-cache'` won't show version, so check the page source for `v2.16.22` in
   the footer, or just ask Chuck. If it's already deployed, skip to step 3.

2. **If NOT yet pushed**, the commit is already made locally (see `git log`). Give Chuck these
   exact commands to run from his own Mac terminal — do NOT attempt `git push` from the device
   bridge:
   ```
   cd ~/Desktop/gc_tracker
   rm -f .git/index.lock
   git push origin main
   ```
   (That's the standing order: `cd` first, then the lock-file removal, then push — reversing the
   first two deletes the wrong file, per project convention.) Wait for Railway to redeploy
   (auto-deploys on push to `main`) before continuing.

3. **Post-deploy live verification** (plan §7 Phase D step 5 — the one item this session's
   thorough pre-deploy verification couldn't cover, since it requires a live deployed instance):
   - A few real, UNFLAGGED requests (no `?pg_shadow=1`, no admin session — i.e. exactly what a
     real visitor sends) against `https://gcgeartracker.com/api/browse` for a couple of
     representative shapes (no-filter/all-stores, one facet filter, one sort). Confirm sane
     `total_count`/item output — you no longer have a legacy-path response to diff against for a
     real anonymous request post-cutover (that's the point of the cutover), so this is a
     sanity/smoke check, not a byte-diff. If you want an actual diff, an ADMIN session with
     `?pg_shadow=1` still returns the same Postgres-backed response with diagnostic fields
     attached — compare that against what the page renders for a logged-out tab.
   - Watch Railway's deploy logs for a few minutes for any `[pg] tier1 browse failed, falling
     back to JSON path: ...` lines — that would mean real requests are hitting the fallback,
     which is safe (users see zero impact) but worth knowing about and investigating.
   - Spot-check page load speed / responsiveness on the no-filter/all-stores view (the dominant
     traffic pattern) — v2.16.20's harness measured ~9.5x faster for this case at synthetic
     scale; confirm it feels faster in production too, not just per the harness numbers.

4. **If everything looks clean after a few minutes of real traffic**: nothing else to do — Phase
   D is complete. Update HANDOFF.md/HANDOFF_PROMPT.md with a short confirmation note (not a new
   version bump — no code changes, just confirming the already-shipped v2.16.22 is healthy in
   production).

5. **If something looks wrong**: the rollback lever is putting `_is_admin()` back in front of the
   routing condition in `api_browse()` (see the v2.16.22 HANDOFF.md entry for the exact before/
   after) — that's a one-line-condition revert, not a data-recovery operation, since JSON
   dual-write never stopped. Bump `APP_VERSION`, document why in HANDOFF.md, give Chuck the push
   commands the same way as step 2.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds (the v2.16.16 incident's lesson).
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.

## What's explicitly out of scope this next session

- **The NEW-item anchor/tagging bug** (`bug_new_item_anchor_scope.md` in project memory) —
  Chuck has a fresh, concrete repro (items with today's date not tagged NEW) and this is clearly
  still an open, possibly-worsening issue, but it's UNRELATED to Postgres. Per Chuck's explicit
  decision, it gets its own dedicated session right after Phase D fully lands, narrowly scoped to
  the two next-steps already identified in that memory file (get specific SKUs from Chuck, read
  `parse_products()`'s date-field mapping code). Do NOT start investigating it during this
  post-deploy-check session unless Chuck explicitly redirects.
- Tier 2 / keyword search in SQL (Phase E) — not started, not needed unless Tier 1's residual
  cost (the "active want-list AND zero facets AND all-stores" case) turns out to matter in
  practice.
- Retiring `gc_category_cache.json` (Phase F) — not until Phase D has run clean in production for
  a while (Chuck's call on how long).
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.

Current version: **v2.16.22** (committed locally; push status — see step 1). Last updated:
2026-09-02.
