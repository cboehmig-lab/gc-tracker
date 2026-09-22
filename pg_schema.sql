-- GC Gear Tracker — Postgres catalog schema (Phase A of the flat-JSON -> Postgres
-- migration, see POSTGRES_MIGRATION_PLAN.md). Applied by both gc_tracker_app.py's
-- _init_pg_schema() (at app startup, if DATABASE_URL is set) and
-- migrate_cat_cache_to_pg.py (the one-off backfill script) — keep this file as the
-- single source of truth for the DDL rather than duplicating it in either script.
--
-- date_listed / first_seen / price_drop_since are TEXT, not TIMESTAMPTZ, on purpose:
-- they keep the exact same string values and lexicographic-comparison semantics
-- _cat_cache uses today (including the date-only-vs-full-ISO-timestamp handling in
-- _norm_item_date()). Converting representation AND comparison semantics in the same
-- pass as the storage engine risks a NEW-detection regression in code with a real bug
-- history (v2.10.2, v2.12.2, v2.16.11) — see the migration plan §2 for the full
-- reasoning. Revisit as a separate, later cleanup once the storage migration itself
-- is proven stable.

CREATE TABLE IF NOT EXISTS items (
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
    price_drop_since  TEXT NOT NULL DEFAULT '',
    store             TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    image_id          TEXT NOT NULL DEFAULT '',
    is_vintage        BOOLEAN NOT NULL DEFAULT FALSE,
    available         BOOLEAN NOT NULL DEFAULT TRUE,
    date_listed       TEXT NOT NULL DEFAULT '',
    first_seen        TEXT NOT NULL DEFAULT ''
);

-- The dominant query shape is "available items, optionally filtered by store/
-- brand/condition/category/subcategory, sorted, paginated" (see plan §3, Tier 1).
CREATE INDEX IF NOT EXISTS idx_items_available   ON items (available);
CREATE INDEX IF NOT EXISTS idx_items_store       ON items (store)       WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_brand       ON items (brand)       WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_condition   ON items (condition)   WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_category    ON items (category)    WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_subcategory ON items (subcategory) WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_date_listed ON items (date_listed DESC) WHERE available;
CREATE INDEX IF NOT EXISTS idx_items_price       ON items (price)       WHERE available;

-- Phase F (2026-09-22, POSTGRES_PHASE_F_DESIGN.md §7 stage 1): a generated tsvector column
-- backing Postgres full-text search, replacing the Python regex `_compile_query`/`_kw_match`
-- matcher (which searches `name_l + " " + brand_l` — see _kw_match() in gc_tracker_app.py).
-- 'simple' config on purpose — no stemming/stopword removal — so matching stays close to
-- today's literal whole-word semantics rather than introducing new fuzziness nobody asked for.
ALTER TABLE items ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(brand, ''))
    ) STORED;

-- ==CONCURRENT-INDEXES==
-- Everything below this marker CANNOT be executed as part of the same multi-statement blob as
-- everything above: CREATE INDEX CONCURRENTLY is rejected outright by Postgres when it runs
-- inside a transaction block, and both callers of this file (_init_pg_schema() in
-- gc_tracker_app.py and migrate_cat_cache_to_pg.py) apply everything above this marker as one
-- cur.execute() call inside an explicit transaction (autocommit=False). Both callers split this
-- file on this exact marker string and run the statement below on its own autocommit=True
-- connection instead. Keep exactly ONE statement below this marker — see the callers' comments
-- for why multiple statements here would reintroduce the same implicit-transaction problem even
-- under autocommit.
--
-- Operational note: if the app restarts mid-build (a Railway redeploy racing this), Postgres
-- can leave an INVALID index behind under this exact name. IF NOT EXISTS treats "exists" as
-- "a relation with this name is present", not "is valid", so a leftover invalid index silently
-- blocks any future automatic rebuild. Check `SELECT indexrelid::regclass, indisvalid FROM
-- pg_index WHERE indexrelid = 'idx_items_search_vector'::regclass;` if search ever seems to be
-- falling back to a sequential scan on `items`; `DROP INDEX CONCURRENTLY idx_items_search_vector`
-- and let the next app startup rebuild it if so.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_items_search_vector ON items USING gin (search_vector);
