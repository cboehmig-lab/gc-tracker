# GC Gear Tracker — Flat-JSON Catalog → Postgres Migration Plan
*Drafted 2026-08-31. Discussion/planning document — no code shipped yet. Current version at time of writing: v2.16.13.*

## 0. Scope and recommendation

In scope: the **inventory catalog** only — today's `gc_category_cache.json` / `_cat_cache` (~92K items, ~53MB), the memoized `_build_base_item_list()`, `/api/browse`'s filter/sort/paginate/facet-count logic, and the scan write path in `_run()` (upsert + sold-marking + NEW-anchor).

Out of scope, recommend leaving as-is: `gc_users.db` (SQLite — accounts, per-user watchlist/keywords/favorites/saved-searches/last_anchor). It's small (28KB locally, presumably similar in prod), low write volume, already has WAL mode and daily VACUUM-INTO backups, and isn't the thing causing memory growth or slow queries. Moving it to Postgres would be pure risk with no measured upside — SQLite is the right tool for a small per-user KV-ish table on a single-writer app. Recommend revisiting only if you ever need to run more than one gunicorn worker/instance (which would also need `_run_queues`/`_lock`/SSE state externalized — a bigger, separate architectural change).

Also out of scope: `/newdeals`'s `gc_new_deals_cache.json` (new-inventory admin page). Same shape of problem could exist there but it's admin-only, low traffic, not implicated in the memory/perf investigations — defer.

**Recommendation: move the catalog to Postgres, but do it as a staged rollout with a dual-write period and a byte-for-byte A/B diff gate before any cutover, matching how v2.16.4/v2.16.12 were verified.** The full 92K-row Python query engine (especially the want-list boolean/wildcard keyword matcher) is not worth reimplementing in SQL this pass — see §3 for the hybrid approach that avoids that rewrite while still fixing the memory problem for the dominant traffic pattern.

## 1. Why this over the alternatives already ruled out

BigQuery: ruled out before this session (analytical warehouse, per-query latency overhead, ~228K/month `/api/browse` calls likely blow the free tier) — not revisited here.

SQLite-for-catalog-too / DuckDB: viable technically, but Postgres is the better fit specifically *because* Railway hosts it as a managed, separately-scaled service — the current problem is a single process holding 92K rows in Python-land under concurrent thread load; an embedded DB in the same process doesn't change that fact, a separate DB process does (its own memory, its own OS page cache, and importantly its own restart lifecycle so a leak/growth pattern there doesn't take the app down). Postgres also gets you real concurrent-write safety when scan writes and browse reads overlap, which SQLite's single-writer model makes more awkward under gthread concurrency.

## 2. Schema

One table, no ORM (matches the app's existing lightweight raw-SQL style with `sqlite3`/`_user_db()` — recommend plain `psycopg2` here too, not SQLAlchemy, to keep the codebase's single-file simplicity):

```sql
CREATE TABLE items (
    sku               TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '',
    brand             TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL DEFAULT '',
    subcategory       TEXT NOT NULL DEFAULT '',
    condition         TEXT NOT NULL DEFAULT '',
    condition_note    TEXT NOT NULL DEFAULT '',
    price             NUMERIC(10,2) NOT NULL DEFAULT 0,
    list_price        NUMERIC(10,2) NOT NULL DEFAULT 0,
    has_price_drop    BOOLEAN NOT NULL DEFAULT FALSE,
    price_drop        NUMERIC(10,2) NOT NULL DEFAULT 0,
    price_drop_since  TEXT NOT NULL DEFAULT '',      -- kept as the existing ISO-string-or-'' shape
    store             TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    image_id          TEXT NOT NULL DEFAULT '',
    is_vintage        BOOLEAN NOT NULL DEFAULT FALSE,
    available         BOOLEAN NOT NULL DEFAULT TRUE,
    date_listed       TEXT NOT NULL DEFAULT '',      -- keep as TEXT initially, see note below
    first_seen        TEXT NOT NULL DEFAULT ''
);

-- The dominant query shape is "available items, optionally filtered by store/
-- brand/condition/category/subcategory/price range/vintage, sorted, paginated."
CREATE INDEX idx_items_available        ON items (available);
CREATE INDEX idx_items_store            ON items (store)        WHERE available;
CREATE INDEX idx_items_brand            ON items (brand)        WHERE available;
CREATE INDEX idx_items_condition        ON items (condition)    WHERE available;
CREATE INDEX idx_items_category         ON items (category)     WHERE available;
CREATE INDEX idx_items_subcategory      ON items (subcategory)  WHERE available;
CREATE INDEX idx_items_date_listed      ON items (date_listed DESC) WHERE available;
CREATE INDEX idx_items_price            ON items (price)        WHERE available;
-- Composite, for the single most common query (Chuck's own case): all stores,
-- no filters, sorted by date. Postgres can also combine the single-column
-- indexes above via bitmap AND for less common filter combos.
```

