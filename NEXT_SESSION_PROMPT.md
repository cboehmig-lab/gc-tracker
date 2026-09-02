# Next Session Prompt — Postgres Migration Phase C (SQL Tier-1 shadow read path)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first for full architecture/version history,
then `POSTGRES_MIGRATION_PLAN.md` (repo root) for the complete 6-phase migration plan — this
session is Phase C specifically, sections §3 and §7 of that doc.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud
sandbox filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-02, v2.16.19)

Phase A (schema + backfill) and Phase B (dual-write) are both **done and verified against live
production**, not just locally. The original Phase A backfill was run from a stale local file
and left Postgres badly incomplete; that was fixed two versions ago (v2.16.18 added an
admin-triggered full backfill from the live in-process `_cat_cache`, v2.16.19 fixed a
pre-existing CSP bug that was silently blocking that backfill's "Run Now" button — see
HANDOFF.md's v2.16.18/19 entries for the full incident writeups). Final parity check, run for
real against production:

```
json_total: 436,343   pg_total: 436,343   missing_in_pg: 0   extra_in_pg: 0   field_mismatches: 0
```

Postgres now mirrors `_cat_cache` exactly, and Phase B's dual-write (live since 2026-08-31)
keeps it that way as new scans run. `_cat_cache`/`gc_category_cache.json` is still the sole
source of truth for every read — nothing on the request path reads from Postgres yet.

## The task: Phase C — SQL Tier-1 read path, shadow-mode, diffed offline

**This is NOT the cutover.** Phase D (a separate, later session) is where `/api/browse` actually
starts reading from Postgres for real users. This session builds and verifies the SQL path
without touching real traffic.

Per POSTGRES_MIGRATION_PLAN.md §3, "Tier 1" is the dominant case: no want-list keywords or
`filter_q` free-text search active — just the simple facet filters (store/brand/condition/
category/subcategory/price range/vintage/watched/price-drop) or no filters at all. This is most
of `/api/browse`'s traffic (Chuck's own primary use — browsing all 298 stores unfiltered — is
this case). Tier 2 (keyword/free-text search active) is explicitly **out of scope** for Phase C
— see §3's reasoning for why the existing `_kw_match` engine isn't being reimplemented in SQL
this pass; that's Phase E, later, if it's even still needed after Tier 1 ships.

### What to actually build, in order

1. **Connection pooling** (plan §5) — not built yet. Current Postgres code (`_pg_sync_scan`,
   `_pg_parity_check`, `_pg_full_backfill`) opens one ad-hoc `psycopg2.connect()` per call,
   which is fine for admin-only/scan-triggered paths but not for something hit on every
   `/api/browse` call. Add a `psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=12)`
   created once at module load, with a context-manager helper mirroring `_user_db()`'s
   `with`-based pattern for SQLite. Sized for `--workers=1 --threads=8` (8 request threads + the
   scan thread + headroom) — see §5 for the exact reasoning.

2. **The Tier 1 SQL query** — translate `_apply_base()`'s simple-filter path (store/brand/
   condition/category/subcategory/price/vintage/watched/price-drop, date-gating, sort, facet
   counts, pagination) into `WHERE`/`ORDER BY`/`LIMIT`/`OFFSET` against the `items` table, plus
   the facet-count aggregate queries (mirror the v2.16.12 "single pass" approach, in SQL this
   time — one query with `FILTER (WHERE ...)` clauses, or the 4 separate `GROUP BY`s, whichever
   profiles better). Gate this behind an admin/dev-only flag (e.g. `?pg_shadow=1`) — it must not
   be reachable by normal users this session.

3. **A/B diff harness** — a Flask `test_client()`-based script (same rigor as v2.16.12's own
   perf-verification harness — read that HANDOFF.md entry for the pattern) that fires a
   representative set of request shapes at both the legacy JSON path and the new Postgres path
   against production-scale data, and diffs the JSON output field-by-field. Cover: no filters/
   all stores; single store; multiple stores; each facet filter alone and combined; each sort
   field/direction; multiple pages (not just page 1); the default NEW-on-top sort; watched-only;
   price-drop-only; vintage-only; price range. **Fix any mismatch found — do not tune tolerances
   or drop cases to make the diff pass.**

4. **Verify at production scale** — the real `_cat_cache`/Postgres now both have ~436K rows
   (not ~92K like the plan doc's original estimate — it was written before the 436K-vs-111K
   history was fully understood). Time both paths under that actual scale, not a small sample.

5. Only after the offline diff is clean: **optionally** wire up shadow-mode logging in
   production (both paths run, only JSON's result is served, mismatches logged) for a burn-in
   period, if you want extra confidence beyond the offline harness. This is optional and your
   call whether to do it this session or leave it for next time — either way, the *served*
   response must keep coming from the JSON path this session, unconditionally, for every real
   user.

### Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — but
  per the v2.16.16 incident (routes crashed production because `py_compile` doesn't catch
  import-time/module-order bugs), **also actually import the module** in a disposable venv
  (`python3 -m venv` + install the same deps as `requirements.txt` — the repo's own `.venv` is
  tied to this Mac's Homebrew Python and isn't importable from the device-bridge sandbox) and
  confirm the route table builds, for any change touching Flask routes. Consider going one step
  further and booting gunicorn locally with the real `--workers=1 --worker-class=gthread`
  config and curling the routes, like the v2.16.18/19 sessions did.
- All JS lives in `static/gc.js` (or a new file under `static/`, following the `static/
  admin-task.js` precedent from v2.16.19) — CSP blocks inline scripts AND inline `onclick=`/
  event-handler attributes (`script-src 'self'`, no `'unsafe-inline'`, no nonce). Use `data-*`
  attributes + `addEventListener`, never `onclick="..."` in generated HTML.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump,
  written in enough detail that a fresh session could pick up the reasoning.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.
- Test locally before proposing anything's done — the A/B diff harness above IS the test for
  this phase; don't skip it or ship the Tier 1 path without it passing clean.

## What's explicitly out of scope this session

- Actually cutting `/api/browse` over to Postgres for real traffic (Phase D).
- Tier 2 / keyword search in SQL (Phase E, and only if still needed).
- Retiring `gc_category_cache.json` (Phase F).
- `/newdeals`'s separate new-inventory cache — staying flat-JSON, not part of this migration
  (see plan §0).
- The NEW-item anchor scope bug (`bug_new_item_anchor_scope.md`) — separate, deferred thread,
  do not touch it.

Current version: **v2.16.19**. Last updated: 2026-09-02.
