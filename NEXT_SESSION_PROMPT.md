# Next Session Prompt — Postgres Migration Phase D: the real /api/browse cutover
Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first for full architecture/version history —
especially the v2.16.20 and v2.16.21 entries — then `POSTGRES_MIGRATION_PLAN.md` (repo root) §7
Phase D for the cutover plan this session implements.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud
sandbox filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-02, v2.16.21, deployed and live)

Phases A/B/C are all done, deployed, and verified — including against REAL production data, not
just synthetic. Phase C's `_pg_tier1_eligible()`/`_pg_tier1_browse()` (the pure-SQL Tier 1 read
path) has been live behind `?pg_shadow=1` (admin/dev-only) since v2.16.20, with one real bug
found via live spot-checking and fixed in v2.16.21 (`store_count` — see that HANDOFF.md entry).
Three live spot-checks against production (no-filter/all-stores, a single-store filter, a
price sort) all came back byte-identical after the fix.

**This session's job**: make the Postgres Tier 1 path serve REAL USERS, not just admins with a
query flag. This is Phase D — read POSTGRES_MIGRATION_PLAN.md §7's Phase D paragraph before
starting, it's short and specific about scope.

## The task, in order

1. **Expand live spot-checking BEFORE touching any cutover code.** Only 3 request shapes have
   been checked against real production so far — the v2.16.20 session's offline harness covered
   37 cases against synthetic data, but v2.16.21 proved synthetic data has real blind spots
   (the empty-store bug). Before flipping anything, spot-check `?pg_shadow=1` against production
   for the shapes NOT yet tried live: a couple of the facet filters (brand/condition/category/
   subcategory) alone and combined, the watched-only filter, price-drop-only, vintage-only, a
   price range, at least one more sort field (date, condition), and page 2+ of a result set. Use
   the browser-console diff pattern from the v2.16.20/21 sessions (fetch both paths with the
   same body, strip `_pg_shadow`/`_pg_shadow_ms`, deep-compare). Fix anything that doesn't match
   — read the code, reproduce locally against a scratch Postgres first, same discipline as
   v2.16.21 — before proceeding to the actual cutover.

2. **The cutover itself**: in `api_browse()`, the `_pg_shadow_requested and _is_admin()` gate
   (added in v2.16.20, just before the "Build full item list" comment) currently guards whether
   `_pg_tier1_browse()` runs at all. Change this so that whenever `_pg_tier1_eligible(fq,
   _has_kw, sort_field)` is true AND `_PG_POOL is not None`, the Postgres path runs for EVERY
   user — not just admins, not just with the query flag. Keep `?pg_shadow=1` as an explicit
   *forced* path for debugging/admin use (e.g. still useful to force-compare even after cutover),
   but it should no longer be the GATE for whether real users get it. The `try/except` fallback
   to the legacy JSON path on any error must stay exactly as it is — that's the safety net for
   both admin and real-user requests alike.

   Tier 2 (keyword/`filter_q` requests) keeps falling through to the unmodified legacy JSON path
   exactly as today — `_pg_tier1_eligible()` already returns `False` for those, no change needed
   there.

3. **The "trivial spots" per the plan** — `/api/state`'s `total_items` figure and the
   `has_store_data` check. Read `api_state()`'s actual code first (don't assume its shape) and
   decide whether these are worth moving to a cheap Postgres `COUNT(*)` query or better left as
   is — they're cheap in the JSON path already (no full-list materialization), so this may not
   be worth the risk/complexity. Use judgment; the plan flags them as "trivial," not mandatory.

4. **Verify like it's a real cutover, because it is one**: `python3 -m py_compile` + `node
   --check`, a disposable-venv import + route-table check, a real gunicorn boot with the
   Procfile's exact flags and curl against a few routes — same as every session in this arc. Then
   the SAME 37-case offline diff harness pattern (scratch Postgres + synthetic data in the CLOUD
   sandbox, not the device — recreate `gen_data.py`/`load_pg.py`/`diff_harness.py` fresh, they
   don't persist across sessions) run one more time against the cutover code path directly (not
   through the `?pg_shadow=1` flag) to confirm real users get identical output. Include at least
   one deliberately-sparse/malformed row (empty store, empty brand, empty category) in the
   synthetic data this time — that's the direct lesson from v2.16.21.

5. **After deploying**: do a few more live spot-checks as an actual (not `pg_shadow`-flagged)
   normal user request, and watch Railway's logs for a few minutes for `[pg]` error lines. The
   rollback lever is trivial if anything's wrong — flip step 2's condition back to
   admin-only-with-flag, JSON dual-write never stopped running.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — but per
  the v2.16.16 incident, **also actually import the module** in a disposable venv and confirm the
  route table builds, for any change touching Flask routes. Boot gunicorn locally with the real
  `--workers=1 --worker-class=gthread` config and curl the routes — this session touches the
  actual `/api/browse` request path for real users, verify accordingly.
- **A synthetic-data offline diff is necessary but not sufficient** (v2.16.21 is direct proof) —
  live spot-checking against real production is required too, both before AND after the cutover.
- The device-bridge shell has no network path to Railway's Postgres — any scale/diff testing
  needs a scratch Postgres + synthetic dataset in the cloud sandbox, recreated fresh each time.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump,
  written in enough detail that a fresh session could pick up the reasoning.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.

## What's explicitly out of scope this session

- **The NEW-item anchor/tagging bug** (`bug_new_item_anchor_scope.md`) — Chuck has a fresh,
  concrete repro (items with today's date not tagged NEW) and this is clearly still an open,
  possibly-worsening issue, but it's UNRELATED to Postgres (it's `_run()`'s scan/anchor logic,
  not the read path) and Chuck explicitly asked to finish Phase D first. **Do not start
  investigating this during the Phase D session** — it gets its own dedicated session right
  after, narrowly scoped to the two next-steps already identified in that memory file (get
  specific SKUs from Chuck, read `parse_products()`'s date-field mapping code). Mentioning it
  here only so a fresh session doesn't lose track of it or think it's resolved.
- Tier 2 / keyword search in SQL (Phase E, and only if still needed after Tier 1 fully ships).
- Retiring `gc_category_cache.json` (Phase F) — not until Phase D has run clean for a while.
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration.

Current version: **v2.16.21**. Last updated: 2026-09-02.