**`date_listed` stays TEXT, not TIMESTAMPTZ, on purpose initially.** The app's `_norm_item_date()` treats date-only values (`"2026-05-05"`) as end-of-day and does string comparison today; there's a real, documented history of subtle bugs in this exact area (v2.10.2, v2.12.2, v2.16.11). Converting the representation and the comparison semantics in the same migration as the storage engine is how you get a NEW-detection regression that's hard to attribute. Recommendation: keep it a string column with the exact same values `_cat_cache` stores today, do lexicographic comparison in SQL (`date_listed > :threshold`) exactly as Python does now (works because the app already normalizes date-only values before comparing), and revisit TIMESTAMPTZ as a *separate*, later cleanup once the storage migration itself is proven stable. Same reasoning applies to `first_seen` and `price_drop_since`.

No `id` surrogate key — `sku` is already the natural key everywhere in the code (`_cat_cache[sku]`), reuse it as the Postgres primary key so every `sku`-keyed lookup in the scan-merge code translates directly.

## 3. Read path — the two-tier strategy (the key design decision)

**Tier 1 — pure SQL (the dominant case, per v2.16.4's own finding that Chuck personally browses all 298 stores with no filter, and per the /api/browse traffic volume driving the memory investigation): no want-list keywords active, only "simple" filters (store/brand/condition/category/subcategory/price range/vintage/watched/price-drop) or no filters at all.**

This entire case moves to SQL: `WHERE available [AND store = ANY(:stores)] [AND ...other facet filters...] [AND date_listed > :user_last_scan_gate] ORDER BY <sort col> LIMIT :per_page OFFSET :offset`. Facet counts become 4 `GROUP BY` aggregate queries (or one query with 4 `FILTER (WHERE ...)` clauses in a single pass, mirroring the v2.16.12 "single pass" optimization but in SQL). The NEW-on-top tiering becomes `ORDER BY (date_listed > :anchor) DESC, <primary sort>` (no keyword tier needed since no keywords active). This is the case that currently materializes and re-scans the full 92K-item Python list on every request — moving it to SQL is what actually kills the memory-growth mechanism, because the "large temporary Python structures under concurrent thread load" (the confirmed root cause in `perf_railway_memory_growth_2026-08-31.md`) stop being built for this traffic at all. This is almost certainly the majority of `/api/browse` calls.

**Tier 2 — hybrid, for when want-list keywords or `filter_q` free text are active.**

The existing keyword matcher (`_kw_match`, the bucketed plain-word/phrase/wildcard/comma-AND/bool-OR-NOT compiler) is genuinely sophisticated — it has its own multi-session bug history (v2.13.1 DoS caps, v2.14.4 cap-starvation fix, v2.16.0 operators, v2.16.3 colon-prefix). Reimplementing that as Postgres `tsquery`/`pg_trgm` expressions is a real project on its own and would need the same fuzz-testing rigor that Python version got — not something to rush alongside a storage migration. Instead: push every filter SQL *can* express (store/brand/condition/category/subcategory/price/vintage/date-gating/availability) into the `WHERE` clause first, fetch that narrowed row set from Postgres, then run the existing unmodified Python `_kw_match`/`_apply_base`/sort/tiering/paginate logic over that set exactly as today. Zero behavior risk (it's the same code), and it's still strictly better than today whenever facet filters narrow the set — only genuinely regresses to "materialize everything" when a user has an active want list AND zero facet filters AND browses all stores, which is a real but narrower case than today's "every single browse call" cost. Phase 2 (a real SQL-native keyword engine) can be scoped later if Tier 2's residual cost is still a problem after Tier 1 ships — don't build it speculatively now.

## 4. Write path — mapping the scan/merge logic

`_run()`'s per-product merge loop (reads `cached = _cat_cache.get(sku, {})`, merges with new Algolia data, writes back) becomes: batch-`SELECT sku, has_price_drop, price_drop_since, first_seen, is_vintage, condition_note FROM items WHERE sku = ANY(:skus)` once per scan (not per item) to build the same "existing row" merge-source dict Python already builds from `_cat_cache`, then do the same merge logic in Python (unchanged), then bulk-upsert via `INSERT ... ON CONFLICT (sku) DO UPDATE SET ...` using `psycopg2.extras.execute_values` (single round-trip for all ~92K rows on a nationwide scan, not one UPDATE per item).

**Sold-marking** (`for sku, cached in _cat_cache.items(): if sku not in ids_this_run and (nationwide or store in scanned_store_set): available=False`): stage `ids_this_run` into a temp table (`CREATE TEMP TABLE run_skus (sku TEXT PRIMARY KEY)` + `execute_values` insert), then one anti-join UPDATE: `UPDATE items SET available=false WHERE available AND NOT EXISTS (SELECT 1 FROM run_skus WHERE run_skus.sku=items.sku) AND (:nationwide OR store = ANY(:scanned_store_set))`. This preserves the exact v2.16.11 coverage-gap safety logic (incomplete stores excluded from `scanned_store_set`, nationwide skips entirely if `scan_incomplete`) — the Postgres version is a direct translation of the same Python-computed set, not new logic.

**NEW-anchor computation** (`max(date_listed across all_products)` when `coverage_ok`): stays exactly as-is — it's computed from `all_products` (this run's in-memory Python list, not the DB) today and should keep being computed that way; no DB round-trip needed for this part at all. Same for the `coverage_ok`/`incomplete_stores`/`scan_incomplete` bookkeeping — none of that touches the catalog storage layer, so it's unaffected by this migration.

## 5. Connection pooling under `--workers=1 --threads=8`

One process, up to 8 concurrent request threads plus 1 background scan thread plus the periodic `malloc_trim` thread (that one never touches the DB). Recommend `psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=12)` created once at module load (same lifecycle as today's single `sqlite3.connect()`-per-call pattern in `_user_db()`, just pooled since Postgres connections are more expensive to establish than SQLite's). Each request does `conn = _pg_pool.getconn()` / `try: ... finally: _pg_pool.putconn(conn)` — a context-manager helper mirroring `_user_db()`'s `with` pattern. 12 max connections comfortably covers 8 request threads + scan thread + headroom, and stays well inside Railway managed Postgres's default connection ceiling (worth confirming the exact number for whatever plan you provision, but it's not a tight constraint at this scale — see §7).

## 6. Provisioning — cost and what it involves (needs your go-ahead, not doing this without it)

Railway doesn't sell "a Postgres add-on" as a fixed line item — it's the same usage-based metering as the rest of your Railway resources: **~$0.00000386/GB·s** RAM, **~$0.00000772/vCPU·s**, **~$0.00000006/GB·s** volume storage, drawn from your plan's monthly credit (Hobby: $5/mo credit, 48 vCPU/48GB RAM/5GB storage ceiling per service). A Postgres instance sized for a 92K-row table (a few hundred MB of actual data, well under the 5GB Hobby ceiling) with modest idle RAM (Postgres itself typically settles well under 512MB for a dataset this size) should be a small fraction of the existing $5 Hobby credit — plausibly close to free on top of what you're already running the Flask app on, but I haven't seen your actual Railway plan/current usage, so I'd want you to check your dashboard's current credit headroom before we provision rather than me asserting a number. Provisioning itself is one click in the Railway dashboard (New → Database → PostgreSQL in your existing project) and hands you a `DATABASE_URL` env var automatically wired into the service. **I won't provision this — say the word and I'll walk you through the dashboard steps, or you can do it yourself in under a minute.**

Sources: [Railway Pricing Calculator](https://makerkit.dev/pricing-calculator/railway), [Railway Pricing 2026 overview](https://www.srvrlss.io/provider/railway/), Railway's own pricing page (railway.com/pricing).

## 7. Rollback-safe, diff-verified cutover — phased, each phase its own version bump

**Phase A — schema + backfill (no behavior change).** Add `psycopg2-binary` to `requirements.txt`. New `_pg_schema.sql` (or inline `CREATE TABLE IF NOT EXISTS` at startup, matching the existing `_init_user_db()` pattern). One-off backfill script (`migrate_cat_cache_to_pg.py`, run manually from the Mac against Railway's `DATABASE_URL`, not part of the request path) that reads `gc_category_cache.json` and bulk-inserts every row. Verify: row count matches `len(_cat_cache)`, spot-check a sample of rows field-by-field against the JSON.

**Phase B — dual-write (no read-path change, no user-visible behavior change).** `_run()` keeps writing `_cat_cache`/JSON exactly as today (unchanged, still the source of truth) and additionally does the upsert + sold-marking Postgres writes from §4, best-effort and non-fatal (same pattern as the existing `_set_user_data` anchor-persist try/except) — a Postgres hiccup must never break a scan. Runs in production for some number of scans (your call how long) so Postgres accumulates real, live-verified data before anything reads from it.

**Phase C — shadow-mode Tier 1 read path, diffed offline first.** Implement the SQL-only Tier 1 query path from §3 behind a flag (e.g. `?pg_shadow=1`, admin/dev-only). Build a Flask `test_client()` A/B harness — same approach as v2.16.12 — that fires a representative set of request shapes (no filters/all stores; single/multi-store; each facet filter alone and combined; each sort field/direction; multiple pages; the NEW-tier default sort; watched-only; price-drop-only; vintage-only; price range) at both the legacy JSON path and the new Postgres path against a copy of the real production-scale cache, and diffs the JSON output field-by-field. Fix any mismatch before proceeding — do not tune tolerances to make a diff pass. Then optionally run shadow-mode in production for a burn-in period (log-only diffs, zero user impact) if you want extra confidence beyond the offline diff.

**Phase D — Tier 1 cutover.** Flip `/api/browse` (and the trivial spots — `/api/state`'s `total_items`, the `has_store_data` check) to Postgres for the no-keyword/simple-filter case; keyword/`filter_q` requests still fall through to the existing JSON-based path unchanged. JSON dual-write continues — this is the rollback lever: reverting is flipping the flag back, not a data-recovery operation, because JSON never stopped being maintained.

**Phase E — Tier 2 hybrid cutover.** Same diff-then-flip discipline for the keyword/free-text case (§3 Tier 2): SQL narrows by facets, existing Python keyword code runs unchanged on the result.

**Phase F — retire JSON, once you're comfortable (measured in weeks of clean production traffic, your call).** Stop dual-writing `gc_category_cache.json`; Postgres becomes sole source of truth. Only after this phase does the original memory-growth mechanism (materializing the full 92K-item list under concurrent load) become structurally impossible rather than just avoided on the hot path — worth stating plainly since it's the whole point of doing this instead of just keeping the `malloc_trim` mitigation (v2.16.13) forever.

Each phase gets its own `APP_VERSION` bump and its own `HANDOFF.md`/`HANDOFF_PROMPT.md` changelog entry with the same level of detail as the v2.16.10–13 entries, per project convention — not one giant "did Postgres" bump.

## 8. Open questions for you before Phase A starts

1. Go-ahead to provision Railway Postgres (§6) — yes/no, and do you want me to walk you through the dashboard or will you do it.
2. How long do you want Phase B (dual-write) to run in production before I build the Phase C diff harness — a few scans, a day, longer?
3. Is `/newdeals`'s separate new-inventory cache in scope for a *future* follow-on migration, or staying flat-JSON indefinitely (my recommendation: leave it, it's not implicated in the perf/memory findings)?
4. Any objection to `psycopg2` (raw SQL, matching the existing `sqlite3` style) over an ORM like SQLAlchemy? I'm recommending raw SQL for consistency with the rest of the single-file app's style and to keep the diff surface small, but flagging it as a real choice rather than assuming.
