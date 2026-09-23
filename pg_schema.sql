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
--
-- v2.16.35: hyphens are normalized to spaces BEFORE tokenizing (regexp_replace below), matching
-- gc_tracker_app.py's own _KW_SPLIT_RE (\W+) tokenizer, which always splits on a hyphen. Without
-- this, Postgres's 'simple' parser treats a hyphen immediately followed by digits as a NEGATIVE
-- NUMBER sign (confirmed: to_tsvector('simple', 'ES-335') emits lexeme '-335', not '335'), so a
-- plain search for '335' silently never matched a hyphenated model number like "ES-335" — see the
-- diff-check-run addenda in POSTGRES_PHASE_F_DESIGN.md for the real-production repro.
--
-- v2.16.36: slashes get the SAME normalization, for a different Postgres parser quirk found via
-- the v2.16.35 production diff-check re-run: Postgres's 'simple' parser treats a bare "word/word"
-- pattern as a single fused compound lexeme rather than splitting it (confirmed:
-- to_tsvector('simple', 'Mesa/Boogie Rectifier') emits ONE lexeme 'mesa/boogie', never separate
-- 'mesa'/'boogie' lexemes), so a plain search for "mesa" never matched "Mesa/Boogie"-branded
-- items. Same fix, same reasoning as the hyphen case above — normalize on both the stored column
-- and every query-side to_tsquery/phraseto_tsquery call (see gc_tracker_app.py's
-- _tsquery_compile_term) so both sides tokenize identically, and matches _KW_SPLIT_RE's own
-- \W+ splitting (which already treats '/' as a separator, same as '-').
--
-- If this column already exists from before v2.16.36 with an OLDER expression (either the
-- original non-normalized one, or v2.16.35's hyphen-only one), _init_pg_schema() and
-- migrate_cat_cache_to_pg.py both migrate it (DROP + this ADD, which rebuilds it fresh) before
-- reaching this statement — see _pg_migrate_search_vector_if_stale() in gc_tracker_app.py, which
-- detects staleness by checking for the '/' literal in the stored expression (present only once
-- this v2.16.36 version has actually been applied).
-- v2.16.39: the hyphen+slash replace above is GENERALIZED to "every run of non-alphanumeric
-- characters becomes one space" — '[^[:alnum:]]+'. Found via the v2.16.38 production browse diff
-- check: Postgres's 'simple' parser also fuses "word.word" (e.g. "Dr.scientist", "K.Line") into a
-- single 'host' lexeme, same class of bug as the v2.16.36 Mesa/Boogie slash fix. Rather than keep
-- finding these one punctuation mark at a time, every separator is now normalized, which matches
-- the Python matcher's own \W+ tokenization. The query side (_tsquery_compile_term) applies the
-- IDENTICAL regexp_replace in SQL, so both sides always tokenize the same way regardless of the
-- database's locale/ctype classification of non-ASCII letters.
ALTER TABLE items ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', regexp_replace(coalesce(name, '') || ' ' || coalesce(brand, ''), '[^[:alnum:]]+', ' ', 'g'))
    ) STORED;

-- v2.16.35: pg_trgm backs a literal, punctuation-preserving, substring-anywhere match for
-- QUOTED-exact want-list/search terms (e.g. '"jam pedal"'), replacing a strict tsquery phrase
-- match (phraseto_tsquery) that couldn't reproduce two things the old Python regex substring
-- matcher did for free: matching "Jam Pedals" (plural) from a singular "jam pedal" query (no
-- stemming in the 'simple' config), and treating "Mr. Black" / "mr black" as different queries
-- (to_tsvector/phraseto_tsquery strip punctuation entirely, so both converged on the same
-- result in testing). See gc_tracker_app.py's _tsquery_compile_term for where this is used — a
-- plain ILIKE '%...%' against name+brand, backed by the trigram index below rather than a
-- sequential scan.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ==CONCURRENT-INDEXES==
-- Everything below this marker CANNOT be executed as part of the same multi-statement blob as
-- everything above: CREATE INDEX CONCURRENTLY is rejected outright by Postgres when it runs
-- inside a transaction block, and both callers of this file (_init_pg_schema() in
-- gc_tracker_app.py and migrate_cat_cache_to_pg.py) apply everything above this marker as one
-- cur.execute() call inside an explicit transaction (autocommit=False). Both callers split this
-- file on this exact marker string, then (v2.16.35) run EACH ';'-separated statement below it as
-- its OWN cur.execute() call on a shared autocommit=True connection. Before v2.16.35 this section
-- was limited to "exactly ONE statement", because sending SEVERAL statements in a single
-- cur.execute() call — even on an autocommit connection — has Postgres implicitly wrap that whole
-- simple-query message in one transaction, which CONCURRENTLY still refuses; splitting into
-- separate cur.execute() calls (each its own simple-query message) avoids that, so this section
-- can now hold more than one CONCURRENTLY statement, each on its own line ending in `;`.
--
-- Operational note: if the app restarts mid-build (a Railway redeploy racing this), Postgres
-- can leave an INVALID index behind under one of these exact names. IF NOT EXISTS treats "exists"
-- as "a relation with this name is present", not "is valid", so a leftover invalid index silently
-- blocks any future automatic rebuild. Check `SELECT indexrelid::regclass, indisvalid FROM
-- pg_index WHERE indexrelid = '<name>'::regclass;` if search ever seems to be falling back to a
-- sequential scan on `items`; `DROP INDEX CONCURRENTLY <name>` and let the next app startup
-- rebuild it if so.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_items_search_vector ON items USING gin (search_vector);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_items_name_brand_trgm ON items USING gin ((coalesce(name, '') || ' ' || coalesce(brand, '')) gin_trgm_ops);
