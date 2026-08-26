# Next Session Prompt — Data Hosting: Flat JSON Cache vs. BigQuery (or similar)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask web app deployed on Railway that lets users track Guitar Center used inventory for new listings. Read the full architecture doc first:

`/Users/charles.boehmig/Desktop/gc_tracker/HANDOFF.md`

**Goal for this session:** Chuck has been doing some data-infrastructure work at his day job and wants to talk through whether the current inventory storage approach should move to BigQuery (or another proper database/warehouse) instead of what it uses today.

## What the app currently does (for reference, verify against HANDOFF.md before assuming anything is still accurate)
- Inventory scan results are cached in a single flat JSON file, `gc_category_cache.json` — currently ~91-92K items, ~51-53MB
- That file is loaded into an in-memory Python dict (`_cat_cache`) on the Railway dyno, memoized/reloaded by file mtime (`_load_cat_cache()`, v2.13.0 pattern)
- `/api/browse` builds and re-filters an in-memory list of lightweight item dicts (`_build_base_item_list()`, memoized by cache mtime, added v2.16.4 for a perf fix — see HANDOFF.md's E2 entry)
- The JSON file lives on a Railway persistent volume (`DATA_DIR` env var), not in the git repo
- User accounts/watchlists/want-lists/favorites are separate, in SQLite (`gc_users.db`) — that part is NOT in scope for this discussion, only the inventory cache is
- Single dev-mode Flask process (`threaded=True`, not gunicorn) — noted in HANDOFF.md as a known limitation, relevant context for any "how much traffic/concurrency" discussion

## What to actually do this session
This is a discussion/evaluation session, not a build session — don't touch code until Chuck decides something is worth doing.

1. Ask Chuck what specifically prompted this (what he saw at work, what problem he's hoping to solve — slow queries? wanting SQL-style analytics over historical data? cost? something else?)
2. Get a clear picture of current pain points, if any, with the flat-JSON approach at current scale (~92K items, ~283 registered users as of 2026-08-26)
3. Walk through realistic options and genuine trade-offs, not just "BigQuery is more scalable" boilerplate:
   - Stay as-is (flat JSON + in-memory dict)
   - A real embedded/lightweight DB (SQLite for inventory too, DuckDB, etc.) — cheap, still fits Railway's model, no new vendor
   - Postgres (Railway has native Postgres) — relational, transactional, still "boring" ops-wise
   - BigQuery specifically — this is a data warehouse built for large-scale analytical queries, not a low-latency transactional backend for a live web app; worth being honest that it may be a mismatch for "user loads a page and needs a filtered list in <100ms" unless paired with a serving layer
4. Consider what actually matters for gcgeartracker.com's real usage pattern: it's a live, filtered, user-facing browse UI (latency-sensitive), not an analytics dashboard — any recommendation should be grounded in that, not in what's trendy at Chuck's job
5. If a change looks genuinely worth it, scope out a migration plan (data model, what changes in `gc_tracker_app.py`, cost, Railway compatibility, rollback plan) — but only as far as Chuck wants to take it this session

## Standing project constraints (apply if any code work happens)
- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change
- Verify with `python3 -m py_compile gc_tracker_app.py` and `node --check static/gc.js`
- All JS lives in `static/gc.js` (CSP blocks inline scripts)
- Git pushes must happen from Chuck's Mac terminal, never from the sandbox — `cd ~/Desktop/gc_tracker` FIRST, then `rm -f .git/index.lock`, then `git add`/`commit`/`push origin main`
- Update `HANDOFF.md` / `HANDOFF_PROMPT.md` with changelog entries for any version bump

Current version: **v2.16.8** (condition_note ⓘ, normal cursor). Last updated: 2026-08-26.
