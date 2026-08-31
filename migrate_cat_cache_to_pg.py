#!/usr/bin/env python3
"""
One-off backfill: gc_category_cache.json -> Postgres `items` table.

Part of the flat-JSON -> Postgres catalog migration, Phase A.
See POSTGRES_MIGRATION_PLAN.md for the full plan.

IMPORTANT: run this from Chuck's own Mac terminal, NOT from inside the Cowork
sandbox — the sandbox's device shell has no network access to Railway, same
reason git pushes have to happen from the real terminal too.

Safe to re-run: uses INSERT ... ON CONFLICT (sku) DO UPDATE, so running it
again later (e.g. after a fresh scan) just re-syncs current state rather than
erroring or duplicating rows.

This script does NOT touch gc_category_cache.json, gc_tracker_app.py's request
paths, or anything already deployed — it only writes to the new Postgres
`items` table. Nothing about the running app changes as a result of running
this.

Usage:
    cd ~/Desktop/gc_tracker
    python3 -m pip install psycopg2-binary   # if not already installed locally
    export DATABASE_URL="<copy from Railway dashboard -> Postgres service -> Variables -> DATABASE_URL (click the eye icon to reveal)>"
    python3 migrate_cat_cache_to_pg.py                      # uses ./gc_category_cache.json
    python3 migrate_cat_cache_to_pg.py /path/to/other.json  # or an explicit path

    Or, if you have the Railway CLI installed and linked to this project:
    railway run python3 migrate_cat_cache_to_pg.py
"""
import sys, os, json, time
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("Missing psycopg2. Run:  python3 -m pip install psycopg2-binary")

SCRIPT_DIR = Path(__file__).parent
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
CACHE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else (SCRIPT_DIR / "gc_category_cache.json")
SCHEMA_FILE = SCRIPT_DIR / "pg_schema.sql"

# Same 20-column shape as _cat_cache's per-item dict (see _run()'s merge block
# and _build_base_item_list() in gc_tracker_app.py) plus the `sku` key.
COLS = ["sku", "name", "brand", "category", "subcategory", "condition", "condition_note",
        "price", "list_price", "has_price_drop", "price_drop", "price_drop_since",
        "store", "location", "url", "image_id", "is_vintage", "available",
        "date_listed", "first_seen"]


def row_for(sku: str, it: dict) -> tuple:
    return (
        sku,
        it.get("name", "") or "",
        it.get("brand", "") or "",
        it.get("category", "") or "",
        it.get("subcategory", "") or "",
        it.get("condition", "") or "",
        it.get("condition_note", "") or "",
        it.get("price", 0) or 0,
        it.get("list_price", 0) or 0,
        bool(it.get("has_price_drop", False)),
        it.get("price_drop", 0) or 0,
        it.get("price_drop_since", "") or "",
        it.get("store", "") or "",
        it.get("location", "") or it.get("store", "") or "",
        it.get("url", "") or "",
        it.get("image_id", "") or "",
        bool(it.get("is_vintage", False)),
        it.get("available", True),
        it.get("date_listed", "") or "",
        it.get("first_seen", "") or "",
    )


def main():
    if not DATABASE_URL:
        sys.exit("ERROR: set DATABASE_URL first — see this script's docstring for where to find it.")
    if not CACHE_PATH.exists():
        sys.exit(f"ERROR: {CACHE_PATH} not found.")

    print(f"Reading {CACHE_PATH} ...")
    t0 = time.time()
    cache = json.loads(CACHE_PATH.read_text())
    print(f"  {len(cache):,} items loaded in {time.time() - t0:.1f}s")

    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = False
    try:
        print("Ensuring schema exists (pg_schema.sql)...")
        with conn.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text())
        conn.commit()

        rows = [row_for(sku, it) for sku, it in cache.items()]
        insert_sql = (
            f"INSERT INTO items ({', '.join(COLS)}) VALUES %s "
            f"ON CONFLICT (sku) DO UPDATE SET "
            f"{', '.join(f'{c}=EXCLUDED.{c}' for c in COLS if c != 'sku')}"
        )
        print(f"Upserting {len(rows):,} rows...")
        t0 = time.time()
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, insert_sql, rows, page_size=2000)
        conn.commit()
        print(f"  done in {time.time() - t0:.1f}s")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM items")
            pg_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM items WHERE available")
            pg_available = cur.fetchone()[0]
    finally:
        conn.close()

    json_count = len(cache)
    json_available = sum(1 for v in cache.values() if v.get("available", True))
    print()
    print(f"Verification:")
    print(f"  JSON total items:      {json_count:,}")
    print(f"  Postgres total rows:   {pg_count:,}")
    print(f"  JSON available:        {json_available:,}")
    print(f"  Postgres available:    {pg_available:,}")
    if pg_count != json_count or pg_available != json_available:
        print("  MISMATCH — investigate before trusting this backfill for anything.")
        sys.exit(1)
    print("  OK — counts match exactly.")


if __name__ == "__main__":
    main()
