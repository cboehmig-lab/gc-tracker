#!/usr/bin/env python3
"""
Guitar Center Used Inventory Tracker — Web App
------------------------------------------------
Run with:  python3 gc_tracker_app.py
Then open: http://localhost:5050
"""

import html as _html
import hmac
import json, os, re, sys, time, threading, queue, webbrowser, random, sqlite3
import gc as _gc, ctypes as _ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FutureTimeoutError
from concurrent.futures import wait as _futures_wait, FIRST_COMPLETED as _FIRST_COMPLETED
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path


def _sleep(base: float, jitter: float = 0.5):
    """Sleep for base ± jitter seconds to avoid looking like a bot."""
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))

try:
    from flask import (Flask, request, jsonify, Response, stream_with_context,
                       session, redirect, send_file)
    from werkzeug.security import generate_password_hash, check_password_hash
except ImportError:
    sys.exit("Missing Flask. Run:  pip3 install flask requests openpyxl")

try:
    import requests as http
except ImportError:
    sys.exit("Missing requests. Run:  pip3 install flask requests openpyxl")

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("Missing openpyxl. Run:  pip3 install openpyxl")

try:
    from authlib.integrations.flask_client import OAuth as _AuthlibOAuth
    _AUTHLIB_AVAILABLE = True
except ImportError:
    _AUTHLIB_AVAILABLE = False

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

# ── Paths & config ────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
DATA_DIR       = Path(os.environ.get("DATA_DIR", SCRIPT_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE     = DATA_DIR / "gc_state.json"
OUTPUT_FILE    = DATA_DIR / "gc_new_inventory.xlsx"
STORES_CACHE   = DATA_DIR / "gc_stores_cache.json"
FAVORITES_FILE = DATA_DIR / "gc_favorites.json"
WATCHLIST_FILE   = DATA_DIR / "gc_watchlist.json"
KEYWORDS_FILE    = DATA_DIR / "gc_keywords.json"
STORE_COORDS_FILE    = DATA_DIR / "gc_store_coords.json"
NEW_DEALS_CACHE_FILE = DATA_DIR / "gc_new_deals_cache.json"


PORT              = int(os.environ.get("PORT", 5050))
APP_PASSWORD      = (os.environ.get("APP_PASSWORD") or "").strip()
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "").strip()
ADMIN_EMAIL       = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
PG_DATABASE_URL   = (os.environ.get("DATABASE_URL") or "").strip()

# ── User accounts (SQLite) ────────────────────────────────────────────────────
USER_DB = DATA_DIR / "gc_users.db"

def _user_db():
    """Open a connection to the user database."""
    conn = sqlite3.connect(str(USER_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ── Daily user-DB backup (v2.15.3, 2026-07 audit "do now" #1) ─────────────────
# gc_users.db is the only non-regenerable data on the volume. VACUUM INTO makes an
# atomic snapshot from a live DB; we keep the last 7 dailies in DATA_DIR/backups.
# Piggybacks on the after_request hook — first request of each UTC day pays ~ms.
_BACKUP_DIR  = DATA_DIR / "backups"
_BACKUP_KEEP = 7
_backup_lock = threading.Lock()
_last_backup_day = None

def _maybe_backup_users_db():
    global _last_backup_day
    today = datetime.utcnow().strftime("%Y%m%d")
    if _last_backup_day == today:
        return
    if not _backup_lock.acquire(blocking=False):
        return  # another request thread is on it
    try:
        if _last_backup_day == today:
            return
        dest = _BACKUP_DIR / f"gc_users_{today}.db"
        if not dest.exists() and USER_DB.exists():
            _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _BACKUP_DIR / f".gc_users_{today}.db.tmp"
            if tmp.exists():
                tmp.unlink()
            conn = sqlite3.connect(str(USER_DB))
            try:
                conn.execute(f"VACUUM INTO '{tmp}'")  # path is fully server-controlled
            finally:
                conn.close()
            os.replace(tmp, dest)
            for old in sorted(_BACKUP_DIR.glob("gc_users_*.db"))[:-_BACKUP_KEEP]:
                old.unlink()
        _last_backup_day = today  # set even on skip; retry next UTC day
    except Exception as e:
        print(f"[backup] users-db backup failed: {type(e).__name__}: {e}")
        _last_backup_day = today  # don't hammer a persistently failing backup
    finally:
        _backup_lock.release()

def _init_user_db():
    with _user_db() as conn:
        # WAL: better concurrent read/write behavior on the threaded server; the
        # setting is persistent in the DB file, so once is enough. (2026-07 audit)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                email         TEXT    UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                google_id     TEXT    UNIQUE,
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_data (
                user_id        INTEGER PRIMARY KEY REFERENCES users(id),
                watchlist      TEXT    DEFAULT '{}',
                keywords       TEXT    DEFAULT '[]',
                favorites      TEXT    DEFAULT '[]',
                last_run       TEXT    DEFAULT '',
                new_ids        TEXT    DEFAULT '[]',
                saved_searches TEXT    DEFAULT '[]',
                last_anchor    TEXT    DEFAULT '',
                updated_at     TEXT    DEFAULT ''
            )
        """)
        # Migration: add saved_searches column for existing databases
        try:
            conn.execute("ALTER TABLE user_data ADD COLUMN saved_searches TEXT DEFAULT '[]'")
        except Exception:
            pass  # Column already exists
        # Migration: add last_anchor column for existing databases (v2.10.18)
        # Per-user anchor for NEW detection — replaces the buggy global-cache anchor
        # which was contaminated by other users' scans.
        try:
            conn.execute("ALTER TABLE user_data ADD COLUMN last_anchor TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists
        # Migration: add google_id column for existing databases
        # NOTE: SQLite ALTER TABLE ADD COLUMN cannot include UNIQUE — add column
        # first, then create the index separately.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id
                ON users(google_id) WHERE google_id IS NOT NULL
            """)
        except Exception:
            pass
        # Migration: add deleted_at column for soft-delete / scheduled deletion (v2.11.2)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
        except Exception:
            pass  # Column already exists
        # Migration: add last_login column (v2.12.4)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        except Exception:
            pass  # Column already exists
        # ── Want List email alerts (v2.18.0, step 1 — see EMAIL_ALERTS_DESIGN.md) ──
        # Alert addresses live ONLY here, Fernet-encrypted (email_enc). email_bidx is a
        # keyed HMAC of the normalized address (per-address rate limits / bounce matching)
        # — neither is readable without the Railway env keys. users.email is a separate,
        # older column (registration / Google) and is never used for alerts.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN alerts_beta INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_email (
                user_id      INTEGER PRIMARY KEY REFERENCES users(id),
                email_enc    TEXT NOT NULL,
                email_bidx   TEXT NOT NULL,
                confirmed_at TEXT,
                created_at   TEXT,
                updated_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_settings (
                user_id    INTEGER PRIMARY KEY REFERENCES users(id),
                frequency  TEXT    DEFAULT 'hourly',
                paused     INTEGER DEFAULT 0,
                suppressed TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_codes (
                user_id    INTEGER PRIMARY KEY REFERENCES users(id),
                email_enc  TEXT NOT NULL,
                email_bidx TEXT NOT NULL,
                code_hash  TEXT NOT NULL,
                expires_at REAL NOT NULL,
                attempts   INTEGER DEFAULT 0,
                sent_at    REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_code_sends (
                user_id    INTEGER NOT NULL,
                email_bidx TEXT    NOT NULL,
                sent_at    REAL    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_code_sends_t ON alert_code_sends(sent_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_sends (
                day   TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_meta (
                k TEXT PRIMARY KEY,
                v TEXT
            )
        """)
        # ── v2.19.0 (step 2, daily alert engine) ──
        # alert_settings.mode: 'all' (every Want List pill except ones switched off in
        # alert_pills) or 'selected' (only pills switched on). anchor: normalized
        # date_listed (same form as the NEW rule's _norm_item_date) — the user's next
        # alert covers available items listed after it. frequency is left in place,
        # unused (alerts are daily only since 2026-10-08).
        # v2.21.0: checked_at = wall time of the user's last alert check (bounds the
        # first_seen half of the NEW rule and which price drops count as new).
        for _col in ("mode TEXT DEFAULT 'all'", "anchor TEXT", "last_alert_at TEXT", "checked_at TEXT"):
            try:
                conn.execute(f"ALTER TABLE alert_settings ADD COLUMN {_col}")
            except Exception:
                pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_pills (
                user_id    INTEGER NOT NULL,
                keyword    TEXT    NOT NULL,
                on_        INTEGER NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, keyword)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_sent (
                user_id INTEGER NOT NULL,
                sku     TEXT    NOT NULL,
                sent_at TEXT    NOT NULL,
                batch   TEXT,
                PRIMARY KEY (user_id, sku)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_sent_t ON alert_sent(sent_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_batches (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL,
                skus       TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_batches_u ON alert_batches(user_id, created_at)")
        # ── v2.21.0: price-drop ledger — the price we last emailed each user about
        # for a dropped listing; a later email only repeats it if it drops further.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_drop_sent (
                user_id INTEGER NOT NULL,
                sku     TEXT    NOT NULL,
                price   REAL    NOT NULL,
                sent_at TEXT    NOT NULL,
                PRIMARY KEY (user_id, sku)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_drop_sent_t ON alert_drop_sent(sent_at)")
        conn.commit()

def _user_by_username(username: str) -> dict | None:
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        return dict(row) if row else None

def _touch_last_login(user_id: int) -> None:
    """Stamp the current UTC time as this user's last login."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
        conn.commit()

def _user_by_id(user_id: int) -> dict | None:
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

# Every per-user table except users itself. Account deletion (admin "now", the lazy
# scheduled purge, the Google import-merge, and later self-service deletion) goes
# through _purge_user_rows so a new per-user table can't be forgotten in one path.
# (v2.18.0) Add new per-user tables HERE.
_PER_USER_TABLES = ("user_data", "alert_email", "alert_settings", "alert_codes",
                    "alert_code_sends", "alert_pills", "alert_sent", "alert_batches",
                    "alert_drop_sent")

def _purge_user_rows(conn, user_id: int) -> None:
    """Hard-delete every row belonging to user_id (caller commits)."""
    for t in _PER_USER_TABLES:
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))  # t is from the constant above
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))

def _user_by_email(email: str) -> dict | None:
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None

def _user_by_google_id(google_id: str) -> dict | None:
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        return dict(row) if row else None

def _gen_google_username(display_name: str) -> str:
    """Generate a unique username from a Google display name."""
    base = re.sub(r'[^A-Za-z0-9_\-]', '', display_name.replace(' ', '_'))[:25]
    if len(base) < 3:
        base = 'user'
    candidate = base
    i = 1
    with _user_db() as conn:
        while conn.execute("SELECT id FROM users WHERE username=?", (candidate,)).fetchone():
            candidate = f"{base}_{i}"
            i += 1
    return candidate

def _get_user_data(user_id: int) -> dict:
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM user_data WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"watchlist": {}, "keywords": [], "favorites": [], "last_run": "", "new_ids": [], "saved_searches": [], "last_anchor": ""}
    try:
        ss = json.loads(row["saved_searches"] or "[]")
    except Exception:
        ss = []
    # last_anchor column may not exist on rows from before the migration ran in
    # this process; sqlite3.Row raises IndexError for missing keys, so guard it.
    try:
        last_anchor = row["last_anchor"] or ""
    except (KeyError, IndexError):
        last_anchor = ""
    return {
        "watchlist":      json.loads(row["watchlist"] or "{}"),
        "keywords":       json.loads(row["keywords"]  or "[]"),
        "favorites":      json.loads(row["favorites"] or "[]"),
        "last_run":       row["last_run"] or "",
        "new_ids":        json.loads(row["new_ids"]   or "[]"),
        "saved_searches": ss,
        "last_anchor":    last_anchor,
    }

def _set_user_data(user_id: int, **kwargs):
    """Update one or more user_data fields. Valid keys: watchlist, keywords, favorites, last_run, new_ids, saved_searches, last_anchor"""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_data (user_id, updated_at) VALUES (?,?)",
            (user_id, now)
        )
        for field, value in kwargs.items():
            if field in ("watchlist", "keywords", "favorites", "new_ids", "saved_searches"):
                conn.execute(
                    f"UPDATE user_data SET {field}=?, updated_at=? WHERE user_id=?",
                    (json.dumps(value), now, user_id)
                )
            elif field in ("last_run", "last_anchor"):
                # Plain TEXT fields — stored as-is (not JSON-encoded)
                try:
                    conn.execute(
                        f"UPDATE user_data SET {field}=?, updated_at=? WHERE user_id=?",
                        (value, now, user_id)
                    )
                except sqlite3.OperationalError:
                    # last_anchor column missing on this connection (very rare —
                    # migration runs at startup, but tolerate it anyway)
                    pass
        conn.commit()

_init_user_db()

# ── HTTP session ──────────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_HEADERS = {
    "User-Agent":                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Cache-Control":             "max-age=0",
}

_http = http.Session()
_http.headers.update(_HEADERS)

# Load persisted cookies if available
COOKIE_FILE = DATA_DIR / "gc_cookies.json"

def _load_cookies():
    if COOKIE_FILE.exists():
        try:
            cookies = json.loads(COOKIE_FILE.read_text())
            _http.cookies.update(cookies)
        except Exception:
            pass

def _save_cookies():
    try:
        COOKIE_FILE.write_text(json.dumps(dict(_http.cookies)))
    except Exception:
        pass

def _rotate_ua():
    """Pick a random User-Agent for the next request."""
    _http.headers["User-Agent"] = random.choice(_USER_AGENTS)

# ── Postgres catalog store ────────────────────────────────────────────────────
# The item catalog lives ONLY in the Postgres `items` table (every item ever
# seen, available and sold — the sold history is kept on purpose). History:
# Phase A (v2.16.14) created this schema as a migration target for the old
# flat-JSON catalog (_cat_cache / gc_category_cache.json); Phases B-F moved
# every read and write over; v2.17.0 (Phase F step 5c) deleted the JSON catalog.
# See POSTGRES_MIGRATION_PLAN.md / POSTGRES_PHASE_F_DESIGN.md.
# Schema setup is guarded end-to-end so a missing DATABASE_URL (local dev), a
# missing psycopg2 install, or an unreachable DB never blocks app startup (the
# site then answers 503 on catalog reads until Postgres is reachable).
#
# Schema lives in pg_schema.sql (repo root).
PG_SCHEMA_FILE = SCRIPT_DIR / "pg_schema.sql"

# pg_schema.sql splits on this marker (see that file's own comment above the marker for the
# full reasoning): everything above it is safe to run as one multi-statement blob inside a
# normal transaction; everything below it is CREATE INDEX CONCURRENTLY, which Postgres rejects
# outright if it runs inside any transaction block, so it must be sent on its own
# autocommit=True connection as its own single statement instead.
PG_SCHEMA_CONCURRENT_MARKER = "-- ==CONCURRENT-INDEXES=="

def _pg_concurrent_statements(concurrent_sql):
    """Splits the CONCURRENT-INDEXES section of pg_schema.sql into individual
    statements, one per cur.execute() call (v2.16.35 — see PG_SCHEMA_CONCURRENT_MARKER's
    comment and pg_schema.sql's own comment above that marker for why: sending several
    statements in a single cur.execute() call has Postgres implicitly wrap that whole
    simple-query message in one transaction even under autocommit, and CREATE INDEX
    CONCURRENTLY is rejected inside any transaction block).

    Full-line SQL comments are stripped before splitting on ';' — pg_schema.sql's own
    comments include example commands that end in ';' (e.g. the "DROP INDEX CONCURRENTLY
    <name>" operational note), which a naive split-on-';' would otherwise mistake for a
    statement boundary, producing an empty/garbage "statement" that fails execute()."""
    code_only = "\n".join(
        line for line in concurrent_sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    return [s.strip() for s in code_only.split(";") if s.strip()]

def _pg_migrate_search_vector_if_stale(conn):
    """One-time self-migration: if items.search_vector already exists but was built from an
    OLDER generated expression than the current one in pg_schema.sql, drop it so the
    schema-init step right after this recreates it fresh. No-ops forever after the first
    deploy that runs this successfully. DROP COLUMN also drops the dependent GIN index
    (confirmed — no CASCADE needed); the concurrent-index step below rebuilds it, alongside
    the trigram index.

    Version history (see pg_schema.sql's own comments for the full reasoning on each):
      - pre-v2.16.35: no regexp_replace at all.
      - v2.16.35: hyphens normalized to spaces (fixes the hyphen-before-digit
        negative-number-sign tokenization bug, e.g. "ES-335" wrongly emitting lexeme '-335').
      - v2.16.36: slashes ALSO normalized to spaces (fixes a different Postgres parser quirk —
        "word/word" fuses into one compound lexeme instead of splitting, e.g. "Mesa/Boogie"
        never matching a plain "mesa" search).
      - v2.16.39: generalized to '[^[:alnum:]]+' -> ' ' (every run of non-alphanumerics),
        after the v2.16.38 production browse diff check found "word.word" ALSO fusing into
        one lexeme ("Dr.scientist"). Staleness marker is now the '[:alnum:]' literal —
        absent from every older expression. (The paragraph below describes the v2.16.36
        check, kept for history.)

    Staleness is detected by checking for the '/' literal in the stored expression string —
    present only once the current (v2.16.36+) expression has actually been applied. This
    correctly catches BOTH older versions (pre-v2.16.35 has no regexp_replace at all and no
    '/'; v2.16.35 has regexp_replace but still no '/' literal, since it only handles hyphens),
    while a naive 'regexp_replace' not in ... check (v2.16.35's own check) would have wrongly
    treated the v2.16.35 hyphen-only expression as already current."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('items')")
        if cur.fetchone()[0] is None:
            return  # fresh deploy, items table doesn't exist yet — nothing to migrate
        cur.execute("""
            SELECT pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attrdef d
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE d.adrelid = 'items'::regclass AND a.attname = 'search_vector'
        """)
        row = cur.fetchone()
        if row and row[0] and "[:alnum:]" not in row[0]:
            cur.execute("ALTER TABLE items DROP COLUMN search_vector")
            conn.commit()
            print("[pg] migrated search_vector to punctuation-normalized generated expression")

def _init_pg_schema():
    if not (_PSYCOPG2_AVAILABLE and PG_DATABASE_URL):
        return
    try:
        schema_sql = PG_SCHEMA_FILE.read_text()
    except OSError as e:
        print(f"[pg] schema init skipped — can't read {PG_SCHEMA_FILE.name}: {e}")
        return
    main_sql, marker_found, concurrent_sql = schema_sql.partition(PG_SCHEMA_CONCURRENT_MARKER)
    try:
        conn = psycopg2.connect(PG_DATABASE_URL, connect_timeout=10)
        try:
            _pg_migrate_search_vector_if_stale(conn)
            with conn.cursor() as cur:
                cur.execute(main_sql)
            conn.commit()
        finally:
            conn.close()
        print("[pg] items table ready")
    except Exception as e:
        # Never let a Postgres hiccup block startup — same tolerance as every
        # other best-effort persistence path in this app.
        print(f"[pg] schema init skipped: {type(e).__name__}: {e}")
        return  # don't attempt the concurrent-index step against a schema that may not have applied

    if not (marker_found and concurrent_sql.strip()):
        return
    # CREATE INDEX CONCURRENTLY statements, each its own cur.execute() call on a shared
    # autocommit connection — see _pg_concurrent_statements() and the marker comment in
    # pg_schema.sql. Best-effort and non-blocking like the step above: queries still run
    # (just slower) without these indexes, so a slow build or a hiccup here never
    # blocks startup. One statement failing (e.g. a leftover
    # INVALID index from a prior interrupted build) doesn't abort the rest.
    try:
        conn = psycopg2.connect(PG_DATABASE_URL, connect_timeout=10)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for stmt in _pg_concurrent_statements(concurrent_sql):
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        print(f"[pg] concurrent statement skipped: {type(e).__name__}: {e} — {stmt[:80]}")
        finally:
            conn.close()
        print("[pg] concurrent indexes ready")
    except Exception as e:
        print(f"[pg] concurrent index build skipped: {type(e).__name__}: {e}")

_init_pg_schema()

# ── Postgres connection pool (Phase C, v2.16.20) ──────────────────────────────
# Rare admin/startup Postgres paths (_init_pg_schema, the admin data export)
# open one ad-hoc psycopg2.connect() per call — fine for rare, background-thread-triggered
# work, but not for something hit on every /api/browse request once Phase D
# cuts over. See POSTGRES_MIGRATION_PLAN.md §5. Sized for this app's
# `--workers=1 --worker-class=gthread --threads=8` Procfile: up to 8 concurrent
# request threads + the scan thread + headroom for the admin-only paths above
# (which still open their own ad-hoc connections — not migrated to the pool,
# since they're rare and not on any hot path; migrating them is a follow-up,
# not required for Phase C). Created once at module load, torn down never
# (lives for the process lifetime).
_PG_POOL = None
if _PSYCOPG2_AVAILABLE and PG_DATABASE_URL:
    try:
        import psycopg2.pool
        _PG_POOL = psycopg2.pool.ThreadedConnectionPool(
            2, 12, PG_DATABASE_URL, connect_timeout=10)
        print("[pg] connection pool ready (minconn=2, maxconn=12)")
    except Exception as e:
        # Same tolerance as _init_pg_schema(): startup never blocks on it. Since
        # v2.16.44 (Phase F 4c) /api/browse has no JSON fallback, so with no
        # pool it answers 503 (see api_browse) until the next restart.
        print(f"[pg] connection pool init skipped: {type(e).__name__}: {e}")
        _PG_POOL = None

import contextlib as _contextlib

@_contextlib.contextmanager
def _pg_conn():
    """Yield a pooled Postgres connection, committing on a clean exit and
    rolling back + discarding the connection back to the pool on error —
    mirrors _user_db()'s `with`-based pattern for SQLite, just pooled since
    Postgres connections are more expensive to establish than SQLite's.

    (v2.16.44) When _PG_VALIDATE_CONN is set for the current request (only on
    /api/browse's one retry after a connection-class error — see
    _pg_is_conn_error), each connection is pinged with SELECT 1 first and any
    dead one is closed and replaced. A Postgres restart leaves every idle
    pooled connection dead; without this a retry could just draw the next
    dead one. Normal requests never pay for the ping."""
    if _PG_POOL is None:
        raise RuntimeError("Postgres connection pool not available")
    _tc = time.perf_counter()
    conn = _PG_POOL.getconn()
    _timing_phase("conn", (time.perf_counter() - _tc) * 1000.0)
    if _PG_VALIDATE_CONN.get():
        for _ in range(_PG_POOL.maxconn + 1):
            try:
                with conn.cursor() as _c:
                    _c.execute("SELECT 1")
                conn.rollback()
                break
            except Exception:
                _PG_POOL.putconn(conn, close=True)
                conn = _PG_POOL.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        # A dead connection can't roll back either; don't let that second
        # error mask the real one. The pool drops closed connections itself.
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _PG_POOL.putconn(conn)


import contextvars as _contextvars
_PG_VALIDATE_CONN = _contextvars.ContextVar("_PG_VALIDATE_CONN", default=False)


def _pg_is_conn_error(exc):
    """True for errors meaning 'this pooled connection was dead' (Postgres
    restarted, network blip, idle timeout) rather than a bad query — the
    only kind worth one retry."""
    if not _PSYCOPG2_AVAILABLE:
        return False
    import psycopg2 as _pg2
    return isinstance(exc, (_pg2.OperationalError, _pg2.InterfaceError))

def _pg_read(fn):
    """Run a read-only Postgres callable, retrying ONCE with connection
    validation if it failed on a dead pooled connection (same policy as
    /api/browse since v2.16.44 — see _pg_conn / _pg_is_conn_error). Real
    query errors propagate unchanged. (v2.16.46, Phase F step 5a)"""
    try:
        return fn()
    except Exception as e:
        if not _pg_is_conn_error(e):
            raise
        _tok = _PG_VALIDATE_CONN.set(True)
        try:
            return fn()
        finally:
            _PG_VALIDATE_CONN.reset(_tok)


# ── Postgres scan write path (Phase F step 5b-ii, v2.16.48) ───────────────────
# Postgres is the scan's source of truth. _run() reads each found SKU's prior
# state from Postgres (_pg_scan_prior_fetch), merges with _merge_scan_item(),
# then writes the merged rows AND the sold-marking in ONE transaction
# (_pg_write_scan) BEFORE sending "done" — so a client's post-scan /api/browse
# always sees this scan's results, and the next scan's prior read always sees
# them too. A write failure is a failed scan (error to the user, nothing else
# saved, the user's NEW anchor not advanced) — never silent.
#
# History: Phase B (v2.16.15) added _pg_sync_scan, a best-effort background
# mirror of the JSON path; v2.16.47 (5b-i) shadow-compared a Postgres-prior
# merge against the JSON path on real scans (clean) before this cutover; until
# v2.17.0 (5c) a write-only JSON backup was still written after each scan.
_PG_UPSERT_COLS = ["sku", "name", "brand", "category", "subcategory", "condition",
                   "condition_note", "price", "list_price", "has_price_drop", "price_drop",
                   "price_drop_since", "store", "location", "url", "image_id", "is_vintage",
                   "available", "date_listed", "first_seen", "store_inferred"]
# (store_inferred added v2.16.50.)
_PG_UPSERT_SQL = (
    f"INSERT INTO items ({', '.join(_PG_UPSERT_COLS)}) VALUES %s "
    f"ON CONFLICT (sku) DO UPDATE SET "
    f"{', '.join(f'{c}=EXCLUDED.{c}' for c in _PG_UPSERT_COLS if c != 'sku')}"
)

def _pg_row_for(sku: str, it: dict) -> tuple:
    return (
        sku, it.get("name", "") or "", it.get("brand", "") or "", it.get("category", "") or "",
        it.get("subcategory", "") or "", it.get("condition", "") or "",
        it.get("condition_note", "") or "", it.get("price", 0) or 0, it.get("list_price", 0) or 0,
        bool(it.get("has_price_drop", False)), it.get("price_drop", 0) or 0,
        it.get("price_drop_since", "") or "", it.get("store", "") or "",
        it.get("location", "") or it.get("store", "") or "", it.get("url", "") or "",
        it.get("image_id", "") or "", bool(it.get("is_vintage", False)),
        it.get("available", True), it.get("date_listed", "") or "", it.get("first_seen", "") or "",
        bool(it.get("store_inferred", False)),
    )


# The per-item merge (moved verbatim out of _run() in v2.16.47). `cached` is the
# prior record for this SKU ({} if never seen); only .get() is used on it, so a
# dict built from a Postgres row (_PG_PRIOR_COLS) works. Note: Postgres can't
# tell a missing first_seen/price_drop_since from '' — a prior row with '' keeps
# '' (the JSON path gave a missing key run_time). '' first_seen means "always
# visible" in browse, so this never hides an item.
def _merge_scan_item(p: dict, cached: dict, run_time: str) -> dict:
    cat       = p.get("category") or cached.get("category", "")
    subcat    = p.get("subcategory") or cached.get("subcategory", "")
    condition = p.get("condition") or cached.get("condition", "")
    brand     = p.get("brand") or cached.get("brand", "")
    location  = p.get("location") or cached.get("location", p.get("store", ""))
    # Price drop detection — use Algolia's native priceDrop flag + listPrice field
    new_price      = p.get("price") or 0
    new_list_price = p.get("list_price") or 0
    has_price_drop = bool(p.get("has_price_drop", False))
    price_drop_amt = round(new_list_price - new_price, 2) if (has_price_drop and new_list_price > new_price) else 0
    # Track when we FIRST detected this drop (preserves timestamp across scans)
    prev_had_drop = bool(cached.get("has_price_drop", False))
    if has_price_drop and not prev_had_drop:
        price_drop_since = run_time          # newly dropped this scan
    elif has_price_drop:
        price_drop_since = cached.get("price_drop_since", run_time)  # preserve
    else:
        price_drop_since = ""                # no longer dropped
    return {
        "category":          cat,
        "subcategory":       subcat,
        "condition":         condition,
        "brand":             brand,
        "name":              p.get("name", ""),
        "url":               p.get("url", ""),
        "store":             p.get("store", ""),
        "location":          location,
        "price":             new_price,
        "list_price":        new_list_price,
        "has_price_drop":    has_price_drop,
        "price_drop":        price_drop_amt,
        "price_drop_since":  price_drop_since,
        "available":         True,
        "date_listed":       p.get("date_listed") or cached.get("date_listed", ""),
        "image_id":          p.get("image_id") or cached.get("image_id", ""),
        # GC vintage flag (premiumGear=="Vintage"), captured at scan time
        "is_vintage":        bool(p.get("is_vintage", cached.get("is_vintage", False))),
        # Staff-written "Condition & Details" note extracted from longDescription
        # (v2.16.6) — often empty, that's expected (not every item has one).
        "condition_note":    p.get("condition_note") or cached.get("condition_note", ""),
        # first_seen: when our system first encountered this item
        "first_seen":        cached.get("first_seen", run_time),
        # v2.16.50: store came from the location, not Algolia's stores array
        # (see _fill_missing_stores). Recomputed every scan, so it clears by
        # itself once Algolia lists the store again.
        "store_inferred":    bool(p.get("store_inferred", False)),
    }


# Prior state = the whole stored row (every upsert column except sku). The merge
# only uses some of them; all of them are needed to tell whether a found item
# changed at all (v2.16.49 — unchanged rows are not rewritten, see
# _pg_row_unchanged).
_PG_PRIOR_COLS = _PG_UPSERT_COLS[1:]
_PG_NUMERIC_COLS = {"price", "list_price", "price_drop"}   # NUMERIC(10,2) in Postgres
_PG_BOOL_COLS = {"has_price_drop", "is_vintage", "available", "store_inferred"}


def _pg_row_unchanged(row: tuple, prior: tuple) -> bool:
    """True if `row` (a _pg_row_for tuple, sku first) would store exactly what
    `prior` (the stored row in _PG_PRIOR_COLS order) already holds, so the
    upsert can be skipped. Numeric columns compare at cent precision (the
    column is NUMERIC(10,2), so e.g. 199.999 is stored as 200.00 and a later
    scan sending 199.999 again counts as unchanged); booleans by truth value;
    everything else exactly. (v2.16.49)"""
    for i, col in enumerate(_PG_PRIOR_COLS, start=1):
        a, b = row[i], prior[i - 1]
        if col in _PG_NUMERIC_COLS:
            try:
                if abs(round(float(a or 0), 2) - float(b or 0)) > 0.001:
                    return False
            except (TypeError, ValueError):
                return False
        elif col in _PG_BOOL_COLS:
            if bool(a) != bool(b):
                return False
        elif (a or "") != (b or ""):
            return False
    return True

# Serializes the prior-read → merge → write section of scans. The scan _lock
# normally does this, but /api/stop's 5s force-unlock watchdog can release
# _lock while a stopped scan is still writing; without this a new scan could
# read prior state before that write commits.
_PG_SCAN_DB_LOCK = threading.Lock()
_PG_SCAN_DB_LOCK_TIMEOUT = 180

_PG_SCAN_WRITE_ATTEMPTS = 3
_PG_SCAN_WRITE_BACKOFF = (2, 5)   # seconds before attempts 2 and 3
# Per-process counters (reset on deploy). Admin: POST /api/browse?pg_shadow=1
# -> `_pg_scan_writes`. `failed` = scans that reported an error to the user
# because Postgres couldn't be read or written; `retried` = attempts repeated
# after a connection error (a retry that then succeeded counts only here).
_PG_SCAN_WRITES = {"ok": 0, "failed": 0, "retried": 0, "since": None,
                   "last_ok_at": None, "last_ms": None, "last_rows": 0, "last_sold": 0,
                   "last_changed": 0, "last_read_ms": None, "last_write_ms": None,
                   "last_error": "", "last_error_at": None}


# Nationwide scan coverage (v2.16.51). A nationwide scan must account for
# Algolia's own nbHits (within this tolerance, < one 240-hit page — covers items
# listed/sold during the ~40s scan) or it's treated as incomplete: no sold-marking,
# NEW anchor not advanced. Suspect pages (empty, short, unparsed hits, or adding
# no new items) are re-fetched first, one at a time. Admin: POST
# /api/browse?pg_shadow=1 -> `_scan_coverage` (last scan's detail + recent 20).
_NATIONWIDE_COVERAGE_TOLERANCE = 120
_NATIONWIDE_PAGE_RETRY_ROUNDS = 2
_NATIONWIDE_PAGE_RETRY_MAX = 40
_SCAN_COVERAGE = {"last": None, "recent": []}


def _pg_scan_note(kind, **kw):
    st = _PG_SCAN_WRITES
    if st["since"] is None:
        st["since"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    st[kind] += 1
    st.update(kw)


# (v2.17.3, Phase G S3) Every writer of the items table bumps this BEFORE it
# writes (under _PG_SCAN_DB_LOCK). A scan's prior-state prefetch records the
# generation when it starts; if the number differs when the scan is ready to
# merge, something wrote in between and the prefetch is discarded.
_PG_CATALOG_GEN = [0]
_PG_CATALOG_GEN_LOCK = threading.Lock()


def _pg_catalog_gen_bump():
    with _PG_CATALOG_GEN_LOCK:
        _PG_CATALOG_GEN[0] += 1


def _pg_scan_prior_prefetch(stores=None):
    """(v2.17.3) Prior state of every AVAILABLE item (in `stores` if given), read
    while the scan is still fetching from Algolia: {sku: tuple in _PG_PRIOR_COLS
    order} — the same row shape _pg_scan_prior_fetch returns. The scan later
    reads only the found SKUs missing from this (new or reappearing items)."""
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                sql = (f"SELECT sku, {', '.join(_PG_PRIOR_COLS)} FROM items WHERE available")
                if stores is not None:
                    cur.execute(sql + " AND store = ANY(%s)", (list(stores),))
                else:
                    cur.execute(sql)
                return {r[0]: r[1:] for r in cur.fetchall()}
    return _pg_read(_q)


class _PriorPrefetch:
    """Runs _pg_scan_prior_prefetch in a background thread. result() returns
    the dict, or None if it failed, timed out, or the catalog was written
    after it started (generation changed) — the caller then does the normal
    full read. Measurement: .ms (read time), .note (why it wasn't used)."""
    def __init__(self, stores=None):
        with _PG_CATALOG_GEN_LOCK:
            self.gen = _PG_CATALOG_GEN[0]
        self.data, self.ms, self.note = None, None, ""
        self._t = threading.Thread(target=self._go, args=(stores,), daemon=True)
        self._t.start()

    def _go(self, stores):
        t0 = time.time()
        try:
            self.data = _pg_scan_prior_prefetch(stores)
        except Exception as e:
            self.note = f"prefetch failed: {type(e).__name__}: {e}"[:200]
        self.ms = int((time.time() - t0) * 1000)

    def result(self, timeout=60):
        """Call while holding _PG_SCAN_DB_LOCK (so no writer can slip in after the check)."""
        self._t.join(timeout)
        if self._t.is_alive():
            self.note = "prefetch still running"
            return None
        if self.data is None:
            return None
        with _PG_CATALOG_GEN_LOCK:
            if _PG_CATALOG_GEN[0] != self.gen:
                self.note = "catalog written since prefetch started"
                self.data = None
                return None
        return self.data


def _pg_scan_prior_fetch(skus):
    """Prior state for `skus` from Postgres: {sku: tuple in _PG_PRIOR_COLS order}.
    One read-only pooled transaction (temp table + join), retried once on a
    dead pooled connection via _pg_read. Raises on failure."""
    if not skus:
        return {}
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE _prior_skus (sku TEXT PRIMARY KEY) ON COMMIT DROP")
                psycopg2.extras.execute_values(
                    cur, "INSERT INTO _prior_skus (sku) VALUES %s ON CONFLICT DO NOTHING",
                    [(s,) for s in skus], page_size=5000)
                cur.execute(
                    f"SELECT i.sku, {', '.join('i.' + c for c in _PG_PRIOR_COLS)} "
                    f"FROM items i JOIN _prior_skus s ON s.sku = i.sku")
                return {r[0]: r[1:] for r in cur.fetchall()}
    return _pg_read(_q)


def _pg_write_scan(rows: list, run_ids: set, sold_scope, send=None) -> set:
    """Write one scan to Postgres in ONE transaction: upsert `rows` (the
    _pg_row_for tuples that are new or changed — v2.16.49; unchanged rows are
    skipped by the caller), then (if sold_scope is not None) mark unavailable every available item in
    scope that this run didn't see, returning those SKUs. sold_scope: None
    (no sold-marking: stopped / incomplete nationwide scan), "nationwide", or a
    list of fully-scanned store names (empty list = nothing to mark).
    Idempotent, so a connection error retries the whole transaction (up to
    _PG_SCAN_WRITE_ATTEMPTS, validating pooled connections on retry); any
    other error, or the last attempt's, raises."""
    exclude = set(run_ids)   # every SKU this run saw (changed or not) — never marked sold
    last_exc = None
    _pg_catalog_gen_bump()   # v2.17.3: invalidates any prior-state prefetch in flight
    for attempt in range(_PG_SCAN_WRITE_ATTEMPTS):
        _tok = _PG_VALIDATE_CONN.set(attempt > 0)
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    if rows:
                        psycopg2.extras.execute_values(cur, _PG_UPSERT_SQL, rows, page_size=2000)
                    sold = set()
                    if sold_scope == "nationwide" or sold_scope:
                        cur.execute("CREATE TEMP TABLE _run_skus (sku TEXT PRIMARY KEY) ON COMMIT DROP")
                        if exclude:
                            psycopg2.extras.execute_values(
                                cur, "INSERT INTO _run_skus (sku) VALUES %s",
                                [(s,) for s in exclude], page_size=5000)
                        if sold_scope == "nationwide":
                            cur.execute("""
                                UPDATE items SET available=false
                                WHERE available
                                AND NOT EXISTS (SELECT 1 FROM _run_skus WHERE _run_skus.sku = items.sku)
                                RETURNING sku
                            """)
                        else:
                            cur.execute("""
                                UPDATE items SET available=false
                                WHERE available
                                AND NOT EXISTS (SELECT 1 FROM _run_skus WHERE _run_skus.sku = items.sku)
                                AND store = ANY(%s)
                                AND NOT store_inferred
                                RETURNING sku
                            """, (list(sold_scope),))
                        sold = {r[0] for r in cur.fetchall()}
            _pg_catalog_gen_bump()   # v2.17.8: after commit too (browse aggregate cache)
            return sold
        except Exception as e:
            last_exc = e
            if not _pg_is_conn_error(e) or attempt == _PG_SCAN_WRITE_ATTEMPTS - 1:
                _pg_catalog_gen_bump()
                raise
            _pg_scan_note("retried", last_error=f"{type(e).__name__}: {e}"[:300],
                          last_error_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
            print(f"[pg] scan write attempt {attempt + 1} failed ({type(e).__name__}: {e}) — retrying")
            if send:
                send({"type": "progress", "msg": "  database hiccup while saving — retrying…"})
            time.sleep(_PG_SCAN_WRITE_BACKOFF[min(attempt, len(_PG_SCAN_WRITE_BACKOFF) - 1)])
        finally:
            _PG_VALIDATE_CONN.reset(_tok)
    raise last_exc  # unreachable


# ── Store list ────────────────────────────────────────────────────────────────

FALLBACK_STORES: list[str] = []  # Populated from GC live data via Validate Stores


def get_store_list() -> list[str]:
    cached = []
    if STORES_CACHE.exists():
        try:
            cached = json.loads(STORES_CACHE.read_text()).get("stores", [])
        except Exception:
            pass
    blocklist = _get_blocklist()
    return sorted(set(cached) - blocklist)


_US_STATES = [
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc",
]

def _fetch_state_stores(state: str) -> list[str]:
    """Fetch store city names for a single state from stores.guitarcenter.com.
    Only extracts names from confirmed store-page URLs matching /{state}/{city}/{id},
    which is the only reliable signal — headings and link text pick up nav garbage."""
    return [name for name, _ in _fetch_state_stores_with_state(state)]


def _fetch_state_stores_with_state(state: str) -> list[tuple]:
    """Like _fetch_state_stores but returns (name, state_abbr) tuples."""
    try:
        r = _http.get(f"https://stores.guitarcenter.com/{state}/", timeout=10)
        if r.status_code != 200:
            return []
        html = r.text
        # Only trust URLs in the form /state/city-slug/numeric-id
        slug_to_name = {}
        for slug in re.findall(
            rf'href="/{re.escape(state)}/([a-z][a-z0-9\-]+)/(\d+)(?:/[^"]*)?"',
            html
        ):
            city_slug, store_id = slug
            name = " ".join(w.capitalize() for w in city_slug.split("-"))
            slug_to_name[city_slug] = name
        return [(name, state.upper()) for name in slug_to_name.values()]
    except Exception:
        return []


def _build_store_coords(send_progress=None, force: bool = False):
    """Geocode all known stores using Algolia's 'storeName' field as the query.

    For each store, we first pull ONE hit from Algolia to discover the human-readable
    storeName (e.g. 'South Austin, TX' — a real neighborhood Nominatim recognises),
    then geocode that string directly via Nominatim. This is vastly more reliable than
    querying 'Guitar Center {store}' because GC stores aren't POIs in Nominatim's DB,
    but their storeName strings are real geographic places.

    Stores with zero live used inventory (e.g. closed stores still in the cache) can't
    be resolved via Algolia and are reported as 'no items'. They fall back to a
    last-ditch '{store}, {state}' Nominatim query using state context from GC's state
    pages, so permanently-closed stores still have SOME chance of getting coords.

    If force=True, re-geocodes all stores even if already in the coords file.
    Writes gc_store_coords.json. Returns {store: {lat, lng, source}}.
    """
    def _send(msg):
        if send_progress:
            send_progress(msg)

    known_stores = get_store_list()
    total = len(known_stores)
    _send(f"Step 1: Found {total} stores in cache.")

    # Load existing coords so we can skip already-geocoded stores (unless force=True).
    existing: dict = {}
    if STORE_COORDS_FILE.exists():
        try:
            existing = json.loads(STORE_COORDS_FILE.read_text())
        except Exception:
            pass
    coords: dict = dict(existing) if not force else {}

    # Load pre-seeded coords from CSV-derived seed file (committed alongside the app).
    # These use real ZIP-code centroids so they're more accurate than city geocoding.
    # Seed entries are used as-is and skip the Algolia+Nominatim pipeline entirely.
    seed_file = Path(__file__).parent / "gc_store_coords_seed.json"
    if seed_file.exists():
        try:
            seed = json.loads(seed_file.read_text())
            loaded = 0
            for store, data in seed.items():
                if force or store not in coords:
                    coords[store] = data
                    loaded += 1
            _send(f"  Loaded {loaded} pre-seeded coords from gc_store_coords_seed.json.")
        except Exception as e:
            _send(f"  Warning: could not load seed file: {e}")

    # Collect state context as a last-ditch fallback for dead stores
    # (stores with no Algolia items — can't determine storeName from the API).
    _send("Step 2: Scraping state pages for fallback state context…")
    name_to_state: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_state_stores_with_state, st): st for st in _US_STATES}
        for future in as_completed(futures):
            try:
                for name, state in future.result():
                    if name not in name_to_state:
                        name_to_state[name] = state
            except Exception:
                pass

    # Step 3: Hit Algolia once per store to get storeName.
    # Algolia has no rate limit for this volume — ~235 req completes in ~30 sec.
    _send("Step 3: Fetching storeName from Algolia for each active store…")
    store_to_location: dict[str, str] = {}
    no_items: list[str] = []
    # Skip stores already resolved (existing file OR seed). Seed always wins —
    # it comes from real street addresses so we never need to re-geocode it.
    todo = [s for s in known_stores if s not in coords]
    for i, store in enumerate(todo, 1):
        try:
            data = fetch_page(store, 1)
            hits = data.get("results", [{}])[0].get("hits", [])
            if not hits:
                no_items.append(store)
                continue
            sn = (hits[0].get("storeName") or "").strip()
            if sn:
                store_to_location[store] = sn
            else:
                no_items.append(store)
        except Exception as e:
            _send(f"  Algolia error for {store}: {type(e).__name__}")
            no_items.append(store)
        if i % 50 == 0:
            _send(f"  [{i}/{len(todo)}] {len(store_to_location)} storeNames, {len(no_items)} no-items so far…")

    _send(f"  Got storeName for {len(store_to_location)} stores; {len(no_items)} had no items.")
    if no_items:
        sample = ", ".join(no_items[:15])
        more = f" (+{len(no_items)-15} more)" if len(no_items) > 15 else ""
        _send(f"  No-items stores (likely closed): {sample}{more}")

    # Step 4: Geocode each storeName via Nominatim (1 req/sec per ToS).
    # Fresh session with clean API headers — NOT the shared _http session which
    # carries browser-impersonation headers that Nominatim rejects. (v2.1.3 fix)
    nom_session = http.Session()
    nom_session.headers.update({
        "User-Agent": "GCTracker/2.2 (personal tool; non-commercial)",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })

    def _nom(query: str):
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?q={http.utils.quote(query)}&format=json&limit=1&countrycodes=us"
        )
        r = nom_session.get(url, timeout=10)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        return (data[0] if data else None), None

    _send(f"Step 4: Geocoding {len(store_to_location)} storeNames via Nominatim (1/sec)…")
    failed: list[str] = []
    succeeded = 0

    # Primary pass: stores with storeName from Algolia.
    loc_items = sorted(store_to_location.items())
    for i, (store, location) in enumerate(loc_items, 1):
        try:
            result, err = _nom(location)
            if not result:
                # Fallback: strip "at <venue>" suffix from storeName.
                # e.g. "Yonkers at Ridge Hill, NY" → "Yonkers, NY"
                # Handles GC stores named after the shopping center they're in.
                stripped = re.sub(r'\s+at\s+[^,]+', '', location, flags=re.IGNORECASE).strip()
                if stripped != location:
                    time.sleep(1.0)
                    result, err = _nom(stripped)
                    if result:
                        location = stripped  # record the simpler query as source
            if result:
                coords[store] = {
                    "lat": float(result["lat"]),
                    "lng": float(result["lon"]),
                    "source": location,
                }
                succeeded += 1
            else:
                failed.append(f"{store} (storeName={store_to_location[store]}{', '+err if err else ''})")
        except Exception as e:
            failed.append(f"{store} ({type(e).__name__})")

        if i % 25 == 0:
            _send(f"  [{i}/{len(loc_items)}] {succeeded} geocoded so far…")
            STORE_COORDS_FILE.write_text(json.dumps(coords, indent=2))

        time.sleep(1.0)

    # Last-ditch pass: no-items stores, try "{store}, {state}" with state context.
    # These are likely closed, but we give them one shot so the coords file is complete.
    dead_attempted = 0
    dead_ok = 0
    for store in no_items:
        state = name_to_state.get(store, "")
        if not state:
            continue  # nothing we can do without state
        dead_attempted += 1
        query = f"{store}, {state}"
        try:
            result, _err = _nom(query)
            if result:
                coords[store] = {
                    "lat": float(result["lat"]),
                    "lng": float(result["lon"]),
                    "source": f"fallback-no-items: {query}",
                }
                dead_ok += 1
        except Exception:
            pass
        time.sleep(1.0)

    if dead_attempted:
        _send(f"  Last-ditch no-items pass: {dead_ok}/{dead_attempted} resolved via state context.")

    STORE_COORDS_FILE.write_text(json.dumps(coords, indent=2))
    skipped = total - len(todo)
    _send(f"\n✓ Done — {succeeded + dead_ok} newly geocoded, {skipped} skipped (cached), "
          f"{len(no_items)} no-items ({dead_ok} recovered), {len(failed)} failed. "
          f"Total coords: {len(coords)}/{total}.")
    if failed:
        _send(f"  Failed: {', '.join(failed[:20])}{'…' if len(failed)>20 else ''}")
    return coords


def _extract_stores_from_used_page(html: str) -> list[str]:
    """Extract all store names from GC's used inventory page filter facets.
    The __NEXT_DATA__ blob or page HTML contains the complete list of valid store
    names exactly as the filters=stores: parameter expects them."""
    stores = []

    # Strategy 1: __NEXT_DATA__ — find facet values for the 'stores' facet
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            nd = json.loads(m.group(1))
            # Walk looking for arrays of facet values near a 'stores' key
            def find_store_facets(obj, depth=0):
                if depth > 12: return
                if isinstance(obj, dict):
                    # Look for facet arrays keyed by 'stores' or containing store-like values
                    for k, v in obj.items():
                        if k.lower() in ('stores', 'store') and isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    val = item.get('displayValue') or item.get('value') or item.get('name') or ''
                                elif isinstance(item, str):
                                    val = item
                                else:
                                    continue
                                if isinstance(val, str) and 2 < len(val) < 60:
                                    stores.append(val)
                        elif isinstance(v, (dict, list)):
                            find_store_facets(v, depth + 1)
                elif isinstance(obj, list):
                    for item in obj:
                        find_store_facets(item, depth + 1)
            find_store_facets(nd)
        except Exception:
            pass

    # Strategy 2: look for displayValue patterns near "stores" in the raw JSON
    if len(stores) < 10:
        # Find JSON arrays that look like store facets
        for m2 in re.finditer(r'"(?:stores|store)"[^[]*(\[[^\]]{100,}\])', html, re.DOTALL):
            try:
                arr = json.loads(m2.group(1))
                for item in arr:
                    if isinstance(item, dict):
                        val = item.get('displayValue') or item.get('value') or ''
                        if isinstance(val, str) and 2 < len(val) < 60:
                            stores.append(val)
            except Exception:
                pass

    return stores


def refresh_store_list(send_progress=None) -> list[str]:
    """Fetch authoritative store list from GC's used inventory page filter facets,
    then fall back to state-by-state scraping. Removes blocklisted stores."""
    live_names = []

    # Strategy 1: fetch GC's used inventory page and extract store names from filter facets
    # This is the gold standard — these are the exact names the filter system accepts
    try:
        r = _http.get("https://www.guitarcenter.com/Used/", timeout=20)
        if r.status_code == 200:
            live_names = _extract_stores_from_used_page(r.text)
    except Exception:
        pass

    # Strategy 2: scrape stores.guitarcenter.com state by state in parallel
    if len(live_names) < 50:
        try:
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_fetch_state_stores, st): st for st in _US_STATES}
                for future in as_completed(futures):
                    try:
                        live_names.extend(future.result())
                    except Exception:
                        pass
        except Exception:
            pass

    # Strategy 3: main stores page URL pattern
    if len(live_names) < 20:
        try:
            r = _http.get("https://www.guitarcenter.com/Stores/", timeout=15)
            r.raise_for_status()
            html = r.text
            for slug in re.findall(r'href="https?://stores\.guitarcenter\.com/([a-z]{2})/([a-z][a-z0-9\-]+)/(\d+)"', html):
                _, city_slug, _ = slug
                name = " ".join(w.capitalize() for w in city_slug.split("-"))
                live_names.append(name)
        except Exception:
            pass

    # Strip nav garbage
    _NAV_GARBAGE = {
        "find your local guitar center store", "my account", "sign in", "track order",
        "returns", "faqs", "store locator", "guitar center lessons", "guitar center",
        "home", "shop all", "new arrivals", "top sellers", "on sale", "price drop",
        "used", "vintage", "sell your gear", "financing", "outlet", "deals",
        "daily pick", "gc pro", "lessons", "repairs", "rentals", "riffs blog",
        "accessibility statement", "privacy policy", "terms of use", "site map",
        "careers", "about", "contact us", "press room", "service", "support",
        "all rights reserved", "california transparency", "do not sell",
    }
    live_names = [
        n for n in live_names
        if n.strip().lower() not in _NAV_GARBAGE
        and len(n.strip()) >= 3
        and not any(bad in n.lower() for bad in ("guitar center", "my account", "sign in",
                                                   "track order", "©", "all rights"))
    ]

    blocklist = _get_blocklist()
    merged = sorted(set(live_names) - blocklist)
    STORES_CACHE.write_text(json.dumps({
        "stores":      merged,
        "live_count":  len(set(live_names)),
        "updated":     datetime.now().isoformat(),
    }))
    return merged


def get_store_info() -> dict:
    """Return metadata about the store list (count, last updated, live vs fallback)."""
    if STORES_CACHE.exists():
        try:
            d = json.loads(STORES_CACHE.read_text())
            return {
                "count":      len(d.get("stores", [])),
                "live_count": d.get("live_count", 0),
                "updated":    d.get("updated", ""),
            }
        except Exception:
            pass
    return {"count": len(FALLBACK_STORES), "live_count": 0, "updated": ""}


# ── Favorites ─────────────────────────────────────────────────────────────────
# (load_favorites/save_favorites and load_keywords/save_keywords removed in v2.14.5
#  along with their dead API routes — 2026-07 audit E3. The FAVORITES_FILE /
#  KEYWORDS_FILE constants stay: admin export/import/reset reference the paths.)

# ── GC scraping ───────────────────────────────────────────────────────────────

PAGE_SIZE = 240

def _fmt_date(d: str) -> str:
    """Convert YYYY-MM-DD to M/D/YY."""
    try:
        from datetime import date
        dt = date.fromisoformat(d[:10])
        return f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        return d



def _clean_name(name: str) -> str:
    """Strip redundant 'Used ' prefix from item names."""
    name = name.strip()
    if name.lower().startswith("used "):
        name = name[5:].strip()
    return name


ALGOLIA_APP_ID  = os.environ.get("ALGOLIA_APP_ID", "")
ALGOLIA_API_KEY = os.environ.get("ALGOLIA_API_KEY", "")
ALGOLIA_INDEX   = "cD-guitarcenter"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "x-algolia-application-id": ALGOLIA_APP_ID,
    "x-algolia-api-key":        ALGOLIA_API_KEY,
    "Content-Type":             "application/json",
}

# (v2.17.3, Phase G S2) Every hit attribute parse_products() reads. A "lean"
# request asks Algolia for only these — no facet counts, no highlight/snippet
# blocks, no extra response fields. Live v2.17.1 pages were 630 KB each (295 MB
# per nationwide scan) with everything requested. If parse_products ever reads
# a new attribute it MUST be added here — the nationwide scan's page-1 check
# (full vs lean, parsed results must match) falls back to full pages if not.
_SCAN_HIT_ATTRS = [
    "objectID", "displayName", "name", "price", "listPrice", "longDescription",
    "priceDrop", "seoUrl", "brand", "condition", "categories", "categoriesSlug",
    "startDate", "creationDate", "storeName", "stores", "imageId", "premiumGear",
]


def fetch_page(store_name: str = None, page: int = 1, _stats: list | None = None,
               lean: bool = False, since_ts: int | None = None) -> dict:
    """Fetch one page of used inventory via Algolia API.
    If store_name is provided, filters to that store.
    If store_name is None, fetches ALL used inventory nationwide.
    lean=True (v2.17.3): request only _SCAN_HIT_ATTRS and no facets — same
    hits, same order (same filters / ruleContexts), much smaller response.
    since_ts (v2.17.4, quick pass): only items listed at/after since_ts. (v2.17.5)
    That's `startDate >= since OR creationDate >= since*1000`: live 2026-09-30 GC's
    recent listings have startDate 0 and parse_products dates them from
    creationDate (ms) instead — a startDate-only window returned nothing. The OR
    is a superset of every item whose parsed date_listed >= since."""
    import time as _time
    ts = int(_time.time())
    facet_filters = [
        "categoryPageIds:Used",
        "condition.lvl0:Used",
    ]
    if store_name:
        facet_filters.append([f"stores:{store_name}"])
    payload = {"requests": [{
        "indexName":     ALGOLIA_INDEX,
        "analyticsTags": ["Did Not Search"],
        "facetFilters":  facet_filters,
        "facets":        ["*"],
        "hitsPerPage":   240,
        "maxValuesPerFacet": 10,
        "numericFilters": [f"startDate<={ts}"],
        "page":          page - 1,
        "query":         "",
        "ruleContexts":  ["used-page", "primary_itemtype", "extension_itemtype"],
        "attributesToRetrieve": ["*"],
    }]}
    if since_ts is not None:
        payload["requests"][0]["numericFilters"].append(
            [f"startDate>={int(since_ts)}", f"creationDate>={int(since_ts) * 1000}"])
    if lean:
        req = payload["requests"][0]
        req.pop("facets", None)
        req.pop("maxValuesPerFacet", None)
        req["attributesToRetrieve"] = list(_SCAN_HIT_ATTRS)
        req["attributesToHighlight"] = []
        req["attributesToSnippet"] = []
        req["responseFields"] = ["hits", "nbHits", "nbPages", "page"]
    _t0 = time.perf_counter()
    r = _http.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    _t1 = time.perf_counter()
    out = r.json()
    if _stats is not None:   # Phase G timing (v2.17.1): (request ms, bytes, json-decode ms)
        _stats.append(((_t1 - _t0) * 1000.0, len(r.content), (time.perf_counter() - _t1) * 1000.0))
    return out


# ── New Deals helpers ──────────────────────────────────────────────────────────
def _lean_page1_ok(full_data, lean_box) -> tuple[bool, str]:
    """(v2.17.3) Decide whether this nationwide scan may use lean pages: page 1
    fetched both ways must report the same nbHits/nbPages and parse to identical
    items for every SKU present in both (a few SKUs may differ if a listing
    changed between the two requests — at least 90% must overlap). Any doubt →
    (False, reason) and the scan uses full pages exactly as before."""
    try:
        if not full_data:
            return False, "no full page 1"
        if "err" in lean_box or "d" not in lean_box:
            return False, "lean page 1 failed: " + lean_box.get("err", "timeout")
        rf = (full_data.get("results") or [{}])[0]
        rl = (lean_box["d"].get("results") or [{}])[0]
        if (rf.get("nbHits"), rf.get("nbPages")) != (rl.get("nbHits"), rl.get("nbPages")):
            return False, f"nbHits/nbPages differ ({rf.get('nbHits')}/{rf.get('nbPages')} vs {rl.get('nbHits')}/{rl.get('nbPages')})"
        pf = {p["id"]: p for p in parse_products(full_data, None)}
        pl = {p["id"]: p for p in parse_products(lean_box["d"], None)}
        if not pf:
            if not rf.get("nbHits"):
                return True, "empty (quick window with no listings)"   # v2.17.5: nothing to compare
            return False, "full page 1 parsed to 0 items"
        common = pf.keys() & pl.keys()
        if len(common) < 0.9 * len(pf):
            return False, f"only {len(common)} of {len(pf)} page-1 items in both"
        for sku in common:
            if pf[sku] != pl[sku]:
                diff = sorted(k for k in set(pf[sku]) | set(pl[sku]) if pf[sku].get(k) != pl[sku].get(k))
                return False, f"parsed item {sku} differs in {diff[:6]}"
        return True, f"page 1 identical ({len(common)} items)"
    except Exception as e:
        return False, f"check failed: {type(e).__name__}: {e}"[:200]


_new_deals_cache: dict | None = None

_SOFTWARE_KEYWORDS = {
    "software", "plug-in", "plug in", "plugin", "virtual instrument",
    "digital download", "pro audio software", "ilok", "(download)",
    "sample pack", "sample library", "expansion pack", "loop library",
}

def _is_software_item(name: str, category: str) -> bool:
    """Return True if the item appears to be software/a plugin (by name or category)."""
    text = ((name or "") + " " + (category or "")).lower()
    return any(kw in text for kw in _SOFTWARE_KEYWORDS)

def _fetch_new_page(page: int):
    """Fetch one page of new GC inventory from Algolia. Returns (hits, nb_pages)."""
    import time as _time
    ts = int(_time.time())
    payload = {"requests": [{
        "indexName":     ALGOLIA_INDEX,
        "analyticsTags": ["Did Not Search"],
        "facetFilters":  ["condition.lvl0:New"],
        "facets":        ["*"],
        "hitsPerPage":   240,
        "numericFilters": [f"startDate<={ts}"],
        "page":          page,
    }]}
    r = _http.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    res = r.json()["results"][0]
    return res["hits"], res.get("nbPages", 1)

def _load_new_deals_cache() -> dict | None:
    global _new_deals_cache
    if _new_deals_cache is not None:
        return _new_deals_cache
    if NEW_DEALS_CACHE_FILE.exists():
        try:
            _new_deals_cache = json.loads(NEW_DEALS_CACHE_FILE.read_text())
            return _new_deals_cache
        except Exception:
            pass
    return None

def _save_new_deals_cache(items: dict, last_updated: str):
    global _new_deals_cache
    data = {"last_updated": last_updated, "items": items}
    # Atomic write (temp + os.replace) — same truncation-on-crash class the category
    # cache had before v2.13.1; a redeploy mid-write left a corrupt file. (2026-07 audit E6)
    tmp = NEW_DEALS_CACHE_FILE.parent / (NEW_DEALS_CACHE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, separators=(',', ':')))
    os.replace(tmp, NEW_DEALS_CACHE_FILE)
    _new_deals_cache = data



_CONDITION_MAP = {
    "new":          "New",
    "likenew":      "Like New",
    "excellent":    "Excellent",
    "great":        "Great",
    "verygood":     "Very Good",
    "good":         "Good",
    "fair":         "Fair",
    "poor":         "Poor",
    "usedcondition":"Used",
    "refurbished":  "Refurbished",
    "blemished":    "Blemished",
}

def _parse_condition(raw: str) -> str:
    """Normalise a schema.org itemCondition URL or plain text to a readable label."""
    if not raw:
        return ""
    # Strip schema.org URL prefix, e.g. "https://schema.org/GoodCondition" → "GoodCondition"
    key = raw.split("/")[-1].lower().replace("condition", "").replace(" ", "").replace("-", "")
    return _CONDITION_MAP.get(key, raw.split("/")[-1])  # fall back to raw tail if unknown




# ── Condition & Details note extraction (v2.16.6) ────────────────────────────
# GC's `longDescription` field (already pulled on every scan via
# attributesToRetrieve:["*"] — zero extra Algolia calls) sometimes ends with a
# staff-written freeform note, HTML-stripped-to-plaintext by Algolia's own
# indexing: "...marketing paragraph...  Condition &amp; Details   Includes
# Hardshell Case". Confirmed present on real live items (probe_condition_
# details.py, gitignored); confirmed NOT always present (many items have no
# such section) and NOT always about accessories (one sampled item's note was
# about cosmetic scuffs and country of manufacture instead). So: extract just
# the short suffix and store it — do NOT store the full longDescription in the
# cache (it's a full marketing paragraph per item; at ~90-100K items that's
# real bytes for text nobody would read twice).
_COND_DETAILS_RE = re.compile(r'condition\s*&(?:amp;)?\s*details\s*', re.IGNORECASE)

def _extract_condition_note(long_description: str) -> str:
    """Return the freeform staff note after a 'Condition & Details' marker in
    longDescription, or '' if that section isn't present. Collapses the extra
    whitespace left behind where Algolia stripped the source HTML tags."""
    if not long_description:
        return ""
    m = _COND_DETAILS_RE.search(long_description)
    if not m:
        return ""
    note = long_description[m.end():].strip()
    note = re.sub(r'\s+', ' ', note)
    return note[:300]  # sound cap — these are short staff notes, not essays

def _fill_missing_stores(products: list) -> int:
    """(v2.16.50) Algolia sometimes returns a used item whose `stores` array is
    empty while `storeName` ("Austin, TX", our `location`) is still set. Those
    items were saved with store='' — invisible to every store-filtered browse,
    only reachable nationwide (live 2026-09-29: 74 available items across 65
    locations, still being created by current scans). Fill `store` from the
    location's usual store, learned from THIS scan's own products that have
    both fields (a nationwide scan covers every location). Most common store
    wins; ties break alphabetically so the result is deterministic. Items with
    no location at all (online/warehouse inventory — 31 live) stay storeless
    and nationwide-only. Filled items get store_inferred=True: a store-scoped
    Algolia query (facetFilters stores:<name>) never returns them, so store
    scans skip them when marking sold (see _pg_write_scan); nationwide scans
    still mark them sold normally. Mutates `products`; returns how many were
    filled."""
    from collections import Counter
    by_loc = {}
    for p in products:
        st, loc = p.get("store") or "", p.get("location") or ""
        if st and loc:
            by_loc.setdefault(loc, Counter())[st] += 1
    if not by_loc:
        return 0
    best = {loc: min(c.items(), key=lambda kv: (-kv[1], kv[0]))[0] for loc, c in by_loc.items()}
    filled = 0
    for p in products:
        if not p.get("store"):
            st = best.get(p.get("location") or "")
            if st:
                p["store"] = st
                p["store_inferred"] = True
                filled += 1
    return filled


def parse_products(data, store_name: str = None) -> list[dict]:
    """Parse products from Algolia API response. store_name can be None for all-stores queries."""
    if isinstance(data, dict):
        products = []
        try:
            results = data.get("results", [])
            if not results:
                return []
            hits = results[0].get("hits", [])
            for hit in hits:
                sku   = str(hit.get("objectID") or "").strip()
                name  = _clean_name(hit.get("displayName") or hit.get("name") or "")
                if not sku or not name:
                    continue
                price_raw = hit.get("price") or 0
                list_price_raw = hit.get("listPrice") or 0
                condition_note = _extract_condition_note(hit.get("longDescription") or "")
                # Fall back to listPrice if price is absent (listPrice is the original/regular price)
                if not price_raw and list_price_raw:
                    price_raw = list_price_raw
                try:    price = float(price_raw) if price_raw else None
                except: price = None
                try:    list_price = float(list_price_raw) if list_price_raw else 0.0
                except: list_price = 0.0
                has_price_drop = bool(hit.get("priceDrop", False))
                seo_url = hit.get("seoUrl") or ""
                url = ("https://www.guitarcenter.com" + seo_url) if seo_url else ""
                # Brand
                brand = hit.get("brand") or ""
                # Condition: "Used > Great" → "Great"
                condition = hit.get("condition") or {}
                if isinstance(condition, dict):
                    lvl1 = condition.get("lvl1") or condition.get("lvl0") or ""
                    condition = lvl1.split(">")[-1].strip() if ">" in lvl1 else lvl1
                elif isinstance(condition, str):
                    condition = condition.split(">")[-1].strip()
                condition = _parse_condition(condition) if condition else ""
                # Category from categories array: [{lvl0: "Guitars", lvl1: "Guitars > Electric Guitars", ...}]
                cats = hit.get("categories") or []
                cats_slug = hit.get("categoriesSlug") or {}
                if cats and isinstance(cats, list) and isinstance(cats[0], dict):
                    category    = cats[0].get("lvl0") or ""
                    subcategory = cats_slug.get("lvl1") or ""
                    # Fallback: parse lvl1 from full hierarchy if slug not available
                    if not subcategory:
                        lvl1_full = cats[0].get("lvl1") or ""
                        subcategory = lvl1_full.split(">")[-1].strip() if ">" in lvl1_full else ""
                else:
                    category, subcategory = "", ""
                # Date listed from startDate (seconds timestamp) — when
                # the item was published to the storefront.  Falls back to
                # creationDate (milliseconds) if startDate is missing.
                start_ts   = hit.get("startDate") or 0
                creation_ts = hit.get("creationDate") or 0
                try:
                    if start_ts:
                        date_str = datetime.utcfromtimestamp(float(start_ts)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    elif creation_ts:
                        date_str = datetime.utcfromtimestamp(float(creation_ts) / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        date_str = ""
                except Exception:
                    date_str = ""
                # Location: storeName gives "Austin, TX" format
                location = hit.get("storeName") or store_name or ""
                # Store: from hit's stores array when querying all stores
                hit_stores = hit.get("stores") or []
                store = store_name or (hit_stores[0] if hit_stores else "")
                # Image ID for thumbnail hover
                image_id = hit.get("imageId") or ""
                # GC's own vintage classification (premiumGear == "Vintage") — the
                # signal behind the public /Vintage dept. List-tolerant just in case.
                _pg = hit.get("premiumGear")
                is_vintage = (_pg == "Vintage") or (isinstance(_pg, list) and "Vintage" in _pg)
                products.append({
                    "id":             sku,
                    "name":           name,
                    "brand":          brand,
                    "price":          price,
                    "list_price":     list_price,
                    "has_price_drop": has_price_drop,
                    "store":          store,
                    "location":       location,
                    "url":            url,
                    "condition":      condition,
                    "category":       category,
                    "subcategory":    subcategory,
                    "date_listed":    date_str,
                    "image_id":       image_id,
                    "is_vintage":     is_vintage,
                    "condition_note": condition_note,
                })
        except Exception:
            pass
        return products
    return []


def _clean_gc_cat(s: str) -> str:
    """Strip 'Used ' prefix from GC category breadcrumb names."""
    s = s.strip()
    if s.lower().startswith("used "):
        s = s[5:].strip()
    return s


def _find_breadcrumbs_in_json(data, depth: int = 0):
    """Recursively search for a breadcrumb array inside __NEXT_DATA__ JSON."""
    if depth > 8:
        return None
    if isinstance(data, dict):
        for key in ("breadcrumbs", "breadcrumb", "breadCrumbs", "Breadcrumbs",
                    "crumbs", "navCrumbs", "categoryPath", "categories"):
            val = data.get(key)
            if isinstance(val, list) and len(val) >= 2:
                name_keys = ("name", "displayName", "label", "text", "title")
                if all(isinstance(v, dict) and any(k in v for k in name_keys) for v in val):
                    return val
        for v in data.values():
            if isinstance(v, (dict, list)):
                result = _find_breadcrumbs_in_json(v, depth + 1)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                result = _find_breadcrumbs_in_json(item, depth + 1)
                if result:
                    return result
    return None


def _extract_condition_from_html(html: str) -> str:
    """Extract condition label from a GC page (listing or product page).
    Prioritises the visible 'Condition: X' text that appears on both page types."""

    _VALID = {"new", "like new", "excellent", "great", "very good", "good", "fair", "poor",
              "blemished", "refurbished", "used"}

    # Strategy A: plain visible text — "Condition: Good" / "Condition: Very Good"
    # This is the most reliable; it's what the shopper sees on the page.
    m = re.search(r'[Cc]ondition\s*[:\-–]\s*([A-Za-z][A-Za-z\s]{1,20}?)(?:\s*[<\n\r,]|$)', html)
    if m:
        val = m.group(1).strip().rstrip(".,;")
        if val.lower() in _VALID:
            return val.title()

    # Strategy B: JSON-LD itemCondition on a Product page
    for block in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            d = json.loads(block)
            offers = None
            if d.get("@type") == "Product":
                offers = d.get("offers", {})
            elif d.get("@type") == "CollectionPage":
                items = d.get("mainEntity", {}).get("itemListElement", [])
                if items:
                    offers = items[0].get("item", {}).get("offers", {})
            if offers:
                raw = offers.get("itemCondition", "")
                if raw:
                    parsed = _parse_condition(raw)
                    if parsed.lower() not in ("used", "usedcondition") and parsed:
                        return parsed
        except Exception:
            pass

    # Strategy C: __NEXT_DATA__ JSON — look for condition keys
    m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m2:
        try:
            nd = json.loads(m2.group(1))
            cond = _find_key_in_json(nd, ("conditionDisplayName", "usedCondition",
                                          "productCondition", "itemCondition", "condition"))
            if cond and str(cond).lower() in _VALID:
                return str(cond).strip().title()
        except Exception:
            pass

    # Strategy D: data attributes / inline JSON strings
    for pat in [
        r'data-condition="([^"]+)"',
        r'"conditionDisplayName"\s*:\s*"([^"]+)"',
        r'"usedCondition"\s*:\s*"([^"]+)"',
    ]:
        m3 = re.search(pat, html)
        if m3:
            val = m3.group(1).strip()
            if val.lower() in _VALID:
                return val.title()

    return ""


def _find_key_in_json(data, keys: tuple, depth: int = 0):
    """Recursively search a JSON structure for any of the given keys."""
    if depth > 10:
        return None
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], str) and data[k]:
                return data[k]
        for v in data.values():
            result = _find_key_in_json(v, keys, depth + 1)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_key_in_json(item, keys, depth + 1)
            if result:
                return result
    return None


def fetch_page_data(url: str, name: str) -> tuple[str, str, str]:
    """Fetch (category, subcategory, condition) from a GC product page URL.
    Tries JSON-LD BreadcrumbList first, then __NEXT_DATA__, then keyword fallback."""
    try:
        r = _http.get(url, timeout=15)
        if r.status_code != 200:
            cat, subcat = classify_by_name(name)
            return cat, subcat, ""
        html = r.text

        condition = _extract_condition_from_html(html)

        # Strategy 1: JSON-LD BreadcrumbList
        for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        ):
            try:
                d = json.loads(block)
                if d.get("@type") == "BreadcrumbList":
                    els = sorted(d.get("itemListElement", []),
                                 key=lambda x: x.get("position", 0))
                    names = []
                    for el in els:
                        n = ((el.get("item") or {}).get("name") or el.get("name") or "").strip()
                        if n and n.lower() not in ("home", "used & vintage", "used"):
                            names.append(_clean_gc_cat(n))
                    if names:
                        cat    = names[0]
                        subcat = names[2] if len(names) >= 3 else (names[1] if len(names) >= 2 else "")
                        return cat, subcat, condition
            except Exception:
                pass

        # Strategy 2: __NEXT_DATA__ JSON blob (Next.js server-side props)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                nd = json.loads(m.group(1))
                crumbs = _find_breadcrumbs_in_json(nd)
                if crumbs:
                    names = []
                    for c in crumbs:
                        n = (c.get("displayName") or c.get("name") or
                             c.get("label") or c.get("text") or "").strip()
                        if n and n.lower() not in ("home", "used & vintage", "used"):
                            names.append(_clean_gc_cat(n))
                    if names:
                        cat    = names[0]
                        subcat = names[2] if len(names) >= 3 else (names[1] if len(names) >= 2 else "")
                        return cat, subcat, condition
            except Exception:
                pass

        cat, subcat = classify_by_name(name)
        return cat, subcat, condition

    except Exception:
        pass

    # Final fallback: keyword classification
    cat, subcat = classify_by_name(name)
    return cat, subcat, ""


# Keep old name as alias for any callers
def classify_by_name(name: str) -> tuple[str, str]:
    """Infer category and subcategory from product name using keyword matching.
    Returns (category, subcategory). Fast — no HTTP requests required."""
    n = name.lower()

    # ── Wireless Systems (before mic/recording so 'wireless' routes here) ────
    if re.search(r'wireless system|wireless mic|wireless guitar|wireless transmitter'
                 r'|in.ear wireless|iem wireless|\bqlxd\b|\bulgx\b|\bglxd\b|\bgldx\b'
                 r'|\bslxd\b|\bpgxd\b|\bbgxd\b|\bew\d|\batwr\b', n):
        return ("Microphones & Wireless", "Wireless Systems")

    # ── Amplifiers & Cabinets — check BEFORE guitars ──────────────────────────
    # "Guitar Combo Amp", "Guitar Cabinet", "Guitar Amp Head" all contain 'guitar'
    # so we must catch amp-type gear first.
    _amp_kw = re.search(
        r'combo amp|amp combo|amp head|guitar amp|tube amp|solid.state amp|valve amp'
        r'|practice amp|\bcabinet\b|\bcab\b|speaker cab|speaker cabinet'
        r'|\d+\s*[wW]\s*(combo|head|amp)\b|(combo|head)\s*\d+\s*[wW]'
        r'|\bx\d+\b.*amp|\bamp\b.*\bhead\b', n)
    if _amp_kw:
        if re.search(r'\bbass\b', n) and not re.search(r'drum|snare|cymbal', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'keyboard|piano', n):
            return ("Amplifiers & Effects", "Keyboard Amplifiers")
        if re.search(r'acoustic', n):
            return ("Amplifiers & Effects", "Acoustic Amplifiers")
        return ("Amplifiers & Effects", "Guitar Amplifiers")

    # ── Powered Monitors / Studio Monitors / PA Speakers ─────────────────────
    if re.search(r'powered monitor|studio monitor|reference monitor|nearfield|'
                 r'pair.*monitor|monitor.*pair|powered speaker|pa speaker|'
                 r'\blp-\d|kali audio|yamaha hs\d|adam a\d|krk\b|rokit\b|'
                 r'genelec|focal alpha|jbl.*(lsr|305|306|308|310|series3)', n):
        if re.search(r'studio|reference|nearfield|kali|krk|rokit|genelec|focal|adam\b', n):
            return ("Recording", "Studio Monitors")
        return ("Live Sound", "PA Speakers")

    # ── Bass (before guitar) ──────────────────────────────────────────────────
    if re.search(r'\bbass\b', n) and not re.search(r'drum|cymbal|hi.hat|snare|bassoon', n):
        if re.search(r'acoustic|upright|stand.?up|arco|double bass', n):
            return ("Bass", "Acoustic Bass Guitars")
        if re.search(r'amp|amplifier|cabinet|combo|head\b|cab\b', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'pedal|effect|pre.?amp|di\b|direct box', n):
            return ("Amplifiers & Effects", "Bass Effects")
        return ("Bass", "Electric Bass Guitars")

    # ── Guitars ───────────────────────────────────────────────────────────────
    guitar_kw = (r'guitar|stratocaster|strat\b|telecaster|tele\b|les paul|sg\b'
                 r'|flying.?v|explorer\b|jazzmaster|jaguar\b|mustang\b'
                 r'|semi.hollow|hollow.body|archtop|resonator|dobro'
                 r'|banjo|mandolin|ukulele|squier|epiphone|prs\b|gretsch'
                 r'|rickenbacker|es.?[0-9]')
    if re.search(guitar_kw, n):
        if re.search(r'banjo', n):
            return ("Folk & Traditional Instruments", "Banjos")
        if re.search(r'mandolin', n):
            return ("Folk & Traditional Instruments", "Mandolins")
        if re.search(r'ukulele', n):
            return ("Folk & Traditional Instruments", "Ukuleles")
        if re.search(r'acoustic|classical|nylon|parlor|dreadnought|folk|fingerstyle|12.string', n):
            return ("Guitars", "Acoustic Guitars")
        if re.search(r'classical|nylon|spanish', n):
            return ("Guitars", "Classical & Nylon Guitars")
        return ("Guitars", "Electric Guitars")

    # ── Effects & Pedals ──────────────────────────────────────────────────────
    if re.search(r'pedal|effect\b|reverb\b|delay\b|distortion|overdrive|fuzz\b|wah\b'
                 r'|chorus\b|flanger|phaser|compressor|tremolo|boost\b|looper|tuner\b'
                 r'|pedalboard|multi.effect|octave\b|harmonizer|pitch shift', n):
        return ("Amplifiers & Effects", "Effects Pedals & Processors")

    # ── Amplifiers (broader — standalone \bamp\b not caught above) ────────────
    if re.search(r'\bamp\b|amplifier', n):
        if re.search(r'\bbass\b', n):
            return ("Amplifiers & Effects", "Bass Amplifiers")
        if re.search(r'keyboard|piano', n):
            return ("Amplifiers & Effects", "Keyboard Amplifiers")
        return ("Amplifiers & Effects", "Guitar Amplifiers")

    # ── Drums & Percussion ────────────────────────────────────────────────────
    if re.search(r'drum|snare|cymbal|hi.?hat|bass drum|\btom\b|drum kit|drum set'
                 r'|drum throne|djembe|cajon|bongo|conga|percussion|cowbell'
                 r'|tambourine|marimba|xylophone|vibraphone|timpani|electronic drum'
                 r'|volca beats|volca drum|tr.?\d{2,3}|drum machine|beat.*machine', n):
        if re.search(r'electronic|digital|e.?drum|drum machine|volca|tr.?\d', n):
            return ("Drums & Percussion", "Electronic Drums")
        if re.search(r'cymbal|hi.?hat', n):
            return ("Drums & Percussion", "Cymbals")
        if re.search(r'snare', n):
            return ("Drums & Percussion", "Snare Drums")
        if re.search(r'djembe|bongo|conga|cajon|hand drum', n):
            return ("Drums & Percussion", "Hand Drums")
        return ("Drums & Percussion", "Drum Sets")

    # ── Keyboards & MIDI ──────────────────────────────────────────────────────
    if re.search(r'keyboard|piano|organ\b|synth|synthesizer|workstation\b'
                 r'|midi controller|electric piano|stage piano|arranger|clav'
                 r'|wurlitzer|rhodes\b|nord\b|sound module|volca\b|groovebox'
                 r'|roland\b.*\b(jd|juno|jupiter|fa|rd|fp|gaia)'
                 r'|korg\b|yamaha\b.*\b(psr|cp|ck|np|p-\d|montage|motif)', n):
        if re.search(r'midi|controller\b', n):
            return ("Keyboards & MIDI", "MIDI Controllers")
        if re.search(r'synth|synthesizer|volca|groovebox|sound module', n):
            return ("Keyboards & MIDI", "Synthesizers & Sound Modules")
        if re.search(r'organ', n):
            return ("Keyboards & MIDI", "Organs")
        if re.search(r'digital piano|stage piano|acoustic piano', n):
            return ("Keyboards & MIDI", "Digital Pianos")
        return ("Keyboards & MIDI", "Keyboards")

    # ── Recording & Studio ────────────────────────────────────────────────────
    if re.search(r'audio interface|recording interface|usb interface|thunderbolt interface', n):
        return ("Recording", "Audio Interfaces")
    if re.search(r'microphone|condenser mic|dynamic mic|ribbon mic|vocal mic\b', n):
        return ("Recording", "Microphones")
    if re.search(r'\bmic\b', n) and not re.search(r'microphone stand', n):
        return ("Recording", "Microphones")
    if re.search(r'preamp|pre.?amplifier|channel strip|outboard', n):
        return ("Recording", "Preamps & Channel Strips")
    if re.search(r'mixer|mixing console|mixing board|analog mixer|digital mixer', n):
        return ("Recording", "Mixers")
    if re.search(r'headphone|headset|earphone|in.ear monitor|iem\b', n):
        return ("Recording", "Headphones & Monitoring")
    if re.search(r'audio recorder|field recorder|multitrack|interface\b', n):
        return ("Recording", "Audio Interfaces")

    # ── DJ Equipment ─────────────────────────────────────────────────────────
    if re.search(r'\bdj\b|turntable|cdj\b|serato|traktor|rekordbox|dj mixer|dj controller', n):
        return ("DJ Equipment & Lighting", "DJ Equipment")

    # ── Live Sound ────────────────────────────────────────────────────────────
    if re.search(r'\bpa\b|powered speaker|live sound|subwoofer|stage monitor'
                 r'|line array|public address', n):
        return ("Live Sound", "PA Systems")

    # ── Accessories ───────────────────────────────────────────────────────────
    if re.search(r'\bstrap\b|guitar strap|instrument strap', n):
        return ("Accessories", "Straps")
    if re.search(r'\bstring\b|guitar string|bass string', n):
        return ("Accessories", "Strings")
    if re.search(r'\bcase\b|gig bag|hardshell|soft case', n):
        return ("Accessories", "Cases & Bags")
    if re.search(r'\bstand\b|guitar stand|amp stand|keyboard stand', n):
        return ("Accessories", "Stands & Racks")
    if re.search(r'\bcable\b|instrument cable|patch cable|speaker cable', n):
        return ("Accessories", "Cables")
    if re.search(r'\bpick\b|plectrum', n):
        return ("Accessories", "Picks")

    return ("", "")


def scrape_store(store_name: str, send, stop_event: threading.Event) -> tuple[list[dict], set, bool]:
    """Returns (all_products_found, ids_seen_this_store, complete).

    `complete` is False when this store's fetch was cut short by an error or a
    user-initiated stop, meaning what we got back is a PARTIAL, possibly-stale
    view of this store's inventory — not "this store genuinely has fewer items
    than before." Callers use this to avoid two mistakes: (1) marking this
    store's previously-cached items as sold just because they didn't show up
    in an incomplete fetch, and (2) letting this run's max date_listed (which
    may be missing whatever this store's freshest listings are) advance the
    user's NEW-detection anchor past items we never actually got a chance to
    see. A natural "ran out of pages" or "store genuinely has 0 items right
    now" finish is still `complete = True` — a confirmed 404 (store gone) is
    also `complete = True` since that's a real, not partial, answer. (v2.16.11)
    """
    all_products, ids_seen = [], set()
    complete = True
    page = 1
    while page <= 50:
        if stop_event.is_set():
            send({"type": "progress", "msg": f"  [{store_name}] stopped."})
            complete = False
            break
        try:
            data = fetch_page(store_name, page)
        except Exception as e:
            if "404" in str(e):
                send({"type": "progress", "msg": f"  [{store_name}] not found — removing from store list."})
                _remove_invalid_store(store_name)
                # Confirmed gone is a complete, real answer — not a partial fetch.
            elif page == 1:
                # One quick retry on page 1 only — a fanout of ~298 near-simultaneous
                # per-store requests routinely trips a transient timeout/connection
                # error on a handful of stores; a single immediate retry clears most
                # of those without meaningfully slowing the scan down. (v2.16.11)
                send({"type": "progress", "msg": f"  [{store_name}] error: {e} — retrying once…"})
                time.sleep(0.75)
                try:
                    data = fetch_page(store_name, page)
                except Exception as e2:
                    send({"type": "progress", "msg": f"  [{store_name}] retry failed: {e2}"})
                    complete = False
                    break
            else:
                send({"type": "progress", "msg": f"  [{store_name}] error: {e}"})
                complete = False
                break
        products = parse_products(data, store_name)
        if not products:
            break
        if all(p["id"] in ids_seen for p in products):
            break
        for p in products:
            if p["id"] not in ids_seen:
                all_products.append(p)
                ids_seen.add(p["id"])
        # Algolia tells us total pages via nbPages
        try:
            nb_pages = data.get("results", [{}])[0].get("nbPages", 1)
            if page >= nb_pages:
                break
        except Exception:
            if len(products) < PAGE_SIZE:
                break
        page += 1
        # No sleep needed between Algolia API pages
    send({"type": "progress", "msg": f"  [{store_name}] {len(all_products)} items"})
    return all_products, ids_seen, complete


def _remove_invalid_store(store_name: str):
    """Remove a store that returned 404 from the stores cache.
    Also saves to a blocklist so it stays removed after refreshes."""
    # Remove from cache
    if STORES_CACHE.exists():
        try:
            d = json.loads(STORES_CACHE.read_text())
            stores = d.get("stores", [])
            if store_name in stores:
                stores.remove(store_name)
                d["stores"] = stores
                STORES_CACHE.write_text(json.dumps(d))
        except Exception:
            pass
    # Add to persistent blocklist
    blocklist_file = DATA_DIR / "gc_invalid_stores.json"
    try:
        blocklist = json.loads(blocklist_file.read_text()) if blocklist_file.exists() else []
        if store_name not in blocklist:
            blocklist.append(store_name)
            blocklist_file.write_text(json.dumps(sorted(blocklist)))
    except Exception:
        pass


def _get_blocklist() -> set:
    """Return the set of stores confirmed invalid (404'd)."""
    blocklist_file = DATA_DIR / "gc_invalid_stores.json"
    try:
        if blocklist_file.exists():
            return set(json.loads(blocklist_file.read_text()))
    except Exception:
        pass
    return set()


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "seen_ids": [], "item_dates": {}}



# ── Excel ─────────────────────────────────────────────────────────────────────

_COLS    = ["Status", "Date Listed", "Item Name", "Brand", "Condition", "Category", "Subcategory", "Price", "Location", "Link"]
_WIDTHS  = [8, 14, 50, 16, 14, 22, 22, 12, 18, 70]
_HDR_FILL = PatternFill("solid", start_color="1F3864", end_color="1F3864")
_HDR_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=11)
_ROW_FONT = Font(name="Arial", size=10)
_NEW_FONT = Font(name="Arial", bold=True, size=10)
_ALT_FILL = PatternFill("solid", start_color="DCE6F1", end_color="DCE6F1")

def _fmt_row(ws, r):
    fill = _ALT_FILL if r % 2 == 0 else None
    for col in range(1, len(_COLS) + 1):
        c = ws.cell(r, col)
        c.font = _ROW_FONT
        if fill: c.fill = fill

def write_excel(new_items: list[dict]):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n  = len(new_items)

    # If existing file has old column count, back it up and start fresh
    if OUTPUT_FILE.exists():
        try:
            wb_check = load_workbook(OUTPUT_FILE)
            if wb_check.active.max_column != len(_COLS):
                backup = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + "_backup" + OUTPUT_FILE.suffix)
                OUTPUT_FILE.rename(backup)
        except Exception:
            pass

    if OUTPUT_FILE.exists():
        wb = load_workbook(OUTPUT_FILE)
        ws = wb.active
        ws.insert_rows(2, amount=n)
        for i, item in enumerate(new_items):
            r = 2 + i
            date_listed = item.get("date_listed") or ""
            ws.cell(r, 1, "New"); ws.cell(r, 2, _fmt_date(date_listed) if date_listed else ts)
            ws.cell(r, 3, item["name"]); ws.cell(r, 4, item.get("brand", ""))
            ws.cell(r, 5, item.get("condition", ""))
            ws.cell(r, 6, item.get("category", "")); ws.cell(r, 7, item.get("subcategory", ""))
            pc = ws.cell(r, 8, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 9, item.get("location") or item.get("store", ""))
            lc = ws.cell(r, 10, item["url"] or "")
            if item["url"]: lc.hyperlink = item["url"]; lc.style = "Hyperlink"
            _fmt_row(ws, r); ws.cell(r, 1).font = _NEW_FONT
        for r in range(2 + n, ws.max_row + 1):
            _fmt_row(ws, r)
    else:
        wb = Workbook(); ws = wb.active
        ws.title = "New Inventory"; ws.freeze_panes = "A2"
        ws.append(_COLS)
        for ci in range(1, len(_COLS) + 1):
            c = ws.cell(1, ci); c.fill = _HDR_FILL; c.font = _HDR_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 22
        for ci, w in enumerate(_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
        for i, item in enumerate(new_items):
            r = 2 + i
            date_listed = item.get("date_listed") or ""
            ws.cell(r, 1, "New"); ws.cell(r, 2, _fmt_date(date_listed) if date_listed else ts)
            ws.cell(r, 3, item["name"]); ws.cell(r, 4, item.get("brand", ""))
            ws.cell(r, 5, item.get("condition", ""))
            ws.cell(r, 6, item.get("category", "")); ws.cell(r, 7, item.get("subcategory", ""))
            pc = ws.cell(r, 8, item["price"]); pc.number_format = '$#,##0.00'
            ws.cell(r, 9, item.get("location") or item.get("store", ""))
            lc = ws.cell(r, 10, item["url"] or "")
            if item["url"]: lc.hyperlink = item["url"]; lc.style = "Hyperlink"
            _fmt_row(ws, r); ws.cell(r, 1).font = _NEW_FONT
    wb.save(OUTPUT_FILE)


# ── Flask ─────────────────────────────────────────────────────────────────────

app             = Flask(__name__)
_secret = os.environ.get("SECRET_KEY", "").strip()
if not _secret:
    raise RuntimeError("SECRET_KEY env var is required — refusing to start with no secret")
app.secret_key  = _secret
# Secure session cookie settings
# SESSION_COOKIE_SECURE=True means the cookie is only sent over HTTPS.
# We enable it when running on Railway (RAILWAY_ENVIRONMENT is set); local dev
# is HTTP so we leave it off there to avoid breaking local testing.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("RAILWAY_ENVIRONMENT") is not None
# ── Request timing (v2.17.1, Phase G step 1 "measure first") ─────────────────
# Lightweight per-request timing so we fix what users actually wait on. Adds NO
# behavior change: it only measures and reports.
#
#   * Every request (except the /api/progress SSE stream) is timed from the first
#     before_request hook to the last after_request hook — i.e. including
#     Flask-Compress (these hooks are registered BEFORE Compress(app), and Flask
#     runs after_request hooks in reverse registration order, so ours runs last).
#     Not included: time queued in gunicorn before a thread picks the request up,
#     network time, and body streaming of static files (compressed lazily).
#   * Requests are grouped by route (the Flask rule, e.g. "GET /store/<slug>").
#     /api/browse is split by request shape ("browse all", "browse stores
#     +want", …) because a plain first page and a Want List page cost very
#     different amounts. Group count is bounded (fixed routes + ≤ 48 browse shapes).
#   * _pg_browse / _pg_conn record per-phase times (q1 totals, facets, q3, page,
#     kwflags, build, conn = pool checkout) for the current request.
#   * Each response gets a `Server-Timing` header (app;dur=… plus phases), so the
#     browser's DevTools and PerformanceResourceTiming.serverTiming see the split
#     between server time and network time.
#   * Railway logs: `[timing] SLOW <route> <ms>ms …` for any request over
#     _TIMING_SLOW_MS, and every _TIMING_SUMMARY_SECS a `[timing] summary` block
#     (one line per route active in that window: n, p50/p90/p99/max, 5xx).
#   * Admin: GET /api/timing (JSON, cumulative since restart + scans);
#     also attached to POST /api/browse?pg_shadow=1 as `_timing`.
#     GET /api/timing?reset=1 clears the counters.
# Also tracks requests in flight at arrival (how often >1 request overlaps on the
# single gunicorn worker — the evidence for / against more workers) and each scan's
# wall time split into fetch / save / finish.
from flask import g as _g, has_request_context as _has_request_context
from collections import deque as _deque

_TIMING_LOCK = threading.Lock()
_TIMING_RING = 1000            # samples kept per route for cumulative percentiles
_TIMING_WINDOW_CAP = 5000      # samples kept per route per summary window
_TIMING_SLOW_MS = 1500
_TIMING_SUMMARY_SECS = 900     # 15 minutes
_TIMING_SKIP_PREFIXES = ("/api/progress",)   # SSE: open for minutes by design


def _timing_fresh():
    return {"since": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "routes": {}, "inflight": 0, "inflight_max": 0,
            "inflight_at_arrival": {}, "window_start": time.time(), "scans": []}


_TIMING = _timing_fresh()


def _timing_phase(name, ms):
    """Add `ms` to phase `name` for the current request (accumulates, so a phase
    that runs twice — e.g. two pool checkouts — sums). No-op outside a request."""
    if not _has_request_context():
        return
    ph = getattr(_g, "_timing_phases", None)
    if ph is None:
        ph = _g._timing_phases = {}
    ph[name] = ph.get(name, 0.0) + ms


def _timing_set_key(key):
    """Override the route group for this request (used by /api/browse shapes)."""
    if _has_request_context():
        _g._timing_key = key


def _timing_pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return round(s[k], 1)


def _timing_stats(vals):
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "p50": _timing_pct(vals, 50), "p90": _timing_pct(vals, 90),
            "p99": _timing_pct(vals, 99), "max": round(max(vals), 1),
            "avg": round(sum(vals) / len(vals), 1)}


@app.before_request
def _timing_start():
    if request.path.startswith(_TIMING_SKIP_PREFIXES):
        return None
    _g._timing_t0 = time.perf_counter()
    with _TIMING_LOCK:
        n = _TIMING["inflight"]          # requests already running when this one arrived
        _TIMING["inflight"] = n + 1
        _TIMING["inflight_max"] = max(_TIMING["inflight_max"], n + 1)
        b = str(n) if n < 4 else "4+"
        _TIMING["inflight_at_arrival"][b] = _TIMING["inflight_at_arrival"].get(b, 0) + 1
    _g._timing_counted = True
    return None


def _timing_route_key():
    k = getattr(_g, "_timing_key", None)
    if k:
        return k
    rule = request.url_rule.rule if request.url_rule is not None else "(unmatched)"
    return f"{request.method} {rule}"


def _timing_summary_locked(now):
    """Build the summary lines for the window just ended and reset the window."""
    lines = []
    for key in sorted(_TIMING["routes"]):
        r = _TIMING["routes"][key]
        win = r["win"]
        if not win:
            continue
        st = _timing_stats(win)
        lines.append(f"[timing]   {key}: n {st['n']}, p50 {st['p50']}ms, p90 {st['p90']}ms, "
                     f"p99 {st['p99']}ms, max {st['max']}ms, 5xx {r['win_5xx']}")
        r["win"] = []
        r["win_5xx"] = 0
    mins = int(round((now - _TIMING["window_start"]) / 60.0))
    _TIMING["window_start"] = now
    if lines:
        lines.insert(0, f"[timing] summary, last {mins} min (v{APP_VERSION}):")
    return lines


@app.after_request
def _timing_finish(response):
    if not getattr(_g, "_timing_counted", False):
        return response
    _g._timing_counted = False     # never count twice
    t0 = getattr(_g, "_timing_t0", None)
    ms = (time.perf_counter() - t0) * 1000.0 if t0 is not None else 0.0
    phases = getattr(_g, "_timing_phases", None) or {}
    key = _timing_route_key()
    status = response.status_code
    if status == 200 and request.url_rule is not None and request.url_rule.endpoint == "static":
        # One group per real static file (bounded: only files that exist get a 200).
        key = f"GET /static/{(request.view_args or {}).get('filename', '')}"
    now = time.time()
    summary = []
    with _TIMING_LOCK:
        _TIMING["inflight"] = max(0, _TIMING["inflight"] - 1)
        r = _TIMING["routes"].get(key)
        if r is None:
            r = _TIMING["routes"][key] = {"ms": _deque(maxlen=_TIMING_RING), "win": [],
                                         "n": 0, "n_5xx": 0, "n_4xx": 0, "win_5xx": 0,
                                         "phases": {}}
        r["n"] += 1
        r["ms"].append(ms)
        if len(r["win"]) < _TIMING_WINDOW_CAP:
            r["win"].append(ms)
        if status >= 500:
            r["n_5xx"] += 1
            r["win_5xx"] += 1
        elif status >= 400:
            r["n_4xx"] += 1
        for name, v in phases.items():
            r["phases"].setdefault(name, _deque(maxlen=_TIMING_RING)).append(v)
        if now - _TIMING["window_start"] >= _TIMING_SUMMARY_SECS:
            summary = _timing_summary_locked(now)
    # Server-Timing: app first, then phases in the order they ran.
    st = [f"app;dur={ms:.1f}"] + [f"{n};dur={v:.1f}" for n, v in phases.items()]
    response.headers["Server-Timing"] = ", ".join(st)
    if ms >= _TIMING_SLOW_MS:
        ph = " ".join(f"{n}={v:.0f}" for n, v in phases.items())
        print(f"[timing] SLOW {key} {ms:.0f}ms status {status}" + (f" ({ph})" if ph else ""))
    for line in summary:
        print(line)
    return response


@app.teardown_request
def _timing_teardown(exc):
    # A request that raised before after_request ran (unhandled exception) still
    # has to leave the in-flight gauge.
    if getattr(_g, "_timing_counted", False):
        _g._timing_counted = False
        with _TIMING_LOCK:
            _TIMING["inflight"] = max(0, _TIMING["inflight"] - 1)


def _timing_note_scan(**kw):
    """Record one scan's wall time split (called from _run just before 'done')."""
    kw["at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _TIMING_LOCK:
        _TIMING["scans"].append(kw)
        del _TIMING["scans"][:-20]


def _timing_report():
    with _TIMING_LOCK:
        routes = {}
        for key, r in _TIMING["routes"].items():
            d = _timing_stats(list(r["ms"]))
            d["count"] = r["n"]
            d["n_5xx"] = r["n_5xx"]
            d["n_4xx"] = r["n_4xx"]
            if r["phases"]:
                d["phases"] = {n: _timing_stats(list(v)) for n, v in r["phases"].items()}
            routes[key] = d
        arrivals = dict(_TIMING["inflight_at_arrival"])
        out = {"version": APP_VERSION, "since": _TIMING["since"],
               "inflight_now": _TIMING["inflight"], "inflight_max": _TIMING["inflight_max"],
               "inflight_at_arrival": arrivals,
               "note": "ms = server time from first before_request to last after_request "
                       "(incl. compression; excl. gunicorn queueing and network). "
                       "Percentiles over the last %d requests per route." % _TIMING_RING,
               "scans": list(_TIMING["scans"])}
    out["routes"] = dict(sorted(routes.items(), key=lambda kv: -(kv[1].get("count") or 0)))
    return out


@app.route("/api/timing")
def api_timing():
    """Admin-only request timing report (Phase G step 1). ?reset=1 clears it."""
    if not _is_admin():
        return jsonify({"error": "Not found"}), 404
    if request.args.get("reset") == "1":
        global _TIMING
        with _TIMING_LOCK:
            fresh = _timing_fresh()
            fresh["inflight"] = _TIMING["inflight"]   # keep the live gauge honest
            _TIMING = fresh
    return jsonify(_timing_report())


# Cap request bodies (413 before parsing). /api/browse is unauthenticated and parses
# request.json in full before any per-field cap applies; without this a huge JSON body
# is a memory DoS. Largest legit payload (240-store array + 750-keyword want list +
# admin import) is well under 1 MB. (2026-07 audit M1)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB
# Static assets are safe to cache long-term because their URLs carry ?v=APP_VERSION
# (see the template replacements at the bottom of the file) — a deploy changes the
# URL, so stale caches can't survive a version bump. (2026-07 audit S7)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # 1 year
# Railway does NOT gzip responses (verified 2026-07-06: no Content-Encoding on
# static/gc.js) — 194KB gc.js + 53KB gc.css went over the wire uncompressed, and so
# did every /api/browse JSON page. Flask-Compress gzips html/css/js/json (default
# mimetype list; text/event-stream is untouched, so SSE is safe). (2026-07 audit S7)
try:
    from flask_compress import Compress
    # Newer Flask serves .js as text/javascript, which is NOT in flask-compress's
    # default mimetype list — without this line gc.js stays uncompressed (the whole
    # point). svg included for og-image/favicon.
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "text/xml",
        "text/javascript", "application/javascript",
        "application/json", "image/svg+xml",
    ]
    # Static files are streamed responses, and flask-compress's streaming algorithm
    # list excludes gzip by default (["zstd","br","deflate"]) — a gzip-only client
    # (curl -H 'Accept-Encoding: gzip', older agents) would get static uncompressed.
    app.config["COMPRESS_ALGORITHM_STREAMING"] = ["zstd", "br", "gzip", "deflate"]
    Compress(app)
except ImportError:
    print("[warn] flask-compress not installed — responses will be uncompressed")

# ProxyFix: Railway terminates TLS so Flask sees HTTP; this makes url_for() produce https://
if os.environ.get("RAILWAY_ENVIRONMENT"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── Google OAuth setup ────────────────────────────────────────────────────────
# Uses direct HTTP requests (no authlib session dependency) to avoid Railway
# proxy state-mismatch issues. State is stored server-side in _oauth_pending.
_GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_GOOGLE_OAUTH_ENABLED = bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)

_oauth_pending: dict = {}   # state_token → {next_url, expires}

# ── Login rate-limiting (in-memory, per IP) ────────────────────────────────────
# Keyed by IP → list of attempt timestamps. Pruned on each check.
_login_attempts: dict = {}
_LOGIN_WINDOW   = 300   # seconds (5 min rolling window)
_LOGIN_MAX      = 10    # max failed attempts before lockout

# ── Scan rate-limiting (in-memory, per IP) ─────────────────────────────────────
# Prevents unauthenticated bots from hammering /api/run and exhausting Algolia quota.
_scan_last: dict = {}   # IP → last scan start timestamp
_SCAN_COOLDOWN = 60     # seconds between scans per IP (logged-in users are exempt)

def _check_login_rate(ip: str) -> bool:
    """Return True (allowed) or False (rate-limited). Only counts failed attempts."""
    now      = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_MAX

def _record_login_failure(ip: str):
    now = time.time()
    bucket = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    bucket.append(now)
    _login_attempts[ip] = bucket

def _client_ip() -> str:
    """Canonical client IP for rate-limiting and logging.
    ProxyFix(x_for=1) is applied on Railway, so request.remote_addr is
    already normalized to the real client IP — no need to read the
    X-Forwarded-For header directly (which clients can spoof)."""
    return request.remote_addr or "unknown"

def _safe_next(raw: str, default: str) -> str:
    """Validate a ?next= redirect target so it can only point back at this site.
    Rejects anything that isn't a single-slash relative path. In particular this
    blocks '//host' (protocol-relative) AND the backslash trick '/\\host', which
    browsers normalize to '//host' → an off-site open redirect. Also rejects any
    embedded CR/LF/TAB to avoid header/redirect smuggling."""
    if (not raw
            or not raw.startswith("/")
            or raw.startswith("//")
            or "\\" in raw
            or any(c in raw for c in ("\r", "\n", "\t"))):
        return default
    return raw

_q              = queue.Queue()        # legacy fallback (kept for non-run endpoints)
_run_queues: dict[str, list[queue.Queue]] = {}  # run_id → list of subscriber queues (fan-out)
_run_queues_lock = threading.Lock()
_lock           = threading.Lock()
_stop_event     = threading.Event()
_current_run_id: str   = ""   # run_id of the scan currently in progress (empty if none)
_current_run_time: str = ""   # run_time of the current scan

import uuid as _uuid

# ── Device access tracking ─────────────────────────────────────────────────────
_DEVICE_LOG       = DATA_DIR / "gc_device_log.jsonl"
_device_log_lock  = threading.Lock()
_seen_today: set  = set()   # (device_id, date) pairs already written today

def _log_device(device_id: str):
    """Append one line to gc_device_log.jsonl the first time a device is seen each day."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    key   = (device_id, today)
    if key in _seen_today:
        return
    _seen_today.add(key)
    entry = json.dumps({
        "date":       today,
        "time":       datetime.utcnow().strftime("%H:%M:%SZ"),
        "device_id":  device_id,
        "ua":         request.headers.get("User-Agent", "")[:120],
        "ip":         _client_ip(),
    })
    with _device_log_lock:
        with open(_DEVICE_LOG, "a") as f:
            f.write(entry + "\n")

# ── Old-domain redirect (301 to gcgeartracker.com) ───────────────────────────
@app.before_request
def _redirect_old_domain():
    """301-redirect any request arriving on the old hostname to gcgeartracker.com."""
    host = request.host.split(":")[0].lower()
    if host == "gctracker.animalsintrees.com":
        target = "https://gcgeartracker.com" + request.full_path.rstrip("?")
        return redirect(target, code=301)

# ── CSRF protection (Origin check on state-changing requests) ─────────────────
@app.before_request
def _csrf_check():
    """Block cross-origin POST/PUT/DELETE/PATCH requests.
    JSON APIs already get implicit protection (browsers won't send
    Content-Type: application/json cross-origin without CORS preflight),
    but this adds an explicit Origin/Referer check as defense-in-depth."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin  = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    # Allow requests with no Origin (e.g. same-origin, curl, server-to-server)
    if not origin and not referer:
        return None
    host = request.host  # e.g. "gctracker.animalsintrees.com" or "localhost:5050"
    # Check Origin header first
    if origin:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        if parsed.netloc == host:
            return None
        return jsonify({"error": "Cross-origin request blocked."}), 403
    # Fallback: check Referer
    if referer:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        if parsed.netloc == host:
            return None
        return jsonify({"error": "Cross-origin request blocked."}), 403
    return None

@app.after_request
def _track_device(response):
    """Set a long-lived device cookie, log first visit of each day, and add security headers."""
    # Security headers — applied to every response
    response.headers.setdefault("X-Frame-Options",        "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy",        "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy",     "camera=(), microphone=(), geolocation=()")
    # Cross-origin isolation for the top-level document. OAuth here is redirect-based
    # (no popup relies on window.opener), so 'same-origin-allow-popups' is safe and
    # earns the security-scanner credit. NOTE: we deliberately do NOT set
    # Cross-Origin-Resource-Policy globally — it would stop social crawlers (Twitter/
    # Slack/Facebook) from fetching the cross-origin OG image.
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    # HSTS — only on Railway (HTTPS); tells browsers to always use HTTPS for this domain
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # CSP — inline scripts/styles needed (single-file app), but block everything else.
    # default-src 'none' forces explicit allowlists for every resource type.
    # frame-ancestors 'none' prevents clickjacking (stronger than X-Frame-Options alone).
    response.headers.setdefault("Content-Security-Policy",
        "default-src 'none'; "
        "script-src 'self' https://accounts.google.com https://apis.google.com https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://accounts.google.com; "
        "img-src 'self' data: https://media.guitarcenter.com https://*.googleusercontent.com; "
        "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com https://www.googleapis.com https://api.zippopotam.us https://www.google-analytics.com; "
        "frame-src https://accounts.google.com; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https://accounts.google.com; "
        "frame-ancestors 'none'"
    )
    # Skip SSE streams for device tracking (cookie/logging only, headers already set above)
    if request.path.startswith("/api/progress"):
        return response
    _maybe_backup_users_db()
    device_id = request.cookies.get("gt_device_id")
    if not device_id:
        device_id = str(_uuid.uuid4())
        # 2-year cookie — survives browser restarts; Secure only on Railway (HTTPS)
        response.set_cookie("gt_device_id", device_id,
                            max_age=60*60*24*730, httponly=True, samesite="Lax",
                            secure=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
                            path="/")
    _log_device(device_id)
    return response

# (v2.17.4) Per-run message backlog, replayed to anyone who subscribes late. A
# quick scan can finish in well under a second — before the browser's
# EventSource has even connected to /api/progress — and messages sent before a
# subscriber existed used to be lost (the client then never got "done").
# Kept _RUN_BACKLOG_KEEP_SECS after the run starts; pruned on each new run
# (which also drops the old runs' never-read first queues — a slow leak before).
_RUN_BACKLOG: dict = {}                # run_id -> {"t": start time, "msgs": [...]}
_RUN_BACKLOG_CAP = 400                 # progress lines kept per run ("done" always kept)
_RUN_BACKLOG_KEEP_SECS = 900


def _create_run_queue() -> tuple[str, queue.Queue]:
    """Start a new run: create a fan-out entry and return (run_id, first_subscriber_queue)."""
    global _current_run_id
    run_id = _uuid.uuid4().hex[:12]
    q = queue.Queue()
    now = time.time()
    with _run_queues_lock:
        for old in [r for r, b in _RUN_BACKLOG.items() if now - b["t"] > _RUN_BACKLOG_KEEP_SECS]:
            _RUN_BACKLOG.pop(old, None)
            _run_queues.pop(old, None)
        _run_queues[run_id] = [q]
        _RUN_BACKLOG[run_id] = {"t": now, "msgs": []}
        _current_run_id = run_id
    return run_id, q

def _subscribe_to_run(run_id: str) -> queue.Queue | None:
    """Join a run. Returns a new subscriber queue pre-filled with every message the
    run has sent so far (v2.17.4), or None if the run is unknown / expired."""
    q = queue.Queue()
    with _run_queues_lock:
        if run_id not in _run_queues:
            if run_id not in _RUN_BACKLOG:
                return None
            _run_queues[run_id] = []      # finished and cleaned up, backlog still held
        for m_ in _RUN_BACKLOG.get(run_id, {}).get("msgs", []):
            q.put(m_)
        _run_queues[run_id].append(q)
    return q

def _broadcast(run_id: str, msg):
    """Send a message to all subscriber queues for a run (and its backlog)."""
    with _run_queues_lock:
        subscribers = list(_run_queues.get(run_id, []))
        b = _RUN_BACKLOG.get(run_id)
        if b is not None and (len(b["msgs"]) < _RUN_BACKLOG_CAP or msg.get("type") == "done"):
            b["msgs"].append(msg)
    for q in subscribers:
        q.put(msg)

def _get_run_queue(run_id: str) -> queue.Queue | None:
    """Return the first subscriber queue (legacy helper, unused by _run directly)."""
    with _run_queues_lock:
        subs = _run_queues.get(run_id)
        return subs[0] if subs else None

def _cleanup_subscriber(run_id: str, q: queue.Queue):
    """Remove one subscriber queue. If it's the last, remove the whole run."""
    global _current_run_id
    with _run_queues_lock:
        subs = _run_queues.get(run_id)
        if subs and q in subs:
            subs.remove(q)
        if not subs:
            _run_queues.pop(run_id, None)
            if _current_run_id == run_id:
                _current_run_id = ""

def _cleanup_run_queue(run_id: str):
    """Remove all subscribers for a run (called when scan finishes)."""
    global _current_run_id
    with _run_queues_lock:
        _run_queues.pop(run_id, None)
        if _current_run_id == run_id:
            _current_run_id = ""


def optional_user_context(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Site access is open — no login required.
        # Individual sensitive endpoints (e.g. /api/reset) enforce their own password.
        return f(*args, **kwargs)
    return decorated

# ── Admin session auth ────────────────────────────────────────────────────────
# Replaces the old ?pw= query-string pattern so the admin password never
# appears in URLs, browser history, or server/proxy logs.

_ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Admin Login</title>
<style>
body{background:#111;color:#eee;font-family:monospace;display:flex;align-items:center;
     justify-content:center;height:100vh;margin:0}
.box{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:10px;padding:40px;width:320px}
h2{text-align:center;margin-bottom:16px;color:#fff}
input{width:100%;padding:10px;background:#222;border:1px solid #444;color:#eee;
      border-radius:4px;margin-bottom:12px;box-sizing:border-box;font-size:1rem}
button{width:100%;padding:10px;background:#c00;color:#fff;border:none;
       border-radius:4px;cursor:pointer;font-size:1rem}
.err{color:#f88;text-align:center;margin-bottom:12px;font-size:.85rem}
</style></head><body>
<div class="box"><h2>Admin Login</h2>
<form method="POST">
  {err}
  <input type="hidden" name="_csrf" value="{csrf}">
  <input name="pw" type="password" placeholder="Admin password" autofocus>
  <button type="submit">Enter</button>
</form>
</div></body></html>"""

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    import secrets as _secrets
    if request.method == "GET":
        token = _secrets.token_hex(32)
        session["_admin_csrf"] = token
        html = _ADMIN_LOGIN_HTML.replace('{err}','').replace('{csrf}', token)
        return Response(html, content_type="text/html")
    # Validate CSRF token before anything else
    submitted = request.form.get("_csrf") or ""
    expected  = session.get("_admin_csrf") or ""
    if not expected or not hmac.compare_digest(submitted, expected):
        token = _secrets.token_hex(32)
        session["_admin_csrf"] = token
        return Response(
            _ADMIN_LOGIN_HTML.replace('{err}','<div class="err">Invalid request. Please reload and try again.</div>').replace('{csrf}', token),
            status=403, content_type="text/html")
    ip = _client_ip()
    if not _check_login_rate(ip):
        token = _secrets.token_hex(32)
        session["_admin_csrf"] = token
        return Response(
            _ADMIN_LOGIN_HTML.replace('{err}','<div class="err">Too many attempts. Please wait a few minutes.</div>').replace('{csrf}', token),
            status=429, content_type="text/html")
    pw = (request.form.get("pw") or "").strip()
    admin_pw = APP_PASSWORD
    if not admin_pw or not hmac.compare_digest(pw, admin_pw):
        _record_login_failure(ip)
        print(f"[Admin] Failed login attempt from {ip}")
        token = _secrets.token_hex(32)
        session["_admin_csrf"] = token
        return Response(
            _ADMIN_LOGIN_HTML.replace('{err}','<div class="err">Incorrect password.</div>').replace('{csrf}', token),
            status=401, content_type="text/html")
    session["admin"] = True
    session.pop("_admin_csrf", None)
    next_url = _safe_next(request.args.get("next", "/admin/users"), "/admin/users")
    return redirect(next_url)

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")

def _is_admin() -> bool:
    """Check if the current request has admin access.
    Two paths: (1) explicit admin session from /admin/login password form,
    (2) logged-in Google user whose email matches ADMIN_EMAIL env var.

    IMPORTANT: the email check requires google_id to be set (i.e. the account
    must have authenticated via Google, not just claimed the email at registration).
    Without this guard, any user who registers with ADMIN_EMAIL as their
    self-reported email would pass the check."""
    if bool(session.get("admin")) and bool(APP_PASSWORD):
        return True
    if ADMIN_EMAIL:
        user_id = session.get("user_id")
        if user_id:
            user = _user_by_id(user_id)
            if (user
                    and user.get("google_id")   # must have authenticated via Google
                    and (user.get("email") or "").strip().lower() == ADMIN_EMAIL):
                return True
    return False

def _require_admin():
    """Return a 403 or redirect if not admin, else None.
    Normal path: log in via Google on the main app → admin footer link appears.
    Break-glass: /admin/login still exists for password-based access if Google auth breaks."""
    if _is_admin():
        return None
    # If not logged in at all, send to main app login
    if not session.get("user_id"):
        return redirect("/")
    # Logged in but not admin — show a plain 403
    return Response(
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>403</title>"
        "<style>body{background:#111;color:#888;font-family:monospace;display:flex;"
        "align-items:center;justify-content:center;height:100vh;margin:0}"
        ".box{text-align:center}.box h1{color:#fff;font-size:1.4rem;margin-bottom:8px}"
        ".box a{color:#666;font-size:.85rem}</style></head>"
        "<body><div class='box'><h1>403 — Not authorized</h1>"
        "<a href='/'>← Back to app</a></div></body></html>",
        status=403, content_type="text/html"
    )

def _require_admin_api():
    """For POST API endpoints — return a JSON 401 if not admin, else None."""
    if _is_admin():
        return None
    return jsonify({"error": "Unauthorized"}), 401

def _admin_page_csrf() -> str:
    """Return (creating if needed) a CSRF token for admin POST actions.
    The login flow pops the token on success, so post-login pages call this
    to ensure one always exists in the session."""
    import secrets as _secrets
    if not session.get("_admin_csrf"):
        session["_admin_csrf"] = _secrets.token_hex(32)
    return session["_admin_csrf"]

def _check_admin_csrf_header() -> bool:
    """Validate the X-CSRF-Token header against the session token."""
    submitted = request.headers.get("X-CSRF-Token", "")
    expected  = session.get("_admin_csrf") or ""
    if not expected or not submitted:
        return False
    return hmac.compare_digest(submitted, expected)


# (Dead sitewide-password login page + /login + GET /logout removed in v2.14.5 —
#  session["logged_in"] was written there and read nowhere; GET /logout was also a
#  CSRF force-logout. Admin auth lives at /admin/login; user auth is /api/login +
#  Google OAuth. 2026-07 audit L3/E3.)

# ── User account API ──────────────────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
def api_register():
    ip = _client_ip()
    if not _check_login_rate(ip):
        return jsonify({"error": "Too many attempts from this device. Please wait a few minutes."}), 429
    _record_login_failure(ip)   # count every register attempt to limit spam
    data     = request.json or {}
    username = re.sub(r'[^A-Za-z0-9_\-]', '', (data.get("username") or "").strip())
    password = (data.get("password") or "").strip()
    if not username or len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters (letters, numbers, _ -)"}), 400
    if len(username) > 30:
        return jsonify({"error": "Username must be 30 characters or fewer."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if _user_by_username(username):
        return jsonify({"error": "That username is already taken."}), 409
    email = (data.get("email") or "").strip().lower() or None
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        return jsonify({"error": "Please enter a valid email address, or leave it blank."}), 400
    if email:
        with _user_db() as conn:
            if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                return jsonify({"error": "That email is already linked to another account."}), 409
    pw_hash = generate_password_hash(password)
    now     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        cur     = conn.execute(
            "INSERT INTO users (username, email, password_hash, created_at) VALUES (?,?,?,?)",
            (username, email, pw_hash, now)
        )
        user_id = cur.lastrowid
        conn.commit()
    session.permanent = True
    session["user_id"]       = user_id
    session["user_username"] = username
    return jsonify({"status": "registered", "username": username, "google_linked": False, "data": _get_user_data(user_id)})

@app.route("/api/login", methods=["POST"])
def api_login():
    ip = _client_ip()
    if not _check_login_rate(ip):
        return jsonify({"error": "Too many login attempts. Please wait a few minutes and try again."}), 429
    data     = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    user     = _user_by_username(username)
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        _record_login_failure(ip)
        if user and user.get("google_id") and not user.get("password_hash"):
            return jsonify({"error": "This account uses Google sign-in. Please use the 'Sign in with Google' button."}), 401
        return jsonify({"error": "Incorrect username or password."}), 401
    session.permanent = True
    session["user_id"]       = user["id"]
    session["user_username"] = username
    _touch_last_login(user["id"])
    return jsonify({"status": "ok", "username": username, "google_linked": bool(user.get("google_id")), "data": _get_user_data(user["id"])})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id",       None)
    session.pop("user_username", None)
    return jsonify({"status": "logged_out"})

# ── Google OAuth routes ───────────────────────────────────────────────────────

@app.route("/api/auth/google")
def auth_google():
    if not _GOOGLE_OAUTH_ENABLED:
        return "Google Sign-In is not configured.", 501
    import secrets as _sec, urllib.parse as _up
    from flask import url_for
    # Prevent open redirect — only allow same-site relative paths (also blocks the
    # '/\\host' backslash trick that browsers turn into a protocol-relative '//host').
    next_url     = _safe_next(request.args.get("next", "/"), "/")
    redirect_uri = url_for("auth_google_callback", _external=True)
    # v2.22.1: purpose=delete = re-confirm the SIGNED-IN user before self-service
    # account deletion. Bound to that user id; the callback never logs anyone in for it.
    purpose = "delete" if request.args.get("purpose") == "delete" else ""
    if purpose and not session.get("user_id"):
        return redirect("/?account_delete=failed")
    # Store state server-side — avoids session cookie issues on Railway proxy
    state = _sec.token_urlsafe(32)
    _oauth_pending[state] = {"next_url": next_url, "expires": time.time() + 600,
                             "purpose": purpose, "uid": session.get("user_id") if purpose else None}
    # Purge expired states
    now_t = time.time()
    for k in [k for k, v in list(_oauth_pending.items()) if v["expires"] < now_t]:
        _oauth_pending.pop(k, None)
    params = {
        "client_id":     _GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
    }
    if purpose:
        params["prompt"] = "select_account"   # make them actively pick the account again
        params["max_age"] = "0"               # and ask Google to re-authenticate
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + _up.urlencode(params))

@app.route("/api/auth/google/callback")
def auth_google_callback():
    import urllib.parse as _up, traceback as _tb
    try:
        return _auth_google_callback_inner()
    except Exception as exc:
        print(f"[Google OAuth] UNHANDLED:\n{_tb.format_exc()}")
        return redirect("/?google_error=1")

def _auth_google_callback_inner():
    if not _GOOGLE_OAUTH_ENABLED:
        return "Google Sign-In is not configured.", 501
    import urllib.parse as _up
    from flask import url_for

    state = request.args.get("state", "")
    code  = request.args.get("code",  "")

    # Validate state against server-side dict (no session needed)
    pending = _oauth_pending.pop(state, None)
    if not pending or pending["expires"] < time.time():
        return redirect("/?google_error=1")
    next_url     = pending["next_url"]
    redirect_uri = url_for("auth_google_callback", _external=True)

    try:
        # Exchange code for token directly — no authlib session dependency
        token_resp = http.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     _GOOGLE_CLIENT_ID,
            "client_secret": _GOOGLE_CLIENT_SECRET,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=10)
        token_data   = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise ValueError(f"No access_token: {token_data}")
        # Fetch user info
        ui_resp  = http.get("https://www.googleapis.com/oauth2/v3/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        userinfo       = ui_resp.json()
        google_id      = userinfo.get("sub", "")
        email          = (userinfo.get("email") or "").strip().lower()
        email_verified = bool(userinfo.get("email_verified", False))
        name           = (userinfo.get("name") or "").strip()
    except Exception as exc:
        print(f"[Google OAuth] token/userinfo error: {exc}")
        return redirect("/?google_error=1")

    if not google_id:
        return redirect("/?google_error=1")

    # v2.22.1: re-confirmation for self-service account deletion. Only marks the
    # session as confirmed (10 min) when the Google account is the one linked to
    # the signed-in user; the deletion itself still needs the user's final click.
    if pending.get("purpose") == "delete":
        uid = session.get("user_id")
        user = _user_by_id(uid) if uid else None
        if (not user or uid != pending.get("uid") or not user.get("google_id")
                or not hmac.compare_digest(str(user["google_id"]), str(google_id))):
            return redirect("/?account_delete=failed")
        session["delete_ok"] = {"uid": uid, "exp": time.time() + 600}
        return redirect("/?account_delete=confirm")

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1) Already linked by google_id → just log in
    user = _user_by_google_id(google_id)
    if user:
        session.permanent        = True
        session["user_id"]       = user["id"]
        session["user_username"] = user["username"]
        _touch_last_login(user["id"])
        return redirect(next_url)

    # 2) Email matches an existing account → link & log in
    # Require email_verified to prevent account takeover via unverified Google emails
    if email and email_verified:
        user = _user_by_email(email)
        if user:
            try:
                with _user_db() as conn:
                    conn.execute("UPDATE users SET google_id=? WHERE id=?", (google_id, user["id"]))
                    conn.commit()
            except Exception as exc:
                print(f"[Google OAuth] DB link error: {exc}")
            session.permanent        = True
            session["user_id"]       = user["id"]
            session["user_username"] = user["username"]
            _touch_last_login(user["id"])
            return redirect(next_url)

    # 3) New Google user → create account
    # password_hash='' (not None) so existing DBs with NOT NULL constraint don't error
    username = _gen_google_username(name or (email.split("@")[0] if email else "user"))
    try:
        with _user_db() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, google_id, created_at) VALUES (?,?,?,?,?)",
                (username, email or None, "", google_id, now)
            )
            user_id = cur.lastrowid
            conn.commit()
    except Exception as exc:
        print(f"[Google OAuth] DB insert error: {exc}")
        return redirect("/?google_error=1")
    session.permanent        = True
    session["user_id"]       = user_id
    session["user_username"] = username
    _touch_last_login(user_id)
    # Flag new Google users so the frontend shows the welcome/setup modal
    sep = "&" if "?" in next_url else "?"
    return redirect(next_url + sep + "google_new=1")

@app.route("/api/account/delete", methods=["POST"])
def api_account_delete():
    """v2.22.1 self-service account deletion — immediate and permanent (Chuck,
    2026-10-07). Confirmed by the account password, or by a fresh Google
    re-sign-in (session["delete_ok"], set by the Google callback, 10 min).
    Everything goes through _purge_user_rows (all per-user tables + users)."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Please sign in first."}), 401
    user = _user_by_id(uid)
    if not user:
        session.pop("user_id", None); session.pop("user_username", None)
        return jsonify({"error": "Account not found."}), 404
    ok_tok = session.get("delete_ok") or {}
    google_ok = ok_tok.get("uid") == uid and float(ok_tok.get("exp") or 0) > time.time()
    if not google_ok:
        ip = _client_ip()
        if not _check_login_rate(ip):
            return jsonify({"error": "Too many attempts. Please wait a few minutes and try again."}), 429
        password = str((request.json or {}).get("password") or "")
        if not user.get("password_hash"):
            return jsonify({"error": "Please confirm with Google first."}), 400
        if not password or not check_password_hash(user["password_hash"], password.strip()):
            _record_login_failure(ip)
            return jsonify({"error": "That password isn't right."}), 401
    with _user_db() as conn:
        _purge_user_rows(conn, uid)
        conn.commit()
    for k in ("user_id", "user_username", "delete_ok"):
        session.pop(k, None)
    print(f"[account] user {uid} deleted their account (self-service, {'google' if google_ok else 'password'})")
    return jsonify({"ok": True})

@app.route("/api/auth/config")
def auth_config():
    return jsonify({"google_oauth": _GOOGLE_OAUTH_ENABLED})

@app.route("/api/me")
def api_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"logged_in": False})
    user = _user_by_id(user_id)
    return jsonify({
        "logged_in":    True,
        "username":     session.get("user_username", ""),
        "google_linked": bool(user and user.get("google_id")),
        "has_password": bool(user and user.get("password_hash")),   # v2.22.1 (account deletion confirm)
        "has_email":    bool(user and user.get("email")),
        "is_admin":     _is_admin(),
        "data":         _get_user_data(user_id),
    })

@app.route("/api/sync", methods=["POST"])
def api_sync():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in."}), 401
    data   = request.json or {}
    kwargs = {}
    for field in ("watchlist", "keywords", "favorites", "new_ids", "saved_searches"):
        if field in data:
            kwargs[field] = data[field]
    if "last_run" in data:
        kwargs["last_run"] = data["last_run"]
    if "last_anchor" in data:
        kwargs["last_anchor"] = data["last_anchor"] or ""
    if kwargs:
        _set_user_data(user_id, **kwargs)
    return jsonify({"status": "synced"})

@app.route("/api/setup-google-account", methods=["POST"])
def api_setup_google_account():
    """Set/change username after Google sign-in, optionally importing an existing account."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in."}), 401
    ip = _client_ip()
    if not _check_login_rate(ip):
        return jsonify({"error": "Too many attempts. Please wait a few minutes."}), 429
    data         = request.json or {}
    new_username = re.sub(r'[^A-Za-z0-9_\-]', '', (data.get("username") or "").strip())
    import_pw    = (data.get("import_password") or "").strip()
    if not new_username or len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters (letters, numbers, _ -)"}), 400
    if len(new_username) > 30:
        return jsonify({"error": "Username must be 30 characters or fewer."}), 400
    existing = _user_by_username(new_username)
    if existing and existing["id"] != user_id:
        # Username belongs to another account — need password to import it
        if not import_pw:
            return jsonify({"error": "taken"}), 409
        if not existing.get("password_hash") or not check_password_hash(existing["password_hash"], import_pw):
            _record_login_failure(ip)
            return jsonify({"error": "wrong_password"}), 401
        # Credentials verified — merge old account into current Google account
        old_data = _get_user_data(existing["id"])
        new_data = _get_user_data(user_id)
        merged = {
            "watchlist":      {**new_data["watchlist"], **old_data["watchlist"]},
            "keywords":       old_data["keywords"]       if old_data["keywords"]       else new_data["keywords"],
            "favorites":      list(set(new_data["favorites"] + old_data["favorites"])),
            "saved_searches": old_data["saved_searches"] if old_data["saved_searches"] else new_data["saved_searches"],
            "last_run":       old_data["last_run"]       or new_data["last_run"],
            "new_ids":        old_data["new_ids"]        if old_data["new_ids"]        else new_data["new_ids"],
            "last_anchor":    max(old_data.get("last_anchor", ""), new_data.get("last_anchor", "")),
        }
        _set_user_data(user_id, **merged)
        with _user_db() as conn:
            _purge_user_rows(conn, existing["id"])
            conn.execute("UPDATE users SET username=? WHERE id=?", (new_username, user_id))
            conn.commit()
        session["user_username"] = new_username
        return jsonify({"status": "imported", "username": new_username, "data": _get_user_data(user_id)})
    # Username available (or unchanged) — just update it
    with _user_db() as conn:
        conn.execute("UPDATE users SET username=? WHERE id=?", (new_username, user_id))
        conn.commit()
    session["user_username"] = new_username
    return jsonify({"status": "ok", "username": new_username, "data": _get_user_data(user_id)})

_ADMIN_NAV_LINKS = [
    ("/admin/users",            "👤 Users"),
    ("/admin/alerts",           "✉ Alerts"),
    ("/admin/devices",          "📡 Devices"),
    ("/admin/listing-patterns", "📊 Listing Patterns"),
    ("/admin/build-coords",     "🗺 Build Coords"),
    ("/admin/validate-stores",  "✓ Validate Stores"),
]

def _admin_nav(current: str) -> str:
    """Render a top nav bar for admin pages. `current` is the active path."""
    links = []
    for path, label in _ADMIN_NAV_LINKS:
        if path == current:
            links.append(f'<span style="color:#fff;font-weight:700">{label}</span>')
        else:
            links.append(f'<a href="{path}" style="color:#888;text-decoration:none">{label}</a>')
    links_html = ' &nbsp;·&nbsp; '.join(links)
    return (
        '<nav style="background:#1a1a1a;border-bottom:1px solid #2e2e2e;padding:10px 24px;'
        'margin:-24px -24px 28px -24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">'
        f'<a href="/" style="color:#c00;font-weight:700;text-decoration:none;margin-right:8px;font-size:.85rem">← App</a>'
        f'<span style="color:#333">|</span>'
        f'<span style="font-size:.82rem">{links_html}</span>'
        '</nav>'
    )


@app.route("/admin/devices")
def admin_devices():
    """Session-protected device access summary page."""
    denied = _require_admin()
    if denied:
        return denied

    # Parse log
    entries = []
    if _DEVICE_LOG.exists():
        for line in _DEVICE_LOG.read_text().splitlines():
            line = line.strip()
            if line:
                try: entries.append(json.loads(line))
                except: pass

    # Aggregate
    from collections import defaultdict
    unique_devices  = {e["device_id"] for e in entries}
    by_device       = defaultdict(list)
    by_date         = defaultdict(set)
    for e in entries:
        by_device[e["device_id"]].append(e)
        by_date[e["date"]].add(e["device_id"])

    rows = []
    for did, evts in sorted(by_device.items(), key=lambda x: x[1][-1]["date"], reverse=True):
        last  = evts[-1]
        first = evts[0]
        ua    = last.get("ua", "")
        # Guess platform
        if "iPhone" in ua or "iPad" in ua:    platform = "📱 iOS"
        elif "Android" in ua:                  platform = "📱 Android"
        elif "Macintosh" in ua:                platform = "💻 Mac"
        elif "Windows" in ua:                  platform = "🖥 Windows"
        elif "Linux" in ua:                    platform = "🖥 Linux"
        else:                                  platform = "❓ Unknown"
        rows.append({
            "id":       did[:8] + "…",
            "platform": platform,
            "first":    first["date"],
            "last":     last["date"] + " " + last["time"],
            "days":     len(evts),
            "ip":       last.get("ip", ""),
        })

    # Daily active table
    daily = sorted(by_date.items(), reverse=True)[:30]

    html  = ['<!DOCTYPE html><html><head><meta charset="UTF-8">']
    html += ['<title>Device Log</title>']
    html += ['<style>body{background:#111;color:#ddd;font-family:monospace;padding:24px;font-size:.88rem}']
    html += ['h1{color:#fff;margin-bottom:4px}h2{color:#aaa;font-size:1rem;margin:24px 0 8px}']
    html += ['table{border-collapse:collapse;width:100%;max-width:900px}']
    html += ['th{background:#1e1e1e;padding:8px 12px;text-align:left;border-bottom:2px solid #333;color:#aaa}']
    html += ['td{padding:6px 12px;border-bottom:1px solid #222}tr:hover td{background:#1a1a1a}']
    html += ['</style></head><body>']
    html += [_admin_nav('/admin/devices')]
    html += [f'<h1>📊 Device Tracker</h1>']
    html += [f'<p style="color:#666">{len(unique_devices)} unique devices &nbsp;·&nbsp; {len(entries)} total day-visits &nbsp;·&nbsp; {len(entries) and entries[-1]["date"]} last activity</p>']

    html += ['<h2>All Devices</h2><table>']
    html += ['<tr><th>ID</th><th>Platform</th><th>First seen</th><th>Last seen</th><th>Days active</th><th>IP</th></tr>']
    for r in rows:
        html += [f'<tr><td>{_html.escape(str(r["id"]))}</td><td>{_html.escape(str(r["platform"]))}</td><td>{_html.escape(str(r["first"]))}</td>'
                 f'<td>{_html.escape(str(r["last"]))}</td><td>{int(r["days"])}</td><td>{_html.escape(str(r["ip"]))}</td></tr>']
    html += ['</table>']

    html += ['<h2>Daily Active Devices (last 30 days)</h2><table>']
    html += ['<tr><th>Date</th><th>Unique devices</th></tr>']
    for date, devs in daily:
        html += [f'<tr><td>{date}</td><td>{len(devs)}</td></tr>']
    html += ['</table></body></html>']

    return Response("".join(html), content_type="text/html")


def _admin_task_page(title: str, api_path: str, description: str,
                     options_html: str = "", nav_current: str = "") -> str:
    """Shared HTML template for long-running admin task pages (build-coords,
    validate-stores).

    CSP note (v2.16.19): this template used to have its Run/SSE-progress logic as an
    inline <script> with onclick="run()" baked directly into the returned HTML. CSP's
    script-src is 'self' only (no 'unsafe-inline', no nonce) -- the browser silently
    refused to run that inline script AND the inline onclick handler, so "Run Now" did
    nothing on every page built from this template, since well before this was
    noticed. No console error appears for a blocked inline *handler* failing to
    attach (only for a blocked <script> tag's content), which is how it went
    undetected. Fixed by moving the logic to static/admin-task.js (same-origin, so
    'self' allows it) and passing config via data-api-path on #run-btn instead of
    templating raw JS -- matches the data-*-attribute + external-JS pattern every
    other onclick="..." in this app was already converted to back in v2.10.18.

    options_html: optional HTML snippet inserted above the Run button (e.g.
    checkboxes). The one existing use -- Build Coords' "force re-geocode" checkbox --
    must use id="force-cb": static/admin-task.js looks for that specific id and
    includes {force: <checked>} in the POST body if present. There's no generic
    extra-field mechanism; add one in admin-task.js if a second page ever needs a
    different field.
    Auth is handled by the caller via _require_admin().
    """
    safe_api  = api_path.replace('"', '&quot;')
    safe_title = title.replace('<', '').replace('>', '')
    safe_desc  = description.replace('<', '').replace('>', '')
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{safe_title} — GC Tracker Admin</title>
<style>
  body{{background:#111;color:#ddd;font-family:monospace;padding:40px;max-width:800px}}
  h2{{color:#eee;margin-bottom:4px}} p{{color:#888;margin-top:0}}
  button{{padding:8px 20px;background:#c00;color:#fff;border:none;border-radius:5px;
          font-size:1rem;cursor:pointer;margin-top:16px}}
  button:disabled{{background:#555;cursor:default}}
  #log{{margin-top:20px;background:#1a1a1a;border:1px solid #333;border-radius:6px;
        padding:16px;min-height:120px;white-space:pre-wrap;font-size:.82rem;line-height:1.5}}
  .done{{color:#4ade80}} .err{{color:#f88}}
</style></head><body>
{_admin_nav(nav_current)}
<h2>🛠 {safe_title}</h2>
<p>{safe_desc}</p>
{options_html}
<button id="run-btn" data-api-path="{safe_api}">▶ Run Now</button>
<div id="log">Waiting…</div>
<script src="/static/admin-task.js"></script>
</body></html>"""


@app.route("/admin/build-coords")
def admin_build_coords():
    """Admin page to geocode all stores and build gc_store_coords.json."""
    denied = _require_admin()
    if denied:
        return denied
    html = _admin_task_page(
        title="Build Store Coordinates",
        api_path="/api/build-store-coords",
        description="Pulls 'storeName' (e.g. 'South Austin, TX') from Algolia for each "
                    "active store, then geocodes that string via Nominatim (~1 req/sec). "
                    "Takes ~5 min. Skips stores already in gc_store_coords.json unless "
                    "'Force re-geocode all' is checked.",
        options_html='<label style="display:block;margin-top:14px;color:#bbb;cursor:pointer">'
                     '<input type="checkbox" id="force-cb" style="vertical-align:middle"> '
                     'Force re-geocode all stores (even cached ones)</label>',
        nav_current="/admin/build-coords",
    )
    return Response(html, content_type="text/html")


@app.route("/admin/validate-stores")
def admin_validate_stores():
    """Admin page to validate and clean up the store list."""
    denied = _require_admin()
    if denied:
        return denied
    html = _admin_task_page(
        title="Validate Stores",
        api_path="/api/validate-stores",
        description="Checks every store for 404s, auto-removes dead stores, "
                    "renames any whose slugs changed, then rebuilds the store list from GC live data. "
                    "Takes ~0.5s per store.",
        nav_current="/admin/validate-stores",
    )
    return Response(html, content_type="text/html")


@app.route("/admin/users")
def admin_users():
    """Session-protected user account summary page."""
    denied = _require_admin()
    if denied:
        return denied

    # Auto-purge any users whose scheduled deletion date has passed
    from datetime import timezone as _tz
    now_iso = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        due = [r["id"] for r in conn.execute(
            "SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at <= ?", (now_iso,)
        ).fetchall()]
        for uid_del in due:
            _purge_user_rows(conn, uid_del)
        if due:
            conn.commit()

    # Load all users + their data
    with _user_db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT u.id, u.username, u.created_at, u.deleted_at, u.alerts_beta, "
            "       u.last_login, d.last_run, d.updated_at "
            "FROM users u "
            "LEFT JOIN user_data d ON d.user_id = u.id "
            "ORDER BY u.created_at DESC"
        ).fetchall()]

        # Count watchlist/keyword items per user
        # Cross-reference watchlist against the catalog so SKUs we've never seen
        # don't inflate the count. (v2.16.46, Phase F 5a) Catalog = Postgres
        # items (every SKU ever synced, available or not — same set the JSON
        # catalog held); one query for all users' SKUs.
        _rows = {}
        for u in users:
            _rows[u["id"]] = conn.execute(
                "SELECT watchlist, keywords, favorites FROM user_data WHERE user_id=?",
                (u["id"],)
            ).fetchone()
        _wl_by_user = {}
        for _uid, row in _rows.items():
            try:
                _wl_by_user[_uid] = list(json.loads(row["watchlist"] or "{}")) if row else []
            except Exception:
                _wl_by_user[_uid] = None
        _all_wl = sorted({s for v in _wl_by_user.values() if v for s in v})
        _known = set()
        if _all_wl:
            def _q():
                with _pg_conn() as pconn:
                    with pconn.cursor() as cur:
                        cur.execute("SELECT sku FROM items WHERE sku = ANY(%s)", (_all_wl,))
                        return {r[0] for r in cur.fetchall()}
            try:
                _known = _pg_read(_q)
            except Exception as e:
                print(f"[pg] admin users watchlist lookup failed: {type(e).__name__}: {e}")
        for u in users:
            row = _rows[u["id"]]
            if row:
                _wl = _wl_by_user.get(u["id"])
                u["wl_count"] = sum(1 for sku in _wl if sku in _known) if _wl is not None else 0
                try: u["kw_count"]  = len(json.loads(row["keywords"]  or "[]"))
                except: u["kw_count"] = 0
                try: u["fav_count"] = len(json.loads(row["favorites"] or "[]"))
                except: u["fav_count"] = 0
            else:
                u["wl_count"] = u["kw_count"] = u["fav_count"] = 0

    def _fmt(ts):
        if not ts: return "—"
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            return dt.strftime("%b %d, %Y  %H:%M UTC")
        except: return ts

    csrf_token = _admin_page_csrf()
    # v2.22.0: alerts state per user (never the address).
    with _user_db() as conn:
        _al_conf = {r["user_id"] for r in conn.execute(
            "SELECT user_id FROM alert_email WHERE confirmed_at IS NOT NULL").fetchall()}
        _al_set = {r["user_id"]: dict(r) for r in conn.execute(
            "SELECT user_id, paused, suppressed FROM alert_settings").fetchall()}
        _al_code = {r["user_id"] for r in conn.execute("SELECT user_id FROM alert_codes").fetchall()}
    rows_html = ""
    for u in users:
        last_scan      = _fmt(u.get("last_run"))
        joined         = _fmt(u.get("created_at"))
        last_login_fmt = _fmt(u.get("last_login"))
        uid            = int(u["id"])
        uname_safe  = _html.escape(str(u["username"]))
        deleted_at  = u.get("deleted_at") or ""
        if deleted_at:
            # Show delete-on date and Cancel / Delete Now options
            try:
                del_dt = datetime.strptime(deleted_at, "%Y-%m-%dT%H:%M:%SZ")
                del_label = del_dt.strftime("Deletes %b %d")
            except Exception:
                del_label = "Scheduled"
            action_html = (
                f'<span style="color:#a05050;font-size:.75rem">{del_label}</span> '
                f'<form method="POST" action="/admin/delete-user" style="display:inline">'
                f'<input type="hidden" name="id" value="{uid}">'
                f'<input type="hidden" name="_csrf" value="{csrf_token}">'
                f'<input type="hidden" name="action" value="cancel">'
                f'<button type="submit" style="background:#1a3a1a;color:#8fc88f;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:.75rem">Undo</button>'
                f'</form> '
                f'<form method="POST" action="/admin/delete-user" style="display:inline"'
                f' onsubmit="return confirm(\'Permanently delete {uname_safe} right now?\')">'
                f'<input type="hidden" name="id" value="{uid}">'
                f'<input type="hidden" name="_csrf" value="{csrf_token}">'
                f'<input type="hidden" name="action" value="now">'
                f'<button type="submit" style="background:#600;color:#fcc;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:.75rem">Delete Now</button>'
                f'</form>'
            )
            row_style = ' style="opacity:.6"'
        else:
            action_html = (
                f'<form method="POST" action="/admin/delete-user" style="display:inline"'
                f' onsubmit="return confirm(\'Schedule {uname_safe} for deletion in 10 days?\')">'
                f'<input type="hidden" name="id" value="{uid}">'
                f'<input type="hidden" name="_csrf" value="{csrf_token}">'
                f'<input type="hidden" name="action" value="schedule">'
                f'<button type="submit" style="background:#600;color:#fcc;border:none;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:.78rem">✕ Delete</button>'
                f'</form>'
            )
            row_style = ''
        rows_html += (
            f'<tr{row_style}>'
            f'<td>{uname_safe}</td>'
            f'<td data-value="{_html.escape(u.get("created_at",""))}">{_html.escape(str(joined))}</td>'
            f'<td data-value="{_html.escape(u.get("last_run","") or "")}">{_html.escape(str(last_scan))}</td>'
            f'<td data-value="{_html.escape(u.get("last_login","") or "")}">{_html.escape(str(last_login_fmt))}</td>'
            f'<td style="text-align:center">{int(u["wl_count"])}</td>'
            f'<td style="text-align:center">{int(u["kw_count"])}</td>'
            f'<td style="text-align:center">{int(u["fav_count"])}</td>'
            f'<td style="text-align:center;white-space:nowrap" data-value="{1 if u.get("alerts_beta") else 0}">{_alerts_admin_cell(u, csrf_token, _al_conf, _al_set, _al_code)}</td>'
            f'<td style="text-align:center">{action_html}</td>'
            f'</tr>'
        )

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Users</title>
<style>
body{{background:#111;color:#ddd;font-family:monospace;padding:24px;font-size:.88rem}}
h1{{color:#fff;margin-bottom:4px}}
.stat{{color:#eee;font-size:1.1rem;font-weight:bold;margin-bottom:6px}}
.sub{{color:#666;margin-bottom:20px;font-size:.82rem}}
table{{border-collapse:collapse;width:100%;max-width:1100px}}
th{{background:#1e1e1e;padding:8px 14px;text-align:left;border-bottom:2px solid #333;color:#aaa}}
th[data-col]:not([data-col="-1"]){{cursor:pointer;user-select:none}}
th[data-col]:not([data-col="-1"]):hover{{color:#fff}}
th.sort-asc::after{{content:" ↑";color:#7af}}
th.sort-desc::after{{content:" ↓";color:#7af}}
td{{padding:7px 14px;border-bottom:1px solid #222}}
tr:hover td{{background:#1a1a1a}}
a{{color:#888;text-decoration:none;font-size:.78rem}}
</style>
<script src="/static/admin.js"></script>
</head><body>
{_admin_nav('/admin/users')}
<h1>👤 User Accounts</h1>
<div class="stat">{len(users)} user{"s" if len(users) != 1 else ""}</div>
<div class="sub"><a href="/admin/devices">→ Device log</a></div>
<table>
<tr>
  <th data-col="0">Username</th>
  <th data-col="1">Joined</th>
  <th data-col="2">Last scan</th>
  <th data-col="3">Last login</th>
  <th data-col="4" style="text-align:center">Watch</th>
  <th data-col="5" style="text-align:center">Want</th>
  <th data-col="6" style="text-align:center">Favs</th>
  <th data-col="7" style="text-align:center">Alerts</th>
  <th data-col="-1"></th>
</tr>
{rows_html if rows_html else '<tr><td colspan="9" style="color:#555;padding:20px">No accounts yet.</td></tr>'}
</table>
</body></html>"""

    return Response(html, mimetype="text/html")


def _alerts_admin_cell(u: dict, csrf: str, confirmed: set, settings: dict, codes: set) -> str:
    """/admin/users "Alerts" cell: beta switch + state (no address). v2.22.0."""
    uid = int(u["id"])
    on = bool(u.get("alerts_beta"))
    st = settings.get(uid) or {}
    if uid in confirmed:
        state = ("bounced" if st.get("suppressed") else "paused" if st.get("paused") else "on")
    elif uid in codes:
        state = "code sent"
    else:
        state = "no email"
    color = {"on": "#4ade80", "paused": "#e8c060", "bounced": "#e88", "code sent": "#9cf"}.get(state, "#777")
    btn = ("background:#1a3a1a;color:#8fc88f" if not on else "background:#3a2a1a;color:#e8c060")
    return (f'<span style="color:{color};font-size:.75rem">{state}</span> '
            f'<form method="POST" action="/admin/alerts-beta" style="display:inline">'
            f'<input type="hidden" name="id" value="{uid}">'
            f'<input type="hidden" name="_csrf" value="{_html.escape(csrf)}">'
            f'<input type="hidden" name="on" value="{0 if on else 1}">'
            f'<button type="submit" style="{btn};border:none;border-radius:4px;padding:2px 8px;cursor:pointer;'
            f'font-size:.75rem">{"Beta ✓ — turn off" if on else "Add to beta"}</button></form>')


@app.route("/admin/alerts-beta", methods=["POST"])
def admin_alerts_beta():
    """Turn one account's alerts beta flag on/off (v2.22.0). Off = that account
    stops getting alert emails at the next run (_alerts_subscribers skips
    non-beta accounts) and loses the Email alerts panel; its address and
    settings are kept, so turning it back on resumes where it was."""
    denied = _require_admin()
    if denied:
        return denied
    submitted = request.form.get("_csrf", "")
    expected  = session.get("_admin_csrf") or ""
    if not expected or not submitted or not hmac.compare_digest(submitted, expected):
        return Response("Invalid CSRF token — go back and reload the page.", status=403, content_type="text/plain")
    try:
        user_id = int(request.form.get("id", ""))
    except (ValueError, TypeError):
        return Response("Invalid user id.", status=400, content_type="text/plain")
    if not _user_by_id(user_id):
        return Response("User not found.", status=404, content_type="text/plain")
    on = request.form.get("on") == "1"
    with _user_db() as conn:
        conn.execute("UPDATE users SET alerts_beta=? WHERE id=?", (1 if on else 0, user_id))
        conn.commit()
    print(f"[alerts] beta flag {'on' if on else 'off'} for user {user_id}")
    return redirect("/admin/users")


@app.route("/admin/delete-user", methods=["POST"])
def admin_delete_user():
    """Schedule a user for deletion (soft-delete) or cancel/confirm from the admin panel."""
    denied = _require_admin()
    if denied:
        return denied
    # CSRF: validate form token
    submitted = request.form.get("_csrf", "")
    expected  = session.get("_admin_csrf") or ""
    if not expected or not submitted or not hmac.compare_digest(submitted, expected):
        return Response("Invalid CSRF token — go back and reload the page.", status=403, content_type="text/plain")
    user_id = request.form.get("id", "")
    action  = request.form.get("action", "schedule")  # "schedule" | "cancel" | "now"
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return Response("Invalid user id.", status=400, content_type="text/plain")
    user = _user_by_id(user_id)
    if not user:
        return Response("User not found.", status=404, content_type="text/plain")
    with _user_db() as conn:
        if action == "now":
            _purge_user_rows(conn, user_id)
        elif action == "cancel":
            conn.execute("UPDATE users SET deleted_at=NULL WHERE id=?", (user_id,))
        else:  # schedule
            from datetime import timezone
            delete_on = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("UPDATE users SET deleted_at=? WHERE id=?", (delete_on, user_id))
    return redirect("/admin/users")


# ── Want List email alerts — step 1 plumbing (v2.18.0) ────────────────────────
# Design: EMAIL_ALERTS_DESIGN.md. Step 1 = Postmark send wrapper, encrypted address
# storage, confirm-by-code flow, admin-only test page. No user-visible UI yet.
#
# PRIVACY RULES for everything in this section (and later alert code):
#   * An address is decrypted in exactly two places: building a Postmark request
#     (send_email's `to`) and the masked hint shown to its own owner.
#   * Never print/log an address, a subject, a code, Postmark's error *message*
#     (it can echo the address) or an exception's text from the send path —
#     log tags, Postmark MessageID / ErrorCode and exception TYPE names only.
#   * Never put an address in a URL, a response other than the owner's masked
#     hint, or an admin page.
#   * Missing/invalid env vars disable alerts; they never stop the app booting
#     (the v2.16.16 crash lesson).
import hashlib as _hashlib
import secrets as _secrets_alerts
try:
    from cryptography.fernet import Fernet as _Fernet, MultiFernet as _MultiFernet, InvalidToken as _InvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

_POSTMARK_TOKEN   = (os.environ.get("POSTMARK_SERVER_TOKEN") or "").strip()
_POSTMARK_URL     = "https://api.postmarkapp.com/email"
_ALERTS_FROM      = (os.environ.get("ALERTS_FROM") or "GC Gear Tracker <alerts@gcgeartracker.com>").strip()
_ALERTS_HMAC_KEY  = (os.environ.get("ALERTS_HMAC_KEY") or "").strip().encode()
try:
    # Hard global ceiling on emails per UTC day (all kinds). Postmark bills overage
    # automatically, so this is the only brake. Default sized for the free
    # developer plan (100/month); raise via env when on Basic (~500).
    _ALERTS_DAILY_CEILING = max(0, int(os.environ.get("ALERTS_DAILY_CEILING") or 50))
except ValueError:
    _ALERTS_DAILY_CEILING = 50

_CODE_TTL_S          = 15 * 60   # confirmation code lifetime
_CODE_MAX_ATTEMPTS   = 5         # wrong guesses per code before it's burned
_CODE_MIN_GAP_S      = 60        # per user: one code email per minute
_CODE_MAX_PER_HOUR   = 5         # per user
_CODE_MAX_PER_ADDR_D = 5         # per address (blind index), per 24 h
_CODE_MAX_GLOBAL_H   = 30        # all users, per hour — caps abuse of the code mailer

def _alerts_build_fernet():
    keys = [k for k in ((os.environ.get("ALERTS_EMAIL_KEY") or "").strip(),
                        (os.environ.get("ALERTS_EMAIL_KEY_OLD") or "").strip()) if k]
    if not keys or not _CRYPTO_AVAILABLE:
        return None
    try:
        return _MultiFernet([_Fernet(k.encode()) for k in keys])  # first key encrypts; all decrypt
    except Exception:
        return None

_ALERTS_FERNET = _alerts_build_fernet()
_ALERTS_MISSING = []
if not _CRYPTO_AVAILABLE:
    _ALERTS_MISSING.append("cryptography package")
if not _POSTMARK_TOKEN:
    _ALERTS_MISSING.append("POSTMARK_SERVER_TOKEN")
if _ALERTS_FERNET is None:
    _ALERTS_MISSING.append("ALERTS_EMAIL_KEY (missing or not a valid Fernet key)")
if len(_ALERTS_HMAC_KEY) < 32:
    _ALERTS_MISSING.append("ALERTS_HMAC_KEY (missing or shorter than 32 chars)")
_ALERTS_READY = not _ALERTS_MISSING
print("[alerts] ready" if _ALERTS_READY else f"[alerts] disabled — missing/invalid: {', '.join(_ALERTS_MISSING)}")

_EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"'.]{2,}$")

def _norm_email(raw: str) -> str:
    return (raw or "").strip().lower()

def _valid_email(e: str) -> bool:
    return 3 <= len(e) <= 254 and bool(_EMAIL_RE.match(e))

def _enc_email(e: str) -> str:
    return _ALERTS_FERNET.encrypt(e.encode()).decode()

def _dec_email(tok: str) -> str | None:
    try:
        return _ALERTS_FERNET.decrypt(tok.encode()).decode()
    except (_InvalidToken, AttributeError, ValueError):
        return None

def _alerts_hmac(purpose: str, msg: str) -> str:
    """Keyed hash with a purpose label so one key can't be replayed across uses."""
    return hmac.new(_ALERTS_HMAC_KEY, f"{purpose}:{msg}".encode(), _hashlib.sha256).hexdigest()

def _email_bidx(e: str) -> str:
    return _alerts_hmac("bidx", _norm_email(e))

def _hash_code(user_id: int, code: str) -> str:
    return _alerts_hmac("code", f"{user_id}:{code}")

def _mask_email(e: str) -> str:
    local, _, domain = (e or "").partition("@")
    if not local or not domain:
        return "•••"
    return f"{local[0]}•••@{domain}"

def _alerts_allowed() -> bool:
    """Who can see/use alerts right now: the admin, or a signed-in account whose
    beta flag (users.alerts_beta, toggled on /admin/users) is on. v2.22.0."""
    uid = session.get("user_id")
    if not uid:
        return False
    if _is_admin():
        return True
    u = _user_by_id(uid)
    return bool(u and u.get("alerts_beta") and not u.get("deleted_at"))

def _alerts_day() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def _alerts_sends_today() -> int:
    with _user_db() as conn:
        row = conn.execute("SELECT count FROM alert_sends WHERE day=?", (_alerts_day(),)).fetchone()
    return int(row["count"]) if row else 0

def _alerts_take_send_slot() -> bool:
    """Atomically count one send against today's ceiling. False = ceiling reached."""
    day = _alerts_day()
    with _user_db() as conn:
        cur = conn.execute(
            "INSERT INTO alert_sends(day, count) VALUES(?, 1) "
            "ON CONFLICT(day) DO UPDATE SET count = count + 1 WHERE count < ?",
            (day, _ALERTS_DAILY_CEILING))
        took = cur.rowcount == 1 and _ALERTS_DAILY_CEILING > 0
        if not took:
            first = conn.execute("SELECT v FROM alert_meta WHERE k='ceiling_hit'").fetchone()
            if not first or first["v"] != day:
                conn.execute("INSERT INTO alert_meta(k, v) VALUES('ceiling_hit', ?) "
                             "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (day,))
                print(f"[alerts] DAILY SEND CEILING REACHED ({_ALERTS_DAILY_CEILING}) — sends paused until 00:00 UTC")
        conn.commit()
    return took

def send_email(to: str, subject: str, text: str, html: str | None = None, *,
               tag: str, stream: str = "outbound", headers: dict | None = None) -> tuple[bool, str]:
    """The ONLY way the app sends email. Returns (ok, short_reason). Never logs
    `to` or `subject`. Open/click tracking always off."""
    if not _ALERTS_READY:
        return False, "not_configured"
    if not _alerts_take_send_slot():
        return False, "ceiling"
    payload = {
        "From": _ALERTS_FROM, "To": to, "Subject": subject, "TextBody": text,
        "MessageStream": stream, "Tag": tag,
        "TrackOpens": False, "TrackLinks": "None",
    }
    if html:
        payload["HtmlBody"] = html
    if headers:
        payload["Headers"] = [{"Name": k, "Value": v} for k, v in headers.items()]
    try:
        r = http.post(_POSTMARK_URL, json=payload, timeout=10, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "X-Postmark-Server-Token": _POSTMARK_TOKEN})
        try:
            body = r.json()
        except ValueError:
            body = {}
        code = body.get("ErrorCode")
        if r.status_code == 200 and code == 0:
            print(f"[alerts] sent tag={tag} id={body.get('MessageID', '?')}")
            return True, "sent"
        print(f"[alerts] send FAILED tag={tag} http={r.status_code} postmark_code={code}")
        return False, f"postmark_{code}"
    except Exception as e:
        print(f"[alerts] send FAILED tag={tag} exc={type(e).__name__}")
        return False, "network"

# ── Confirmation flow (shared by the JSON API and the admin test page) ────────

def _alerts_status(user_id: int) -> dict:
    with _user_db() as conn:
        em  = conn.execute("SELECT email_enc, confirmed_at FROM alert_email WHERE user_id=?", (user_id,)).fetchone()
        st  = conn.execute("SELECT mode, paused, suppressed FROM alert_settings WHERE user_id=?", (user_id,)).fetchone()
        pc  = conn.execute("SELECT expires_at, attempts FROM alert_codes WHERE user_id=?", (user_id,)).fetchone()
    masked = None
    if em and _ALERTS_READY:
        addr = _dec_email(em["email_enc"])
        masked = _mask_email(addr) if addr else "(unreadable — key changed?)"
    pending = bool(pc and pc["expires_at"] > time.time() and pc["attempts"] < _CODE_MAX_ATTEMPTS)
    return {
        "ready":      _ALERTS_READY,
        "confirmed":  bool(em and em["confirmed_at"]),
        "masked":     masked,
        "mode":       (st["mode"] or "all") if st else "all",
        "paused":     bool(st["paused"]) if st else False,
        "suppressed": (st["suppressed"] or None) if st else None,
        "code_pending": pending,
        "code_expires_in": int(pc["expires_at"] - time.time()) if pending else 0,
    }

def _alerts_start(user_id: int, raw_email: str) -> tuple[bool, str, int]:
    """Send a 6-digit code to a NEW address (first setup or change). The address is
    stored only encrypted, in alert_codes, until the code is confirmed."""
    if not _ALERTS_READY:
        return False, "Email alerts aren't configured on the server yet.", 503
    email = _norm_email(raw_email)
    if not _valid_email(email):
        return False, "Please enter a valid email address.", 400
    bidx, now = _email_bidx(email), time.time()
    with _user_db() as conn:
        conn.execute("DELETE FROM alert_code_sends WHERE sent_at < ?", (now - 86400 * 2,))
        last_user = conn.execute("SELECT MAX(sent_at) AS t, "
                                 "SUM(CASE WHEN sent_at > ? THEN 1 ELSE 0 END) AS h "
                                 "FROM alert_code_sends WHERE user_id=?", (now - 3600, user_id)).fetchone()
        per_addr  = conn.execute("SELECT COUNT(*) AS n FROM alert_code_sends WHERE email_bidx=? AND sent_at > ?",
                                 (bidx, now - 86400)).fetchone()["n"]
        global_h  = conn.execute("SELECT COUNT(*) AS n FROM alert_code_sends WHERE sent_at > ?",
                                 (now - 3600,)).fetchone()["n"]
        conn.commit()
    if last_user["t"] and now - last_user["t"] < _CODE_MIN_GAP_S:
        return False, "A code was just sent — please wait a minute before asking for another.", 429
    if (last_user["h"] or 0) >= _CODE_MAX_PER_HOUR or per_addr >= _CODE_MAX_PER_ADDR_D:
        return False, "Too many codes requested. Please try again later.", 429
    if global_h >= _CODE_MAX_GLOBAL_H:
        print("[alerts] global code-send cap hit this hour")
        return False, "Too many codes requested. Please try again later.", 429
    code = f"{_secrets_alerts.randbelow(1_000_000):06d}"
    with _user_db() as conn:
        conn.execute("INSERT INTO alert_code_sends(user_id, email_bidx, sent_at) VALUES(?,?,?)",
                     (user_id, bidx, now))
        conn.execute(
            "INSERT INTO alert_codes(user_id, email_enc, email_bidx, code_hash, expires_at, attempts, sent_at) "
            "VALUES(?,?,?,?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET email_enc=excluded.email_enc, "
            "email_bidx=excluded.email_bidx, code_hash=excluded.code_hash, expires_at=excluded.expires_at, "
            "attempts=0, sent_at=excluded.sent_at",
            (user_id, _enc_email(email), bidx, _hash_code(user_id, code), now + _CODE_TTL_S, now))
        conn.commit()
    text = (f"Your GC Gear Tracker confirmation code is {code}\n\n"
            f"Type it into the box on gcgeartracker.com to turn on email alerts. "
            f"It expires in {_CODE_TTL_S // 60} minutes.\n\n"
            "If you didn't ask for this, ignore this email. You won't get anything else from us "
            "unless the code is entered.\n\n— GC Gear Tracker (gcgeartracker.com)\n")
    html = (f"<p>Your GC Gear Tracker confirmation code is</p>"
            f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:4px;font-family:monospace\">{code}</p>"
            f"<p>Type it into the box on gcgeartracker.com to turn on email alerts. "
            f"It expires in {_CODE_TTL_S // 60} minutes.</p>"
            "<p style=\"color:#666\">If you didn't ask for this, ignore this email. You won't get anything "
            "else from us unless the code is entered.</p>")
    ok, why = send_email(email, f"Your GC Gear Tracker code: {code}", text, html, tag="confirm-code")
    if not ok:
        with _user_db() as conn:  # don't leave a code the user never received
            conn.execute("DELETE FROM alert_codes WHERE user_id=?", (user_id,))
            conn.commit()
        if why == "ceiling":
            return False, "Email sending is paused for today. Please try again tomorrow.", 503
        return False, "We couldn't send the code. Please check the address and try again.", 502
    return True, f"Code sent to {_mask_email(email)}. It expires in {_CODE_TTL_S // 60} minutes.", 200

def _alerts_confirm(user_id: int, raw_code: str) -> tuple[bool, str, int]:
    if not _ALERTS_READY:
        return False, "Email alerts aren't configured on the server yet.", 503
    code = re.sub(r"\D", "", raw_code or "")
    now = time.time()
    with _user_db() as conn:
        row = conn.execute("SELECT * FROM alert_codes WHERE user_id=?", (user_id,)).fetchone()
        if not row or row["expires_at"] < now or row["attempts"] >= _CODE_MAX_ATTEMPTS:
            if row:
                conn.execute("DELETE FROM alert_codes WHERE user_id=?", (user_id,))
                conn.commit()
            return False, "That code has expired. Please request a new one.", 400
        if len(code) != 6 or not hmac.compare_digest(_hash_code(user_id, code), row["code_hash"]):
            conn.execute("UPDATE alert_codes SET attempts = attempts + 1 WHERE user_id=?", (user_id,))
            conn.commit()
            left = _CODE_MAX_ATTEMPTS - row["attempts"] - 1
            return False, (f"That code isn't right. {left} tr{'y' if left == 1 else 'ies'} left."
                           if left > 0 else "Too many wrong codes. Please request a new one."), 400
        stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO alert_email(user_id, email_enc, email_bidx, confirmed_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET email_enc=excluded.email_enc, "
            "email_bidx=excluded.email_bidx, confirmed_at=excluded.confirmed_at, updated_at=excluded.updated_at",
            (user_id, row["email_enc"], row["email_bidx"], stamp, stamp, stamp))
        # v2.19.0: a new subscriber's first alert covers only listings after now
        # (anchor = newest available date_listed), never the back catalog. A
        # changed address keeps its existing settings row (INSERT OR IGNORE).
        conn.execute("INSERT OR IGNORE INTO alert_settings(user_id, frequency, paused, mode, anchor, checked_at, updated_at) "
                     "VALUES(?, 'daily', 0, 'all', ?, ?, ?)", (user_id, _alerts_initial_anchor(), stamp, stamp))
        # v2.22.0: a newly confirmed address replaces one that bounced.
        conn.execute("UPDATE alert_settings SET suppressed=NULL WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM alert_codes WHERE user_id=?", (user_id,))
        conn.commit()
    return True, "Email confirmed.", 200

def _alerts_set_paused(user_id: int, pause: bool) -> tuple[bool, str, int]:
    with _user_db() as conn:
        em = conn.execute("SELECT 1 FROM alert_email WHERE user_id=? AND confirmed_at IS NOT NULL",
                          (user_id,)).fetchone()
    if not em:
        return False, "Add and confirm an email address first.", 400
    _alerts_apply_pause(user_id, pause)
    return True, ("Alerts paused." if pause else "Alerts are on."), 200


def _alerts_apply_pause(user_id: int, pause: bool) -> None:
    """Pause or resume one user's alerts. Resuming starts from now: anchor =
    newest listing, checked_at = now, so no backlog of items or price drops."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        conn.execute("INSERT OR IGNORE INTO alert_settings(user_id, frequency, paused, mode, updated_at) "
                     "VALUES(?, 'daily', 0, 'all', ?)", (user_id, now))
        conn.execute("UPDATE alert_settings SET paused=?, updated_at=? WHERE user_id=?",
                     (1 if pause else 0, now, user_id))
        if not pause:
            conn.execute("UPDATE alert_settings SET anchor=?, checked_at=? WHERE user_id=?",
                         (_alerts_initial_anchor(), now, user_id))
        conn.commit()


def _alerts_test_send(user_id: int) -> tuple[bool, str, int]:
    if not _ALERTS_READY:
        return False, "Email alerts aren't configured on the server yet.", 503
    with _user_db() as conn:
        em = conn.execute("SELECT email_enc FROM alert_email WHERE user_id=? AND confirmed_at IS NOT NULL",
                          (user_id,)).fetchone()
    addr = _dec_email(em["email_enc"]) if em else None
    if not addr:
        return False, "No confirmed email on this account.", 400
    text = ("This is a test email from GC Gear Tracker. If you're reading it, your daily alerts "
            "can reach you.\n\n— GC Gear Tracker (gcgeartracker.com)\n")
    html = ("<p>This is a test email from GC Gear Tracker. If you're reading it, your daily alerts "
            "can reach you.</p><p style=\"color:#666\">— GC Gear Tracker (gcgeartracker.com)</p>")
    ok, why = send_email(addr, "GC Gear Tracker test email", text, html, tag="test")
    if ok:
        return True, f"Test email sent to {_mask_email(addr)}.", 200
    if why == "ceiling":
        return False, "Email sending is paused for today (daily limit reached).", 503
    return False, f"Send failed ({why}).", 502

def _alerts_remove(user_id: int) -> tuple[bool, str, int]:
    """'Remove my email': the address and everything tied to it, gone.
    alert_code_sends is deliberately kept: it's the code-mailer rate-limit log
    (user id + keyed hash, no address), pruned after 48 h — deleting it here would
    let remove → re-add reset the limits. Account deletion still purges it."""
    with _user_db() as conn:
        for t in ("alert_email", "alert_codes", "alert_settings", "alert_pills", "alert_sent", "alert_batches",
                  "alert_drop_sent"):
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (user_id,))
        conn.commit()
    return True, "Your email address has been removed.", 200

# ── JSON API (used by the step-3 UI; admin-only until the beta) ───────────────

def _alerts_json(result):
    ok, msg, status = result
    return jsonify({"ok": ok, "message": msg, **({} if ok else {"error": msg})}), status

@app.route("/api/alerts/status")
def api_alerts_status():
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return jsonify(_alerts_status(session["user_id"]))

@app.route("/api/alerts/email/start", methods=["POST"])
def api_alerts_email_start():
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return _alerts_json(_alerts_start(session["user_id"], (request.json or {}).get("email", "")))

@app.route("/api/alerts/email/confirm", methods=["POST"])
def api_alerts_email_confirm():
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return _alerts_json(_alerts_confirm(session["user_id"], str((request.json or {}).get("code", ""))))

@app.route("/api/alerts/test-send", methods=["POST"])
def api_alerts_test_send():
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return _alerts_json(_alerts_test_send(session["user_id"]))

@app.route("/api/alerts/pause", methods=["POST"])
def api_alerts_pause():
    """v2.22.0 settings panel: {"paused": true|false}. Turning alerts back on
    starts fresh from now (no backlog), same as the email's resume link."""
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return _alerts_json(_alerts_set_paused(session["user_id"], bool((request.json or {}).get("paused"))))

@app.route("/api/alerts/email/remove", methods=["POST"])
def api_alerts_email_remove():
    if not _alerts_allowed():
        return jsonify({"error": "Not found"}), 404
    return _alerts_json(_alerts_remove(session["user_id"]))

# ── Admin test page (plain forms, no JS — CSP-safe) ───────────────────────────

@app.route("/admin/alerts", methods=["GET", "POST"])
def admin_alerts():
    denied = _require_admin()
    if denied:
        return denied
    uid = session.get("user_id")
    preview = ""
    if request.method == "POST":
        submitted = request.form.get("_csrf", "")
        expected  = session.get("_admin_csrf") or ""
        if not expected or not submitted or not hmac.compare_digest(submitted, expected):
            return Response("Invalid CSRF token — go back and reload the page.", status=403, content_type="text/plain")
        if not uid:
            session["_alerts_flash"] = "Log in with your Google admin account (not the break-glass password) to test."
            return redirect("/admin/alerts")
        action = request.form.get("action", "")
        if action == "start":
            res = _alerts_start(uid, request.form.get("email", ""))
        elif action == "confirm":
            res = _alerts_confirm(uid, request.form.get("code", ""))
        elif action == "test":
            res = _alerts_test_send(uid)
        elif action == "remove":
            res = _alerts_remove(uid)
        elif action == "preview":       # v2.19.0: rendered inline (too big for the session cookie)
            _msg, preview = _alerts_admin_preview(uid)
            session["_alerts_flash"] = "✓ " + _msg
            res = None
        elif action == "send_mine":
            res = (True, _alerts_admin_send_mine(uid), 200)
        elif action == "rewind":
            res = (True, _alerts_admin_rewind(uid), 200)
        elif action in ("switch_on", "switch_off"):
            _alerts_meta_set("global_switch", "on" if action == "switch_on" else "off")
            res = (True, f"Global switch {'ON' if action == 'switch_on' else 'OFF'}.", 200)
        else:
            res = (False, "Unknown action.", 400)
        if res is not None:
            session["_alerts_flash"] = ("✓ " if res[0] else "✕ ") + res[1]
            return redirect("/admin/alerts")

    flash = session.pop("_alerts_flash", "")
    csrf  = _admin_page_csrf()
    st    = _alerts_status(uid) if uid else None
    yn    = lambda b: '<span style="color:#8fc88f">yes</span>' if b else '<span style="color:#e88">NO</span>'
    env_rows = "".join(
        f"<tr><td>{_html.escape(name)}</td><td>{yn(ok)}</td></tr>" for name, ok in (
            ("cryptography package", _CRYPTO_AVAILABLE),
            ("POSTMARK_SERVER_TOKEN set", bool(_POSTMARK_TOKEN)),
            ("ALERTS_EMAIL_KEY valid", _ALERTS_FERNET is not None),
            ("ALERTS_HMAC_KEY ≥ 32 chars", len(_ALERTS_HMAC_KEY) >= 32),
        ))
    if st:
        me = (f"Confirmed address: <b>{_html.escape(st['masked'] or '—')}</b>" if st["confirmed"]
              else "No confirmed address.")
        if st["code_pending"]:
            me += f" &nbsp;·&nbsp; code pending, expires in {st['code_expires_in'] // 60} min"
    else:
        me = "Not logged in as a site user (break-glass admin login) — the forms need a Google admin login."
    def form(action, inner, label, color="#253"):
        return (f'<form method="POST" action="/admin/alerts" style="margin:10px 0">'
                f'<input type="hidden" name="_csrf" value="{csrf}">'
                f'<input type="hidden" name="action" value="{action}">{inner}'
                f'<button type="submit" style="background:{color};color:#eee;border:none;border-radius:4px;'
                f'padding:5px 14px;cursor:pointer;font-family:monospace">{label}</button></form>')
    inp = ('style="background:#1a1a1a;color:#eee;border:1px solid #333;border-radius:4px;padding:5px 8px;'
           'font-family:monospace;margin-right:8px;width:260px"')
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Alerts</title>
<style>body{{background:#111;color:#ddd;font-family:monospace;padding:24px;font-size:.88rem}}
h1{{color:#fff}} h2{{color:#ccc;font-size:1rem;margin-top:28px}} td{{padding:3px 14px 3px 0}}
.flash{{background:#1e1e1e;border:1px solid #333;padding:8px 12px;border-radius:4px;margin:12px 0}}
.note{{color:#777;font-size:.8rem}}</style></head><body>
{_admin_nav('/admin/alerts')}
<h1>✉ Email alerts</h1>
<div>Alerts ready: {yn(_ALERTS_READY)} &nbsp;·&nbsp; From: {_html.escape(_ALERTS_FROM)}
 &nbsp;·&nbsp; Sent today (UTC): {_alerts_sends_today()} / {_ALERTS_DAILY_CEILING}</div>
<table style="margin-top:10px">{env_rows}</table>
{f'<div class="flash">{_html.escape(flash)}</div>' if flash else ''}
<h2>Your alert address</h2>
<div>{me}</div>
{form("start", f'<input type="email" name="email" placeholder="address to confirm" autocomplete="off" required {inp}>', "Send code")}
{form("confirm", f'<input type="text" name="code" inputmode="numeric" maxlength="7" placeholder="6-digit code" autocomplete="one-time-code" required {inp}>', "Confirm")}
{form("test", "", "Send test email")}
{form("remove", "", "Remove my email", "#600")}
<h2>Daily alert (10 AM ET)</h2>
<div>{_alerts_admin_summary_html()}</div>
{form("preview", "", "Preview my alert (sends nothing)")}
{form("send_mine", "", "Send my alert now")}
{form("rewind", "", "Rewind my window 24 h (testing)", "#443")}
{form("switch_off" if _alerts_global_on() else "switch_on", "", "Turn global switch OFF" if _alerts_global_on() else "Turn global switch ON", "#600" if _alerts_global_on() else "#253")}
{f'<pre style="background:#1a1a1a;border:1px solid #333;padding:12px;white-space:pre-wrap;max-width:760px">{_html.escape(preview)}</pre>' if preview else ''}
<p class="note">Addresses are stored encrypted (Fernet, key in Railway env) and are never shown here
except your own, masked. Codes are stored hashed and expire in {_CODE_TTL_S // 60} min.</p>
</body></html>"""
    return Response(html, mimetype="text/html")


# ── Daily alert engine (v2.19.0, email alerts step 2 — EMAIL_ALERTS_DESIGN.md) ─
# Once per ET day at/after 10:00 America/New_York (catch-up until 13:00, then the
# day is skipped): run the background nationwide sweep, then for every eligible
# user email the still-available Want List matches listed since their last alert.
# No matches → no email. Same privacy rules as the section above: an address is
# decrypted only to build the Postmark request; nothing here logs an address,
# subject, keyword or SKU list — counts and tags only.
from datetime import timezone as _timezone
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ALERTS_TZ = _ZoneInfo("America/New_York")
except Exception:          # no tz database — engine stays off, app boots normally
    _ALERTS_TZ = None
    print("[alerts] engine disabled — America/New_York time zone unavailable (tzdata)")

_ALERTS_RUN_HOUR        = 10      # ET
_ALERTS_CATCHUP_END     = 13      # ET; after this a missed day is skipped
_ALERTS_RETRY_MIN       = 20      # incomplete sweep → retry after this many minutes
_ALERTS_MAX_ATTEMPTS    = 3       # sweep tries per day before sending from what's there
_ALERTS_MAX_ITEMS       = 10      # items shown per email section (v2.21.0: 3 sections x 10)
_ALERTS_PILL_ROW_CAP    = 500     # rows read per pill per run (a day is ~hundreds)
_ALERTS_SENT_KEEP_DAYS  = 180
_ALERTS_SITE            = "https://gcgeartracker.com"
_ALERTS_ADV_LOCK_KEY    = 7101987019   # pg_try_advisory_lock key for the daily job
_ALERTS_WEBHOOK_SECRET  = (os.environ.get("ALERTS_WEBHOOK_SECRET") or "").strip()
_ALERTS_SCHEDULER_ON    = (os.environ.get("ALERTS_SCHEDULER") or "on").strip().lower() != "off"
_ALERTS_DL_NORM_SQL     = "(CASE WHEN length(date_listed) = 10 THEN date_listed || 'T23:59:59Z' ELSE date_listed END)"
_ALERTS_RUN_STATE       = {"day": None, "attempts": 0, "next_try": 0.0, "running": False}
_ALERTS_RUN_LOCK        = threading.Lock()   # one run (scheduled or admin) at a time in-process


def _alerts_norm_dl(d: str) -> str:
    """Same normalization as the NEW rule (_run's _norm_item_date)."""
    return d + "T23:59:59Z" if d and len(d) == 10 else (d or "")


def _alerts_meta_get(k: str, default=None):
    with _user_db() as conn:
        row = conn.execute("SELECT v FROM alert_meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def _alerts_meta_set(k: str, v: str) -> None:
    with _user_db() as conn:
        conn.execute("INSERT INTO alert_meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        conn.commit()


def _alerts_global_on() -> bool:
    return (_alerts_meta_get("global_switch", "on") or "on") == "on"


def _alerts_pg_max_listed() -> str | None:
    """Newest normalized date_listed among available items (None if no Postgres)."""
    if _PG_POOL is None:
        return None
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX({_ALERTS_DL_NORM_SQL}) FROM items WHERE available")
                return cur.fetchone()[0]
    try:
        return _pg_read(_q)
    except Exception as e:
        print(f"[alerts] max date_listed read failed: {type(e).__name__}")
        return None


def _alerts_initial_anchor() -> str:
    """Anchor for a newly confirmed address: newest available listing, or now."""
    return _alerts_pg_max_listed() or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _alerts_user_row_is_admin(u: dict) -> bool:
    return bool(ADMIN_EMAIL and u.get("google_id") and (u.get("email") or "").strip().lower() == ADMIN_EMAIL)


def _alerts_subscribers(only_user: int | None = None) -> list[dict]:
    """Confirmed subscribers allowed to get alerts right now: admin, or the
    per-user beta flag. (The global switch is checked by the caller.)"""
    sql = ("SELECT u.id AS user_id, u.email, u.google_id, u.alerts_beta, u.deleted_at, "
           "e.email_enc, s.paused, s.suppressed, s.mode, s.anchor, s.checked_at "
           "FROM alert_email e JOIN users u ON u.id = e.user_id "
           "LEFT JOIN alert_settings s ON s.user_id = e.user_id "
           "WHERE e.confirmed_at IS NOT NULL")
    args = ()
    if only_user is not None:
        sql += " AND u.id = ?"
        args = (only_user,)
    with _user_db() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY u.id", args).fetchall()]
    return [r for r in rows if not r["deleted_at"] and (r["alerts_beta"] or _alerts_user_row_is_admin(r))]


def _alerts_active_pills(user_id: int, keywords: list, mode: str) -> list[str]:
    """The Want List pills this user is alerted on: all of them, in Want List
    order, after the same per-shape caps the site applies. (v2.22.0, Chuck
    2026-10-09: everyone gets the default — no per-pill choices; the old
    alert_pills rows and `mode` are ignored.)"""
    return _kw_accept_capped([k for k in keywords if isinstance(k, str)])


def _alerts_pill_label(kw: str) -> str:
    return kw.lstrip("=").strip()


def _alerts_quoted(lab: str) -> str:
    """Curly-quote a pill label for prose, unless it's already a "quoted" search."""
    return lab if len(lab) >= 2 and lab[0] == '"' and lab[-1] == '"' else f"\u201c{lab}\u201d"


def _alerts_pill_id(user_id: int, kw: str) -> str:
    return _alerts_hmac("pill", f"{user_id}:{kw}")[:16]


def _alerts_link_token(user_id: int, action: str, pid: str = "-") -> str:
    """action: 'u' pause all, 'r' resume, 'p' stop one pill (pid = _alerts_pill_id).
    No expiry; dead once the address is removed (the routes check alert_email)."""
    body = f"{user_id}.{action}.{pid}"
    return f"{body}.{_alerts_hmac('link', body)[:32]}"


def _alerts_parse_token(tok: str):
    parts = (tok or "").split(".")
    if len(parts) != 4 or not parts[0].isdigit() or parts[1] not in ("u", "r", "p"):
        return None
    body = ".".join(parts[:3])
    if not _ALERTS_READY or not hmac.compare_digest(_alerts_hmac("link", body)[:32], parts[3]):
        return None
    return int(parts[0]), parts[1], parts[2]


_ALERTS_ITEM_COLS = ("sku, name, brand, price, list_price, has_price_drop, price_drop, condition, store, url, "
                     f"{_ALERTS_DL_NORM_SQL} AS dl, image_id, price_drop_since, first_seen")


def _alerts_row_item(r) -> dict:
    return {"sku": r[0], "name": r[1] or "", "brand": r[2] or "",
            "price": float(r[3] or 0), "list_price": float(r[4] or 0),
            "has_price_drop": bool(r[5]), "price_drop": float(r[6] or 0),
            "condition": r[7] or "", "store": r[8] or "", "url": r[9] or "",
            "dl": r[10] or "", "image_id": r[11] or "", "pds": r[12] or "", "fs": r[13] or "",
            "pills": []}


def _alerts_find_matches(user_id: int, pills: list[str], lo: str, hi: str,
                         fs_lo: str = "", fs_hi: str = "") -> tuple[list[dict], int]:
    """New Want List items: available items matching any pill that were first seen
    by the site in (fs_lo, fs_hi] whatever their listed date (v2.22.2 — the site's
    NEW rule; the listed-date window (lo, hi] is only used if no first-seen window
    is given) — minus the ledger. Newest listed first. Returns (items,
    pills_skipped). Each item carries `pills`."""
    skipped = 0
    def _q():
        nonlocal skipped
        out = {}
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                for kw in pills:
                    try:
                        frag, prm = _tsquery_want_list_entry(kw)
                    except _TsqueryUnsupported:
                        skipped += 1
                        continue
                    if frag is None:
                        continue
                    # v2.22.2: same NEW rule as the site — first seen by the site in
                    # (fs_lo, fs_hi], whatever the listed date. The listed-date
                    # window is only a fallback if no first-seen window is given.
                    if fs_lo and fs_hi:
                        win = "(first_seen <> '' AND first_seen > %s AND first_seen <= %s)"
                        wprm = [fs_lo, fs_hi]
                    else:
                        win = f"({_ALERTS_DL_NORM_SQL} > %s AND {_ALERTS_DL_NORM_SQL} <= %s)"
                        wprm = [lo, hi]
                    cur.execute(
                        f"SELECT {_ALERTS_ITEM_COLS} FROM items WHERE available AND {win} "
                        f"AND ({frag}) ORDER BY dl DESC, sku LIMIT {_ALERTS_PILL_ROW_CAP}",
                        wprm + list(prm))
                    for r in cur.fetchall():
                        it = out.get(r[0]) or out.setdefault(r[0], _alerts_row_item(r))
                        it["pills"].append(kw)
        return out
    skipped = 0
    found = _pg_read(_q)
    if found:
        with _user_db() as conn:
            sent = {r["sku"] for r in conn.execute(
                f"SELECT sku FROM alert_sent WHERE user_id=? AND sku IN ({','.join('?' * len(found))})",
                (user_id, *found.keys())).fetchall()}
        for sku in sent:
            found.pop(sku, None)
    items = sorted(found.values(), key=lambda it: (it["dl"], it["sku"]), reverse=True)
    return items, skipped


def _alerts_drop_ledger(user_id: int, skus) -> dict:
    """{sku: price we last emailed this user about} for the given SKUs."""
    skus = list(skus)
    out = {}
    with _user_db() as conn:
        for i in range(0, len(skus), 500):
            part = skus[i:i + 500]
            for r in conn.execute(
                    f"SELECT sku, price FROM alert_drop_sent WHERE user_id=? AND sku IN ({','.join('?' * len(part))})",
                    (user_id, *part)).fetchall():
                out[r["sku"]] = float(r["price"] or 0)
    return out


def _alerts_pick_drops(user_id: int, cands: dict, since: str) -> list[dict]:
    """v2.21.0 price-drop rule (Chuck, 2026-10-09): a listing goes in today's email if
    GC flags it as dropped and EITHER we never emailed this user about its drop and
    the drop started after `since` (their last alert check), OR its price is now
    lower than the price we last emailed them about. Each kept item gets `was`
    (old price: the previously alerted price, else GC's list price). Newest drop first."""
    ledger = _alerts_drop_ledger(user_id, cands.keys())
    keep = []
    for sku, it in cands.items():
        if not it["has_price_drop"] or it["price"] <= 0:
            continue
        prev = ledger.get(sku)
        if prev is None:
            if it["pds"] and it["pds"] > since:
                it["was"] = it["price"] + it["price_drop"] if it["price_drop"] > 0 else it["list_price"]
                keep.append(it)
        elif it["price"] < prev - 0.005:
            it["was"] = prev
            keep.append(it)
    keep.sort(key=lambda it: (it["pds"], it["sku"]), reverse=True)
    return keep


def _alerts_find_want_drops(user_id: int, pills: list[str], since: str) -> list[dict]:
    """Want List price drops: available, GC-flagged price drops matching any pill
    (all stores), filtered by _alerts_pick_drops."""
    if not pills:
        return []
    def _q():
        out = {}
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                for kw in pills:
                    try:
                        frag, prm = _tsquery_want_list_entry(kw)
                    except _TsqueryUnsupported:
                        continue
                    if frag is None:
                        continue
                    cur.execute(
                        f"SELECT {_ALERTS_ITEM_COLS} FROM items WHERE available AND has_price_drop "
                        f"AND ({frag}) ORDER BY price_drop_since DESC, sku LIMIT {_ALERTS_PILL_ROW_CAP}",
                        list(prm))
                    for r in cur.fetchall():
                        it = out.get(r[0]) or out.setdefault(r[0], _alerts_row_item(r))
                        it["pills"].append(kw)
        return out
    return _alerts_pick_drops(user_id, _pg_read(_q), since)


def _alerts_find_watch_drops(user_id: int, watch_skus: list, since: str) -> list[dict]:
    """Watch List price drops: the user's watched SKUs that are available and
    GC-flagged as dropped, filtered by _alerts_pick_drops."""
    skus = [s for s in watch_skus if isinstance(s, str) and s][:5000]
    if not skus:
        return []
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_ALERTS_ITEM_COLS} FROM items WHERE available AND has_price_drop "
                            f"AND sku = ANY(%s)", (skus,))
                return {r[0]: _alerts_row_item(r) for r in cur.fetchall()}
    return _alerts_pick_drops(user_id, _pg_read(_q), since)


def _alerts_money(v: float) -> str:
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def _alerts_listed_label(dl: str) -> str:
    """'Oct 9, 8:42 AM ET' from a normalized date_listed; date-only → 'Oct 9'."""
    if not dl:
        return ""
    try:
        if dl.endswith("T23:59:59Z"):          # normalized date-only value
            d = datetime.strptime(dl[:10], "%Y-%m-%d")
            return f"{d:%b} {d.day}"
        t = datetime.strptime(dl[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_timezone.utc)
        if _ALERTS_TZ is not None:
            t = t.astimezone(_ALERTS_TZ)
            h = t.hour % 12 or 12
            return f"{t:%b} {t.day}, {h}:{t:%M} {'AM' if t.hour < 12 else 'PM'} ET"
        return f"{t:%b} {t.day}"
    except ValueError:
        return ""


def _alerts_listed_day(dl: str) -> str:
    """'Oct 9' — the ET calendar day an item was listed (v2.19.4: no time of day)."""
    lab = _alerts_listed_label(dl)
    return lab.split(",")[0] if lab else ""


def _alerts_build_email(user_id: int, new_items: list[dict], want_drops: list[dict],
                        watch_drops: list[dict], pills: list[str], batch_id: str) -> tuple[str, str, str, dict]:
    """(subject, text, html, headers) for one user's daily alert. v2.21.0 layout
    (mockup approved by Chuck 2026-10-09): three sections — New Want List Items,
    Want List Price Drops, Watch List Price Drops — up to _ALERTS_MAX_ITEMS each,
    newest first, each ending in a red button ("See N more on GC Gear Tracker"
    when there are more); an empty section says "No new … today."; no dark-gray
    text anywhere; the matched Want List terms (with stop links) at the bottom."""
    esc = _html.escape
    F = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    SUB = "#e6e6e6"           # secondary text — light, never dark gray (Chuck)
    n_new, n_wd, n_wt = len(new_items), len(want_drops), len(watch_drops)
    total = n_new + n_wd + n_wt
    n_drops = n_wd + n_wt

    def _cut(s: str, n: int = 140) -> str:
        return s if len(s) <= n else s[:n - 1].rstrip() + "…"

    # Subject (Chuck): one item → "New Want List Item: <name>" / "Price Drop: <name>, now $X";
    # otherwise "Gear Alert: N new Want List items, M price drops".
    if total == 1 and n_new:
        subject = f"New Want List Item: {_cut(new_items[0]['name'] or 'new match')}"
    elif total == 1:
        it = (want_drops or watch_drops)[0]
        subject = f"Price Drop: {_cut(it['name'] or 'watched item')}, now {_alerts_money(it['price'])}"
    else:
        parts = []
        if n_new:
            parts.append(f"{n_new} new Want List item{'s' if n_new != 1 else ''}")
        if n_drops:
            parts.append(f"{n_drops} price drop{'s' if n_drops != 1 else ''}")
        subject = "Gear Alert: " + ", ".join(parts)
    subject = subject[:180]

    base = f"{_ALERTS_SITE}/?alert={batch_id}"
    links = {"new": base, "wantdrops": base + "&view=wantdrops", "watchdrops": base + "&view=watchdrops"}
    pause_url = f"{_ALERTS_SITE}/alerts/u/{_alerts_link_token(user_id, 'u')}"
    manage_url = f"{_ALERTS_SITE}/"
    matched_pills = [p for p in pills if any(p in it["pills"] for it in new_items + want_drops)]
    term_labels = [_alerts_pill_label(p) for p in matched_pills]   # v2.22.0: no per-term stop links
    note = ("Listings were available when we checked at 10 AM ET and may have sold since. "
            "Items that were listed and sold between checks may not appear.")
    today = ""
    if _ALERTS_TZ is not None:
        _now = datetime.now(_timezone.utc).astimezone(_ALERTS_TZ)
        today = f"{_now:%b} {_now.day}"
    sections = [
        ("new", "New Want List Items", new_items, "No new Want List items today.",
         "Open my Want List on GC Gear Tracker"),
        ("wantdrops", "Want List Price Drops", want_drops, "No new Want List price drops today.",
         "See Want List price drops on GC Gear Tracker"),
        ("watchdrops", "Watch List Price Drops", watch_drops, "No new Watch List price drops today.",
         "See Watch List price drops on GC Gear Tracker"),
    ]

    def _was(it) -> float:
        if "was" in it:
            return float(it["was"] or 0)
        if it["has_price_drop"] and it["price_drop"] > 0:
            return it["price"] + it["price_drop"]
        return 0.0

    # ── plain text ──
    t = ["GC Gear Tracker — daily alert" + (f" · {today}" if today else ""), ""]
    for key, title, items, empty, btn in sections:
        t += [title.upper(), ""]
        if not items:
            t += [empty, ""]
            continue
        shown = items[:_ALERTS_MAX_ITEMS]
        for it in shown:
            price = _alerts_money(it["price"])
            w = _was(it)
            if w > it["price"]:
                price += f" (was {_alerts_money(w)}, down {_alerts_money(w - it['price'])})"
            bits = [price] + [b for b in (it["condition"], it["store"]) if b]
            _ld = _alerts_listed_day(it["dl"])
            if _ld:
                bits.append(f"Listed {_ld}")
            t += [it["name"], "  " + " · ".join(bits), f"  {_ALERTS_SITE}/go/{it['sku']}", ""]
        more = len(items) - len(shown)
        t += [(f"See {more} more on GC Gear Tracker" if more > 0 else btn) + f": {links[key]}", ""]
    t += [f"Note: {note}", ""]
    if term_labels:
        t += ["Matched Want List terms: " + ", ".join(term_labels), ""]
    t += ["You're getting this because you turned on email alerts at gcgeartracker.com.",
          f"Manage alerts: {manage_url}", f"Pause all alerts: {pause_url}",
          "This mailbox isn't monitored — use the links above to stop or pause alerts.", "",
          "GC Gear Tracker is independent and not affiliated with Guitar Center."]

    # ── HTML (tables + inline styles + bgcolor so it survives email clients) ──
    def _row(it, last: bool) -> str:
        go = esc(f"{_ALERTS_SITE}/go/{it['sku']}")
        img = (f'<img src="https://media.guitarcenter.com/is/image/MMGS7/{esc(it["image_id"])}-00-200x200.jpg" '
               f'width="72" height="72" alt="{esc(it["name"][:60])}" style="display:block;width:72px;height:72px;border-radius:6px;'
               f'background:#252525;border:0;object-fit:cover">' if it.get("image_id") else
               '<div style="width:72px;height:72px;border-radius:6px;background:#252525"></div>')
        price = f'<span style="color:#ffffff;font-weight:700">{esc(_alerts_money(it["price"]))}</span>'
        w = _was(it)
        if w > it["price"]:
            price = (f'<span style="color:{SUB};text-decoration:line-through;font-size:12px">'
                     f'{esc(_alerts_money(w))}</span>&nbsp;' + price +
                     f'&nbsp;<span style="color:#4ade80;font-size:12px;font-weight:700">&darr; '
                     f'{esc(_alerts_money(w - it["price"]))}</span>')
        cond = (f'&nbsp;&nbsp;<span style="color:#ffffff;font-size:12px">{esc(it["condition"])}</span>'
                if it["condition"] else "")
        _ld = _alerts_listed_day(it["dl"])
        meta = " · ".join(esc(x) for x in (it["store"], f"Listed {_ld}" if _ld else "") if x)
        border = "" if last else "border-bottom:1px solid #2a2a2a;"
        return (f'<tr><td style="padding:14px 20px;{border}"><table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0" border="0"><tr><td width="72" valign="top" style="width:72px"><a href="{go}">{img}</a></td>'
                f'<td valign="top" style="padding-left:14px;font-family:{F}">'
                f'<a href="{go}" style="color:#ffffff;font-size:15px;font-weight:600;line-height:1.35;text-decoration:none">'
                f'{esc(it["name"])}</a>'
                f'<div style="margin-top:5px;font-size:14px;line-height:1.4">{price}{cond}</div>'
                f'<div style="margin-top:4px;font-size:12px;color:{SUB};line-height:1.4">{meta}</div>'
                f'</td></tr></table></td></tr>')

    body = []
    for i, (key, title, items, empty, btn) in enumerate(sections):
        top = "" if i == 0 else "border-top:1px solid #3a3a3a;"
        body.append(f'<tr><td style="padding:20px 20px 4px;{top}font-family:{F}">'
                    f'<div style="color:#ffffff;font-size:19px;font-weight:700">{esc(title)}</div></td></tr>')
        if not items:
            body.append(f'<tr><td style="padding:6px 20px 20px;font-family:{F};color:{SUB};font-size:14px;'
                        f'font-style:italic">{esc(empty)}</td></tr>')
            continue
        shown = items[:_ALERTS_MAX_ITEMS]
        body += [_row(it, j == len(shown) - 1) for j, it in enumerate(shown)]
        more = len(items) - len(shown)
        label = f"See {more} more on GC Gear Tracker" if more > 0 else btn
        body.append(f'<tr><td style="padding:6px 20px 20px;font-family:{F}"><table role="presentation" cellpadding="0" '
                    f'cellspacing="0" border="0"><tr><td bgcolor="#cc0000" style="background:#cc0000;border-radius:6px">'
                    f'<a href="{esc(links[key])}" style="display:inline-block;padding:11px 20px;color:#ffffff;'
                    f'font-size:14px;font-weight:700;text-decoration:none;font-family:{F}">{esc(label)}</a>'
                    f'</td></tr></table></td></tr>')
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 8px 0;padding:4px 10px;border-radius:12px;background:#0a2e17;'
        f'border:1px solid #2d6a2d;font-size:12px;color:#4ade80;font-family:{F};white-space:nowrap">{esc(lab)}</span>'
        for lab in term_labels)
    terms_html = (f'<tr><td style="padding:16px 20px 10px;border-top:1px solid #2a2a2a;font-family:{F}">'
                  f'<div style="font-size:11px;font-weight:700;color:#ffffff;letter-spacing:.5px;text-transform:uppercase;'
                  f'margin-bottom:10px">Matched Want List terms in this alert</div>{chips}</td></tr>') if term_labels else ""
    first = (new_items + want_drops + watch_drops)[0]
    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark light"><meta name="supported-color-schemes" content="dark light">'
        f'<title>{esc(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:#111111" bgcolor="#111111">'
        f'<div style="display:none;max-height:0;overflow:hidden;color:#111111">{esc(first["name"])}'
        + (f" and {total - 1} more" if total > 1 else "") + '</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#111111" '
        'style="background:#111111"><tr><td align="center" style="padding:20px 10px">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;background:#1a1a1a;border:1px solid #2e2e2e;border-radius:10px;overflow:hidden" bgcolor="#1a1a1a">'
        f'<tr><td bgcolor="#6a0000" style="background:#6a0000;background-image:linear-gradient(135deg,#4a0000,#7a0000);'
        f'padding:16px 20px;font-family:{F}"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="color:#ffffff;font-size:18px;font-weight:700;font-family:{F}">GC Gear Tracker</td>'
        f'<td align="right" style="color:#ffe3e3;font-size:12px;font-family:{F}">Daily alert'
        + (f" · {esc(today)}" if today else "") + '</td></tr></table></td></tr>'
        + "".join(body) +
        f'<tr><td style="padding:14px 20px 18px;border-top:1px solid #3a3a3a;font-size:12px;color:{SUB};'
        f'line-height:1.5;font-family:{F}">Note: {esc(note)}</td></tr>'
        + terms_html +
        f'<tr><td style="padding:12px 20px 18px;border-top:1px solid #2a2a2a;font-size:12px;color:{SUB};'
        f'line-height:1.6;font-family:{F}">You\'re getting this because you turned on email alerts at '
        f'<a href="{esc(manage_url)}" style="color:#ffffff">gcgeartracker.com</a>.<br>'
        f'<a href="{esc(manage_url)}" style="color:#ffffff">Manage alerts</a> &nbsp;·&nbsp; '
        f'<a href="{esc(pause_url)}" style="color:#ffffff">Pause all alerts</a><br>'
        'This mailbox isn\'t monitored — use the links above to stop or pause alerts.<br>'
        'GC Gear Tracker is independent and not affiliated with Guitar Center.</td></tr>'
        '</table></td></tr></table></body></html>')
    headers = {"List-Unsubscribe": f"<{pause_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return subject, "\n".join(t), html, headers


def _alerts_set_anchor(user_id: int, anchor: str, sent: bool = False, checked_at: str | None = None) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        conn.execute("INSERT OR IGNORE INTO alert_settings(user_id, frequency, paused, mode, updated_at) "
                     "VALUES(?, 'daily', 0, 'all', ?)", (user_id, now))
        if sent:
            conn.execute("UPDATE alert_settings SET anchor=?, last_alert_at=? WHERE user_id=?", (anchor, now, user_id))
        else:
            conn.execute("UPDATE alert_settings SET anchor=? WHERE user_id=?", (anchor, user_id))
        if checked_at:
            conn.execute("UPDATE alert_settings SET checked_at=? WHERE user_id=?", (checked_at, user_id))
        conn.commit()


def _alerts_process_user(u: dict, run_max: str, *, advance: bool = True, dry_run: bool = False) -> dict:
    """One user's daily alert. Returns {"result": sent|none|skipped|init|failed|ceiling|error, ...}.
    dry_run: compute + build the email, change nothing, send nothing.
    v2.21.0: three sections. `checked_at` (wall time of this user's last check)
    bounds the first_seen half of the NEW rule and which price drops count as
    new; when unset (first run after the upgrade) the window is the last 24 h."""
    uid = u["user_id"]
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if u.get("paused") or u.get("suppressed"):
        if advance and not dry_run and run_max:
            _alerts_set_anchor(uid, run_max, checked_at=now)   # no backlog dump when they resume
        return {"result": "skipped"}
    anchor = u.get("anchor")
    if not anchor:
        if not dry_run and run_max:
            _alerts_set_anchor(uid, run_max, checked_at=now)
        return {"result": "init"}
    since = u.get("checked_at") or (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _ud = _get_user_data(uid)
    pills = _alerts_active_pills(uid, _ud.get("keywords") or [], u.get("mode") or "all")
    watch = list((_ud.get("watchlist") or {}).keys())
    hi = max(run_max or anchor, anchor)
    new_items, skipped = ([], 0)
    if pills:
        new_items, skipped = _alerts_find_matches(uid, pills, anchor, hi, since, now)
    # An item appears once: new beats a drop; a watched item's drop goes under Watch List.
    shown = {it["sku"] for it in new_items}
    watch_drops = [it for it in _alerts_find_watch_drops(uid, watch, since) if it["sku"] not in shown]
    shown |= {it["sku"] for it in watch_drops}
    want_drops = [it for it in _alerts_find_want_drops(uid, pills, since) if it["sku"] not in shown]
    counts = {"pills": len(pills), "pills_skipped": skipped, "new": len(new_items),
              "want_drops": len(want_drops), "watch_drops": len(watch_drops)}
    total = len(new_items) + len(want_drops) + len(watch_drops)
    if not total:
        if advance and not dry_run:
            _alerts_set_anchor(uid, hi, checked_at=now)
        return {"result": "none", **counts}
    batch_id = _secrets_alerts.token_urlsafe(12)
    subject, text, html, headers = _alerts_build_email(uid, new_items, want_drops, watch_drops, pills, batch_id)
    if dry_run:
        return {"result": "preview", "matches": total, **counts, "subject": subject, "text": text, "html": html}
    addr = _dec_email(u["email_enc"])
    if not addr:
        return {"result": "error", "why": "undecryptable"}
    ok, why = send_email(addr, subject, text, html, tag="daily-alert", headers=headers)
    if not ok:
        return {"result": "ceiling" if why == "ceiling" else "failed", "why": why}
    with _user_db() as conn:
        conn.executemany("INSERT OR IGNORE INTO alert_sent(user_id, sku, sent_at, batch) VALUES(?,?,?,?)",
                         [(uid, it["sku"], now, batch_id) for it in new_items])
        conn.executemany("INSERT INTO alert_drop_sent(user_id, sku, price, sent_at) VALUES(?,?,?,?) "
                         "ON CONFLICT(user_id, sku) DO UPDATE SET price=excluded.price, sent_at=excluded.sent_at",
                         [(uid, it["sku"], it["price"], now) for it in want_drops + watch_drops])
        conn.execute("INSERT INTO alert_batches(id, user_id, created_at, skus) VALUES(?,?,?,?)",
                     (batch_id, uid, now, json.dumps([it["sku"] for it in new_items + want_drops + watch_drops])))
        conn.commit()
    if advance:
        _alerts_set_anchor(uid, hi, sent=True, checked_at=now)
    return {"result": "sent", "matches": total, **counts}


def _alerts_prune() -> None:
    cut = (datetime.utcnow() - timedelta(days=_ALERTS_SENT_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _user_db() as conn:
        conn.execute("DELETE FROM alert_sent WHERE sent_at < ?", (cut,))
        conn.execute("DELETE FROM alert_drop_sent WHERE sent_at < ?", (cut,))
        conn.execute("DELETE FROM alert_batches WHERE created_at < ?", (cut,))
        conn.commit()


def _alerts_wait_for_sweep(timeout_s: int = 420) -> dict:
    """Run (or join) the background nationwide sweep and wait for its result.
    A click-started sweep already running queues one follow-up sweep, so the
    result we read always comes from a sweep that started after this call."""
    _start_sweep()
    t0 = time.time()
    time.sleep(2)
    while time.time() - t0 < timeout_s:
        with _SWEEP_STATE_LOCK:
            running = _SWEEP_STATE["running"]
            res = dict(_SWEEP_STATE["result"]) if _SWEEP_STATE["result"] else None
        if not running:
            return res or {"complete": False, "error": "no result"}
        time.sleep(3)
    return {"complete": False, "error": "timeout"}


def _alerts_run_users(run_max: str, advance: bool) -> dict:
    counts = {"considered": 0, "sent": 0, "none": 0, "skipped": 0, "init": 0, "failed": 0,
              "ceiling": 0, "error": 0, "matches": 0, "pills_skipped": 0}
    for u in _alerts_subscribers():
        counts["considered"] += 1
        try:
            r = _alerts_process_user(u, run_max, advance=advance)
        except Exception as e:
            print(f"[alerts] user run failed: {type(e).__name__}")
            r = {"result": "error"}
        counts[r["result"]] = counts.get(r["result"], 0) + 1
        counts["matches"] += r.get("matches", 0) if r["result"] == "sent" else 0
        counts["pills_skipped"] += r.get("pills_skipped", 0)
        if r["result"] == "ceiling":
            break             # everyone left keeps their anchor → tomorrow
    return counts


def _alerts_daily_job(day: str, *, sweep=None) -> str:
    """The 10 AM job for ET date `day`. Returns 'done', 'retry', 'busy' or 'locked'.
    `sweep` is injectable for tests (default: _alerts_wait_for_sweep)."""
    if not _ALERTS_RUN_LOCK.acquire(blocking=False):
        return "busy"
    conn = None
    locked = False
    try:
        conn = _PG_POOL.getconn()
        try:
            conn.rollback()
        except Exception:
            pass
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_ALERTS_ADV_LOCK_KEY,))
            locked = bool(cur.fetchone()[0])
        if not locked:
            return "locked"            # another container is running it
        if _alerts_meta_get("last_daily_run") == day:
            return "done"
        st = _ALERTS_RUN_STATE
        if st["day"] != day:
            st.update(day=day, attempts=0, next_try=0.0)
        st["attempts"] += 1
        t0 = time.time()
        res = (sweep or _alerts_wait_for_sweep)()
        complete = bool(res.get("complete"))
        if not complete and st["attempts"] < _ALERTS_MAX_ATTEMPTS:
            st["next_try"] = time.time() + _ALERTS_RETRY_MIN * 60
            print(f"[alerts] daily run {day}: sweep incomplete (try {st['attempts']}), retrying in {_ALERTS_RETRY_MIN} min")
            return "retry"
        run_max = _alerts_pg_max_listed()
        counts = _alerts_run_users(run_max or "", advance=complete) if run_max else {"error": "no postgres"}
        _alerts_prune()
        summary = {"day": day, "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "sweep_complete": complete, "attempts": st["attempts"],
                   "seconds": int(time.time() - t0), **counts}
        _alerts_meta_set("last_daily_run", day)
        _alerts_meta_set("last_run_summary", json.dumps(summary))
        print(f"[alerts] daily run {day}: " + ", ".join(f"{k} {v}" for k, v in summary.items()
                                                         if k not in ("day", "at")))
        return "done"
    finally:
        if conn is not None:
            try:
                if locked:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (_ALERTS_ADV_LOCK_KEY,))
                conn.autocommit = False
            except Exception:
                pass
            _PG_POOL.putconn(conn)
        _ALERTS_RUN_LOCK.release()


def _alerts_due_day(now_utc: datetime | None = None) -> str | None:
    """ET date string if the daily job is due now (10:00 ≤ ET < 13:00 and not
    yet run today), else None."""
    if _ALERTS_TZ is None:
        return None
    now = (now_utc or datetime.now(_timezone.utc)).astimezone(_ALERTS_TZ)
    if not (_ALERTS_RUN_HOUR <= now.hour < _ALERTS_CATCHUP_END):
        return None
    day = now.date().isoformat()
    if _alerts_meta_get("last_daily_run") == day:
        return None
    return day


def _alerts_scheduler_tick(now_utc: datetime | None = None, *, sweep=None) -> str:
    if not (_ALERTS_READY and _PG_POOL is not None and _ALERTS_TZ is not None):
        return "off"
    day = _alerts_due_day(now_utc)
    if not day:
        return "idle"
    st = _ALERTS_RUN_STATE
    if st["day"] == day and time.time() < st["next_try"]:
        return "waiting"
    if not _alerts_global_on():
        _alerts_meta_set("last_daily_run", day)
        _alerts_meta_set("last_run_summary", json.dumps({"day": day, "skipped": "global switch off"}))
        return "switch_off"
    return _alerts_daily_job(day, sweep=sweep)


def _alerts_scheduler_loop():
    time.sleep(90)                     # let boot (pool, schema) settle
    while True:
        try:
            _alerts_scheduler_tick()
        except Exception as e:
            print(f"[alerts] scheduler tick failed: {type(e).__name__}")
        time.sleep(60)


# ── Public routes: item redirect, one-click links, Postmark webhook ───────────

_ALERTS_SKU_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

@app.route("/go/<sku>")
def alerts_go(sku):
    """Email item links: 302 to the item's Guitar Center page. Logs nothing per
    user; one place to swap in affiliate links later."""
    target = "/"
    if _ALERTS_SKU_RE.match(sku or "") and _PG_POOL is not None:
        def _q():
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT url FROM items WHERE sku=%s", (sku,))
                    row = cur.fetchone()
                    return row[0] if row else ""
        try:
            url = _pg_read(_q) or ""
        except Exception:
            url = ""
        if url.startswith("https://www.guitarcenter.com/"):
            target = url
    return redirect(target, code=302)


def _alerts_link_page(title: str, body_html: str, status: int = 200) -> Response:
    page = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{_html.escape(title)} — GC Gear Tracker</title>"
            "<style>body{background:#111;color:#ccc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            "Roboto,sans-serif;padding:48px 20px;line-height:1.6}.box{max-width:480px;margin:0 auto}"
            "h1{color:#fff;font-size:1.3rem}button{background:#b00;color:#fff;border:none;border-radius:4px;"
            "padding:9px 18px;font-size:1rem;cursor:pointer}a{color:#f88}</style></head><body><div class=\"box\">"
            f"<h1>{_html.escape(title)}</h1>{body_html}"
            "<p style=\"margin-top:28px\"><a href=\"/\">GC Gear Tracker</a></p></div></body></html>")
    return Response(page, status=status, mimetype="text/html")


def _alerts_link_user(tok: str):
    """(user_id, action, pid) for a valid token whose user still has an address."""
    parsed = _alerts_parse_token(tok)
    if not parsed:
        return None
    with _user_db() as conn:
        ok = conn.execute("SELECT 1 FROM alert_email WHERE user_id=?", (parsed[0],)).fetchone()
    return parsed if ok else None


@app.route("/alerts/u/<tok>", methods=["GET", "POST"])
def alerts_link_pause(tok):
    """Pause all alerts (also the List-Unsubscribe target). GET only shows a
    button — mail apps' link scanners issue GETs, so a GET never changes
    anything. POST (the button, or an RFC 8058 one-click POST) does it."""
    parsed = _alerts_link_user(tok)
    if not parsed or parsed[1] not in ("u", "r"):
        return _alerts_link_page("Link not valid", "<p>This link has expired or isn't valid.</p>", 404)
    uid, action, _ = parsed
    pause = action == "u"
    if request.method == "POST":
        _alerts_apply_pause(uid, pause)     # resuming starts fresh from now, no backlog
        if request.form.get("List-Unsubscribe") == "One-Click":
            return Response("ok", mimetype="text/plain")
        if pause:
            resume = _alerts_link_token(uid, "r")
            return _alerts_link_page("Alerts paused",
                "<p>You won't get any more alert emails. Your address is kept so you can turn them "
                "back on; to delete it, use “Remove my email” in your alert settings.</p>"
                f"<form method=\"POST\" action=\"/alerts/u/{_html.escape(resume)}\"><button type=\"submit\">"
                "Turn alerts back on</button></form>")
        return _alerts_link_page("Alerts are back on", "<p>You'll get alert emails again, starting with "
                                 "listings and price drops from now on.</p>")
    label = "Pause all alert emails?" if pause else "Turn alert emails back on?"
    btn = "Pause all alerts" if pause else "Turn alerts back on"
    return _alerts_link_page(label, f"<form method=\"POST\"><button type=\"submit\">{btn}</button></form>")


@app.route("/alerts/p/<tok>", methods=["GET", "POST"])
def alerts_link_pill(tok):
    """Per-term "stop alerts" links from emails sent before v2.22.0. Alerts now
    always cover the whole Want List, so this only explains how to stop a term."""
    parsed = _alerts_link_user(tok)
    if not parsed or parsed[1] != "p":
        return _alerts_link_page("Link not valid", "<p>This link has expired or isn't valid.</p>", 404)
    uid = parsed[0]
    pause = _html.escape(_alerts_link_token(uid, "u"))
    return _alerts_link_page("Alerts cover your whole Want List",
        "<p>To stop alerts for one search, remove it from your Want List on gcgeartracker.com.</p>"
        f"<p>To stop all alert emails, <a href=\"/alerts/u/{pause}\" style=\"color:#fff\">pause all alerts</a>.</p>")


_ALERTS_SUPPRESS_BOUNCES = {"HardBounce", "BadEmailAddress", "ManuallyDeactivated", "SpamNotification", "SpamComplaint"}

@app.route("/api/alerts/postmark-webhook", methods=["POST"])
def alerts_postmark_webhook():
    """Postmark Bounce / Spam Complaint / Subscription Change webhooks → suppress
    (or un-suppress) the matching subscriber. HTTP basic auth with
    ALERTS_WEBHOOK_SECRET (Postmark: https://postmark:<secret>@gcgeartracker.com/...).
    The address is only turned into its blind index, never logged or stored."""
    if not _ALERTS_WEBHOOK_SECRET or not _ALERTS_READY:
        return jsonify({"error": "Not found"}), 404
    auth = request.authorization
    if not auth or not hmac.compare_digest((auth.password or "").encode(), _ALERTS_WEBHOOK_SECRET.encode()):
        return Response("Unauthorized", status=401, headers={"WWW-Authenticate": 'Basic realm="webhook"'})
    d = request.get_json(silent=True) or {}
    rtype = d.get("RecordType", "")
    addr, reason = "", None
    if rtype == "Bounce":
        addr = d.get("Email") or ""
        reason = d.get("Type") if d.get("Type") in _ALERTS_SUPPRESS_BOUNCES else None
    elif rtype == "SpamComplaint":
        addr, reason = d.get("Email") or "", "SpamComplaint"
    elif rtype == "SubscriptionChange":
        addr = d.get("Recipient") or ""
        reason = (d.get("SuppressionReason") or "Suppressed") if d.get("SuppressSending") else ""
    if addr and reason is not None:
        bidx = _email_bidx(addr)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with _user_db() as conn:
            uids = [r["user_id"] for r in conn.execute("SELECT user_id FROM alert_email WHERE email_bidx=?", (bidx,))]
            for uid in uids:
                conn.execute("INSERT OR IGNORE INTO alert_settings(user_id, frequency, paused, mode, updated_at) "
                             "VALUES(?, 'daily', 0, 'all', ?)", (uid, now))
                conn.execute("UPDATE alert_settings SET suppressed=?, updated_at=? WHERE user_id=?",
                             (reason or None, now, uid))
            conn.commit()
        print(f"[alerts] webhook {rtype} → {'suppressed' if reason else 'unsuppressed'} {len(uids)} subscriber(s)")
    return jsonify({"ok": True})


# ── Admin helpers (used by /admin/alerts) ─────────────────────────────────────

def _alerts_admin_preview(uid: int) -> tuple[str, str]:
    """(flash, preview_text) — what Chuck's alert would contain right now."""
    subs = _alerts_subscribers(uid)
    if not subs:
        return "No confirmed alert address on this account.", ""
    r = _alerts_process_user(subs[0], _alerts_pg_max_listed() or "", advance=False, dry_run=True)
    if r["result"] == "preview":
        return (f"Preview: {r['new']} new Want List item(s), {r['want_drops']} Want List price drop(s), "
                f"{r['watch_drops']} Watch List price drop(s) — nothing sent.",
                f"Subject: {r['subject']}\n\n{r['text']}")
    msg = {"none": "Nothing new since your last alert (no new items or price drops) — no email would be sent.",
           "skipped": "Your alerts are paused or suppressed — no email would be sent.",
           "init": "No alert window yet (it starts at your next alert)."}.get(r["result"], r["result"])
    return msg, ""


def _alerts_admin_send_mine(uid: int) -> str:
    subs = _alerts_subscribers(uid)
    if not subs:
        return "No confirmed alert address on this account."
    if not _ALERTS_RUN_LOCK.acquire(blocking=False):
        return "The daily run is in progress — try again in a minute."
    try:
        r = _alerts_process_user(subs[0], _alerts_pg_max_listed() or "", advance=True)
    finally:
        _ALERTS_RUN_LOCK.release()
    return {"sent": (f"Sent: {r.get('new', 0)} new item(s), {r.get('want_drops', 0)} Want List drop(s), "
                     f"{r.get('watch_drops', 0)} Watch List drop(s)."),
            "none": "Nothing new since your last alert — nothing sent.",
            "skipped": "Your alerts are paused or suppressed — nothing sent.",
            "init": "Alert window started now; matches will come from listings after this.",
            "ceiling": "Daily send limit reached — nothing sent.",
            }.get(r["result"], f"Send failed ({r.get('why', r['result'])}).")


def _alerts_admin_rewind(uid: int, hours: int = 24) -> str:
    """Testing aid: move your own window back so the next alert has content.
    Already-sent items stay in the ledger and still won't repeat."""
    a = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _alerts_set_anchor(uid, a, checked_at=a)
    return f"Your alert window now starts {hours} h ago ({a})."


def _alerts_admin_summary_html() -> str:
    raw = _alerts_meta_get("last_run_summary")
    try:
        s = json.loads(raw) if raw else None
    except ValueError:
        s = None
    with _user_db() as conn:
        subs = conn.execute("SELECT COUNT(*) AS n FROM alert_email WHERE confirmed_at IS NOT NULL").fetchone()["n"]
        paused = conn.execute("SELECT COUNT(*) AS n FROM alert_settings WHERE paused=1").fetchone()["n"]
        supp = conn.execute("SELECT COUNT(*) AS n FROM alert_settings WHERE suppressed IS NOT NULL").fetchone()["n"]
    ceiling_day = _alerts_meta_get("ceiling_hit")
    esc = _html.escape
    rows = [f"Confirmed subscribers: <b>{subs}</b> (paused {paused}, suppressed {supp}) · eligible now "
            f"(admin/beta): <b>{len(_alerts_subscribers())}</b>",
            f"Global switch: <b>{'ON' if _alerts_global_on() else 'OFF'}</b> · scheduler: "
            f"<b>{'on' if _ALERTS_SCHEDULER_ON else 'OFF (ALERTS_SCHEDULER=off)'}</b> · "
            f"time zone: {'ok' if _ALERTS_TZ else '<span style=color:#e88>MISSING</span>'} · "
            f"webhook secret: {'set' if _ALERTS_WEBHOOK_SECRET else '<span style=color:#e88>not set</span>'}",
            f"Schedule: daily {_ALERTS_RUN_HOUR}:00 ET (catch-up until {_ALERTS_CATCHUP_END}:00 ET); "
            f"last run day: <b>{esc(_alerts_meta_get('last_daily_run') or 'never')}</b>"]
    if s:
        rows.append("Last run: " + esc(", ".join(f"{k} {v}" for k, v in s.items())))
    if ceiling_day == _alerts_day():
        rows.append('<span style="color:#e88">⚠ Daily send ceiling reached today — remaining alerts roll to tomorrow.</span>')
    return "<br>".join(rows)


@app.route("/admin/clear-lock")
def admin_clear_lock():
    """Force-release the global scan lock if it's stuck after a crash.
    Protected by admin session."""
    denied = _require_admin()
    if denied:
        return denied
    if _lock.locked():
        try:
            _lock.release()
            return Response("✓ Lock cleared — scans can now run.", content_type="text/plain")
        except RuntimeError:
            return Response("Lock was already free (release failed).", content_type="text/plain")
    return Response("Lock was not held — nothing to clear.", content_type="text/plain")


@app.route("/admin/listing-patterns")
def admin_listing_patterns():
    """Analyze date_listed distribution across the cached inventory to reveal
    how GC batches new listings — by day, hour-of-day, and minute within hour.
    Protected by admin session."""
    denied = _require_admin()
    if denied:
        return denied

    from collections import Counter

    # (v2.16.46, Phase F 5a) Every catalog row (available or not — same set the
    # JSON catalog held), from Postgres.
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT date_listed FROM items")
                return [r[0] or "" for r in cur.fetchall()]
    try:
        _all_dates = _pg_read(_q)
    except Exception as e:
        return Response(f"Postgres unavailable: {type(e).__name__}", status=503,
                        content_type="text/plain")

    dates, hours, minutes, exact_times, items_no_date = [], [], [], [], 0
    for dl in _all_dates:
        if not dl:
            items_no_date += 1
            continue
        exact_times.append(dl)
        # "2026-04-15T14:23:00Z"
        try:
            date_part  = dl[:10]          # "2026-04-15"
            hour_part  = int(dl[11:13])   # 14
            minute_part= int(dl[14:16])   # 23
            dates.append(date_part)
            hours.append(hour_part)
            minutes.append(minute_part)
        except Exception:
            pass

    total = len(exact_times) + items_no_date
    by_date  = Counter(dates).most_common(60)
    by_hour  = sorted(Counter(hours).items())
    by_minute= sorted(Counter(minutes).items())

    # Look for clustering: what fraction of items land on the exact :00 second?
    on_zero_second = sum(1 for t in exact_times if t.endswith("T00:00:00Z") or t[17:19] == "00")
    on_midnight    = sum(1 for t in exact_times if t[11:19] == "00:00:00")

    # Sample of 40 most recent timestamps (sorted desc)
    recent = sorted(exact_times, reverse=True)[:40]

    S = '<style>body{font-family:monospace;background:#111;color:#ddd;padding:24px;max-width:900px}' \
        'h2{color:#f5c518}table{border-collapse:collapse;width:100%}' \
        'td,th{border:1px solid #333;padding:6px 10px;text-align:right}' \
        'th{background:#222;text-align:center}td:first-child{text-align:left}' \
        '.bar{display:inline-block;background:#c00;height:12px;vertical-align:middle}' \
        '.note{color:#888;font-size:.85em;margin:8px 0}</style>'

    def bar(n, mx):
        w = int(n / mx * 200) if mx else 0
        return f'<span class="bar" style="width:{w}px"></span> {n:,}'

    html = [f'<html><head><title>GC Listing Patterns</title>{S}</head><body>']
    html.append(_admin_nav('/admin/listing-patterns'))
    html.append(f'<h2>GC Listing Pattern Analysis</h2>')
    html.append(f'<p class="note">Total items in cache: <b>{total:,}</b> &nbsp;|&nbsp; '
                f'With date_listed: <b>{len(exact_times):,}</b> &nbsp;|&nbsp; '
                f'Missing date: <b>{items_no_date:,}</b></p>')
    html.append(f'<p class="note">Items landing at exactly midnight UTC: <b>{on_midnight:,}</b> '
                f'({on_midnight/len(exact_times)*100:.1f}%)</p>')
    html.append(f'<p class="note">Items with :00 seconds: <b>{on_zero_second:,}</b> '
                f'({on_zero_second/len(exact_times)*100:.1f}%) — '
                f'(high % = timestamps truncated to the minute, not exact)</p>')

    # By hour of day
    mx_h = max(c for _,c in by_hour) if by_hour else 1
    html.append('<h2>Items by Hour of Day (UTC)</h2><table><tr><th>Hour (UTC)</th><th>Count</th><th>Distribution</th></tr>')
    for h, c in by_hour:
        html.append(f'<tr><td>{h:02d}:00</td><td>{c:,}</td><td>{bar(c, mx_h)}</td></tr>')
    html.append('</table>')

    # By minute within the hour
    mx_m = max(c for _,c in by_minute) if by_minute else 1
    html.append('<h2>Items by Minute Within Hour</h2>'
                '<p class="note">Spikes at :00 or other specific minutes = batch publishing</p>'
                '<table><tr><th>Minute</th><th>Count</th><th>Distribution</th></tr>')
    for m, c in by_minute:
        html.append(f'<tr><td>:{m:02d}</td><td>{c:,}</td><td>{bar(c, mx_m)}</td></tr>')
    html.append('</table>')

    # By date (most recent first)
    mx_d = max(c for _,c in by_date) if by_date else 1
    html.append('<h2>Items by Date Listed (top 60)</h2><table><tr><th>Date</th><th>Count</th><th>Distribution</th></tr>')
    for d, c in sorted(by_date, reverse=True):
        html.append(f'<tr><td>{d}</td><td>{c:,}</td><td>{bar(c, mx_d)}</td></tr>')
    html.append('</table>')

    # 40 most recent timestamps raw
    html.append('<h2>40 Most Recent date_listed Values</h2>'
                '<p class="note">Look for identical timestamps (batch) vs spread-out (item-by-item)</p>'
                '<table><tr><th>Timestamp (UTC)</th></tr>')
    for t in recent:
        html.append(f'<tr><td>{t}</td></tr>')
    html.append('</table>')

    html.append('</body></html>')
    return Response("".join(html), content_type="text/html")


@app.route("/api/reset", methods=["POST"])
@optional_user_context
def api_reset():
    """Delete scan state files to start fresh. Preserves favorites, watchlist,
    and want list. Since v2.17.0 the item catalog lives only in Postgres and
    is NOT touched here — it holds the sold/delisted history Chuck keeps on
    purpose, and wiping it would also erase every user's watchlist targets."""
    denied = _require_admin_api()
    if denied:
        return denied
    deleted = []
    for f in [STATE_FILE, OUTPUT_FILE,
              DATA_DIR / "gc_last_scan.txt",
              DATA_DIR / "gc_invalid_stores.json",
              DATA_DIR / "gc_condition_diag.json",
              DATA_DIR / "gc_debug_listing.html"]:
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    return jsonify({"deleted": deleted,
                    "status": "Reset complete (item catalog kept). Ready for a fresh baseline."})

@app.route("/api/clear-blocklist", methods=["POST"])
@optional_user_context
def api_clear_blocklist():
    """Remove the invalid stores blocklist so all stores are re-evaluated."""
    denied = _require_admin_api()
    if denied:
        return denied
    f = DATA_DIR / "gc_invalid_stores.json"
    if f.exists():
        f.unlink()
    return jsonify({"status": "Blocklist cleared. Run Validate Stores to re-check all stores."})

def _build_stores_noscript() -> str:
    """Build a <noscript> block listing all known GC store locations for SEO.
    Generated at request time so it always reflects the live store cache."""
    try:
        stores = json.loads(STORES_CACHE.read_text()).get("stores", []) if STORES_CACHE.exists() else []
    except Exception:
        stores = []
    if not stores:
        return ''
    # City names link to the per-store landing pages — the crawl path from "/" to
    # all ~240 store pages (sitemap alone is weaker than real links). (2026-07 audit S4)
    links = ", ".join(
        f'<a href="/store/{_store_slug(s)}" style="color:#777">{_html.escape(s)}</a>'
        for s in stores
    )
    return (
        '<noscript><p style="padding:12px 20px;text-align:center;color:#666;'
        'font-size:.72rem;line-height:1.7">'
        'GC Used Inventory Tracker requires JavaScript. '
        'This tool searches used guitar gear at Guitar Center locations nationwide including: '
        + links +
        '.</p></noscript>'
    )

@app.route("/")
@optional_user_context
def index():
    return HTML_TEMPLATE.replace('<!-- __STORES_NOSCRIPT__ -->', _build_stores_noscript())


# ── Per-store SEO landing pages (v2.14.5, 2026-07 audit S1) ──────────────────
# Server-rendered, zero-JS pages so Google has a city-specific URL/title/snippet
# to rank for "guitar center <city> inventory" queries (the homepage was ranking
# page-1 for these with ~zero CTR because it shows a generic title). Purely
# additive — the main app is untouched. (v2.16.46, Phase F 5a) Data comes from
# Postgres (two small indexed queries per store); rendered pages are memoized
# per store for _STORE_PAGE_TTL seconds — the old key was the JSON file's
# mtime, which step 5 retires. Unauthenticated route, so the TTL also bounds
# how often a crawler can make us hit the DB.

_STORE_PAGE_CACHE: dict = {}   # slug -> (rendered_at_epoch, html)
_STORE_PAGE_TTL = 600          # seconds; scans land every ~few hours

def _store_slug(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower())
    return re.sub(r'-+', '-', s).strip('-')

def _store_slug_map() -> dict:
    """slug -> store name, from the live store cache (4KB read; cheap per request)."""
    try:
        stores = json.loads(STORES_CACHE.read_text()).get("stores", []) if STORES_CACHE.exists() else []
    except Exception:
        stores = []
    return {_store_slug(s): s for s in stores if s}

def _pg_store_page_data(store_name: str):
    """(count, {category: n}, newest 50 items) for one store's available
    inventory, from Postgres. Items are dicts with the keys the page uses."""
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(NULLIF(category, ''), 'Other'), COUNT(*) FROM items "
                    "WHERE available AND store = %s GROUP BY 1 ORDER BY 2 DESC, 1",
                    (store_name,))
                cats = {c: int(n) for c, n in cur.fetchall()}
                cur.execute(
                    "SELECT name, brand, price, condition, date_listed, url FROM items "
                    "WHERE available AND store = %s ORDER BY date_listed DESC, sku ASC LIMIT 50",
                    (store_name,))
                items = [{"name": r[0], "brand": r[1], "price": float(r[2] or 0),
                          "condition": r[3], "date_listed": r[4], "url": r[5]}
                         for r in cur.fetchall()]
        return sum(cats.values()), cats, items
    return _pg_read(_q)


def _render_store_page(store_name: str, slug: str) -> str:
    count, cat_counts, items = _pg_store_page_data(store_name)
    esc = _html.escape
    city = esc(store_name)
    title = f"Guitar Center {city} Used Gear — Live Inventory ({count} items)"
    # Build desc as PLAIN text — it gets esc()'d exactly once at each interpolation
    # point below. v2.15.0 escaped category names here too, so "&" reached the
    # rendered meta description as a literal "&amp;" (double-escape; Google showed
    # it verbatim in snippets). v2.15.1 fix.
    desc = (f"Browse {count} used items currently at the Guitar Center {store_name} store: "
            + ", ".join(f"{c} ({n})" for c, n in sorted(cat_counts.items(), key=lambda x: -x[1])[:4])
            + ". Updated after every scan — free watch list and want list at GC Used Inventory Tracker.")
    rows = []
    for i in items[:50]:
        price = i.get("price") or 0
        rows.append(
            "<tr><td><a href=\"" + esc(i.get("url") or "https://www.guitarcenter.com/") + "\" rel=\"nofollow noopener\">"
            + esc(i.get("name") or "") + "</a></td>"
            + "<td>" + esc(i.get("brand") or "") + "</td>"
            + "<td>" + (f"${price:,.2f}" if price else "") + "</td>"
            + "<td>" + esc(i.get("condition") or "") + "</td>"
            + "<td>" + esc(_fmt_date(i.get("date_listed") or "")) + "</td></tr>"
        )
    cats_html = " · ".join(
        esc(c) + " <b>" + str(n) + "</b>"
        for c, n in sorted(cat_counts.items(), key=lambda x: -x[1])
    )
    item_list_ld = json.dumps({
        "@context": "https://schema.org", "@type": "ItemList",
        "name": f"Used gear at Guitar Center {store_name}",
        "numberOfItems": count,
        "itemListElement": [
            {"@type": "ListItem", "position": n + 1,
             "name": i.get("name") or "", "url": i.get("url") or ""}
            for n, i in enumerate(items[:20])
        ],
    }, separators=(",", ":"))
    breadcrumb_ld = json.dumps({
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "GC Used Inventory Tracker", "item": "https://gcgeartracker.com/"},
            {"@type": "ListItem", "position": 2, "name": f"Guitar Center {store_name}", "item": f"https://gcgeartracker.com/store/{slug}"},
        ],
    }, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="https://gcgeartracker.com/store/{slug}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://gcgeartracker.com/store/{slug}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:image" content="https://gcgeartracker.com/static/og-image.png">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<script type="application/ld+json">{item_list_ld}</script>
<script type="application/ld+json">{breadcrumb_ld}</script>
<style>
body{{background:#111;color:#ddd;font-family:-apple-system,system-ui,sans-serif;margin:0;padding:20px;line-height:1.5}}
main{{max-width:960px;margin:0 auto}}
h1{{color:#fff;font-size:1.35rem;margin:8px 0 4px}}
.sub{{color:#888;font-size:.9rem;margin-bottom:14px}}
.cats{{color:#aaa;font-size:.82rem;margin-bottom:18px}} .cats b{{color:#eee}}
.cta{{display:inline-block;background:#c00;color:#fff;padding:9px 16px;border-radius:6px;text-decoration:none;font-weight:600;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{text-align:left;color:#888;font-weight:600;padding:7px 10px;border-bottom:1px solid #333}}
td{{padding:7px 10px;border-bottom:1px solid #222}}
td a{{color:#e88;text-decoration:none}} td a:hover{{text-decoration:underline}}
.foot{{color:#555;font-size:.75rem;margin-top:22px}} .foot a{{color:#888}}
</style>
</head>
<body><main>
<p class="sub"><a href="/" style="color:#888">← GC Used Inventory Tracker</a></p>
<h1>Guitar Center {city} — Used Gear Inventory</h1>
<p class="sub">{count} used items currently tracked at this store. Newest 50 shown below; the full list is in the free tracker.</p>
<p class="cats">{cats_html}</p>
<a class="cta" href="/?store={slug}">Browse all {count} {city} items in the tracker →</a>
<table>
<thead><tr><th>Item</th><th>Brand</th><th>Price</th><th>Condition</th><th>Listed</th></tr></thead>
<tbody>{"".join(rows) if rows else '<tr><td colspan="5" style="color:#777">No used items at this store right now — check back after the next scan.</td></tr>'}</tbody>
</table>
<p class="foot">Prices and availability change constantly — click any item for the live Guitar Center listing.
Independent tool — not affiliated with Guitar Center, Inc. · <a href="/privacy">Privacy Policy</a></p>
</main></body>
</html>"""

@app.route("/store/<slug>")
def store_page(slug):
    name = _store_slug_map().get(slug)
    if not name:
        return "Not found", 404
    now = time.time()
    hit = _STORE_PAGE_CACHE.get(slug)
    if hit and now - hit[0] < _STORE_PAGE_TTL:
        return hit[1]
    try:
        html_out = _render_store_page(name, slug)
    except Exception as e:
        print(f"[pg] store page {slug!r} failed: {type(e).__name__}: {e}")
        if hit:
            return hit[1]   # stale beats an error page for a crawler
        return "Store inventory is temporarily unavailable — please try again shortly.", 503
    _STORE_PAGE_CACHE[slug] = (now, html_out)
    return html_out

@app.route("/cl")
def cl_page():
    return CL_TEMPLATE

@app.route("/newdeals")
def newdeals_page():
    denied = _require_admin()
    if denied: return denied
    return NEWDEALS_TEMPLATE

@app.route("/api/new-scan", methods=["POST"])
def api_new_scan():
    """Fetch all new GC inventory from Algolia, dedupe by SKU, cache to disk."""
    denied = _require_admin_api()
    if denied: return denied
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import datetime as _dt2
    try:
        hits0, nb_pages = _fetch_new_page(0)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    all_hits = list(hits0)
    if nb_pages > 1:
        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(_fetch_new_page, p): p for p in range(1, nb_pages)}
            for fut in as_completed(futs):
                try:
                    hits, _ = fut.result()
                    all_hits.extend(hits)
                except Exception:
                    pass

    items = {}
    for hit in all_hits:
        sku = str(hit.get("sku") or hit.get("objectID") or "").strip()
        if not sku or sku in items:
            continue
        name = _clean_name(hit.get("displayName") or hit.get("name") or "")
        if not name:
            continue
        try:    price      = float(hit.get("price") or 0)
        except: continue
        try:    list_price = float(hit.get("listPrice") or 0)
        except: list_price = 0.0
        if price <= 0:
            continue
        pct_off = int((1.0 - price / list_price) * 100) if list_price > price > 0 else 0
        # Category: prefer the structured categories array (same as used gear parsing)
        cats_arr = hit.get("categories") or []
        if cats_arr and isinstance(cats_arr, list) and isinstance(cats_arr[0], dict):
            category = cats_arr[0].get("lvl0") or ""
        else:
            # Fallback: categoryPageIds — skip bare "New"/"Used" sentinel values
            cat_ids  = hit.get("categoryPageIds") or []
            category = next((c for c in cat_ids if c and c.lower() not in ("new", "used", "")), "")
        brand    = (hit.get("brand") or "").strip()
        seo_url  = hit.get("seoUrl") or ""
        items[sku] = {
            "name":        name,
            "brand":       brand,
            "category":    category,
            "price":       price,
            "list_price":  list_price,
            "pct_off":     pct_off,
            "url":         ("https://www.guitarcenter.com" + seo_url) if seo_url else "",
            "is_software": _is_software_item(name, category),
        }

    last_updated = _dt2.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_new_deals_cache(items, last_updated)
    return jsonify({"ok": True, "count": len(items), "last_updated": last_updated})


@app.route("/api/new-browse", methods=["POST"])
def api_new_browse():
    """Browse cached new deals with filters + pagination."""
    denied = _require_admin_api()
    if denied: return denied
    import re as _re2
    data    = request.json or {}
    cache   = _load_new_deals_cache()
    if not cache:
        return jsonify({"no_cache": True, "items": [], "total": 0})

    items = list(cache.get("items", {}).values())

    # Software/plugin filter (excluded by default)
    if not bool(data.get("include_software")):
        items = [i for i in items if not i.get("is_software", False)]

    # Keyword search
    fq = (data.get("filter_q") or "").strip().lower()
    if fq:
        tokens = fq.split()
        items = [i for i in items if all(
            t in (i["name"] + " " + i["brand"]).lower() for t in tokens)]

    # Brand / category filters
    brands = [b for b in (data.get("filter_brands") or []) if b]
    if brands:
        items = [i for i in items if i["brand"] in brands]
    cats = [c for c in (data.get("filter_categories") or []) if c]
    if cats:
        items = [i for i in items if i["category"] in cats]

    # % off minimum
    min_pct = int(data.get("filter_min_pct_off") or 0)
    if min_pct > 0:
        items = [i for i in items if i["pct_off"] >= min_pct]

    # Price range
    try:
        pmin = float(data["filter_price_min"]) if data.get("filter_price_min") not in (None, "") else None
    except (TypeError, ValueError):
        pmin = None
    try:
        pmax = float(data["filter_price_max"]) if data.get("filter_price_max") not in (None, "") else None
    except (TypeError, ValueError):
        pmax = None
    if pmin is not None: items = [i for i in items if i["price"] >= pmin]
    if pmax is not None: items = [i for i in items if i["price"] <= pmax]

    # Want list keyword filter — whole-word match per keyword, OR logic across keywords
    if bool(data.get("filter_want_list")):
        keywords = [k.lstrip("=").strip() for k in (data.get("keywords") or []) if k.strip()]
        if keywords:
            pats = [_re2.compile(r'\b' + _re2.escape(k) + r'\b', _re2.IGNORECASE) for k in keywords]
            items = [i for i in items if any(
                p.search(i["name"] + " " + i["brand"]) for p in pats)]

    # Collect available brand + category facets for dropdowns
    all_brands = sorted(set(i["brand"] for i in items if i["brand"]))
    all_cats   = sorted(set(i["category"] for i in items if i["category"]))
    total      = len(items)

    # Sort
    sort_field = data.get("sort") or "pct_off"
    reverse    = (data.get("dir") or "desc") == "desc"
    if sort_field == "price":
        items.sort(key=lambda i: i["price"], reverse=reverse)
    elif sort_field == "list_price":
        items.sort(key=lambda i: i["list_price"], reverse=reverse)
    elif sort_field == "name":
        items.sort(key=lambda i: i["name"].lower(), reverse=reverse)
    elif sort_field == "brand":
        items.sort(key=lambda i: i["brand"].lower(), reverse=reverse)
    elif sort_field == "category":
        items.sort(key=lambda i: i["category"].lower(), reverse=reverse)
    else:  # pct_off default
        items.sort(key=lambda i: i["pct_off"], reverse=reverse)

    # Paginate
    per_page    = 50
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = max(1, min(int(data.get("page") or 1), total_pages))
    offset      = (page - 1) * per_page

    return jsonify({
        "items":        items[offset:offset + per_page],
        "total":        total,
        "page":         page,
        "total_pages":  total_pages,
        "brands":       all_brands,
        "categories":   all_cats,
        "last_updated": cache.get("last_updated", ""),
    })

@app.route("/privacy")
def privacy_page():
    return PRIVACY_TEMPLATE

@app.route("/google73eeaa5f083d2e84.html")
def google_site_verification():
    return "google-site-verification: google73eeaa5f083d2e84.html", 200, {"Content-Type": "text/html"}

@app.route("/robots.txt")
def robots_txt():
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /go/\n"
        "Disallow: /alerts/\n"
        "\n"
        "Sitemap: https://gcgeartracker.com/sitemap.xml\n"
    )
    return content, 200, {"Content-Type": "text/plain"}

@app.route("/sitemap.xml")
def sitemap_xml():
    # Per-store landing pages included with lastmod = last scan date, so Google
    # recrawls city pages after every scan day. (2026-07 audit S3)
    lastmod = ""
    try:
        last_scan_file = DATA_DIR / "gc_last_scan.txt"
        if last_scan_file.exists():
            raw = last_scan_file.read_text().strip()[:10]  # YYYY-MM-DD
            if len(raw) == 10:
                lastmod = f"<lastmod>{raw}</lastmod>"
    except Exception:
        pass
    urls = [
        f'  <url><loc>https://gcgeartracker.com/</loc>{lastmod}'
        '<changefreq>daily</changefreq><priority>1.0</priority></url>',
        '  <url><loc>https://gcgeartracker.com/privacy</loc>'
        '<changefreq>monthly</changefreq><priority>0.3</priority></url>',
    ]
    for slug in sorted(_store_slug_map()):
        urls.append(
            f'  <url><loc>https://gcgeartracker.com/store/{slug}</loc>{lastmod}'
            '<changefreq>daily</changefreq><priority>0.6</priority></url>'
        )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) +
        '\n</urlset>\n'
    )
    return content, 200, {"Content-Type": "application/xml"}

@app.route("/.well-known/security.txt")
def security_txt():
    # RFC 9116 — gives researchers a private channel to report issues instead of
    # posting "this isn't secure" publicly. Update Expires before it lapses.
    content = (
        "Contact: mailto:chuck@gcgeartracker.com\n"
        "Expires: 2027-06-05T00:00:00Z\n"
        "Preferred-Languages: en\n"
        "Canonical: https://gcgeartracker.com/.well-known/security.txt\n"
    )
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

NEWDEALS_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GC New Deals — Admin</title>
<link rel="icon" type="image/svg+xml" href="/static/og-image.svg">
<link rel="stylesheet" href="/static/gc.css">
<link rel="stylesheet" href="/static/newdeals.css">
<!-- __GA__ -->
</head>
<body>
<div class="nd-page">

  <div class="nd-header">
    <a href="/" class="nd-back-link">&#8592; Tracker</a>
    <h1 class="nd-title">GC New Deals <span class="nd-admin-badge">Admin</span></h1>
  </div>

  <div class="nd-status">
    <span id="nd-refresh-time">No data cached yet &#8212; click Refresh to load.</span>
    <span id="nd-item-count"></span>
    <button id="nd-refresh-btn" data-action="refresh">&#8635; Refresh Data</button>
  </div>

  <div class="nd-top-bar" id="nd-top-bar">
    <div class="nd-chips">
      <button class="chip-btn" id="nd-wl-btn" data-action="toggle-wantlist">&#127919; Want List</button>
      <label class="nd-sw-label">
        <input type="checkbox" id="nd-include-sw"> Include Software / Plugins
      </label>
    </div>
    <div class="nd-filter-bar">
      <div class="nd-search-wrap">
        <input type="text" id="nd-search" placeholder="Search items&#8230;" autocomplete="off">
      </div>
      <select id="nd-brand-sel"><option value="">All Brands</option></select>
      <select id="nd-cat-sel"><option value="">All Categories</option></select>
      <select id="nd-pct-sel">
        <option value="0">Any discount</option>
        <option value="20">20%+ off</option>
        <option value="30">30%+ off</option>
        <option value="40" selected>40%+ off</option>
        <option value="50">50%+ off</option>
        <option value="60">60%+ off</option>
      </select>
      <input type="number" id="nd-price-min" placeholder="$Min" min="0">
      <span class="nd-price-sep">&#8211;</span>
      <input type="number" id="nd-price-max" placeholder="$Max" min="0">
      <button id="nd-clear-btn" data-action="clear-filters">&#10005; Clear</button>
    </div>
  </div>

  <div class="nd-results-hdr">
    <span id="nd-result-count"></span>
  </div>

  <div id="nd-empty-msg" class="nd-empty">No data cached yet &#8212; click &#8635; Refresh Data to load inventory.</div>

  <div id="nd-results-wrap" style="display:none">
    <table class="nd-table">
      <thead>
        <tr>
          <th class="nd-th" data-sort="pct_off">% Off</th>
          <th class="nd-th" data-sort="price">Sale Price</th>
          <th class="nd-th" data-sort="list_price">MSRP</th>
          <th class="nd-th nd-th-name" data-sort="name">Name</th>
          <th class="nd-th" data-sort="brand">Brand</th>
          <th class="nd-th" data-sort="category">Category</th>
        </tr>
      </thead>
      <tbody id="nd-tbody"></tbody>
    </table>
  </div>

  <div id="nd-paginator" class="nd-paginator"></div>

  <div id="dev-footer">
    <span><a href="/">&#8592; Main Tracker</a> &nbsp;&#183;&nbsp; GC New Deals (Admin)</span>
  </div>

</div>
<script src="/static/newdeals.js" defer></script>
</body>
</html>
"""

CL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CL Used Gear Search</title>
<link rel="stylesheet" href="/static/cl.css">
<!-- __GA__ -->
</head>
<body>

<header>
  <h1>CL Used Gear Search <span>craigslist aggregator</span></h1>
  <span id="hdr-user"></span>
  <button id="hdr-signout" style="display:none">Sign Out</button>
</header>

<div class="cl-wrap">
  <!-- City sidebar -->
  <div class="cl-left">
    <div class="search-wrap">
      <input id="cl-city-search" type="text" placeholder="Search cities…" autocomplete="off">
      <div class="cl-sel-btns">
        <button class="cl-sel-btn" id="cl-favs-btn">★ Favorites</button>
        <button class="cl-sel-btn" id="cl-select-all-btn">Select All</button>
        <button class="cl-sel-btn" id="cl-clear-all-btn">Clear All</button>
      </div>
    </div>
    <div id="cl-city-list"></div>
  </div>

  <!-- Search + results -->
  <div class="cl-right">
    <div class="cl-search-bar">
      <input id="cl-query" type="text" placeholder="e.g. telecaster, les paul, fender twin…"
        autocomplete="off">
      <span id="cl-status"></span>
      <button id="cl-search-btn">Search</button>
    </div>

    <div class="cl-results-hdr" id="cl-toolbar">
      <button id="cl-watchlist-toggle" class="cl-chip">★ Watch List</button>
      <button id="cl-wantlist-btn" class="cl-chip">🎯 Want List</button>
      <a id="cl-wl-link" style="display:none">Clear Want List Search</a>
    </div>

    <div class="cl-results-hdr" id="cl-results-hdr" style="display:none">
      <span id="cl-count"></span>
      <input id="cl-res-search" type="text" placeholder="Filter results…" autocomplete="off">
    </div>

    <div id="cl-body">
      <div class="cl-empty">Select cities on the left, enter a search term, and press Search.<br><br>
        Searches all selected Craigslist markets simultaneously for used musical gear.</div>
    </div>
  </div>
</div>

<!-- Auth modal -->
<div id="auth-modal" class="open">
  <div class="auth-box">
    <h2>Sign In</h2>
    <p>Sign in with your GC Tracker account to search Craigslist.</p>
    <div id="cl-google-wrap" style="display:none">
      <button class="auth-google-btn" id="cl-auth-google-btn">
        <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.566 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
        Sign in with Google
      </button>
      <div class="auth-divider"><span>or sign in with username</span></div>
    </div>
    <div class="auth-field"><label>Username</label><input id="auth-user" type="text" autocomplete="username"></div>
    <div class="auth-field"><label>Password</label><input id="auth-pw" type="password" autocomplete="current-password"></div>
    <button class="auth-submit" id="cl-login-submit">Sign In</button>
    <div class="auth-err" id="auth-err"></div>
  </div>
</div>

<script src="/static/cl.js"></script>
</body>
</html>"""

@app.route("/download/excel")
@optional_user_context
def download_excel():
    if not OUTPUT_FILE.exists():
        return "No Excel file yet — run the tracker first.", 404
    return send_file(OUTPUT_FILE, as_attachment=True,
                     download_name="gc_new_inventory.xlsx")

@app.route("/api/stores")
@optional_user_context
def api_stores():
    return jsonify({
        "stores":    get_store_list(),
        "info":      get_store_info(),
    })

@app.route("/api/stores/refresh", methods=["POST"])
@optional_user_context
def api_stores_refresh():
    # Admin only: this fans out dozens of synchronous outbound requests to
    # guitarcenter.com AND overwrites the shared store-list cache. Left open it was
    # both an unauthenticated outbound-amplification DoS and a way for anyone to wipe
    # the global store list. Not called by any frontend — it's a maintenance action.
    denied = _require_admin_api()
    if denied:
        return denied
    stores = refresh_store_list()
    info   = get_store_info()
    return jsonify({"stores": stores,
                    "count": len(stores), "info": info})

# (Dead legacy POST /api/favorites removed in v2.14.5 — favorites live in SQLite via
#  /api/sync; no frontend called it. Login-gated since v2.12.28, deleted per 2026-07
#  audit E3. FAVORITES_FILE stays: admin export/import/reset still reference it.)


# Synthetic brand-facet label so brandless items (brand == "") are filterable
# in both /api/browse and /api/saved-search-counts.
NO_BRAND_LABEL = "(none)"


# ── Shared query-matching helpers (v2.16.0) ──────────────────────────────────
# Hoisted from api_browse's function locals so /api/saved-search-counts uses
# IDENTICAL semantics (its old inline copy had drifted: it treated filter_strict
# as whole-word when browse treats it as fuzzy/contains, and it searched 6 fields
# where browse searches name+brand — so counts didn't match applied results).
# Also adds the v2.16.0 operators: leading '-' = NOT on a term, ';' = OR between
# clauses (lowest precedence). Comma/space AND, quotes, wildcards unchanged.
# v2.16.3 adds 'prefix : b1; b2; ...' (_expand_colon_prefix) so a common AND/NOT
# prefix applies to every OR branch instead of just the one it's typed next to.
# These are pure functions of their inputs — no request state.

_SIMPLE_KW_RE = re.compile(r'^\w+$')   # a single word token
_KW_SPLIT_RE  = re.compile(r'\W+')     # tokenizer matching \b boundaries

# ── Colon-prefix OR expansion (v2.16.3) ───────────────────────────────────────
# User-reported gap: 'Mesa, -combo; Angel; Blues; ...' only applies "Mesa" and
# "-combo" to the FIRST clause — ';' has no cross-clause memory by design (no
# parentheses, deliberately, to keep this a flat O(items) matcher on an
# unauthenticated endpoint). 'prefix : branch1; branch2; ...' lets a common
# AND/NOT condition apply to every OR branch by expanding BEFORE the existing,
# already-fuzzed clause compilers ever see it — zero new matching primitives.
_COLON_PREFIX_MAX_BRANCHES = 30  # independent ceiling; expansion multiplies
                                  # token count, so it needs its own cap even
                                  # though the per-entry char/keyword caps exist.

def _find_prefix_colon(s):
    """Index of the first ':' not inside a double-quoted phrase, else -1."""
    in_quotes = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ':' and not in_quotes:
            return i
    return -1

def _expand_colon_prefix(s, join=', '):
    """'prefix : b1; b2; b3' -> 'prefix<join>b1; prefix<join>b2; prefix<join>b3'
    so the prefix (its own comma/dash AND/NOT terms) applies to every OR
    branch instead of just the one it's typed next to. Only the first
    non-quoted ':' is the marker; strings with no such colon are returned
    completely unchanged, so every existing want list / saved search / query
    with no colon in it parses byte-identically to before. `join` matches
    each caller's own AND separator: want-list clauses are comma-AND (', '),
    filter_q clauses are space-AND (' ')."""
    idx = _find_prefix_colon(s)
    if idx == -1:
        return s
    prefix = s[:idx].strip()
    rest = s[idx + 1:]
    if not prefix:
        return s
    branches = [b.strip() for b in rest.split(';') if b.strip()][:_COLON_PREFIX_MAX_BRANCHES]
    if not branches:
        return s
    return '; '.join(prefix + join + b for b in branches)

def _compile_query(query_str, fuzzy=False):
    """Parse a query string into AND-joined terms.
    Syntax:
      Allen          → whole-word match  (won't match Allentown, McAllen)
      "Jam Pedals"   → exact phrase match
      Thorpy, Dane   → comma = AND; each part uses same rules
      OD*            → wildcard: * is a glob wildcard (OD808, OD-1, etc.)
      fuzzy=True     → plain terms use contains matching instead of whole-word
    """
    terms = []
    for part in query_str.split(','):
        part = part.strip()
        if not part:
            continue
        if part.startswith('"') and part.endswith('"') and len(part) > 2:
            terms.append(('exact', part[1:-1].lower()))
        elif '*' in part:
            pieces = [re.escape(p) for p in part.split('*')]
            terms.append(('regex', re.compile('.*'.join(pieces), re.IGNORECASE)))
        elif fuzzy:
            terms.append(('contains', part.lower()))
        else:
            terms.append(('word', re.compile(r'\b' + re.escape(part) + r'\b', re.IGNORECASE)))
    return terms

# search-box (filter_q) tokenizer: ';' OR-clauses of space-separated tokens,
# quoted phrases kept whole, '-tok' negated. Used by _tsquery_filter_q, which
# keeps the old Python compiler's 12-token / 4-clause DoS budget. (The Python
# filter_q matcher itself — _compile_fq_clauses/_fq_text_match/_matches_all/
# _matches_any — was deleted in v2.16.46 once nothing called it.)
_FQ_TOKEN_RE = re.compile(r'-?"[^"]+"|\S+')

def _wl_bool_compile(base):
    """Compile a want-list entry that uses the v2.16.0 ';' (OR) / '-' (NOT)
    syntax. Returns a list of (pos_terms, neg_terms, required_tokens) clauses,
    or None if the entry uses no new syntax (caller falls through to the
    legacy paths — old entries stay byte-identical in behavior). A clause needs
    at least one positive part; an all-negative entry returns None (a want-list
    entry matching "everything except X" would highlight the whole catalog)."""
    parts_all = [p.strip() for cl in base.split(';') for p in cl.split(',')]
    has_neg = any(len(p) > 1 and p[0] == '-' for p in parts_all)
    if ';' not in base and not has_neg:
        return None
    clauses = []
    for cl in base.split(';'):
        cl = cl.strip()
        if not cl:
            continue
        pos, neg, req = [], [], set()
        for p in cl.split(','):
            p = p.strip()
            if not p:
                continue
            if len(p) > 1 and p[0] == '-':
                neg.extend(_compile_query(p[1:]))
            else:
                pos.extend(_compile_query(p))
                pl = p.lower()
                # Required-tokens pre-filter (sound, same trick as the phrase and
                # comma-AND paths): only from plain (non-quoted, non-wildcard) parts.
                if '*' not in pl and not (pl.startswith('"') and pl.endswith('"') and len(pl) > 2):
                    req |= set(_KW_SPLIT_RE.split(pl)) - {''}
        if pos:
            clauses.append((pos, neg, req))
    return clauses or None


# ── Phase F stage 2: tsquery translator (search_vector-backed) ──────────────
# POSTGRES_PHASE_F_DESIGN.md §7 step 2. Since v2.16.40 (step 4b) this is THE
# search implementation for /api/browse (via _pg_browse), and since v2.16.44
# (step 4c) the only one — the Python matcher it was diffed against is gone
# from browse (and, since v2.16.46, from saved-search counts too — the
# Python filter_q matcher was deleted). _wl_bool_compile/_compile_query/
# _expand_colon_prefix are still used by the translator itself and by
# _kw_accept_capped (routing only; nothing matches with their regexes).
#
# Mirrors the ABOVE functions' own parsing/routing decisions one for one
# (same comma=AND, ';'=OR, leading '-'=NOT, same gate for when the OR/NOT
# path even engages) rather than re-deriving the rules independently, so a
# side-by-side read against _wl_bool_compile/_compile_fq_clauses/
# _compile_query is how to audit this for drift. Builds ONE tsquery
# expression per entry as multiple parameterized to_tsquery()/
# phraseto_tsquery() calls joined by SQL's own tsquery operators (&&/||/!!)
# in the query text — never by string-concatenating raw tsquery syntax, so a
# user-typed term can never be interpreted as tsquery operator syntax.
#
# Two DELIBERATE semantic narrowings versus the Python regex matcher, both
# expected to be invisible on real data — the step 3 diff harness must
# specifically exercise wildcard and quoted-phrase entries to confirm that,
# not just trust this reasoning:
#   1. Suffix wildcards ('OD*') translate to a true lexeme-PREFIX match
#      (to_tsquery('simple', 'od:*') — only matches a token that STARTS WITH
#      "od"). The Python regex is unanchored substring matching (val.search()
#      over the whole "name brand" text with no boundary at the wildcard
#      end), so e.g. 'OD*' can also match inside "Wood" today; tsquery's
#      prefix match won't. Tightening, not broadening.
#   2. Quoted "exact phrase" terms ('"Big Muff"') translate to a tsquery
#      phrase (phraseto_tsquery — <->-chained lexemes, word-boundary
#      respecting) rather than the Python path's raw substring containment
#      (_matches_all's `val in text_lower`, which technically allows
#      mid-word matches — '"amp"' would match "trampoline" today).
#      Tightening, not broadening.
# Anything NOT expressible in pure tsquery (a non-suffix wildcard — '*'
# leading, mid-word, multiple, or inside a multi-word term) raises
# _TsqueryUnsupported rather than being silently mistranslated. September
# 2026 production data showed zero real non-suffix-wildcard usage
# (POSTGRES_PHASE_F_DESIGN.md §3), so this is expected to never fire on a
# real want list/saved search, but a caller must not swallow it silently.
# (v2.16.43 made every wildcard shape translatable, so nothing raises it
# today; since 4c /api/browse answers it with a 400 rather than guessing.)
#
# Performance note for step 4, not a step-2 correctness concern: a clause
# that's all-NOT (no positive term — filter_q alone allows this, see
# _tsquery_filter_q below) produces a bare `!!(...)` tsquery, which a GIN
# index can't use directly (negation isn't index-searchable). Rare in
# practice (needs a search of just "-word" with nothing else) but step 4
# should keep it ANDed with an indexed predicate (store/availability) rather
# than ever letting it drive a bare sequential scan of `items`.

class _TsqueryUnsupported(Exception):
    """A parsed term/entry has no pure-tsquery translation (see module notes
    above) — the only case today is a non-suffix wildcard. Callers must
    catch this per-entry, never let it silently drop or mistranslate a
    user's search."""

# Conservative on purpose: only plain word characters plus internal hyphen/
# apostrophe are allowed to be spliced as a bare lexeme (word or word:*) in a
# to_tsquery() argument string — nothing tsquery's own operator syntax
# (&|!():*<>) could ever parse as more than one plain token. Anything else
# either goes through phraseto_tsquery() (a %s parameter, never spliced) or
# raises _TsqueryUnsupported.
_TSQUERY_LEXEME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9'-]*$")

# (v2.16.39) The ONE normalization both sides of every tsquery match go through: every run of
# non-alphanumeric characters becomes a single space. Must stay byte-identical to the
# regexp_replace in pg_schema.sql's search_vector expression. Applied in SQL (not Python) on the
# query side so the database's own [:alnum:] classification is used on both sides.
_PG_SEARCH_NORM_SQL = "regexp_replace(%s, '[^[:alnum:]]+', ' ', 'g')"
# Suffix-wildcard words must be plain ASCII alphanumerics to be spliced as a `word:*` lexeme:
# anything with punctuation would be split by the document-side normalization, so a single
# prefix lexeme could never match it — falls through to _TsqueryUnsupported instead.
_TSQUERY_WILDCARD_WORD_RE = re.compile(r"^[A-Za-z0-9]+$")

def _tsquery_safe_lexeme(word):
    return bool(word) and bool(_TSQUERY_LEXEME_RE.match(word))

def _like_escape(s):
    """Escapes a literal string for safe embedding in an ILIKE pattern under Postgres's
    default ESCAPE '\\'. Order matters: backslash first, THEN the two LIKE wildcard
    characters — escaping % or _ first and backslash second would double-escape the
    backslash that step just introduced."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

def _tsquery_compile_term(part, prefix=False):
    """One already-comma-split piece, mirroring _compile_query's per-part branching
    (quoted-exact / wildcard / plain word-or-phrase). Returns (sql_fragment, params) — a
    COMPLETE boolean SQL predicate (e.g. `search_vector @@ (...)` or an ILIKE clause), NOT
    a bare tsquery/tsvector expression, so callers compose these with plain SQL AND/OR/NOT
    rather than tsquery's &&/||/!! operators (v2.16.35 — see _tsquery_compile_query and
    _tsquery_bool_clauses). Since v2.16.43 every wildcard shape has a translation (leading/
    infix/non-ASCII wildcards -> ILIKE, see below), so this no longer raises in practice;
    _TsqueryUnsupported is kept as the contract for any future untranslatable syntax.

    v2.16.35 changes (POSTGRES_PHASE_F_DESIGN.md's diff-check addenda — real production
    data showed both of these mismatching the old Python matcher):
      - Quoted terms now compile to a pg_trgm-backed ILIKE substring match instead of
        phraseto_tsquery. The 'simple' text-search config has no stemming (so a singular
        quoted query like '"jam pedal"' never matched a plural "Jam Pedals" listing) and
        strips punctuation entirely (so '"Mr. Black"' and '"mr black"' converged on the
        same tsquery). ILIKE against the literal name+brand string restores both: plural/
        substring tolerance (a straight substring match) and punctuation sensitivity —
        matching the old Python matcher's raw substring/regex behavior for quoted terms.
      - Non-quoted terms have hyphens normalized to spaces before hitting to_tsquery/
        phraseto_tsquery, mirroring search_vector's own regexp_replace normalization in
        pg_schema.sql. Without this, Postgres's 'simple' parser treats a hyphen
        immediately followed by digits as a NEGATIVE NUMBER sign (confirmed:
        to_tsvector('simple', 'ES-335') emits lexeme '-335', not '335'), so a plain search
        for '335' silently never matched a hyphenated model number like "ES-335".
      - The wildcard fast path explicitly excludes words containing a hyphen (falls
        through to _TsqueryUnsupported instead) rather than risk a wrong match: unlike
        to_tsvector's document parser, to_tsquery's QUERY parser treats an internal hyphen
        as a phrase separator (to_tsquery('simple', 'ES-3:*') parses as two ADJACENT
        prefix-matched lexemes, 'es':* <-> '-3':*, not one 'es-3' lexeme), and replacing
        the hyphen with a space first doesn't work either — to_tsquery rejects a bare
        space in its query-string mini-language outright. Confirmed via direct Postgres
        testing; no real production want-list/saved-search entry uses a hyphenated suffix
        wildcard today (POSTGRES_PHASE_F_DESIGN.md §3), so falling back to the dormant
        Python matcher for this one rare case is the conservative, correct choice.

    v2.16.36 change (found via the v2.16.35 production diff-check re-run): non-quoted terms
    ALSO get slashes normalized to spaces, same reasoning as hyphens — Postgres's 'simple'
    parser fuses a bare "word/word" pattern into one compound lexeme instead of splitting it
    (confirmed: to_tsvector('simple', 'Mesa/Boogie Rectifier') emits ONE lexeme
    'mesa/boogie'), so a plain search for "mesa" never matched "Mesa/Boogie"-branded items.
    The wildcard fast path needs no separate exclusion for '/' the way it does for '-':
    _TSQUERY_LEXEME_RE's charset never included '/', so a wildcard word containing one
    already falls through to _TsqueryUnsupported on its own — confirmed via direct testing
    that an un-normalized slash wildcard (to_tsquery parses 'mesa/b:*' as one valid lexeme,
    unlike hyphen's phrase-separator behavior) still wouldn't match the now-normalized
    document text anyway, so excluding it is correct either way, and normalizing a wildcard
    word's slash to a space hits the exact same "bare space rejected" syntax error hyphen
    does."""
    if part.startswith('"') and part.endswith('"') and len(part) > 2:
        pattern = '%' + _like_escape(part[1:-1]) + '%'
        return "(coalesce(name, '') || ' ' || coalesce(brand, '')) ILIKE %s", [pattern]
    if '*' in part:
        if part.count('*') == 1 and part.endswith('*'):
            word = part[:-1]
            if word and _TSQUERY_WILDCARD_WORD_RE.match(word):
                return "search_vector @@ to_tsquery('simple', %s)", [word + ':*']
            # (v2.16.40) Trailing wildcard on a multi-word or punctuated term
            # ('Takamine TSP*', 'ES-3*'): split on the SAME separator rule the
            # document side uses (every run of non-alphanumerics), then match
            # the words as an adjacent phrase whose LAST word is a prefix —
            # 'takamine <-> tsp:*'. Found via the v2.16.39 production diff
            # check: one real want-list entry of this shape made every request
            # from that account ineligible for the SQL path. Each piece must be
            # plain ASCII alphanumerics (so splicing it into to_tsquery's
            # mini-language can't be read as operator syntax); anything else —
            # non-ASCII letters, a '*' anywhere but the end — still raises.
            pieces = [w for w in re.split(r'[^A-Za-z0-9]+', word) if w] if word else []
            if (pieces and all(_TSQUERY_WILDCARD_WORD_RE.match(w) for w in pieces)
                    and not re.search(r'[^\x00-\x7f]', word)):
                return ("search_vector @@ to_tsquery('simple', %s)",
                        [" <-> ".join(pieces[:-1] + [pieces[-1] + ':*'])])
        # (v2.16.43) Every other wildcard shape — leading ('*muff'), both ends
        # ('*50s*'), infix ('a*b'), or a trailing wildcard the tsquery paths
        # above can't express ('Höfner B*') — compiles to the SAME pattern the
        # legacy Python matcher uses: _compile_query joins the '*'-split pieces
        # with '.*' and re.search()es it unanchored over "name brand"
        # (case-insensitive), which is exactly ILIKE '%p1%p2%...%' over the
        # same string. Backed by the v2.16.35 pg_trgm index (same expression as
        # quoted terms). Found via the v2.16.40 burn-in: 119 requests in ~17h
        # fell back to the legacy path for '*50s*', and 4c removes that path.
        pattern = '%' + '%'.join(_like_escape(p) for p in part.split('*')) + '%'
        # (Consecutive '%' from '**' or edge '*' are harmless in LIKE; not collapsed, since a
        # naive collapse could swallow an escaped '\\%'.)
        return "(coalesce(name, '') || ' ' || coalesce(brand, '')) ILIKE %s", [pattern]
    # Plain word or an un-quoted multi-word phrase -> phraseto_tsquery, the native
    # equivalent of _compile_query's 'word' \b...\b branch (both are whole-word/phrase,
    # order- and adjacency-preserving). Hyphens and slashes normalized to spaces first —
    # see docstring.
    # (v2.16.39) Normalization now happens in SQL via _PG_SEARCH_NORM_SQL (every run of
    # non-alphanumerics -> one space), identical to search_vector's own expression; this
    # supersedes the v2.16.35/36 Python-side '-'/'/' replace.
    if prefix:
        # (v2.16.45) Search box only — see _tsquery_filter_q: the LAST word of
        # a plain term also matches as the start of a longer word, so 'sm81'
        # finds "SM81LC" and 'strat' finds "Stratocaster" (Chuck missed an
        # SM81LC with plain whole-word matching, 2026-09-25). Split on the
        # same separator rule the document side uses (runs of
        # non-alphanumerics), so 'es-33' -> 'es <-> 33:*'. Pieces are pure
        # alphanumerics (Unicode letters allowed: Postgres [:alnum:] and
        # Python's [^\W_] agree on letters/digits), so splicing them into
        # to_tsquery's mini-language can't be read as operator syntax. A
        # lone one-character term stays whole-word ('a:*' would match nearly
        # everything); as the tail of a phrase it's fine ('ds-1' -> 'ds <->
        # 1:*' finds "DS-1X").
        pieces = [w for w in re.split(r'[\W_]+', part) if w]
        if pieces and (len(pieces) > 1 or len(pieces[0]) >= 2):
            return ("search_vector @@ to_tsquery('simple', %s)",
                    [" <-> ".join(pieces[:-1] + [pieces[-1] + ':*'])])
    return f"search_vector @@ phraseto_tsquery('simple', {_PG_SEARCH_NORM_SQL})", [part]

def _tsquery_compile_query(query_str, prefix=False):
    """Mirrors _compile_query(query_str): comma-separated parts, ANDed. Returns
    (sql_fragment, params), or (None, []) if query_str has no non-empty parts. Raises
    _TsqueryUnsupported if any part isn't pure-tsquery-expressible — never a partial/
    silent translation. v2.16.35: joins with plain SQL AND rather than tsquery's && —
    each part is now a complete boolean predicate, not a bare tsquery (see
    _tsquery_compile_term)."""
    frags, params = [], []
    for part in query_str.split(','):
        part = part.strip()
        if not part:
            continue
        frag, p = _tsquery_compile_term(part, prefix=prefix)
        frags.append(frag)
        params.extend(p)
    if not frags:
        return None, []
    return " AND ".join(frags), params

def _tsquery_bool_clauses(base):
    """Mirrors _wl_bool_compile(base) exactly, including ITS gate for when
    the OR/NOT path applies at all (';' present, OR a leading-dash
    comma-part somewhere — not just 'a dash appears anywhere', so an
    internal hyphen like 'OD-1' correctly does NOT engage this path, same as
    the original). Returns (None, []) when _wl_bool_compile would return
    None (caller falls through to the plain _tsquery_compile_query path,
    exactly like the real matcher-build loop). A clause with no positive
    term is dropped (never engages), matching _wl_bool_compile's own "a
    clause needs at least one positive part" rule — filter_q's version below
    is deliberately different here, see _tsquery_filter_q."""
    parts_all = [p.strip() for cl in base.split(';') for p in cl.split(',')]
    has_neg = any(len(p) > 1 and p[0] == '-' for p in parts_all)
    if ';' not in base and not has_neg:
        return None, []
    clause_frags, params = [], []
    for cl in base.split(';'):
        cl = cl.strip()
        if not cl:
            continue
        pos_frags, neg_frags = [], []
        pos_params, neg_params = [], []
        for p in cl.split(','):
            p = p.strip()
            if not p:
                continue
            if len(p) > 1 and p[0] == '-':
                frag, prm = _tsquery_compile_term(p[1:].strip())
                neg_frags.append(frag)
                neg_params.extend(prm)
            else:
                frag, prm = _tsquery_compile_term(p)
                pos_frags.append(frag)
                pos_params.extend(prm)
        if not pos_frags:
            continue  # matches _wl_bool_compile: needs >=1 positive part
        # v2.16.34 fix: params must be appended in the SAME order the SQL
        # fragments are joined below (positives, then negatives) -- NOT the
        # original left-to-right token order. Before this fix, a negative
        # term appearing before a later positive term in the same clause
        # (e.g. 'A, -B, C') left `params` in encounter order [A, B, C] while
        # `parts` reordered to [fragA, fragC, !!(fragB)] -- so psycopg2
        # substituted B's text into C's placeholder and C's text into B's
        # (negated) placeholder, silently testing the wrong words and
        # sometimes inverting a NOT into a requirement. Confirmed via a real
        # want-list entry ('Ampeg, -pedal: AMG*; AMB*') against a synthetic
        # catalog: the buggy version matched the one item that WAS a pedal
        # and missed both real Ampeg AMG/AMB matches -- see the diff-check
        # addendum in POSTGRES_PHASE_F_DESIGN.md for the full repro.
        #
        # v2.16.35: NOT/AND/OR instead of tsquery's !!/&&/|| -- each frag is now a
        # complete boolean predicate (ILIKE or search_vector @@ (...)), which may mix
        # both types in one clause (e.g. a quoted positive ANDed with a wildcard
        # negative), and those two predicate types don't compose under tsquery's own
        # operators. See _tsquery_compile_term's docstring for why each side changed.
        parts = pos_frags + [f"NOT ({f})" for f in neg_frags]
        clause_frags.append("(" + " AND ".join(parts) + ")")
        params.extend(pos_params)
        params.extend(neg_params)
    if not clause_frags:
        return None, []
    return " OR ".join(clause_frags), params

def _tsquery_want_list_entry(kw):
    """Mirrors the want-list keyword routing the matcher-build loop in
    api_browse() does (strip legacy '=' prefix, v2.16.3 colon-prefix
    expansion, bool-clause path tried first, plain comma-AND/phrase/
    wildcard/quoted path as fallback). Returns (sql_fragment, params), or
    (None, []) for an empty/no-op entry."""
    base = kw.lstrip('=').strip()
    if not base:
        return None, []
    base = _expand_colon_prefix(base, join=', ')
    frag, params = _tsquery_bool_clauses(base)
    if frag is not None:
        return frag, params
    # (v2.16.38) The OR/NOT path ENGAGED (same gate as _wl_bool_compile: ';'
    # present or a leading-dash part) but produced no clause with a positive
    # term — an all-negative entry like "-fender" or "-a; -b". The Python
    # matcher treats that as "never highlight the whole catalog": its
    # _wl_bool_compile returns None and the entry falls through to a literal
    # regex (\b-fender\b) that effectively never matches. Before this fix the
    # translator fell through to _tsquery_compile_query, whose hyphen
    # normalization turned "-fender" into a plain "fender" search — i.e.
    # highlighting exactly the items the user asked to EXCLUDE. Found by the
    # step-4a browse diff harness on synthetic data. Treat it as a no-op.
    parts_all = [p.strip() for cl in base.split(';') for p in cl.split(',')]
    if ';' in base or any(len(p) > 1 and p[0] == '-' for p in parts_all):
        return None, []
    return _tsquery_compile_query(base)

def _tsquery_filter_q(fq, max_tokens=12, max_clauses=4):
    """Mirrors _compile_fq_clauses(fq) exactly: colon-prefix expansion, ';'
    = OR between clauses, SPACE-separated tokens within a clause (quoted
    phrases stay one token, via the same _FQ_TOKEN_RE), leading '-' = NOT.
    Same token/clause budget as the Python path (max_tokens/max_clauses
    default to _compile_fq_clauses' own defaults) so this can't be made to
    do more work than the regex matcher already allows. Unlike
    _tsquery_bool_clauses/_wl_bool_compile, a clause with ONLY negative
    terms is kept (matches _compile_fq_clauses: `if pos or neg:`, not `if
    pos:`) — filter_q genuinely supports "-word" meaning "everything except
    word" as a whole query, want-list entries don't. Returns
    (sql_fragment, params), or (None, []) if fq has no clauses."""
    fq = _expand_colon_prefix(fq, join=' ')
    clause_frags, params = [], []
    budget = max_tokens
    for cl in fq.split(';'):
        cl = cl.strip()
        if not cl or budget <= 0:
            continue
        toks = _FQ_TOKEN_RE.findall(cl)[:budget]
        budget -= len(toks)
        pos_frags, neg_frags = [], []
        pos_params, neg_params = [], []
        for tok in toks:
            is_neg = len(tok) > 1 and tok[0] == '-'
            term_text = tok[1:] if is_neg else tok
            # (v2.16.45) Positive terms prefix-match their last word ('sm81'
            # finds "SM81LC"); negated terms stay whole-word, so '-combo'
            # doesn't also hide "Combination..." — the goal is to not MISS
            # things, and a prefix NOT would hide more. Want-list entries
            # (_tsquery_want_list_entry) are unchanged: whole-word.
            frag, prm = _tsquery_compile_query(term_text, prefix=not is_neg)
            if frag is None:
                continue
            if is_neg:
                neg_frags.append(frag)
                neg_params.extend(prm)
            else:
                pos_frags.append(frag)
                pos_params.extend(prm)
        if pos_frags or neg_frags:
            # v2.16.34 fix: same params/fragment-order bug as
            # _tsquery_bool_clauses above -- see its comment for the full
            # explanation and production repro.
            # v2.16.35: NOT/AND instead of !!/&& -- see _tsquery_bool_clauses' comment.
            parts = pos_frags + [f"NOT ({f})" for f in neg_frags]
            clause_frags.append("(" + " AND ".join(parts) + ")")
            params.extend(pos_params)
            params.extend(neg_params)
        if len(clause_frags) >= max_clauses:
            break
    if not clause_frags:
        return None, []
    return " OR ".join(clause_frags), params


@app.route("/api/saved-search-counts", methods=["POST"])
def api_saved_search_counts():
    """Return match counts for each saved search in a single batch call."""
    # Logged-in only: each search is a COUNT over the catalog, so an unbounded
    # unauthenticated request would be a cheap DoS.
    if not session.get("user_id"):
        return jsonify({"error": "Not logged in."}), 401
    data     = request.json or {}
    searches = data.get("searches", [])
    if not searches:
        return jsonify({"counts": []})
    # Hard cap so even authenticated users can't send thousands of searches.
    searches = searches[:50]

    # v2.16.42: counted in Postgres with _pg_browse(count_only=True) — the exact
    # WHERE /api/browse uses (available-only, per-user scan gate, vintage /
    # watched toggles), so a badge equals what applying the search shows.
    # (v2.16.46, Phase F 5a) The per-search JSON fallback is gone: a search
    # that can't be counted (DB error, untranslatable filter_q) comes back as
    # null and static/gc.js leaves that badge blank.
    user_last_scan = (data.get("user_last_scan") or "").strip()
    wl_ids = set(data.get("watchlist_ids") or [])
    def _f(v):
        try: return float(v) if v is not None and v != '' else None
        except (TypeError, ValueError): return None
    counts = []
    for search in searches:
        n = None
        if _PG_POOL is not None:
            stores = list(search.get("stores") or [])
            f = search.get("filters") or {}
            def _q(stores=stores, f=f):
                return _pg_browse(
                    store_set=set(stores), search_all=not stores,
                    user_last_scan=user_last_scan, kw_entries=[],
                    fq=(f.get("filter_q") or "").lower().strip()[:200],
                    f_brands=f.get("filter_brands") or [], f_conds=f.get("filter_conditions") or [],
                    f_cats=f.get("filter_categories") or [], f_subs=f.get("filter_subcategories") or [],
                    f_watched=bool(f.get("filter_watched")), wl_ids=wl_ids, f_want_only=False,
                    f_price_drop_only=bool(f.get("filter_price_drop_only")),
                    f_vintage_only=bool(f.get("vintage_only")),
                    f_price_min=_f(f.get("filter_price_min")), f_price_max=_f(f.get("filter_price_max")),
                    sort_field="date", sort_dir="desc", user_sorted=True,
                    new_ids=set(), fav_stores=set(), page=1, per_page=1, count_only=True)
            try:
                n = _pg_read(_q)
            except _PgBrowseIneligible:
                n = None
            except Exception as e:
                print(f"[pg] saved-search count failed: {type(e).__name__}: {e}")
                n = None
        counts.append(n)
    return jsonify({"counts": counts})


# ── Algolia key health check ────────────────────────────────────────────────
# The Algolia search key is the single point of failure for scans: if GC ever
# rotates it, scans start returning 401/403 and silently stop finding inventory.
# This endpoint runs the same used-inventory query the scanner uses (hitsPerPage:0)
# so an external monitor can catch a dead key / schema drift within a day. Result
# is cached ~15 min so it can't be hammered to burn GC's Algolia quota
# (≤ ~96 real probes/day no matter how often it's hit). Public, returns no secret.
_ALGOLIA_HEALTH = {"ts": 0.0, "result": None}
_ALGOLIA_HEALTH_TTL = 900  # seconds
# Refresh lock: without it, N concurrent requests arriving after TTL expiry each fire
# a live probe at GC before any writes the result back — the "≤96 probes/day" cap only
# holds if misses are serialized. (2026-07 audit L1)
_ALGOLIA_HEALTH_LOCK = threading.Lock()

@app.route("/api/health/algolia")
def api_health_algolia():
    import time as _t
    now = _t.time()
    cached = _ALGOLIA_HEALTH["result"]
    if cached is not None and (now - _ALGOLIA_HEALTH["ts"]) < _ALGOLIA_HEALTH_TTL:
        return jsonify(dict(cached, cached=True))
    # Serialize the refresh — losers of the race return the (stale) cached value
    # instead of firing their own probe at GC's Algolia.
    if not _ALGOLIA_HEALTH_LOCK.acquire(blocking=False):
        if cached is not None:
            return jsonify(dict(cached, cached=True))
        _ALGOLIA_HEALTH_LOCK.acquire()  # cold start: wait for the first probe
        _ALGOLIA_HEALTH_LOCK.release()
        fresh = _ALGOLIA_HEALTH["result"]
        return jsonify(dict(fresh, cached=True)) if fresh is not None else (jsonify({"ok": False, "error": "unavailable"}), 503)
    try:
        # Re-check under the lock — another thread may have refreshed while we waited.
        cached = _ALGOLIA_HEALTH["result"]
        if cached is not None and (_t.time() - _ALGOLIA_HEALTH["ts"]) < _ALGOLIA_HEALTH_TTL:
            return jsonify(dict(cached, cached=True))
        return _algolia_health_probe(now)
    finally:
        _ALGOLIA_HEALTH_LOCK.release()


def _algolia_health_probe(now: float):
    result = {
        "ok": False, "nbHits": 0, "http_status": None,
        "checked_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cached": False,
    }
    try:
        payload = {"requests": [{
            "indexName":    ALGOLIA_INDEX,
            "facetFilters": ["condition.lvl0:Used"],
            "hitsPerPage":  0,
            "page":         0,
            "query":        "",
        }]}
        r = _http.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=15)
        result["http_status"] = r.status_code
        if r.status_code == 200:
            result["nbHits"] = (r.json().get("results") or [{}])[0].get("nbHits", 0)
            result["ok"] = result["nbHits"] > 0
    except Exception as e:
        result["error"] = type(e).__name__
    _ALGOLIA_HEALTH["ts"] = now
    _ALGOLIA_HEALTH["result"] = result
    return jsonify(result)


# ── Postgres browse: sort whitelist ───────────────────────────────────────────
# (History: these came from Phase C/D's Tier 1 SQL path, _pg_tier1_browse,
# which _pg_browse grew out of; Tier 1 and Tier 2 were deleted in v2.16.44.)
#
# sort_field is user-controlled (the request body), so every column reference
# below comes from a fixed whitelist (_PG_SORT_MAP / the "condition" special
# case) rather than ever being interpolated from the request directly — every
# other value in every WHERE/ORDER BY fragment is passed as a psycopg2 %(name)s
# parameter, never string-formatted into the SQL text.

# sort_field -> (sql column, lowercase-compare?). Mirrors the old Python sort
# `elif` chain (pre-v2.16.44): "price"/"date"/"price_drop_since" get their own raw (unlowered)
# numeric/text comparison exactly like the Python elifs; everything else falls
# in the Python `else` branch, which lowercases — same here. "condition" isn't
# in this map; it gets the quality-rank CASE below, matching the Python
# elif for "condition" (v2.10.18 quality ranking, not alphabetical).
_PG_SORT_MAP = {
    "price":             ("price", False),
    "date":              ("date_listed", False),
    "price_drop_since":  ("price_drop_since", False),
    "name":              ("name", True),
    "brand":             ("brand", True),
    "category":          ("category", True),
    "subcategory":       ("subcategory", True),
    "location":          ("location", True),
    "store":             ("store", True),
    "condition_note":    ("condition_note", True),
    "url":               ("url", True),
    "image_id":          ("image_id", True),
}
_PG_SORTABLE = set(_PG_SORT_MAP) | {"condition"}
_PG_COND_RANK_SQL = (
    "CASE condition "
    "WHEN 'Excellent' THEN 0 WHEN 'Great' THEN 1 WHEN 'Good' THEN 2 "
    "WHEN 'Fair' THEN 3 WHEN 'Poor' THEN 4 ELSE 99 END"
)
_PG_COND_KNOWN_SQL = "condition IN ('Excellent','Great','Good','Fair','Poor')"


# ── Unified Postgres browse path (Phase F step 4; sole path since v2.16.44) ───
# POSTGRES_PHASE_F_DESIGN.md §7 step 4. ONE function serving every
# /api/browse shape — Tier 1's no-search case AND Tier 2's want-list/filter_q
# case — entirely in SQL: availability + store + scan-gate + _apply_base's
# filters + filter_q (via the Stage 2 tsquery translator) + contextual facet
# counts + NEW/want tiering + sort + paginate. Structurally it's the old
# Phase D Tier 1 SQL path with the two things Tier 1 couldn't do bolted in:
#   1. filter_q  -> one more WHERE predicate (_tsquery_filter_q), part of the
#      _apply_base scope exactly like the Python path (so it narrows facets/
#      totals but NOT total_unfiltered/new_count).
#   2. want-list keywords -> a kw_match boolean EXPRESSION, not a WHERE
#      clause. In the Python path kwMatch doesn't filter anything on its own;
#      it feeds (a) the "want list only" filter, (b) the NEW+want tier of the
#      default sort, (c) new_want_count, (d) each returned item's kwMatch flag.
#      Each of those uses it in the matching SQL position below.
#
# LIVE for every request since v2.16.40 (step 4b, with a legacy fallback);
# the ONLY browse path since v2.16.44 (step 4c deleted the legacy Tier 1 /
# Tier 2 / Python path and the 4a comparison tooling).
#
# Performance shape of kw_match — the reason this isn't just
# " OR ".join(_tsquery_want_list_entry(kw) for kw in keywords):
#   * Real want lists run to hundreds of entries, and ~all of them are plain
#     words/phrases, each translating to `search_vector @@ phraseto_tsquery(
#     'simple', %s)`. Hundreds of separate @@ tests per row would be the SQL
#     equivalent of the O(items x keywords) regex loop the Python matcher's
#     bucketing exists to avoid. So every entry whose translation is exactly
#     that single-predicate shape gets MERGED into one tsquery OR-tree —
#     `search_vector @@ (q1 || q2 || ...)` — one @@ per row, and one GIN
#     index probe when it's used as a filter. to_tsquery/phraseto_tsquery
#     with an explicit regconfig are IMMUTABLE, so with literal args Postgres
#     constant-folds the whole tree once at plan time.
#   * Everything else (quoted ILIKE, comma-AND, ';'/'-' bool entries) stays
#     its own OR'd branch — rare, and each still index-backed on its
#     positive side.
#   * The expression is only evaluated where it matters: `is_new AND kw` in
#     the tier ORDER BY / new_want_count (AND short-circuits, so only NEW
#     rows pay for it), as a WHERE predicate only when want-list-only is on,
#     and for the page's own kwMatch flags via a separate tiny query over
#     just that page's SKUs.
#
# Want-list entries honored are exactly the ones that survive the per-shape
# DoS caps (_kw_accept_capped), not the raw list. Any entry or filter_q with no pure-SQL translation
# (_TsqueryUnsupported — none left since v2.16.43 made every wildcard shape translatable)
# makes the WHOLE request ineligible (_PgBrowseIneligible) rather than being
# dropped or guessed — /api/browse answers it with a 400.

# Matches a translated fragment that is exactly ONE `search_vector @@ <tsquery>`
# predicate — the only shape safe to fold into the merged tsquery OR-tree.
_PG_KW_MERGEABLE_RE = re.compile(
    r"^search_vector @@ ("
    + re.escape("phraseto_tsquery('simple', " + _PG_SEARCH_NORM_SQL + ")")
    + "|" + re.escape("to_tsquery('simple', %s)") + r")$")


def _pg_name_params(frag, params, prefix, out):
    """Rewrite a translator fragment's positional %s placeholders as uniquely
    named %(prefixN)s ones, adding the values to `out`. _pg_browse builds all
    its other SQL with named params, and psycopg2
    can't mix positional and named in one statement. Translator fragments
    never contain a literal '%' (ILIKE patterns are passed as parameter
    VALUES, not spliced), so every '%s' is a placeholder."""
    params = list(params)
    assert frag.count("%s") == len(params), (frag, params)
    def _sub(_m):
        key = f"{prefix}{len(out)}"
        out[key] = params.pop(0)
        return f"%({key})s"
    return re.sub(r"%s", _sub, frag)


def _pg_kw_expr(kw_entries, out_params):
    """kw_match boolean SQL expression for a (post-cap) want list, or None if
    no entry produces anything. Raises _TsqueryUnsupported (see above)."""
    merged, others = [], []
    for kw in kw_entries:
        frag, prm = _tsquery_want_list_entry(kw)
        if frag is None:
            continue
        m = _PG_KW_MERGEABLE_RE.match(frag)
        if m:
            merged.append(_pg_name_params(m.group(1), prm, "_kw", out_params))
        else:
            others.append("(" + _pg_name_params(frag, prm, "_kw", out_params) + ")")
    parts = []
    if merged:
        parts.append("search_vector @@ (" + " || ".join(merged) + ")")
    parts.extend(others)
    return ("(" + " OR ".join(parts) + ")") if parts else None


# ── Browse aggregate cache (v2.17.8, Phase G) ──────────────────────────────────
import collections as _collections
_BROWSE_AGG_CACHE = _collections.OrderedDict()   # (gen, key) -> agg dict, LRU
_BROWSE_AGG_LOCK = threading.Lock()
_BROWSE_AGG_MAX = 32          # ~0.5 MB each (the brand list) -> ~16 MB worst case
_BROWSE_AGG_TTL = 900         # seconds; belt and braces — generation is the real invalidation
_BROWSE_AGG_STATS = {"hit": 0, "miss": 0}
_SCAN_GATE_CACHE = {"gen": None, "max_first_seen": None, "noop": {}}


def _browse_agg_get(gen, key):
    with _BROWSE_AGG_LOCK:
        hit = _BROWSE_AGG_CACHE.get((gen, key))
        if hit is not None and time.time() - hit[0] < _BROWSE_AGG_TTL:
            _BROWSE_AGG_CACHE.move_to_end((gen, key))
            _BROWSE_AGG_STATS["hit"] += 1
            return hit[1]
        _BROWSE_AGG_STATS["miss"] += 1
        return None


def _browse_agg_put(gen, key, agg):
    with _BROWSE_AGG_LOCK:
        for k in [k for k in _BROWSE_AGG_CACHE if k[0] != gen]:
            del _BROWSE_AGG_CACHE[k]          # older generations can never be served
        _BROWSE_AGG_CACHE[(gen, key)] = (time.time(), agg)
        _BROWSE_AGG_CACHE.move_to_end((gen, key))
        while len(_BROWSE_AGG_CACHE) > _BROWSE_AGG_MAX:
            _BROWSE_AGG_CACHE.popitem(last=False)


def _pg_scan_gate_is_noop(user_last_scan: str) -> bool:
    """True if no item was first seen after `user_last_scan` (so the browse
    scan gate `first_seen = '' OR first_seen <= uls` matches every row). The
    comparison runs in Postgres (same text collation as the gate itself). Cached
    per catalog generation — the max first_seen only changes when a scan writes.
    Any error → False (keep the gate; always correct, just not shared)."""
    with _PG_CATALOG_GEN_LOCK:
        gen = _PG_CATALOG_GEN[0]
    c = _SCAN_GATE_CACHE
    with _BROWSE_AGG_LOCK:
        if c["gen"] == gen and user_last_scan in c["noop"]:
            return c["noop"][user_last_scan]
    try:
        def _q():
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    if c["gen"] == gen and c["max_first_seen"] is not None:
                        cur.execute("SELECT %s >= %s", (user_last_scan, c["max_first_seen"]))
                        return c["max_first_seen"], bool(cur.fetchone()[0])
                    cur.execute("SELECT MAX(first_seen) FROM items WHERE first_seen <> ''")
                    mfs = cur.fetchone()[0] or ""
                    cur.execute("SELECT %s >= %s", (user_last_scan, mfs))
                    return mfs, bool(cur.fetchone()[0])
        mfs, noop = _pg_read(_q)
    except Exception as e:
        print(f"[pg] scan-gate check failed: {type(e).__name__}: {e}")
        return False
    with _BROWSE_AGG_LOCK:
        if c["gen"] != gen:
            c.update(gen=gen, max_first_seen=mfs, noop={})
        if len(c["noop"]) < 2000:
            c["noop"][user_last_scan] = noop
    return noop


def _pg_browse(*, store_set, search_all, user_last_scan, kw_entries, fq,
               f_brands, f_conds, f_cats, f_subs, f_watched, wl_ids,
               f_want_only, f_price_drop_only, f_vintage_only,
               f_price_min, f_price_max, sort_field, sort_dir, user_sorted,
               new_ids, fav_stores, page, per_page, count_only=False,
               skip_facets_gen=None, f_new_only=False) -> dict:
    """Same JSON shape as api_browse()'s response, for ANY request, computed
    in Postgres. Raises _PgBrowseIneligible (untranslatable search) or any
    DB error — the caller decides what to do (/api/browse: 400/503;
    /api/saved-search-counts: JSON fallback for that search until step 5)."""
    import psycopg2.extras as _pg_extras

    # ── Translate search terms FIRST, before touching the DB ───────────────
    tparams = {}
    try:
        kw_sql = _pg_kw_expr(kw_entries, tparams) if kw_entries else None
        fq_sql = None
        if fq:
            _frag, _prm = _tsquery_filter_q(fq)
            # Python path: `if fq:` then `fq_clauses and _fq_text_match(...)`
            # — a non-empty fq that compiles to no clauses matches NOTHING.
            fq_sql = _pg_name_params(_frag, _prm, "_fq", tparams) if _frag is not None else "FALSE"
    except _TsqueryUnsupported as e:
        raise _PgBrowseIneligible(str(e))
    kw_bool = f"COALESCE({kw_sql}, FALSE)" if kw_sql else "FALSE"

    # (v2.17.8) The per-user scan gate hides rows first seen after the user's
    # last scan. When no such row exists (the user has scanned since the newest
    # first_seen in the catalog) the gate is a no-op — drop it, so this user's
    # aggregates share a cache entry with everyone else in the same position.
    uls_key = user_last_scan or ""
    if user_last_scan and not count_only and _pg_scan_gate_is_noop(user_last_scan):
        user_last_scan = ""
        uls_key = "*"

    # ── Q1 scope: availability + store + per-user scan gate ────────────────
    scope = ["available"]
    params = dict(tparams)
    params["new_ids"] = list(new_ids)
    if not search_all:
        scope.append("store = ANY(%(stores)s)")
        params["stores"] = list(store_set) if store_set else []
    if user_last_scan:
        scope.append("(first_seen = '' OR first_seen <= %(user_last_scan)s)")
        params["user_last_scan"] = user_last_scan
    scope_sql = " AND ".join(scope)

    # ── _apply_base() scope: + filter_q + the non-facet toggles ────────────
    where = list(scope)
    if fq_sql:
        where.append(f"({fq_sql})")
    if f_want_only:
        where.append(f"({kw_sql})" if kw_sql else "FALSE")
    if f_price_drop_only:
        where.append("price_drop > 0")
    if f_new_only:   # v2.22.3 "New" chip: only this user's NEW listings
        where.append("sku = ANY(%(new_ids)s)")
    if f_vintage_only:
        where.append("is_vintage")
    if f_watched:
        where.append("sku = ANY(%(wl_ids)s)")
        params["wl_ids"] = list(wl_ids)
    if f_price_min is not None:
        where.append("price >= %(price_min)s")
        params["price_min"] = f_price_min
    if f_price_max is not None:
        where.append("price <= %(price_max)s")
        params["price_max"] = f_price_max
    base_where_sql = " AND ".join(where)

    # ── Facet clauses (same as the old Phase D Tier 1 path) ────────────────
    def _facet_clause(col, values, key):
        if not values:
            return "TRUE"
        if col == "brand":
            real = [v for v in values if v != NO_BRAND_LABEL]
            want_no_brand = NO_BRAND_LABEL in values
            if real:
                params[key] = real
            if real and want_no_brand:
                return f"(brand = ANY(%({key})s) OR brand = '')"
            if want_no_brand:
                return "brand = ''"
            return f"brand = ANY(%({key})s)"
        params[key] = list(values)
        return f"{col} = ANY(%({key})s)"

    brand_clause = _facet_clause("brand", f_brands, "_f_brand")
    cond_clause  = _facet_clause("condition", f_conds, "_f_cond")
    cat_clause   = _facet_clause("category", f_cats, "_f_cat")
    sub_clause   = _facet_clause("subcategory", f_subs, "_f_sub")
    filtered_where_sql = (f"{base_where_sql} AND {brand_clause} AND {cond_clause} "
                          f"AND {cat_clause} AND {sub_clause}")
    params["no_brand_label"] = NO_BRAND_LABEL

    if count_only:
        # v2.16.42: /api/saved-search-counts asks for just the total_count this
        # exact request would return — same WHERE, so a saved search's badge can't
        # drift from what applying it shows. Returns an int, not the response dict.
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                _tp = time.perf_counter()
                cur.execute(f"SELECT COUNT(*) FROM items WHERE {filtered_where_sql}", params)
                n = int(cur.fetchone()[0])
                _timing_phase("count", (time.perf_counter() - _tp) * 1000.0)
                return n

    # ── ORDER BY (whitelisted columns only — _PG_SORT_MAP) ─────────────────
    is_new_sql = "(sku = ANY(%(new_ids)s))"
    reverse = (sort_dir == "desc")
    order_parts = []
    # v2.22.3 (Chuck, 2026-10-09): plain NEW rows no longer float to the top —
    # they sort by the chosen column (default: listed date, newest first), so
    # late arrivals carry their NEW tag wherever their date puts them; the
    # "Newly Listed" chip (f_new_only) shows just the NEW ones.
    # v2.22.4 (Chuck, 2026-10-10): EXCEPT NEW Want List matches — they still go
    # first (unless the person sorted a column themselves). Inside that group
    # the normal date order puts truly new matches before resurfaced (older-dated)
    # ones; then everything else as above.
    if not user_sorted and kw_sql:
        order_parts.append(f"({is_new_sql} AND {kw_bool}) DESC")
    if sort_field == "condition":
        order_parts.append(f"{_PG_COND_KNOWN_SQL} DESC")
        order_parts.append(f"{_PG_COND_RANK_SQL} {'DESC' if reverse else 'ASC'}")
    else:
        col, lower = _PG_SORT_MAP[sort_field]
        expr = f"LOWER({col})" if lower else col
        order_parts.append(f"{expr} {'DESC' if reverse else 'ASC'}")
    order_parts.append("sku ASC")   # deterministic tiebreak (Phase C finding, v2.16.21)
    order_sql = ", ".join(order_parts)

    # ── (v2.17.8) Aggregates: cached ───────────────────────────────────────
    # total_unfiltered, the four contextual facet lists, total_filtered and
    # store_count depend only on the WHERE clauses (scope, filters, and the
    # want-list keywords only when the Want-List-only toggle is on) — not on the
    # page, the sort, new_ids or fav stores. They used to be recomputed on every
    # request (~540 ms of a ~670 ms all-stores browse, live 2026-10-01), so page
    # flips / sorts paid it again each time. Cached per catalog generation
    # (_PG_CATALOG_GEN — every scan write / import bumps it before AND after
    # committing, so an entry computed while a write was in flight can never be
    # served afterwards). new_count / new_want_count need new_ids and are
    # counted separately below (cheap: primary-key lookups).
    agg_key = (
        "ALL" if search_all else tuple(sorted(store_set or ())), uls_key, fq,
        tuple(kw_entries) if f_want_only else None,
        bool(f_price_drop_only), bool(f_vintage_only),
        tuple(sorted(wl_ids)) if f_watched else None, f_price_min, f_price_max,
        tuple(sorted(new_ids)) if f_new_only else None,
        tuple(sorted(f_brands)), tuple(sorted(f_conds)), tuple(sorted(f_cats)), tuple(sorted(f_subs)),
    )
    with _PG_CATALOG_GEN_LOCK:
        _gen = _PG_CATALOG_GEN[0]
    agg = _browse_agg_get(_gen, agg_key)
    if agg is not None:
        _timing_phase("agg_cache_hit", 0.0)
    with _pg_conn() as conn:
        with conn.cursor(cursor_factory=_pg_extras.RealDictCursor) as cur:
            if agg is None:
                # Q1 — total_unfiltered (pre-_apply_base scope)
                _tp = time.perf_counter()
                cur.execute(f"SELECT COUNT(*) AS total_unfiltered FROM items WHERE {scope_sql}", params)
                total_unfiltered = cur.fetchone()["total_unfiltered"]
                _tq = time.perf_counter(); _timing_phase("q1", (_tq - _tp) * 1000.0); _tp = _tq

                # Q2 — contextual facet counts (each facet: all OTHER facets)
                facet_sql = " UNION ALL ".join([
                    f"SELECT 'brand' AS facet, COALESCE(NULLIF(brand,''), %(no_brand_label)s) AS value, "
                    f"COUNT(*) AS n FROM items WHERE {base_where_sql} AND {cond_clause} AND {cat_clause} "
                    f"AND {sub_clause} GROUP BY value",
                    f"SELECT 'condition' AS facet, condition AS value, COUNT(*) AS n FROM items "
                    f"WHERE {base_where_sql} AND {brand_clause} AND {cat_clause} AND {sub_clause} "
                    f"AND condition <> '' GROUP BY value",
                    f"SELECT 'category' AS facet, category AS value, COUNT(*) AS n FROM items "
                    f"WHERE {base_where_sql} AND {brand_clause} AND {cond_clause} AND {sub_clause} "
                    f"AND category <> '' GROUP BY value",
                    f"SELECT 'subcategory' AS facet, subcategory AS value, COUNT(*) AS n FROM items "
                    f"WHERE {base_where_sql} AND {brand_clause} AND {cond_clause} AND {cat_clause} "
                    f"AND subcategory <> '' GROUP BY value",
                ])
                cur.execute(facet_sql, params)
                brand_ctx, cond_ctx, cat_ctx, sub_ctx = {}, {}, {}, {}
                _ctx = {"brand": brand_ctx, "condition": cond_ctx,
                        "category": cat_ctx, "subcategory": sub_ctx}
                for r in cur.fetchall():
                    _ctx[r["facet"]][r["value"]] = r["n"]
                for b in f_brands: brand_ctx.setdefault(b, 0)
                for c in f_conds:  cond_ctx.setdefault(c, 0)
                for c in f_cats:   cat_ctx.setdefault(c, 0)
                for s_ in f_subs:  sub_ctx.setdefault(s_, 0)
                _tq = time.perf_counter(); _timing_phase("facets", (_tq - _tp) * 1000.0); _tp = _tq

                # Q3 — totals over the fully filtered set
                cur.execute(
                    f"SELECT COUNT(*) AS total_filtered, "
                    f"COUNT(DISTINCT NULLIF(store, '')) AS store_count "
                    f"FROM items WHERE {filtered_where_sql}", params)
                row = cur.fetchone()
                _tq = time.perf_counter(); _timing_phase("q3", (_tq - _tp) * 1000.0); _tp = _tq

                _cond_order = {"Excellent": 0, "Great": 1, "Good": 2, "Fair": 3, "Poor": 4}
                agg = {
                    "total_unfiltered": total_unfiltered,
                    "total_filtered": row["total_filtered"],
                    "store_count": row["store_count"],
                    "brands": [(b, c) for b, c in sorted(brand_ctx.items(), key=lambda x: (-x[1], x[0]))],
                    "conditions": [(c, n) for c, n in sorted(cond_ctx.items(), key=lambda x: _cond_order.get(x[0], 5))],
                    "categories": sorted(cat_ctx.items()),
                    "subcategories": sorted(sub_ctx.items()),
                }
                _browse_agg_put(_gen, agg_key, agg)

            total_unfiltered = agg["total_unfiltered"]
            total_filtered = agg["total_filtered"]
            store_count = agg["store_count"]

            # Per-request NEW counts (were FILTERs inside Q1 / Q3 — same sets)
            new_count = new_want_count = 0
            if new_ids:
                _tp = time.perf_counter()
                _nw = (f"(SELECT COUNT(*) FROM items WHERE {filtered_where_sql} "
                       f"AND {is_new_sql} AND {kw_bool})" if kw_sql else "0")
                cur.execute(
                    f"SELECT (SELECT COUNT(*) FROM items WHERE {scope_sql} AND {is_new_sql}) AS new_count, "
                    f"{_nw} AS new_want_count", params)
                row = cur.fetchone()
                new_count, new_want_count = row["new_count"], row["new_want_count"]
                _timing_phase("newcounts", (time.perf_counter() - _tp) * 1000.0)

            total_pages = max(1, -(-total_filtered // per_page))
            page = min(page, total_pages)
            start = (page - 1) * per_page

            # Q4 — the page
            _tp = time.perf_counter()
            cur.execute(
                f"SELECT sku, name, brand, category, subcategory, condition, condition_note, "
                f"price, list_price, price_drop, price_drop_since, store, location, url, "
                f"image_id, is_vintage, date_listed "
                f"FROM items WHERE {filtered_where_sql} ORDER BY {order_sql} "
                f"LIMIT %(_limit)s OFFSET %(_offset)s",
                {**params, "_limit": per_page, "_offset": start})
            page_rows = cur.fetchall()
            _tq = time.perf_counter(); _timing_phase("page", (_tq - _tp) * 1000.0); _tp = _tq

            # Q5 — kwMatch flags for just this page's rows
            kw_hits = set()
            if kw_sql and page_rows:
                cur.execute(
                    f"SELECT sku FROM items WHERE sku = ANY(%(_page_skus)s) AND {kw_bool}",
                    {**params, "_page_skus": [r["sku"] for r in page_rows]})
                kw_hits = {r["sku"] for r in cur.fetchall()}
                _timing_phase("kwflags", (time.perf_counter() - _tp) * 1000.0)

    page_items = []
    for r in page_rows:
        sku = r["sku"]
        store = r["store"] or ""
        price_raw = float(r["price"] or 0)
        page_items.append({
            "id":               sku,
            "name":             r["name"] or "",
            "brand":            r["brand"] or "",
            "price":            f"${price_raw:,.2f}" if price_raw else "",
            "price_raw":        price_raw,
            "list_price_raw":   float(r["list_price"] or 0),
            "price_drop":       float(r["price_drop"] or 0),
            "price_drop_since": r["price_drop_since"] or "",
            "store":            store,
            "location":         r["location"] or "",
            "url":              r["url"] or "",
            "category":         r["category"] or "",
            "subcategory":      r["subcategory"] or "",
            "condition":        r["condition"] or "",
            "date":             _fmt_date(r["date_listed"] or ""),
            "date_raw":         r["date_listed"] or "",
            "image_id":         r["image_id"] or "",
            "is_vintage":       bool(r["is_vintage"]),
            "condition_note":   r["condition_note"] or "",
            "watched":          sku in wl_ids,
            "isNew":            sku in new_ids,
            "kwMatch":          sku in kw_hits,
            "isFav":            store in fav_stores if fav_stores else False,
        })

    _timing_phase("build", (time.perf_counter() - _tp) * 1000.0)
    out = {
        "items":            page_items,
        "page":             page,
        "per_page":         per_page,
        "total_count":      total_filtered,
        "total_unfiltered": total_unfiltered,
        "total_pages":      total_pages,
        "store_count":      store_count,
        "new_count":        new_count,
        "new_want_count":   new_want_count,
        "no_store_data":    False,
    }
    out["facet_gen"] = _gen
    if skip_facets_gen is not None and skip_facets_gen == _gen:
        # (v2.17.8) The client already has these exact lists (only the page or
        # sort changed, and the catalog hasn't changed since — same generation)
        # — skip ~170 KB of brand counts. static/gc.js keeps its lists. A stale
        # generation (a scan wrote since) gets the full lists as before.
        out["facets_skipped"] = True
    else:
        out["brands"] = [{"name": b, "count": c} for b, c in agg["brands"]]
        out["conditions"] = [{"name": c, "count": n} for c, n in agg["conditions"]]
        out["categories"] = [{"name": c, "count": n} for c, n in agg["categories"]]
        out["subcategories"] = [{"name": s_, "count": n} for s_, n in agg["subcategories"]]
    return out



@app.route("/api/browse", methods=["POST"])
@optional_user_context
def api_browse():
    """Return inventory for the selected stores with server-side pagination,
    sorting, filtering, want-list matching and facet counts — one page at a
    time, so the browser never holds the whole catalog.

    (v2.16.44, Phase F step 4c) Served ONLY by the unified Postgres path
    (_pg_browse, via _browse_compute). The legacy Tier 1 / Tier 2 / Python
    matcher path it used to fall back to, and the step 4a comparison tooling,
    are gone — see HANDOFF.md v2.16.44. There is no fallback any more: a
    database error is reported to the client as a 503 with an `error` field
    (static/gc.js shows a "couldn't load" message) and counted in
    _PG_BROWSE_ERRORS. `?pg_shadow=1` (admin only) still attaches timing and
    those counters to the admin's own response for debugging."""
    data = request.json or {}
    pg_diag = (request.args.get("pg_shadow") == "1") and _is_admin()
    logged_in = bool(session.get("user_id"))
    try:
        try:
            resp = _browse_compute(data, logged_in=logged_in, pg_diag=pg_diag)
        except Exception as e:
            if not _pg_is_conn_error(e):
                raise
            # A dead pooled connection (e.g. after a Postgres restart). The
            # step 4b fallback used to hide these; now retry ONCE with
            # connection validation on (browse is read-only, so a retry is
            # always safe). Counted so it shows up in ?pg_shadow=1.
            _pg_browse_error_note("retried", e)
            _tok = _PG_VALIDATE_CONN.set(True)
            try:
                resp = _browse_compute(data, logged_in=logged_in, pg_diag=pg_diag)
            finally:
                _PG_VALIDATE_CONN.reset(_tok)
    except _PgBrowseIneligible as e:
        # Only reachable if a search term has no SQL translation — none exist
        # since v2.16.43, but the translator keeps _TsqueryUnsupported as its
        # contract for any future syntax, so answer it cleanly, not as a 500.
        _pg_browse_error_note("unsupported", e)
        return jsonify({"items": [], "error": "That search uses syntax we can't run. "
                                              "Try simplifying it."}), 400
    except Exception as e:
        _pg_browse_error_note("error", e)
        print(f"[pg] browse failed: {type(e).__name__}: {e}")
        return jsonify({"items": [], "error": "Couldn't load inventory right now. "
                                              "Please try again in a moment."}), 503
    if pg_diag:
        resp = dict(resp)
        resp["_pg_browse_errors"] = _PG_BROWSE_ERRORS
        resp["_pg_scan_writes"] = _PG_SCAN_WRITES   # v2.16.48, Phase F 5b-ii
        resp["_scan_coverage"] = _SCAN_COVERAGE     # v2.16.51
        resp["_timing"] = _timing_report()           # v2.17.1, Phase G step 1
        resp["_browse_agg_cache"] = dict(_BROWSE_AGG_STATS, entries=len(_BROWSE_AGG_CACHE))   # v2.17.8
    return jsonify(resp)


# Per-process counters for /api/browse requests that failed (v2.16.40 started
# these as fallback counters for the step 4b burn-in; since 4c there is no
# fallback, so they now count failures: `error` = answered 503, `unsupported`
# = answered 400, `retried` = hit a dead pooled connection and was retried
# once — a retry that then succeeded is counted ONLY here). Reset on every deploy/restart.
# Admin-visible via ?pg_shadow=1 -> `_pg_browse_errors`.
_PG_BROWSE_ERRORS = {"unsupported": 0, "error": 0, "retried": 0, "since": None, "last": {},
                     "reasons": {"unsupported": {}, "error": {}, "retried": {}}}
# Distinct reasons tallied per kind, capped so a flood of unique messages
# can't grow memory unbounded (v2.16.43).
_PG_BROWSE_ERROR_REASON_CAP = 50


def _pg_browse_error_note(kind, exc):
    if _PG_BROWSE_ERRORS["since"] is None:
        _PG_BROWSE_ERRORS["since"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    _PG_BROWSE_ERRORS[kind] += 1
    reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    _PG_BROWSE_ERRORS["last"][kind] = {
        "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": reason}
    tally = _PG_BROWSE_ERRORS["reasons"][kind]
    if reason in tally or len(tally) < _PG_BROWSE_ERROR_REASON_CAP:
        tally[reason] = tally.get(reason, 0) + 1
    else:
        tally["(other)"] = tally.get("(other)", 0) + 1


class _PgBrowseIneligible(Exception):
    """_pg_browse() was asked for a search it has no SQL translation for
    (_TsqueryUnsupported from the translator — none left since v2.16.43).
    /api/browse answers it with a 400; /api/saved-search-counts falls back to
    its JSON count for that one search (until step 5 removes that too)."""


# ── Want-list DoS caps (v2.13.1 / v2.14.4 / v2.16.0, kept through 4c) ────────
# Want lists are matched on the public /api/browse endpoint, so the number of
# entries of each costly shape is capped. The caps were designed around the
# old Python matcher's costs, but _pg_browse honors exactly the same accepted
# set (v2.16.38) so moving to SQL changed no user's results — kept as-is in 4c
# for the same reason. Routing mirrors the old matcher's bucketing one-for-one:
#   ';' / leading-'-' bool entries  -> _BOOL cap (or the exotic cap if they
#                                      contain '*', the old v2.13.1 boundary)
#   plain single word                -> uncapped (beyond the overall list cap)
#   wildcard / "quoted"              -> _EXOTIC cap
#   comma-AND                        -> _AND cap
#   any other phrase                 -> _PHRASE cap
# Entries over a cap are dropped, exactly as before.
_PHRASE_KW_CAP = 300
_EXOTIC_KW_CAP = 30
_AND_KW_CAP    = 200
_BOOL_KW_CAP   = 50


def _kw_accept_capped(keywords):
    """The want-list entries (raw strings, original spelling) that survive the
    per-shape caps above, in order. Pure function of the list."""
    phrase_n = exotic_n = and_n = bool_n = 0
    accepted = []
    for kw in keywords:
        base = kw.lstrip('=').strip()      # '=' = legacy strict marker
        if not base:
            continue
        # v2.16.3: expand 'prefix : b1; b2' BEFORE routing (see _expand_colon_prefix).
        base = _expand_colon_prefix(base, join=', ')
        if (';' in base or '-' in base) and _wl_bool_compile(base) is not None:
            if '*' in base:
                if exotic_n < _EXOTIC_KW_CAP:
                    accepted.append(kw)
                    exotic_n += 1
            elif bool_n < _BOOL_KW_CAP:
                accepted.append(kw)
                bool_n += 1
        elif _SIMPLE_KW_RE.match(base):
            accepted.append(kw)
        elif '*' in base or (base.startswith('"') and base.endswith('"') and len(base) > 2):
            if exotic_n < _EXOTIC_KW_CAP:
                accepted.append(kw)
                exotic_n += 1
        elif ',' in base:
            if and_n < _AND_KW_CAP and any(p.strip() for p in base.split(',')):
                accepted.append(kw)
                and_n += 1
        else:
            if phrase_n < _PHRASE_KW_CAP:
                accepted.append(kw)
                phrase_n += 1
    return accepted


def _browse_compute(data, *, logged_in, pg_diag=False):
    """Body of /api/browse: parse/clamp the request, apply the want-list caps,
    and run _pg_browse. Raises _PgBrowseIneligible (untranslatable search) or
    any DB error — api_browse() turns those into error responses."""
    stores = data.get("stores", [])
    search_all = bool(data.get("all_stores"))
    if not stores and not search_all:
        return {"items": [], "no_store_data": True}

    # Pagination params
    page     = max(int(data.get("page", 1)), 1)
    per_page = min(max(int(data.get("per_page", 50)), 10), 200)

    # Sort params (defaults: date descending = newest first). sort_field is
    # user-controlled: anything outside the SQL whitelist (the UI only sends
    # whitelisted columns — static/gc.js _SORT_COLS) gets the default sort
    # rather than an error. It is never interpolated into SQL either way.
    sort_field = data.get("sort_field", "date")
    if sort_field not in _PG_SORTABLE:
        sort_field = "date"
    sort_dir   = data.get("sort_dir", "desc")
    user_sorted = bool(data.get("user_sorted"))
    fav_stores = set(data.get("fav_stores", []))

    # Per-user filtering: only show items first_seen <= this user's last scan time
    user_last_scan = (data.get("user_last_scan") or "").strip()

    # Filter params — all dropdowns are multi-select arrays. filter_q is
    # clamped (v2.13.0 DoS cap, kept) before translation.
    fq       = (data.get("filter_q") or "").lower().strip()[:200]
    f_brands = data.get("filter_brands") or []
    f_conds  = data.get("filter_conditions") or []
    f_cats   = data.get("filter_categories") or []
    f_subs   = data.get("filter_subcategories") or []
    f_watched = bool(data.get("filter_watched"))
    f_want_only = bool(data.get("filter_want_list_only"))
    f_price_drop_only = bool(data.get("filter_price_drop_only"))
    f_new_only = bool(data.get("filter_new_only"))   # v2.22.3 "New" chip
    f_vintage_only = bool(data.get("vintage_only"))
    def _to_float(v):
        try: return float(v) if v is not None and v != '' else None
        except (TypeError, ValueError): return None
    f_price_min = _to_float(data.get("filter_price_min"))
    f_price_max = _to_float(data.get("filter_price_max"))

    # Watchlist and want list come from the client.
    wl_ids   = set(data.get("watchlist_ids", []))
    keywords = data.get("keywords", [])
    # Want-list size guard: dedupe case-insensitively, clamp each entry to 100
    # chars, then cap the list by accountability — logged-in users (large,
    # synced lists, rate-limitable) get 750, anonymous callers 250. Then the
    # per-shape caps (_kw_accept_capped).
    if isinstance(keywords, list):
        _seen = set()
        _dedup = []
        for k in keywords:
            ks = str(k)[:100]
            kl = ks.strip().lower()
            if kl and kl not in _seen:
                _seen.add(kl)
                _dedup.append(ks)
        keywords = _dedup[:(750 if logged_in else 250)]
    else:
        keywords = []
    kw_accepted = _kw_accept_capped(keywords)
    new_ids   = set(data.get("new_ids", []))
    store_set = set(stores) if not search_all else None

    # Phase G timing group (v2.17.1): scope + which costly features are on.
    _shape = ["browse", "all" if search_all else ("1store" if len(stores) == 1 else "stores")]
    if kw_accepted:
        _shape.append("+wantonly" if f_want_only else "+want")
    if fq:
        _shape.append("+q")
    if f_brands or f_conds or f_cats or f_subs or f_watched or f_price_drop_only or f_new_only \
            or f_vintage_only or f_price_min is not None or f_price_max is not None:
        _shape.append("+filter")
    if page > 1:
        _shape.append("+p2")
    _timing_set_key(" ".join(_shape))

    if _PG_POOL is None:
        raise RuntimeError("Postgres pool not available")
    _t0 = time.time()
    resp = _pg_browse(
        store_set=store_set, search_all=search_all,
        user_last_scan=user_last_scan, kw_entries=kw_accepted, fq=fq,
        f_brands=f_brands, f_conds=f_conds, f_cats=f_cats, f_subs=f_subs,
        f_watched=f_watched, wl_ids=wl_ids, f_want_only=f_want_only,
        f_price_drop_only=f_price_drop_only, f_vintage_only=f_vintage_only, f_new_only=f_new_only,
        f_price_min=f_price_min, f_price_max=f_price_max,
        sort_field=sort_field, sort_dir=sort_dir, user_sorted=user_sorted,
        new_ids=new_ids, fav_stores=fav_stores, page=page, per_page=per_page,
        skip_facets_gen=data.get("skip_facets_gen"),
    )
    if pg_diag:
        resp["_pg_browse_ms"] = round((time.time() - _t0) * 1000, 1)
    return resp


# (Dead legacy global-file endpoints removed in v2.14.5 — /api/watchlist GET+POST,
# /api/watchlist/items, /api/keywords GET+POST. Per-user watch/want lists live in
# SQLite via /api/sync; no frontend called any of these (grepped all static/*.js).
# Login-gated since v2.12.32, deleted per 2026-07 audit E3. The load_watchlist/
# save_watchlist helpers and the scan's upkeep of the legacy global
# gc_watchlist.json were deleted in v2.16.48 — nothing read that file.)


# ── v2.20.0: "new to the site" half of the NEW rule ────────────────────────────
# A listing is NEW for a user if its listed date is after their anchor (the
# original rule) OR it was first seen by the site after their last scan — GC
# often makes a listing searchable days or months after its listed date, and
# Chuck wants those flagged too (2026-10-09, reversing the 2026-10-01 call).
# first_seen survives a sale + return, so returns don't count. Backed by
# idx_items_first_seen (pg_schema.sql).
_NEW_LATE_ROW_CAP = 20000


def _pg_first_seen_between(since: str, until: str, stores=None) -> list[tuple[str, str]]:
    """(sku, date_listed) of available items with since < first_seen <= until,
    optionally limited to `stores`. Read-only."""
    def _q():
        sql = ("SELECT sku, date_listed FROM items WHERE available AND first_seen <> '' "
               "AND first_seen > %s AND first_seen <= %s")
        prm = [since, until]
        if stores is not None:
            sql += " AND store = ANY(%s)"
            prm.append(list(stores))
        sql += f" ORDER BY first_seen LIMIT {_NEW_LATE_ROW_CAP}"
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, prm)
                return [(r[0], r[1] or "") for r in cur.fetchall()]
    return _pg_read(_q)


# (v2.16.46) /api/state runs on every page load; the available-item count only
# changes when a scan syncs, so cache it briefly rather than COUNT(*) each time.
_PG_AVAILABLE_COUNT = {"ts": 0.0, "n": None}
_PG_AVAILABLE_COUNT_TTL = 60  # seconds


def _pg_available_count():
    now = time.time()
    if _PG_AVAILABLE_COUNT["n"] is not None and now - _PG_AVAILABLE_COUNT["ts"] < _PG_AVAILABLE_COUNT_TTL:
        return _PG_AVAILABLE_COUNT["n"]
    def _q():
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM items WHERE available")
                return int(cur.fetchone()[0])
    try:
        n = _pg_read(_q)
    except Exception as e:
        print(f"[pg] /api/state count failed: {type(e).__name__}: {e}")
        return _PG_AVAILABLE_COUNT["n"]   # last known value (None if never loaded)
    _PG_AVAILABLE_COUNT.update(ts=now, n=n)
    return n


@app.route("/api/state")
@optional_user_context
def api_state():
    # (v2.16.46, Phase F step 5a) Count from Postgres instead of scanning the
    # in-memory JSON catalog. None if Postgres is unreachable (the page shows
    # 0 — gc.js does `s.total_items || 0`), never a crash.
    total_items = _pg_available_count()
    last_scan_file = DATA_DIR / "gc_last_scan.txt"
    last_scan = last_scan_file.read_text().strip() if last_scan_file.exists() else None
    return jsonify({
        "total_items":  total_items,
        "excel_exists": OUTPUT_FILE.exists(),
        "is_first_run": total_items == 0,   # None (DB down) is not a first run
        "last_scan":    last_scan,
    })

@app.route("/api/run", methods=["POST"])
@optional_user_context
def api_run():
    global _current_run_time
    # (v2.17.4) A quick pass holds _lock for only a few seconds, so wait briefly
    # and run this user's own pass (their own NEW threshold) instead of joining
    # someone else's; a long full/baseline scan still gets joined as before.
    if not _lock.acquire(timeout=8):
        # A scan is already running — subscribe this client to it instead of rejecting
        joined_id = _current_run_id
        joined_time = _current_run_time
        if joined_id and _subscribe_to_run(joined_id) is not None:
            return jsonify({"status": "joined", "run_id": joined_id, "run_time": joined_time})
        # Race: scan just finished between the lock check and subscribe — tell client to retry
        return jsonify({"error": "Scan just finished, please try again."}), 409
    # Rate-limit unauthenticated scan triggers to prevent Algolia quota exhaustion.
    # Logged-in users (user_id in session) are exempt — they have an account to hold accountable.
    user_id = session.get("user_id")
    if not user_id:
        ip  = _client_ip()
        now = time.time()
        if now - _scan_last.get(ip, 0) < _SCAN_COOLDOWN:
            _lock.release()
            return jsonify({"error": "Please wait a moment before starting another scan."}), 429
        _scan_last[ip] = now
    _stop_event.clear()
    data     = request.json
    # Cap the stores array — each entry is an Algolia fan-out; there are only ~240
    # real stores, so anything beyond that is garbage or abuse. (2026-07 audit L2)
    selected = (data.get("stores") or [])[:300]
    baseline = data.get("baseline", False)
    # If user is logged in, use their server-stored last_run (synced across devices).
    # Otherwise fall back to the device's own localStorage value.
    if user_id:
        _udata = _get_user_data(user_id)
        device_last_run    = _udata.get("last_run", "")
        device_last_anchor = _udata.get("last_anchor", "")
    else:
        device_last_run    = (data.get("device_last_run")    or "").strip()
        device_last_anchor = (data.get("device_last_anchor") or "").strip()
    # Compute run_time here so we can return it to the client immediately.
    run_time_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    _current_run_time = run_time_now
    run_id, run_q = _create_run_queue()
    # Also mirror to legacy _q for any other endpoints that read it
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    # v2.17.4 (Phase G S1): a nationwide click is a quick pass + background sweep;
    # _run falls back to "full" itself if there's no usable NEW threshold.
    scan_mode = "quick" if (not baseline and not selected) else "full"
    t = threading.Thread(
        target=_run,
        args=(selected, baseline, run_id, device_last_run, run_time_now, device_last_anchor, user_id),
        kwargs={"mode": scan_mode},
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started", "run_id": run_id, "run_time": run_time_now})

@app.route("/api/set-cookies", methods=["POST"])
@optional_user_context
def api_set_cookies():
    """Import browser cookies into the HTTP session."""
    denied = _require_admin_api()
    if denied:
        return denied
    cookie_string = request.json.get("cookies", "")
    count = 0
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            _http.cookies.set(k.strip(), v.strip(), domain=".guitarcenter.com")
            count += 1
    _save_cookies()
    return jsonify({"imported": count, "status": "Cookies set — try running a store now."})



# Admin data export / import. Since v2.17.0 (Phase F 5c) the "cat_cache" part of
# the bundle is built from / written to Postgres `items` — same bundle shape as
# before (sku -> record dict with the old JSON catalog's keys), so an old export
# still imports. The export streams the ~500K-row catalog straight from a
# server-side cursor instead of building it in memory.
_EXPORT_FILES = [
    ("state",     STATE_FILE),
    ("stores",    STORES_CACHE),
    ("favorites", FAVORITES_FILE),
    ("watchlist", WATCHLIST_FILE),
    ("keywords",  KEYWORDS_FILE),
]
_IMPORT_CHUNK = 5000


def _pg_record_from_row(row: tuple) -> dict:
    """A Postgres `items` row (_PG_UPSERT_COLS order) as the old JSON catalog
    record dict (everything but sku). NUMERIC columns become floats."""
    rec = {}
    for col, v in zip(_PG_UPSERT_COLS[1:], row[1:]):
        if col in _PG_NUMERIC_COLS:
            v = float(v) if v is not None else 0
        rec[col] = v
    return rec


def _export_stream(parts: dict):
    """Yield the export bundle as JSON text: the small file parts, then the
    catalog streamed from Postgres. A mid-stream Postgres failure truncates
    the download (the file won't parse) and is logged."""
    yield "{"
    for name, val in parts.items():
        yield json.dumps(name) + ": " + json.dumps(val) + ", "
    yield '"cat_cache": {'
    n = 0
    try:
        conn = psycopg2.connect(PG_DATABASE_URL, connect_timeout=10)
        try:
            with conn.cursor(name="export_items") as cur:
                cur.itersize = 5000
                cur.execute(f"SELECT {', '.join(_PG_UPSERT_COLS)} FROM items ORDER BY sku")
                for row in cur:
                    yield ("," if n else "") + json.dumps(row[0]) + ": " + json.dumps(_pg_record_from_row(row))
                    n += 1
        finally:
            conn.close()
    except Exception as e:
        print(f"[export] catalog stream failed after {n:,} rows: {type(e).__name__}: {e}")
        return
    print(f"[export] data export streamed {n:,} catalog rows")
    yield "}}"


@app.route("/api/export-data")
@optional_user_context
def api_export_data():
    """Export all data as a JSON bundle for migration (files + Postgres catalog)."""
    denied = _require_admin_api()
    if denied:
        return denied
    if not (_PSYCOPG2_AVAILABLE and PG_DATABASE_URL):
        return jsonify({"error": "Postgres not configured"}), 503
    parts = {}
    for name, path in _EXPORT_FILES:
        if path.exists():
            try:
                parts[name] = json.loads(path.read_text())
            except Exception:
                pass
    from flask import Response
    return Response(
        _export_stream(parts),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=gc_data_export.json"}
    )


@app.route("/api/import-data", methods=["POST"])
@optional_user_context
def api_import_data():
    """Import a data bundle exported from another instance. Requires admin session.
    The bundle's cat_cache is UPSERTED into Postgres (never deletes rows that
    aren't in the bundle), in one transaction, while no scan is running."""
    denied = _require_admin_api()
    if denied:
        return denied
    bundle = request.json or {}
    if not isinstance(bundle, dict):
        return jsonify({"error": "Bundle must be a JSON object."}), 400
    cat = bundle.get("cat_cache")
    if cat is not None and not isinstance(cat, dict):
        return jsonify({"error": "cat_cache must be an object of sku -> record."}), 400
    upserted = 0
    if cat:
        if not (_PSYCOPG2_AVAILABLE and PG_DATABASE_URL and _PG_POOL is not None):
            return jsonify({"error": "Postgres not configured"}), 503
        if not _lock.acquire(blocking=False):
            return jsonify({"error": "A scan is running — try again when it's done."}), 409
        try:
            if not _PG_SCAN_DB_LOCK.acquire(timeout=_PG_SCAN_DB_LOCK_TIMEOUT):
                return jsonify({"error": "A scan is still saving — try again shortly."}), 409
            try:
                _pg_catalog_gen_bump()   # v2.17.3: invalidates any prior-state prefetch
                rows = [_pg_row_for(str(sku), rec) for sku, rec in cat.items()
                        if sku and isinstance(rec, dict)]
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        for i in range(0, len(rows), _IMPORT_CHUNK):
                            psycopg2.extras.execute_values(
                                cur, _PG_UPSERT_SQL, rows[i:i + _IMPORT_CHUNK], page_size=2000)
                upserted = len(rows)
                _pg_catalog_gen_bump()   # v2.17.8: after commit too
            finally:
                _PG_SCAN_DB_LOCK.release()
        except Exception as e:
            print(f"[import] catalog upsert failed: {type(e).__name__}: {e}")
            return jsonify({"error": "Catalog import failed — nothing was written. See server logs."}), 500
        finally:
            try:
                _lock.release()
            except RuntimeError:
                pass
        _PG_AVAILABLE_COUNT["ts"] = 0
    written = []
    for name, path in _EXPORT_FILES:
        if name in bundle:
            path.write_text(json.dumps(bundle[name]))
            written.append(name)
    if cat:
        written.append(f"cat_cache ({upserted:,} rows upserted into Postgres)")
    return jsonify({"imported": written, "status": "Import complete — reload the page."})


@app.route("/api/cl-search")
@optional_user_context
def api_cl_search():
    # Require login. Each call fans out to up to ~75 Craigslist markets (10 concurrent,
    # 12s timeouts each), so leaving it open was an unauthenticated outbound-request
    # amplification / resource-abuse vector. The CL feature is sign-in-only by design.
    if not session.get("user_id"):
        return jsonify({"error": "Not logged in."}), 401
    q = request.args.get("q", "").strip()[:200]
    cities_param = request.args.get("cities", "").strip()
    if not q:
        return jsonify({"error": "No search term provided."})
    _valid_cities = set(_CL_CITIES)
    cities = [c.strip() for c in cities_param.split(",") if c.strip() in _valid_cities] if cities_param else []
    title_only = request.args.get("title_only", "").lower() in ("1", "true", "yes")
    try:
        results = _cl_search(q, cities or None, title_only=title_only)
        return jsonify({"results": results, "count": len(results)})
    except Exception:
        # Don't leak internal exception text to the caller.
        return jsonify({"error": "Search failed. Please try again."})


_CL_CITIES = [
    "atlanta","austin","boston","chicago","dallas","denver","detroit",
    "houston","lasvegas","losangeles","miami","minneapolis","nashville",
    "newyork","philadelphia","phoenix","portland","raleigh","sacramento",
    "saltlakecity","sanantonio","sandiego","sfbay","seattle","stlouis",
    "washingtondc","baltimore","charlotte","cleveland","columbus","fortworth",
    "indianapolis","jacksonville","kansascity","memphis","milwaukee",
    "oklahomacity","orlando","pittsburgh","richmond","riverside","tampabay",
    "tucson","tulsa","virginiabeach","albuquerque","boise","buffalo",
    "cincinnati","desmoines","elpaso","fresno","grandrapids","greensboro",
    "hartford","honolulu","knoxville","louisville","madison","neworleans",
    "norfolk","omaha","providence","rochester","spokane","syracuse",
    "toledo","wichita",
]

_CL_LABELS = {
    "sfbay":"SF Bay Area","newyork":"New York","losangeles":"Los Angeles",
    "washingtondc":"Washington DC","saltlakecity":"Salt Lake City",
    "sandiego":"San Diego","sanantonio":"San Antonio","lasvegas":"Las Vegas",
    "tampabay":"Tampa Bay","kansascity":"Kansas City","grandrapids":"Grand Rapids",
    "desmoines":"Des Moines","fortworth":"Fort Worth","oklahomacity":"Oklahoma City",
    "virginiabeach":"Virginia Beach","neworleans":"New Orleans","stlouis":"St. Louis",
}

def _cl_city_label(city_id: str) -> str:
    return _CL_LABELS.get(city_id, city_id.title())

def _cl_fmt_date(iso: str) -> str:
    try:
        from datetime import datetime as dt
        d = dt.fromisoformat(iso.replace("Z",""))
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except Exception:
        return iso[:10] if iso else ""

def _cl_slugify(text: str) -> str:
    """Convert a title to a CL-style URL slug for matching.
    E.g. 'Fender Telecaster 2019 MIM' → 'fender-telecaster-2019-mim'"""
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')

def _cl_parse_html(html: str, city_id: str) -> list[dict]:
    """Parse CL search results — ItemList JSON-LD + URLs from HTML anchor tags."""
    items = []
    label = _cl_city_label(city_id)

    # Extract post URLs from the HTML — CL puts them in <a class="cl-app-anchor"> or similar
    # Pattern: href="https://cityname.craigslist.org/msa/d/title/1234567890.html"
    post_urls = re.findall(
        r'href="(https?://[a-z]+\.craigslist\.org/[^"]+/d/[^"]+\.html)"',
        html)
    # Dedupe while preserving order
    seen = set()
    post_urls_ordered = []
    for u in post_urls:
        if u not in seen:
            seen.add(u)
            post_urls_ordered.append(u)

    # Build a slug→URL lookup for title-based matching (replaces fragile position-based matching).
    # CL post URLs contain a slugified title: /msa/d/fender-telecaster-2019/1234567890.html
    # We extract the slug and match it against JSON-LD item names.
    _slug_to_urls: dict[str, list[str]] = {}  # slug → [url, ...] (multiple posts can have similar slugs)
    for u in post_urls_ordered:
        try:
            # Extract slug from URL path: /section/d/SLUG/ID.html
            path_parts = u.split('/')
            d_idx = path_parts.index('d') if 'd' in path_parts else -1
            if d_idx >= 0 and d_idx + 1 < len(path_parts):
                slug = path_parts[d_idx + 1]
                if slug not in _slug_to_urls:
                    _slug_to_urls[slug] = []
                _slug_to_urls[slug].append(u)
        except Exception:
            pass

    def _match_url_by_title(name: str) -> str:
        """Find the best matching URL for a JSON-LD item name by comparing title slugs."""
        name_slug = _cl_slugify(name)
        if not name_slug:
            return ""
        name_words = set(name_slug.split('-'))
        best_url = ""
        best_score = 0
        for slug, urls in _slug_to_urls.items():
            if not urls:
                continue
            slug_words = set(slug.split('-'))
            # Score = number of overlapping words (Jaccard-like)
            overlap = len(name_words & slug_words)
            # Require at least 2 word matches to avoid false positives
            if overlap > best_score and overlap >= 2:
                best_score = overlap
                best_url = urls[0]
        # If we found a match, remove the URL from the pool so it can't be reused
        if best_url:
            for slug, urls in _slug_to_urls.items():
                if best_url in urls:
                    urls.remove(best_url)
                    break
        return best_url

    # Find the ItemList JSON-LD block
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue

        entries = data.get("itemListElement", [])
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            item = entry.get("item", {})
            if not isinstance(item, dict):
                continue
            name   = item.get("name", "")
            if not name:
                continue
            # Prefer URL directly from JSON-LD (ListItem.url or item.url/sameAs)
            # — these are authoritative and immune to index-mismatch bugs.
            # Fall back to title-slug matching when JSON-LD omits the URL.
            url = (entry.get("url") or item.get("url") or item.get("sameAs") or "").strip()
            if not url:
                url = _match_url_by_title(name)
            if not url:
                continue
            offers = item.get("offers", {})
            price  = offers.get("price", "")
            try:    price = f"${float(price):,.0f}" if price else ""
            except: price = str(price)
            avail  = offers.get("availableAtOrFrom", {})
            addr   = avail.get("address", {}) if isinstance(avail, dict) else {}
            hood   = addr.get("addressLocality","") or addr.get("addressRegion","")
            loc    = label
            date   = _cl_fmt_date(
                offers.get("validFrom","") or offers.get("availabilityStarts","") or
                item.get("datePosted","") or item.get("dateCreated","") or
                item.get("uploadDate","")
            )
            # Extract thumbnail image
            img = item.get("image", "")
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "") or img.get("contentUrl", "")

            items.append({"title": name, "url": url, "price": price,
                          "location": loc, "date": date, "cityId": city_id,
                          "image": img or ""})
        if items:
            break  # Found and parsed the ItemList, done

    return items


def _cl_search(query: str, cities: list = None, title_only: bool = False) -> list[dict]:
    """Search Craigslist musical instruments across US cities.
    If title_only=True, adds srchType=T to restrict matches to listing titles."""
    import time as _time
    results   = []
    seen_urls = set()
    search_cities = cities if cities else _CL_CITIES

    def _search_city(city_id):
        try:
            # Each thread gets its own session to avoid thread-safety issues
            s = http.Session()
            s.headers.update({
                "User-Agent": random.choice(_USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            })
            # Small random delay to avoid hammering CL simultaneously
            _time.sleep(random.uniform(0.05, 0.3))
            srch_param = "&srchType=T" if title_only else ""
            url = (f"https://{city_id}.craigslist.org/search/msa"
                   f"?query={http.utils.quote(query)}&sort=date{srch_param}")
            r = s.get(url, timeout=12)
            if r.status_code == 200:
                return _cl_parse_html(r.text, city_id)
        except Exception:
            pass
        return []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_search_city, c): c for c in search_cities}
        for future in as_completed(futures):
            for item in future.result():
                title_key = f"{item['title'].lower().strip()}|{item['price']}|{item['cityId']}"
                if title_key not in seen_urls:
                    seen_urls.add(title_key)
                    results.append(item)

    results.sort(key=lambda x: x.get("date",""), reverse=True)
    return results


@app.route("/api/cl-parse-test")
@optional_user_context
def api_cl_parse_test():
    """Test the CL parser on a live page and show what it finds."""
    denied = _require_admin_api()
    if denied:
        return denied
    city = request.args.get("city", "sfbay")
    # Allowlist the city before interpolating it into an outbound URL (matches
    # /api/cl-debug). Admin-only, but removes the SSRF primitive entirely.
    if city not in _CL_CITIES:
        return jsonify({"error": "Unknown city."}), 400
    q    = request.args.get("q", "telecaster")
    try:
        url  = f"https://{city}.craigslist.org/search/msa?query={http.utils.quote(q)}&sort=date"
        r    = _http.get(url, timeout=12)
        html = r.text
        blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        parsed_blocks = []
        for i, b in enumerate(blocks):
            try:
                d = json.loads(b)
                parsed_blocks.append({
                    "index": i,
                    "type": d.get("@type","") if isinstance(d, dict) else type(d).__name__,
                    "keys": list(d.keys())[:15] if isinstance(d, dict) else [],
                    "sample": b[:1200],
                })
            except Exception as e:
                parsed_blocks.append({"index": i, "parse_error": str(e), "raw": b[:400]})
        # Also show raw keys from first listing for date debugging
        raw_keys = {}
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                d = json.loads(block)
                entries = []
                if isinstance(d, list): entries = d
                elif d.get("@type") == "CollectionPage": entries = d.get("mainEntity",{}).get("itemListElement",[])
                elif d.get("@type") in ("ItemList","ListItem"): entries = [d]
                if entries:
                    item = entries[0].get("item", entries[0]) if isinstance(entries[0], dict) else {}
                    offers = item.get("offers", {})
                    raw_keys = {"item_keys": list(item.keys()), "offer_keys": list(offers.keys())}
                    break
            except Exception:
                pass
        results = _cl_parse_html(html, city)
        return jsonify({
            "html_size": len(html),
            "json_ld_block_count": len(blocks),
            "blocks": parsed_blocks,
            "first_listing_keys": raw_keys,
            "parser_results_count": len(results),
            "parser_sample": results[:3],
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/cl-debug")
@optional_user_context
def api_cl_debug():
    """Probe a CL city to find the right section code and response format."""
    denied = _require_admin_api()
    if denied:
        return denied
    city = request.args.get("city", "sfbay")
    if city not in _CL_CITIES:
        return jsonify({"error": f"Unknown city '{city}'. Must be one of the supported CL cities."}), 400
    q    = request.args.get("q", "telecaster")
    out  = {}
    for section in ["msa", "msg", "mso", "mlt"]:
        # Try plain HTML
        try:
            url = f"https://{city}.craigslist.org/search/{section}?query={http.utils.quote(q)}&sort=date"
            r   = _http.get(url, timeout=10)
            out[f"{section}_html"] = {
                "status": r.status_code,
                "size":   len(r.text),
                "has_results": any(x in r.text for x in ["result-row","cl-search-result","listing-id"]),
                "snippet": r.text[2000:2500],
            }
        except Exception as e:
            out[f"{section}_html"] = {"error": str(e)}
        # Try format=json
        try:
            url = f"https://{city}.craigslist.org/search/{section}?query={http.utils.quote(q)}&sort=date&format=json"
            r   = _http.get(url, timeout=10)
            out[f"{section}_json"] = {
                "status":       r.status_code,
                "content_type": r.headers.get("Content-Type",""),
                "size":         len(r.text),
                "snippet":      r.text[:1000],
            }
        except Exception as e:
            out[f"{section}_json"] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/debug-fetch")
@optional_user_context
def api_debug_fetch():
    """Test Algolia API fetch for a store."""
    denied = _require_admin_api()
    if denied:
        return denied
    store = request.args.get("store", "Austin")
    try:
        data     = fetch_page(store, 1)
        products = parse_products(data, store)
        results  = data.get("results", [{}])
        first    = results[0] if results else {}
        return jsonify({
            "store":          store,
            "nb_hits":        first.get("nbHits", 0),
            "nb_pages":       first.get("nbPages", 0),
            "products_found": len(products),
            "sample":         products[:3],
            "raw_hit_sample": first.get("hits", [{}])[:2],
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/debug-condition")
@optional_user_context
def api_debug_condition():
    """Inspect the saved listing HTML to find exactly where condition data lives."""
    denied = _require_admin_api()
    if denied:
        return denied
    debug_file = DATA_DIR / "gc_debug_listing.html"
    if not debug_file.exists():
        return jsonify({"error": "No debug file yet — run the tracker once to save a listing page, then visit this URL."})
    html = debug_file.read_text(errors="replace")

    report = {"html_size": len(html)}

    # 1. All "Condition" occurrences in raw HTML (catches server-rendered text)
    condition_hits = []
    for m in re.finditer(r'.{0,60}[Cc]ondition.{0,60}', html):
        txt = m.group(0).strip()
        if txt not in condition_hits:
            condition_hits.append(txt)
        if len(condition_hits) >= 15:
            break
    report["condition_in_raw_html"] = condition_hits

    # 2. Dig into __NEXT_DATA__ and find ALL keys that contain condition-like words
    nd_condition_fields = {}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            nd = json.loads(m.group(1))
            report["has_next_data"] = True
            report["next_data_size"] = len(m.group(1))

            # Walk every key/value pair and collect anything condition-related
            def walk(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        full = f"{path}.{k}" if path else k
                        if any(c in k.lower() for c in ("condition", "grade", "quality", "rating")):
                            nd_condition_fields[full] = str(v)[:120]
                        if isinstance(v, (dict, list)):
                            walk(v, full)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj[:5]):  # only first 5 items
                        walk(item, f"{path}[{i}]")
            walk(nd)
        except Exception as e:
            report["next_data_parse_error"] = str(e)
    else:
        report["has_next_data"] = False

    report["next_data_condition_fields"] = nd_condition_fields

    # 3. All JSON-LD blocks — show the full offers object for first item
    ld_offers = []
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            d = json.loads(block)
            if d.get("@type") == "CollectionPage":
                items = d.get("mainEntity", {}).get("itemListElement", [])
                for entry in items[:3]:
                    item = entry.get("item", {})
                    ld_offers.append({
                        "name": item.get("name", "")[:60],
                        "offers": item.get("offers", {}),
                    })
        except Exception:
            pass
    report["jsonld_first_3_offers"] = ld_offers

    # 4. Show raw HTML snippet around first .gc product URL
    m2 = re.search(r'https?://www\.guitarcenter\.com/Used/[^"\'<>\s]+\.gc', html)
    if m2:
        start = max(0, m2.start() - 300)
        end = min(len(html), m2.end() + 600)
        report["html_around_first_product_url"] = html[start:end]

    return jsonify(report)

@app.route("/api/debug-condition/reset", methods=["GET", "POST"])
@optional_user_context
def api_debug_condition_reset():
    denied = _require_admin_api()
    if denied:
        return denied
    debug_file = DATA_DIR / "gc_debug_listing.html"
    if debug_file.exists():
        debug_file.unlink()
    return jsonify({"status": "cleared"})

@app.route("/api/debug-condition/diag")
@optional_user_context
def api_debug_condition_diag():
    """Read the condition extraction diagnostic log."""
    denied = _require_admin_api()
    if denied:
        return denied
    diag_file = DATA_DIR / "gc_condition_diag.json"
    if not diag_file.exists():
        return jsonify({"error": "No diagnostic file yet — run the tracker first."})
    return diag_file.read_text()

@app.route("/api/stop", methods=["POST"])
@optional_user_context
def api_stop():
    # Require the run_id of the active scan so external actors cannot stop
    # someone else's scan without first knowing the ID.  The client receives
    # run_id from /api/run and must echo it here.  Admin sessions are exempt
    # so the admin clear-lock page still works without a run_id.
    if not _is_admin():
        data   = request.json or {}
        req_id = (data.get("run_id") or "").strip()
        if not req_id or req_id != _current_run_id:
            return jsonify({"error": "Invalid or missing run_id."}), 403
    _stop_event.set()
    # Force-release lock after a short delay to prevent stuck state
    def _force_unlock():
        import time; time.sleep(5)
        if _lock.locked():
            try: _lock.release()
            except RuntimeError: pass
    threading.Thread(target=_force_unlock, daemon=True).start()
    return jsonify({"status": "stopping"})

@app.route("/api/validate-stores", methods=["POST"])
@optional_user_context
def api_validate_stores():
    denied = _require_admin_api()
    if denied:
        return denied
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break
    t = threading.Thread(target=_validate_stores, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/store-coords")
@optional_user_context
def api_store_coords():
    """Return cached store coordinates JSON (built by /api/build-store-coords)."""
    if STORE_COORDS_FILE.exists():
        try:
            return jsonify(json.loads(STORE_COORDS_FILE.read_text()))
        except Exception:
            pass
    return jsonify({})

@app.route("/api/build-store-coords", methods=["POST"])
@optional_user_context
def api_build_store_coords():
    """Trigger a one-time geocoding run to build gc_store_coords.json.
    Uses the existing SSE stream — progress shows up in the log panel."""
    denied = _require_admin_api()
    if denied:
        return denied
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress."}), 409
    _stop_event.clear()
    while not _q.empty():
        try: _q.get_nowait()
        except queue.Empty: break

    force = bool((request.json or {}).get("force", False))

    def _run():
        try:
            def _send(msg):
                _q.put({"type": "progress", "msg": msg})
            _build_store_coords(_send, force=force)
            _q.put({"type": "done", "baseline": False, "stopped": False,
                    "new_ids": [], "items": []})
        except Exception as e:
            _q.put({"type": "progress", "msg": f"Error: {e}"})
            _q.put({"type": "done", "baseline": False, "stopped": True,
                    "new_ids": [], "items": []})
        finally:
            try: _lock.release()
            except Exception: pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/progress")
@optional_user_context
def api_progress():
    run_id = request.args.get("run_id", "")
    # Each SSE connection gets its own subscriber queue so fan-out works correctly.
    # For non-run endpoints (populate, validate, etc.) fall back to the legacy global queue.
    if run_id:
        my_q = _subscribe_to_run(run_id)
        if my_q is None:
            # Run already finished or never existed — send an empty done so client recovers
            def _empty():
                yield f"data: {json.dumps({'type':'done','new_count':0,'new_items':[],'scanned':0})}\n\n"
            return Response(stream_with_context(_empty()), mimetype="text/event-stream",
                            headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
    else:
        my_q = _q
    def generate():
        try:
            while True:
                try:
                    msg = my_q.get(timeout=30)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("type") == "done":
                        break
                except queue.Empty:
                    yield f"data: {json.dumps({'type':'ping'})}\n\n"
        finally:
            if run_id and my_q is not _q:
                _cleanup_subscriber(run_id, my_q)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


def _check_store_url(store_name: str) -> tuple[bool, str]:
    """Check if a store name works in the GC filter URL.
    Returns (is_valid, working_name). Tries variations if the original fails."""
    def _try(name: str) -> bool:
        try:
            query = f"filters=stores:{name.replace(' ', '%20')}"
            url   = f"https://www.guitarcenter.com/Used/?{query}&page=1"
            r = _http.get(url, timeout=10, allow_redirects=True)
            return r.status_code != 404
        except Exception:
            return True  # network error — assume valid

    if _try(store_name):
        return True, store_name

    # Try stripping state suffix (e.g. "Albany NY" → "Albany")
    parts = store_name.rsplit(' ', 1)
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isupper():
        bare = parts[0]
        if _try(bare):
            return True, bare

    return False, store_name


def _validate_stores():
    """Check every store with a page-1 fetch, auto-fix names, remove 404s, then rebuild."""
    def send(msg): _q.put(msg)
    try:
        stores = get_store_list()
        total  = len(stores)
        removed = []
        renamed = []
        send({"type": "progress", "msg": f"Step 1: Validating {total} stores…"})
        send({"type": "progress", "msg": "About 0.5s per store. You can stop at any time."})

        updated_stores = list(stores)
        for i, store in enumerate(stores, 1):
            if _stop_event.is_set():
                send({"type": "progress", "msg": "⏹ Stopped by user."})
                break
            if i % 25 == 1:
                send({"type": "progress", "msg": f"  [{i}/{total}] checking…"})
            is_valid, working_name = _check_store_url(store)
            if not is_valid:
                _remove_invalid_store(store)
                removed.append(store)
                if store in updated_stores:
                    updated_stores.remove(store)
                send({"type": "progress", "msg": f"  ✗ Removed: {store}"})
            elif working_name != store:
                idx = updated_stores.index(store) if store in updated_stores else -1
                if idx >= 0:
                    updated_stores[idx] = working_name
                renamed.append(f"{store} → {working_name}")
                send({"type": "progress", "msg": f"  ✎ Renamed: {store} → {working_name}"})
            _sleep(0.5, 0.3)  # 0.2–0.8s between store checks

        # Save corrected names back to cache
        if renamed:
            try:
                d = json.loads(STORES_CACHE.read_text()) if STORES_CACHE.exists() else {}
                d["stores"] = sorted(set(updated_stores))
                STORES_CACHE.write_text(json.dumps(d))
            except Exception:
                pass

        if removed:
            send({"type": "progress", "msg": f"\n  Removed {len(removed)}: {', '.join(removed)}"})
        if renamed:
            send({"type": "progress", "msg": f"  Renamed {len(renamed)}: {', '.join(renamed)}"})
        if not removed and not renamed:
            send({"type": "progress", "msg": "\n  All stores validated — none removed or renamed."})

        if not _stop_event.is_set():
            send({"type": "progress", "msg": "\nStep 2: Rebuilding store list from GC's live data…"})
            try:
                new_stores = refresh_store_list()
                send({"type": "progress", "msg": f"  ✓ Store list rebuilt — {len(new_stores)} stores."})
            except Exception as e:
                send({"type": "progress", "msg": f"  Rebuild failed: {e}"})

        final_stores = get_store_list()
        send({"type": "progress", "msg": f"\n✓ Done — {len(final_stores)} valid stores in list."})
        send({"type": "done", "baseline": False, "stopped": _stop_event.is_set(),
              "scanned": total, "new_count": 0, "new_items": [], "all_items": [],
              "gap_fill": True, "fixed": len(removed)})
    except Exception as e:
        # Don't leak exception text over the (public) SSE stream — log it server-side.
        # (2026-07 audit / round-2 deferred L3)
        print(f"[scan] operation failed: {type(e).__name__}: {e}")
        send({"type": "done", "error": "Operation failed — see server logs.", "scanned": 0, "new_count": 0, "new_items": []})
    finally:
        # Guard against the /api/stop 5s force-unlock watchdog already having
        # released this lock if this thread's winddown ran long (RuntimeError:
        # release unlocked lock) — same pattern as admin_clear_lock/_force_unlock.
        # Harmless either way: the lock ends up unlocked regardless. (v2.16.27)
        try:
            _lock.release()
        except RuntimeError:
            pass


# ── Two-phase scan (v2.17.4, Phase G S1) ──────────────────────────────────────
# "Scan for New Listings" used to fetch every used item nationwide (~480 Algolia
# pages, ~15-29 s) before showing anything. NEW detection only compares each
# item's date_listed (= Algolia startDate) with the user's threshold, so a QUICK
# pass that asks Algolia only for items with startDate >= threshold finds exactly
# the same NEW items (and the same new anchor = max date_listed) in a page or
# few. The quick pass saves them and sends "done"; then a background SWEEP (the
# old full nationwide pass, minus the per-user NEW / anchor work) does the
# sold-marking and price updates. Chuck's choices (2026-09-30): the sweep runs
# after each Scan click (one at a time — a click during a sweep doesn't start
# another), and when it finishes the table is NOT redrawn; a status line says
# what changed and the next page flip / filter shows it.
_SWEEP_LOCK = threading.Lock()          # held for the whole sweep
_SWEEP_STATE_LOCK = threading.Lock()    # guards _SWEEP_STATE
_SWEEP_STOP = threading.Event()         # never set by users (their Stop is _stop_event)
_SWEEP_STATE = {"running": False, "started_at": None, "finished_at": None,
                "result": None, "quick_seen": set(), "runs": 0, "again": False}
_QUICK_SLACK_SECS = 3600   # window starts an hour before the threshold



def _quick_since_ts(threshold: str):
    """Epoch seconds for the quick-pass window start, from a NEW threshold string
    (an item date like "2026-09-30T15:04:05Z", a date-only "2026-09-30", or a
    wall-clock last-run time). Date-only → start of that day (the NEW check treats
    it as end of day, so this is a superset). None if unparseable → full scan."""
    t = (threshold or "").strip()
    if not t:
        return None
    try:
        if len(t) == 10:
            dt = datetime.strptime(t, "%Y-%m-%d")
        else:
            dt = datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    import calendar as _cal
    return int(_cal.timegm(dt.timetuple())) - _QUICK_SLACK_SECS


def _start_sweep() -> str:
    """Start the background sweep unless one is already running. Returns
    "started" or "running" (sent to the client in the quick pass's done). A click
    during a sweep queues ONE follow-up sweep (that sweep fetched its pages before
    the click), so every click is covered by a sweep that starts after it; the
    client keeps showing "Checking…" until the follow-up finishes."""
    if not _SWEEP_LOCK.acquire(blocking=False):
        with _SWEEP_STATE_LOCK:
            if _SWEEP_STATE["running"]:
                _SWEEP_STATE["again"] = True
        return "running"
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _SWEEP_STATE_LOCK:
        _SWEEP_STATE.update(running=True, started_at=now, finished_at=None, result=None,
                            quick_seen=set())
        _SWEEP_STATE["runs"] += 1
    def _go():
        try:
            rt = now
            while True:
                _run([], False, run_id="", run_time=rt, mode="sweep")
                with _SWEEP_STATE_LOCK:
                    if not _SWEEP_STATE["again"]:
                        break
                    rt = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    _SWEEP_STATE.update(again=False, started_at=rt, result=None, quick_seen=set())
                    _SWEEP_STATE["runs"] += 1
                print("[sweep] a scan arrived during the sweep — running one follow-up sweep")
        except Exception as e:   # _run records its own errors; belt and braces
            print(f"[sweep] crashed: {type(e).__name__}: {e}")
        finally:
            with _SWEEP_STATE_LOCK:
                _SWEEP_STATE.update(running=False, quick_seen=set(), again=False,
                                    finished_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
                if _SWEEP_STATE["result"] is None:
                    _SWEEP_STATE["result"] = {"complete": False, "error": "sweep ended without a result"}
            _SWEEP_LOCK.release()
    try:
        threading.Thread(target=_go, daemon=True).start()
    except Exception:
        with _SWEEP_STATE_LOCK:
            _SWEEP_STATE["running"] = False
        _SWEEP_LOCK.release()
        raise
    return "started"


@app.route("/api/sweep-status")
def api_sweep_status():
    """Public, tiny: is the background sold/price sweep running, and what did the
    last one find. static/gc.js polls it after a quick scan (v2.17.4)."""
    with _SWEEP_STATE_LOCK:
        st = {k: _SWEEP_STATE[k] for k in ("running", "started_at", "finished_at")}
        st["result"] = dict(_SWEEP_STATE["result"]) if _SWEEP_STATE["result"] else None
    return jsonify(st)


_CATCHUP_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


@app.route("/api/new-catchup", methods=["POST"])
def api_new_catchup():
    """v2.20.0: after a quick pass, the background sweep may add listings the
    site had never seen (late arrivals with older listed dates). When the sweep
    finishes, static/gc.js asks for everything first seen since the user's
    PREVIOUS scan (`since` = the done message's late_since) up to now, adds it to
    its NEW set and moves its last-scan time to `until`, so those listings show
    (tagged NEW) on the next page flip / sort / filter. Public and cheap
    (first_seen index); it holds the scan DB lock only to read, so it never sees
    half of a scan's write."""
    data = request.get_json(silent=True) or {}
    since = str(data.get("since") or "").strip()
    if not _CATCHUP_TS_RE.match(since):
        return jsonify({"error": "bad since"}), 400
    if _PG_POOL is None:
        return jsonify({"error": "unavailable"}), 503
    if not _PG_SCAN_DB_LOCK.acquire(timeout=20):
        return jsonify({"error": "busy"}), 503
    try:
        until = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [] if until <= since else _pg_first_seen_between(since, until)
    except Exception as e:
        print(f"[new] catch-up failed: {type(e).__name__}: {e}")
        return jsonify({"error": "failed"}), 500
    finally:
        _PG_SCAN_DB_LOCK.release()
    dates = [d for _, d in rows if d]
    return jsonify({"new_ids": [sku for sku, _ in rows], "until": until,
                    "max_date": max(dates) if dates else "",
                    # v2.22.3: listed before `since` (the previous scan) — older
                    # listings that only just became visible on GC
                    "older_ids": [sku for sku, d in rows if d and d < since]})


def _run(selected_stores: list[str], baseline: bool, run_id: str = "", device_last_run: str = "", run_time: str = "", device_last_anchor: str = "", user_id: int | None = None,
         mode: str = "full"):
    """mode (v2.17.4, Phase G S1): "full" = the classic scan (baseline, store scans,
    and any nationwide click with no NEW threshold); "quick" = nationwide pass over
    only items listed since the user's NEW threshold — finds every NEW item, saves
    them, sends "done", then starts the background sweep; "sweep" = the background
    full nationwide pass for sold-marking / price changes (no user, no SSE, no
    anchor/NEW work; its own stop event, so a user's Stop doesn't cancel it)."""
    def send(msg):
        if mode == "sweep":
            return                    # background: nobody is listening
        if run_id:
            _broadcast(run_id, msg)   # fan-out to all subscribers
        _q.put(msg)                   # also send to legacy queue for backwards compat
    stop_ev = _SWEEP_STOP if mode == "sweep" else _stop_event
    _t_scan0 = time.time()            # Phase G timing (v2.17.1)
    try:
        # Use the run_time passed in from api_run (computed before thread start)
        # so the client and server share the exact same timestamp.
        if not run_time:
            run_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        stores_to_scan = selected_stores if not baseline else []
        nationwide = baseline or len(stores_to_scan) == 0
        # v2.17.4 (Phase G S1): quick pass window — everything listed since this
        # user's NEW threshold (same precedence as the NEW check below: anchor,
        # else last run, else the global last-scan time), minus an hour of slack.
        _quick_since = None
        if mode == "quick":
            if not nationwide:
                mode = "full"
            else:
                _qthr = device_last_anchor or device_last_run
                if not _qthr:
                    _lsf = DATA_DIR / "gc_last_scan.txt"
                    _qthr = _lsf.read_text().strip() if _lsf.exists() else ""
                _quick_since = _quick_since_ts(_qthr)
                if _quick_since is None:
                    mode = "full"
        label = ("nationwide scan" if nationwide else f"{len(stores_to_scan)} store(s)")
        if mode == "quick":
            label = "check for new listings"
        send({"type":"progress","msg":f"Starting {label}…"})
        # v2.17.3 (Phase G S3): read the stored rows while Algolia is fetched.
        # (v2.17.4) Not for the quick pass — it only needs its few found SKUs.
        _prefetch = None
        if mode != "quick" and _PSYCOPG2_AVAILABLE and PG_DATABASE_URL and _PG_POOL is not None:
            try:
                _prefetch = _PriorPrefetch(None if nationwide else stores_to_scan)
            except Exception as _e:
                print(f"[pg] prior prefetch not started: {type(_e).__name__}: {_e}")

        all_products, ids_this_run = [], set()
        # Track scan coverage gaps so we never let a run that MISSED some stores'
        # or pages' data (timeout, transient error, user stop) silently mark those
        # stores' items sold, or advance the NEW-detection anchor past items we
        # never actually got a chance to see (v2.16.11 — see scrape_store()).
        incomplete_stores: set[str] = set()
        scan_incomplete = False

        if nationwide:
            # ── Nationwide: query ALL used inventory via parallel page fetches ────
            PARALLEL_WORKERS = 15  # concurrent API requests
            send({"type":"progress","msg":("Fetching listings added since your last scan…" if mode == "quick"
                                           else "Fetching all used inventory nationwide via API…")})
            # v2.16.51: coverage accounting. Live 2026-09-29 every nationwide scan
            # came back 1,200-1,440 unique items (exactly 5-6 whole pages of 240)
            # short of Algolia's own nbHits, with no error — and sold-marking then
            # marked those items sold, so a different ~1.3K flickered sold/available
            # on every scan. Per page we now record raw hits / parsed / new-unique,
            # retry suspect pages, and if we still can't account for nbHits the scan
            # is treated as incomplete (no sold-marking, anchor not advanced) —
            # the same rule a failed page already triggers. See _SCAN_COVERAGE.
            cov = {"at": run_time, "nb_hits": 0, "nb_pages": 0, "raw_hits": 0,
                   "empty_pages": [], "short_pages": [], "parse_loss_pages": [],
                   "no_new_pages": [], "retried_pages": 0, "recovered": 0,
                   "unique": 0, "missing": 0, "complete": None}
            page_stats = {}   # pg -> (raw, parsed, new)
            # Phase G timing (v2.17.1): where the fetch phase's time goes.
            _ft = {"req": [], "absorb_ms": 0.0, "t0": time.perf_counter()}
            use_lean = False

            def _raw_hits(d):
                try:
                    return len((d.get("results") or [{}])[0].get("hits") or [])
                except Exception:
                    return 0

            def _absorb(pg, d):
                """Add one page's products; record its stats. Returns #new unique."""
                products = parse_products(d, None)
                new = 0
                for p in products:
                    if p["id"] not in ids_this_run:
                        all_products.append(p)
                        ids_this_run.add(p["id"])
                        new += 1
                prev = page_stats.get(pg)
                page_stats[pg] = (_raw_hits(d), len(products), new + (prev[2] if prev else 0))
                return new

            # First fetch page 1 to learn total pages. (v2.17.3) Page 1 is also
            # fetched "lean" at the same time; the rest of the scan uses lean
            # pages only if both parse to the same items (see _lean_page1_ok).
            _lean_box = {}
            def _lean1():
                try:
                    _lean_box["d"] = fetch_page(None, 1, lean=True, since_ts=_quick_since)
                except Exception as _e:
                    _lean_box["err"] = f"{type(_e).__name__}: {_e}"[:200]
            _lean_t = threading.Thread(target=_lean1, daemon=True)
            _lean_t.start()
            try:
                data1 = fetch_page(None, 1, _ft["req"], since_ts=_quick_since)
            except Exception as e:
                send({"type":"progress","msg":f"  API error on page 1: {e}"})
                data1 = None
            _lean_t.join(25)
            use_lean, _ft["lean_note"] = _lean_page1_ok(data1, _lean_box)
            if not use_lean:
                print(f"[scan] lean pages NOT used this scan: {_ft['lean_note']}")
            if not data1:
                scan_incomplete = True
            if data1:
                _absorb(1, data1)
                try:
                    nb_pages = data1.get("results", [{}])[0].get("nbPages", 1)
                    nb_hits  = data1.get("results", [{}])[0].get("nbHits", 0)
                    send({"type":"progress","msg":(f"  {nb_hits:,} recent listing(s) to check…" if mode == "quick" else
                                                   f"  {nb_hits:,} items across {nb_pages} pages — fetching {PARALLEL_WORKERS} pages at a time…")})
                except Exception:
                    nb_pages, nb_hits = 1, 0
                cov["nb_hits"], cov["nb_pages"] = nb_hits, nb_pages
                # Fetch remaining pages in parallel batches
                remaining = list(range(2, min(nb_pages + 1, 1001)))
                def _fetch_one_page(pg):
                    if stop_ev.is_set():
                        return pg, None, None
                    try:
                        d = fetch_page(None, pg, _ft["req"], lean=use_lean, since_ts=_quick_since)
                        return pg, d, None
                    except Exception as exc:
                        return pg, None, exc
                # (v2.17.3, Phase G S2) One pool, PARALLEL_WORKERS pages in flight at
                # all times: the next page starts as soon as any page finishes. It used
                # to fetch lock-step batches of 15 where each batch waited for its
                # slowest page (live v2.17.1: batch p50 722 ms vs page p50 486 ms).
                # Stall guard (v2.16.10's per-batch ceiling, same 90 s): if NO page
                # completes for PAGE_STALL_TIMEOUT, the unfinished pages are skipped
                # and the scan is incomplete (no sold-marking) — a stuck connection
                # past requests' own timeout= can't hang the scan.
                PAGE_STALL_TIMEOUT = 90
                pool = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
                pending = {pool.submit(_fetch_one_page, pg): pg for pg in remaining}
                done_n = 1   # page 1
                try:
                    while pending:
                        done, _ = _futures_wait(list(pending), timeout=PAGE_STALL_TIMEOUT,
                                                return_when=_FIRST_COMPLETED)
                        if not done:
                            stuck = sorted(pending.values())
                            scan_incomplete = True
                            send({"type":"progress","msg":f"  ⚠ {len(stuck)} page(s) {stuck[:20]} stalled past {PAGE_STALL_TIMEOUT}s — skipping, continuing scan."})
                            break
                        for fut in done:
                            pending.pop(fut, None)
                            pg, data, err = fut.result()
                            done_n += 1
                            if err:
                                send({"type":"progress","msg":f"  API error on page {pg}: {err}"})
                                scan_incomplete = True
                            elif data is not None:
                                _ta = time.perf_counter()
                                _absorb(pg, data)
                                _ft["absorb_ms"] += (time.perf_counter() - _ta) * 1000.0
                            if done_n % PARALLEL_WORKERS == 0 or not pending:
                                send({"type":"progress","msg":f"  page {min(done_n, nb_pages)}/{nb_pages}… ({len(all_products):,} items so far)"})
                finally:
                    # wait=False: never block on a genuinely stuck worker thread (the
                    # hang v2.16.10 fixed); cancel_futures drops pages not yet started
                    # (after a stall, or once a stop has made the rest return at once).
                    pool.shutdown(wait=False, cancel_futures=True)

                # ── Coverage check + retry of suspect pages (v2.16.51) ──────────
                def _suspects():
                    full = min(240, nb_hits)   # hitsPerPage (see fetch_page)
                    out = []
                    for pg in range(1, min(nb_pages, 1000) + 1):
                        st = page_stats.get(pg)
                        if st is None:
                            continue   # errored/stopped page — already makes the scan incomplete
                        raw, parsed, new = st
                        is_last = (pg == nb_pages)
                        if raw == 0 or (not is_last and raw < full) or parsed < raw or new == 0:
                            out.append(pg)
                    return out
                def _missing():
                    return max(0, nb_hits - len(ids_this_run))
                if not stop_ev.is_set() and not scan_incomplete and _missing() > _NATIONWIDE_COVERAGE_TOLERANCE:
                    for attempt in range(_NATIONWIDE_PAGE_RETRY_ROUNDS):
                        sus = _suspects()
                        if not sus or stop_ev.is_set() or _missing() <= _NATIONWIDE_COVERAGE_TOLERANCE:
                            break
                        send({"type":"progress","msg":f"  {_missing():,} items unaccounted for — re-checking {len(sus)} page(s) (round {attempt + 1})…"})
                        gained = 0
                        for pg in sus[:_NATIONWIDE_PAGE_RETRY_MAX]:
                            if stop_ev.is_set():
                                break
                            _sleep(0.3, 0.2)
                            pg_, d, err = _fetch_one_page(pg)
                            cov["retried_pages"] += 1
                            if err or d is None:
                                continue
                            gained += _absorb(pg, d)
                        cov["recovered"] += gained
                        if gained == 0:
                            break
                # Record what we saw (the suspects after retries).
                for pg, (raw, parsed, new) in sorted(page_stats.items()):
                    cov["raw_hits"] += raw
                    if raw == 0:
                        cov["empty_pages"].append(pg)
                    elif pg != nb_pages and raw < min(240, nb_hits):
                        cov["short_pages"].append(pg)
                    if parsed < raw:
                        cov["parse_loss_pages"].append(pg)
                    if new == 0:
                        cov["no_new_pages"].append(pg)
                for k in ("empty_pages", "short_pages", "parse_loss_pages", "no_new_pages"):
                    cov[k] = cov[k][:50]
                cov["unique"] = len(ids_this_run)
                cov["missing"] = _missing()
                if not scan_incomplete and not stop_ev.is_set() and cov["missing"] > _NATIONWIDE_COVERAGE_TOLERANCE:
                    scan_incomplete = True
                    send({"type":"progress","msg":f"  ⚠ {cov['missing']:,} of {nb_hits:,} items never came back from the API — treating this scan as incomplete (nothing marked sold)."})
                cov["complete"] = not scan_incomplete and not stop_ev.is_set()
                cov["mode"] = mode
                print(f"[scan] nationwide{' quick' if mode == 'quick' else (' sweep' if mode == 'sweep' else '')} coverage: nbHits {nb_hits}, pages {nb_pages}, raw hits {cov['raw_hits']}, "
                      f"unique {cov['unique']}, missing {cov['missing']}, empty {cov['empty_pages'][:10]}, "
                      f"short {cov['short_pages'][:10]}, parse-loss {cov['parse_loss_pages'][:10]}, "
                      f"no-new {cov['no_new_pages'][:10]}, retried {cov['retried_pages']}, recovered {cov['recovered']}, "
                      f"complete {cov['complete']}")
                _SCAN_COVERAGE["last"] = cov
                try:   # Phase G timing (v2.17.1) — measurement only
                    _rq = sorted(x[0] for x in _ft["req"])
                    _pct = lambda v, p: round(v[min(len(v) - 1, int(p / 100.0 * (len(v) - 1)))]) if v else None
                    _kb = sum(x[1] for x in _ft["req"]) / 1024.0
                    _dec = sum(x[2] for x in _ft["req"])
                    _fetch_s = (time.perf_counter() - _ft["t0"])
                    _fs = {"mode": "lean" if use_lean else "full", "workers": PARALLEL_WORKERS,
                           "pages": len(_rq), "wall_s": round(_fetch_s, 1),
                           "req_ms_p50": _pct(_rq, 50), "req_ms_p90": _pct(_rq, 90), "req_ms_max": _pct(_rq, 100),
                           "kb_per_page": round(_kb / max(1, len(_rq)), 1), "mb_total": round(_kb / 1024.0, 1),
                           "json_decode_s": round(_dec / 1000.0, 2), "parse_s": round(_ft["absorb_ms"] / 1000.0, 2)}
                    if not use_lean:
                        _fs["lean_note"] = _ft.get("lean_note", "")
                    cov["fetch_timing"] = _fs
                    print("[timing] scan fetch: " + ", ".join(f"{k} {v}" for k, v in _fs.items()))
                except Exception as _e:
                    print(f"[timing] scan fetch stats failed: {type(_e).__name__}: {_e}")
                _SCAN_COVERAGE["recent"].insert(0, {k: cov[k] for k in ("at", "nb_hits", "unique", "missing", "retried_pages", "recovered", "complete")})
                del _SCAN_COVERAGE["recent"][20:]
                if stop_ev.is_set():
                    send({"type":"progress","msg":"⏹ Stopped by user."})
                    scan_incomplete = True
            send({"type":"progress","msg":f"  Fetched {len(all_products):,} items total."})
        else:
            # ── Normal scan: query selected stores in parallel ────────────────
            STORE_WORKERS = 10
            # Hard wall-clock ceiling on the whole batch of stores, scaled by count
            # (12s/store budget — generous headroom over normal completion time,
            # since up to STORE_WORKERS run concurrently). This is what fixes the
            # bug where a single-store scan would sit pinging with no progress and
            # no completion for 10+ minutes: fetch_page()'s requests timeout= only
            # bounds socket-level stalls (no bytes for N seconds), not a slow trickle
            # that keeps the connection technically alive — this bounds the whole
            # wait regardless of what's stalling underneath (v2.16.10).
            STORE_SCAN_TIMEOUT = max(120, len(stores_to_scan) * 12)
            send({"type":"progress","msg":f"Scanning {len(stores_to_scan)} stores ({STORE_WORKERS} at a time)…"})
            completed = [0]
            lock = threading.Lock()
            def _scan_one_store(store):
                if stop_ev.is_set():
                    return store, [], set(), False
                _rotate_ua()
                products, ids, complete = scrape_store(store, send, stop_ev)
                with lock:
                    completed[0] += 1
                    send({"type":"progress","msg":f"  [{completed[0]}/{len(stores_to_scan)}] {store} — {len(products)} items"})
                return store, products, ids, complete
            pool = ThreadPoolExecutor(max_workers=STORE_WORKERS)
            try:
                futures = {pool.submit(_scan_one_store, s): s for s in stores_to_scan}
                try:
                    for fut in as_completed(futures, timeout=STORE_SCAN_TIMEOUT):
                        if stop_ev.is_set():
                            send({"type":"progress","msg":"⏹ Stopped by user."})
                            scan_incomplete = True
                            break
                        store, products, ids, complete = fut.result()
                        if not complete:
                            incomplete_stores.add(store)
                        for p in products:
                            if p["id"] not in ids_this_run:
                                all_products.append(p)
                        ids_this_run |= ids
                except _FutureTimeoutError:
                    stuck = [futures[f] for f in futures if not f.done()]
                    incomplete_stores.update(stuck)
                    send({"type":"progress","msg":f"  ⚠ {stuck} stalled past {STORE_SCAN_TIMEOUT}s — skipping, continuing with what we have."})
            finally:
                # wait=False: never block here on a genuinely stuck worker thread — see note above.
                pool.shutdown(wait=False)

        # (cache-ID snapshot removed — NEW detection now uses startDate timestamps)

        # Items Algolia returned with an empty stores array but a known location
        # get their store filled in (v2.16.50, see _fill_missing_stores).
        _filled = _fill_missing_stores(all_products)
        if _filled:
            send({"type":"progress","msg":f"  {_filled:,} item(s) had no store listed — assigned from their location."})
            print(f"[scan] filled missing store for {_filled} item(s) from location")

        # ── Anchor date for NEW detection (per-user, v2.10.18) ───────────────────
        # The anchor represents "the max date_listed of items this user was exposed
        # to at their last scan." Anything with date_listed > anchor is genuinely new
        # to THIS user. This handles Algolia's 6-12h indexing pipeline delay: items
        # can appear in search results with date_listed values older than the last
        # scan time, which would make them invisible to timestamp-based detection
        # but they'd silently push existing items down the date-sorted table (the
        # "0 new / reordered" bug).
        #
        # IMPORTANT: We use the *per-user* stored anchor (passed in as
        # device_last_anchor), NOT max(date_listed in the catalog). The catalog is the
        # global shared inventory written by EVERY user's scan, so reading it here
        # contaminates the anchor with other users' activity — if Alice scanned five
        # minutes ago, Bob's threshold would jump to Alice's freshest item and Bob
        # would see 0 new items even when items are genuinely new to him. (Bug
        # introduced in v2.10.11, fixed in v2.10.18.)
        anchor_date = device_last_anchor or ""

        # ── Sold-marking scope ─────────────────────────────────────────────────
        # Only mark items sold for stores/coverage we're confident we saw a
        # COMPLETE picture of this run — a store whose fetch errored, timed out,
        # or got abandoned mid-scan (incomplete_stores) is not evidence its items
        # sold, just evidence we didn't finish looking. For a nationwide scan,
        # completeness isn't per-store — if page 1 failed, a page stalled, or the
        # scan was stopped early, we don't know which SKUs we missed, so skip
        # sold-marking entirely rather than risk wiping out items still in stock.
        # (v2.16.11 — see scrape_store()'s `complete` flag.) Applied in SQL by
        # _pg_write_scan since v2.16.48.
        if stop_ev.is_set() or (nationwide and scan_incomplete):
            sold_scope = None
        elif mode == "quick":
            sold_scope = None     # v2.17.4: only saw recent listings — the sweep does sold-marking
        elif nationwide:
            sold_scope = "nationwide"
        else:
            sold_scope = sorted(set(stores_to_scan) - incomplete_stores)

        # ── Postgres: prior state → merge → write (Phase F 5b-ii, v2.16.48) ────
        # Postgres is the source of truth. Read every found SKU's prior state,
        # merge (_merge_scan_item — price-drop carry-forward, first_seen, fallbacks
        # for fields Algolia sent blank), then upsert + sold-mark in ONE
        # transaction BEFORE "done". Any failure fails the scan visibly: nothing
        # is written and the user's NEW anchor /
        # last_run are not advanced, so the next scan simply redoes this one.
        def _save_failed(stage, exc):
            msg = f"{stage}: {type(exc).__name__}: {exc}"[:300]
            _pg_scan_note("failed", last_error=msg,
                          last_error_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
            print(f"[pg] SCAN WRITE FAILED ({stage}{', sweep' if mode == 'sweep' else ''}) — scan not saved: {type(exc).__name__}: {exc}")
            if mode == "sweep":
                with _SWEEP_STATE_LOCK:
                    _SWEEP_STATE["result"] = {"complete": False, "error": f"save failed ({stage})"}
            send({"type": "done",
                  "error": "The scan finished but its results couldn't be saved (database "
                           "unavailable). Nothing was changed — please try again in a few minutes.",
                  "scanned": 0, "new_count": 0, "new_items": []})

        if not (_PSYCOPG2_AVAILABLE and PG_DATABASE_URL and _PG_POOL is not None):
            _save_failed("config", RuntimeError("Postgres not configured"))
            return
        send({"type": "progress", "msg": f"  Saving changes for {len(all_products):,} items…"})
        late_ids = None        # v2.22.2: first-seen NEW list, filled under the DB lock below
        _new_older = 0         # v2.22.3: of those, listed before the previous scan
        _t_db = time.time()
        if not _PG_SCAN_DB_LOCK.acquire(timeout=_PG_SCAN_DB_LOCK_TIMEOUT):
            _save_failed("lock", TimeoutError("previous scan's database write still running"))
            return
        try:
            try:
                _found = {p["id"] for p in all_products}
                prior = _prefetch.result() if _prefetch is not None else None
                if prior is not None:
                    # v2.17.3: only new / reappearing SKUs still need reading.
                    _missing_skus = _found - prior.keys()
                    _pre_n = len(prior)
                    prior.update(_pg_scan_prior_fetch(_missing_skus))
                    _read_note = (f"prefetched {_pre_n:,} in {_prefetch.ms}ms during fetch, "
                                  f"+{len(_missing_skus):,} read now")
                else:
                    prior = _pg_scan_prior_fetch(_found)
                    _read_note = "full read" + (f" ({_prefetch.note})" if _prefetch is not None and _prefetch.note else "")
                _prefetch = None
            except Exception as e:
                _save_failed("prior read", e)
                return
            _read_ms = int((time.time() - _t_db) * 1000)
            # Categories, condition, brand all come from the API — the prior
            # row only fills blanks and carries price_drop_since / first_seen.
            merged = {}
            # v2.20.0: the background sweep stamps first_seen / price_drop_since
            # with the moment it took the DB lock, not when it started fetching.
            # Its writes then never carry a time earlier than a quick pass that
            # read the table before them — so "first seen after your last scan"
            # (the late-arrival half of the NEW rule) can't skip one of them.
            _stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") if mode == "sweep" else run_time
            for p in all_products:
                sku = p["id"]
                if sku in merged:            # duplicate SKU in one run: chain, like the JSON path did
                    cached = merged[sku]
                elif sku in prior:
                    cached = dict(zip(_PG_PRIOR_COLS, prior[sku]))
                else:
                    cached = {}
                rec = _merge_scan_item(p, cached, _stamp)
                merged[sku] = rec
                p["category"]    = rec["category"]
                p["subcategory"] = rec["subcategory"]
                p["condition"]   = rec["condition"]
                p["brand"]       = rec["brand"]
                p["location"]    = rec["location"]
                p["price_drop"]  = rec["price_drop"]
                p["list_price"]  = rec["list_price"]
            # Only new or changed rows are written (v2.16.49). Most of a scan's
            # items are identical to what's stored; rewriting them all cost a
            # full row + GIN/trigram index update each (~11s per nationwide scan).
            changed_rows = []
            _price_drops = 0
            _price_idx = _PG_PRIOR_COLS.index("price")
            for sku, rec in merged.items():
                row = _pg_row_for(sku, rec)
                pr = prior.get(sku)
                if pr is None or not _pg_row_unchanged(row, pr):
                    changed_rows.append(row)
                    try:
                        if pr is not None and pr[_price_idx] is not None and rec.get("price") \
                                and float(rec["price"]) < float(pr[_price_idx]):
                            _price_drops += 1
                    except (TypeError, ValueError):
                        pass
            del prior
            _run_ids = ids_this_run | set(merged)
            if mode == "sweep":
                # v2.17.4: never mark sold anything a quick pass saw listed while
                # this sweep was running (it may have been listed after the sweep
                # fetched that page). Read under _PG_SCAN_DB_LOCK — quick passes
                # add to it under the same lock, before releasing it.
                with _SWEEP_STATE_LOCK:
                    _run_ids |= _SWEEP_STATE["quick_seen"]
            _t_write = time.time()
            try:
                sold = _pg_write_scan(changed_rows, _run_ids, sold_scope, send)
            except Exception as e:
                _save_failed("write", e)
                return
            if mode == "quick":
                with _SWEEP_STATE_LOCK:
                    if _SWEEP_STATE["running"]:
                        _SWEEP_STATE["quick_seen"] |= set(merged)
            _write_ms = int((time.time() - _t_write) * 1000)
            # v2.22.2 NEW rule (Chuck, 2026-10-09): NEW = listings first seen by the
            # site since this user's previous scan (no-history devices: since the
            # last scan anyone ran), whatever their listed date. Read while still
            # holding the DB lock so no other write can land in between.
            # late_ids stays None on failure → the old dated rule is the fallback.
            if mode != "sweep" and not baseline:
                _fs_since = device_last_run
                if not _fs_since:
                    _lsf0 = DATA_DIR / "gc_last_scan.txt"
                    _fs_since = _lsf0.read_text().strip() if _lsf0.exists() else ""
                if _fs_since:
                    try:
                        _fs_rows = _pg_first_seen_between(
                            _fs_since, run_time, None if nationwide else stores_to_scan)
                        late_ids = [r[0] for r in _fs_rows]
                        # v2.22.3: how many were listed before the previous scan, i.e.
                        # older listings that only just became visible on GC.
                        _new_older = sum(1 for _sku, _dl in _fs_rows if _dl and _dl < _fs_since)
                    except Exception as e:
                        late_ids = None
                        print(f"[new] first-seen lookup failed (scan falls back to the dated NEW rule): "
                              f"{type(e).__name__}: {e}")
        finally:
            _PG_SCAN_DB_LOCK.release()
        _db_ms = int((time.time() - _t_db) * 1000)
        _t_db_end = time.time()
        _pg_scan_note("ok", last_ok_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                      last_ms=_db_ms, last_rows=len(merged), last_sold=len(sold),
                      last_changed=len(changed_rows), last_read_ms=_read_ms,
                      last_write_ms=_write_ms)
        _PG_AVAILABLE_COUNT["ts"] = 0   # /api/state: recount now, not up to 60s later
        print(f"[pg] scan saved: {len(merged):,} found, {len(changed_rows):,} new/changed written, "
              f"{len(sold):,} marked sold — read {_read_ms}ms ({_read_note}), write {_write_ms}ms, total {_db_ms}ms")
        send({"type": "progress", "msg": f"  {len(changed_rows):,} new/changed, {len(sold):,} sold."})

        if mode == "sweep":
            # v2.17.4: background sweep — no user, no NEW / anchor work.
            _t_done = time.time()
            _res = {"complete": sold_scope is not None, "found": len(all_products),
                    "sold": len(sold), "changed": len(changed_rows), "price_drops": _price_drops,
                    "total_ms": int((_t_done - _t_scan0) * 1000), "error": ""}
            with _SWEEP_STATE_LOCK:
                _SWEEP_STATE["result"] = _res
            _timing_note_scan(kind="sweep", found=len(all_products), stopped=False,
                              total_ms=_res["total_ms"], fetch_ms=int((_t_db - _t_scan0) * 1000),
                              save_ms=_db_ms, finish_ms=int((_t_done - _t_db_end) * 1000))
            print(f"[timing] sweep done: {len(all_products):,} found, {len(sold):,} sold, "
                  f"{_price_drops:,} price drops, {len(changed_rows):,} changed, complete {_res['complete']}, "
                  f"total {_res['total_ms']}ms (fetch {int((_t_db - _t_scan0) * 1000)}ms, save {_db_ms}ms)")
            return

        send({"type":"progress","msg":f"  {len(all_products):,} products scanned."})

        # Read global last-scan time (fallback when device has no history)
        last_scan_file = DATA_DIR / "gc_last_scan.txt"
        global_prev_scan = last_scan_file.read_text().strip() if last_scan_file.exists() else ""
        # Per-device prev_scan: prefer the device's own last-run timestamp sent
        # from localStorage. Falls back to the global scan time so first-time
        # devices on an existing server don't see the entire catalog as NEW.
        prev_scan_time = device_last_run or global_prev_scan
        # Record this scan's completion time globally (for devices with no history)
        last_scan_file.write_text(run_time)

        def fmt(p):
            date_src = p.get("date_listed") or merged.get(p["id"], {}).get("date_listed", "")
            lp = p.get("list_price") or 0
            return {
                "id":               p["id"],
                "name":             p["name"],
                "brand":            p.get("brand", ""),
                "price":            f"${p['price']:,.2f}" if p["price"] else "",
                "price_raw":        p.get("price") or 0,
                "list_price_raw":   lp,
                "price_drop":       p.get("price_drop", 0),
                "price_drop_since": merged.get(p["id"], {}).get("price_drop_since", ""),
                "store":            p["store"],
                "location":         p.get("location") or p.get("store", ""),
                "url":              p["url"],
                "category":         p.get("category", ""),
                "subcategory":      p.get("subcategory", ""),
                "condition":        p.get("condition", ""),
                "date":             _fmt_date(date_src),
                "date_raw":         date_src,
                "image_id":         p.get("image_id") or merged.get(p["id"], {}).get("image_id", ""),
                "condition_note":   p.get("condition_note") or merged.get(p["id"], {}).get("condition_note", ""),
            }

        # ── Per-device new-item detection ─────────────────────────────────────
        # An item is NEW if date_listed > threshold, where threshold is whichever
        # is more recent: the anchor_date (most recent item in pre-scan cache) or
        # prev_scan_time (last wall-clock scan time). The anchor approach is primary
        # because it's immune to Algolia's indexing pipeline delay — items that appear
        # in search results after our last scan but carry older date_listed values
        # (the "0 new / table reordered" bug) won't pollute the sort without being flagged.
        # GC sometimes stores date-only values ("2026-05-05") with no time component.
        # A plain string compare like "2026-05-05" > "2026-05-05T08:00:00Z" is False
        # (shorter string sorts before longer at that position), so items listed today
        # would never be flagged new once any scan ran today. Fix: treat date-only
        # values as end-of-day ("2026-05-05T23:59:59Z") so they stay new all day.
        def _norm_item_date(d):
            return d + "T23:59:59Z" if d and len(d) == 10 else d

        # Threshold = the max date_listed the user was actually exposed to at their
        # last scan (the "top of their table"). Anything with a newer date_listed is
        # genuinely new to this user.
        # We intentionally do NOT mix in prev_scan_time (wall-clock scan time) here.
        # Wall-clock timestamps are lexicographically larger than date-only strings
        # (e.g. "2026-05-18T08:00:00Z" > "2026-05-17"), so including prev_scan_time
        # in a max() would inflate the threshold above items that are genuinely new —
        # exactly the "5 items between the known item and the 10 new ones, none flagged"
        # bug. Use anchor-only; fall back to prev_scan_time only on first scan.
        _norm_anchor = _norm_item_date(anchor_date) if anchor_date else ""
        threshold = _norm_anchor if _norm_anchor else prev_scan_time

        new_ids_list = []
        if late_ids is not None:
            # v2.22.2 (Chuck, 2026-10-09): NEW is ONLY "first seen by the site since
            # your previous scan". Dropped the listed-date half: it could tag an item
            # the site already had (e.g. a return GC relisted with a fresh date) and
            # anything it caught that was genuinely new is first-seen in the window
            # anyway (this scan's own inserts carry first_seen = run_time).
            new_ids_list = list(dict.fromkeys(late_ids))
        elif not baseline and threshold:
            for p in all_products:
                item_date = p.get("date_listed") or merged.get(p["id"], {}).get("date_listed", "")
                if item_date and _norm_item_date(item_date) > threshold:
                    new_ids_list.append(p["id"])

        # (first_seen is kept when an item sells and comes back, so returns and
        # reappearing items are never NEW under the v2.22.2 rule.)
        _cut = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _old_dated = 0
        if late_ids is not None and new_ids_list:
            _dl_of = {p["id"]: (p.get("date_listed") or "") for p in all_products}
            _old_dated = sum(1 for sku in new_ids_list if (_dl_of.get(sku) or "9") < _cut)
        send({"type":"progress","msg":f"  {len(new_ids_list):,} new items since last scan."})
        if late_ids is not None:
            print(f"[new] {len(new_ids_list)} NEW (first seen since {(device_last_run or 'global last scan')}); "
                  f"of the ones this pass fetched, {_old_dated} listed 3+ days ago")

        # ── Compute new per-user anchor (post-scan) ─────────────────────────────
        # The anchor we persist for this user is the max date_listed across the
        # cache AFTER this scan. It represents "everything I've now been exposed
        # to" — next time this user scans, anything older than this anchor will
        # be treated as already-seen even if Algolia surfaces it freshly (the
        # indexing-delay protection that anchor_date was designed for).
        new_anchor = ""
        # Only let this scan's data advance the anchor if we're confident we saw
        # a COMPLETE picture — one or more stores that errored/timed out/got
        # abandoned (incomplete_stores), or a nationwide scan that hit a page
        # failure or got stopped early (scan_incomplete), means this run's max
        # date_listed may be missing whatever the UNSEEN store(s)/page(s)' freshest
        # listings actually are. Advancing the anchor anyway would silently block
        # those genuinely-new items from ever being flagged NEW once we do see
        # them — the "item shows up but never gets tagged NEW" bug this guards
        # against. Trade-off: on a run with any coverage gap, the anchor simply
        # doesn't move forward this time (safe — worst case, an already-seen item
        # gets re-flagged NEW on a later clean scan) rather than risk moving past
        # something we never actually saw. (v2.16.11)
        coverage_ok = not scan_incomplete and not incomplete_stores
        if all_products and coverage_ok:
            # Use THIS scan's products only — not the catalog, which is global and
            # shared across all users. Using the catalog re-introduces the contamination
            # bug: another user's scan populates it with fresher items, inflating this
            # user's anchor and causing 0-new on their next scan.
            # (v2.10.18 fixed the threshold for the current scan but not persistence.)
            _scan_dates = [p.get("date_listed", "") for p in all_products if p.get("date_listed")]
            if _scan_dates:
                new_anchor = max(_scan_dates)
        elif not coverage_ok:
            send({"type":"progress","msg":"  ⚠ scan had gaps (some stores/pages didn't complete) — NEW-detection anchor not advanced this run."})
        # Don't let the anchor regress: preserve the old anchor if this scan
        # produced no dates (e.g. stopped early). Never include prev_scan_time —
        # wall-clock timestamps inflate the anchor and block same-date new items.
        new_anchor = max(new_anchor, anchor_date or "")

        # Persist server-side for logged-in users (atomic with this scan completing).
        # Guests receive scan_anchor in the SSE done payload and roundtrip via localStorage.
        # NOTE: We persist last_anchor on baseline scans too — a baseline establishes
        # the starting point that future scans compare against.
        if user_id:
            try:
                _set_user_data(user_id, last_anchor=new_anchor, last_run=run_time)
            except Exception:
                pass  # Non-fatal — client will also sync via /api/sync after done

        # For large scans, don't send full item lists via SSE — client will use server-side browse
        large_scan = len(all_products) > 1000
        items_for_sse = [] if large_scan else [fmt(p) for p in all_products[:500]]
        _sweep = None
        _scanned = len(all_products)
        if mode == "quick":
            # v2.17.4: the table always comes from server browse after a quick pass
            # (it only fetched recent listings), and "N Items" shows the catalog size
            # as a full scan would. Then the sold / price sweep runs in the background.
            large_scan, items_for_sse = True, []
            _scanned = _pg_available_count() or len(all_products)
            _sweep = "skipped (stopped)" if stop_ev.is_set() else _start_sweep()
        # Phase G timing (v2.17.1): what the user waited on, start → "done".
        _t_done = time.time()
        _timing_note_scan(kind=("quick" if mode == "quick" else "nationwide") if nationwide else f"{len(stores_to_scan)} store(s)",
                          found=len(all_products), stopped=stop_ev.is_set(),
                          total_ms=int((_t_done - _t_scan0) * 1000),
                          fetch_ms=int((_t_db - _t_scan0) * 1000), save_ms=_db_ms,
                          finish_ms=int((_t_done - _t_db_end) * 1000))
        print(f"[timing] scan done: {('quick' if mode == 'quick' else 'nationwide') if nationwide else f'{len(stores_to_scan)} store(s)'}, "
              f"{len(all_products):,} found, total {int((_t_done - _t_scan0) * 1000)}ms "
              f"(fetch {int((_t_db - _t_scan0) * 1000)}ms, save {_db_ms}ms, "
              f"finish {int((_t_done - _t_db_end) * 1000)}ms)")
        send({
            "type":        "done",
            "baseline":    baseline,
            "stopped":     stop_ev.is_set(),
            "scanned":     _scanned,
            "new_ids":     new_ids_list,
            "sweep":       _sweep,
            "scan_time":   run_time,
            "late_since":  "" if baseline else (device_last_run or ""),   # v2.20.0: /api/new-catchup window start
            "new_older":   _new_older,   # v2.22.3: NEW listings listed before the previous scan
            "scan_anchor": new_anchor,
            "items":       items_for_sse,
            "use_browse":  large_scan,
        })
    except Exception as e:
        if mode == "sweep":
            print(f"[sweep] failed: {type(e).__name__}: {e}")
            with _SWEEP_STATE_LOCK:
                _SWEEP_STATE["result"] = {"complete": False, "error": f"{type(e).__name__}: {e}"[:200]}
        send({"type":"done","error":str(e),"scanned":0,"new_count":0,"new_items":[]})
    finally:
        if mode == "sweep":
            return            # the sweep never holds _lock (see _start_sweep)
        # Guard against the /api/stop 5s force-unlock watchdog already having
        # released this lock if this thread's winddown ran long (RuntimeError:
        # release unlocked lock) — same pattern as admin_clear_lock/_force_unlock.
        # Harmless either way: the lock ends up unlocked regardless. (v2.16.27)
        try:
            _lock.release()
        except RuntimeError:
            pass


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta name='impact-site-verification' value='f9ecacb7-3abe-44ce-947f-4de4d768f015' />
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guitar Center Used Gear Tracker — Browse Inventory by Store Location</title>
<meta name="description" content="Browse used gear at any Guitar Center location. Search guitars, amps, pedals, drums, and more by store, city, condition, and price — updated in real time. Free watch list and want list.">
<link rel="canonical" href="https://gcgeartracker.com/">
<meta property="og:type" content="website">
<meta property="og:url" content="https://gcgeartracker.com/">
<meta property="og:site_name" content="GC Used Inventory Tracker">
<meta property="og:title" content="Guitar Center Used Gear Tracker — Browse Inventory by Store Location">
<meta property="og:description" content="Browse used gear at any Guitar Center location. Filter by store, city, condition, and price across 300+ stores nationwide. Free watch list and want list.">
<meta property="og:image" content="https://gcgeartracker.com/static/og-image.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Guitar Center Used Gear Tracker — Browse Inventory by Store Location">
<meta name="twitter:description" content="Browse used gear at any Guitar Center location. Filter by store, city, condition, and price. Free watch list and want list alerts.">
<meta name="twitter:image" content="https://gcgeartracker.com/static/og-image.png">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebSite","name":"GC Used Inventory Tracker","url":"https://gcgeartracker.com/","description":"Browse used gear at any Guitar Center location across 300+ stores nationwide.","potentialAction":{"@type":"SearchAction","target":{"@type":"EntryPoint","urlTemplate":"https://gcgeartracker.com/?q={search_term_string}"},"query-input":"required name=search_term_string"}}
</script>
<link rel="stylesheet" href="/static/gc.css">
<!-- __GA__ -->
</head>
<body>

<!-- Image thumbnail tooltip -->
<div id="img-tooltip"><img src="" alt=""></div>

<!-- Password modal -->
<!-- Validate stores modal -->
<div id="vs-modal" style="display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.7)" id="vs-backdrop"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;padding:30px 28px;width:360px;z-index:1">
    <h2 style="color:#fff;font-size:1.05rem;margin-bottom:8px">✓ Validate Stores</h2>
    <p style="color:#777;font-size:.82rem;margin-bottom:18px;line-height:1.6">Clear the invalid-stores blocklist before validating?<br><br>
    <b style="color:#ccc">Yes (recommended)</b> — re-checks all stores including any previously removed ones.<br><br>
    <b style="color:#ccc">No</b> — only checks stores currently in your list.</p>
    <div style="display:flex;gap:8px">
      <button id="vs-cancel-btn" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:1px solid #3a3a3a;background:#2a2a2a;color:#aaa">Cancel</button>
      <button id="vs-no-btn" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;background:#444;color:#eee">No</button>
      <button id="vs-yes-btn" style="flex:1;padding:9px;border-radius:5px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;background:#c00;color:#fff">Yes</button>
    </div>
  </div>
</div>


<!-- ── Welcome / auth modal (shown on first visit when not logged in) ── -->
<div id="first-run-modal" style="display:none;position:fixed;inset:0;z-index:200;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.75)" id="first-run-backdrop"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #2e2e2e;border-radius:12px;padding:32px 36px;width:360px;max-width:92vw;z-index:1">
    <h2 style="color:#fff;font-size:1.15rem;margin-bottom:6px">Welcome to GC Used Inventory Tracker</h2>
    <p style="color:#aaa;font-size:.82rem;margin-bottom:20px;line-height:1.5">Track Guitar Center used inventory. Create an account to save your watch list, want list, and favorites across all your devices.</p>
    <!-- Auth tabs -->
    <div style="display:flex;border-bottom:1px solid #2e2e2e;margin-bottom:20px">
      <button id="welcome-tab-login" style="flex:1;padding:8px;background:none;border:none;border-bottom:2px solid #c00;color:#ff5555;font-size:.85rem;font-weight:600;cursor:pointer;margin-bottom:-1px">Sign In</button>
      <button id="welcome-tab-register" style="flex:1;padding:8px;background:none;border:none;border-bottom:2px solid transparent;color:#666;font-size:.85rem;font-weight:600;cursor:pointer;margin-bottom:-1px">Create Account</button>
    </div>
    <!-- Login form -->
    <div id="welcome-form-login">
      <div id="welcome-google-wrap" style="display:none">
        <button class="auth-google-btn">
          <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.566 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          Sign in with Google
        </button>
        <div class="auth-divider"><span>or sign in with username</span></div>
      </div>
      <input class="auth-field" type="text" id="welcome-login-user" placeholder="Username" autocomplete="username">
      <input class="auth-field" type="password" id="welcome-login-pw" placeholder="Password" autocomplete="current-password">
      <div class="auth-err" id="welcome-login-err"></div>
      <button class="auth-submit" id="welcome-login-submit">Sign In</button>
    </div>
    <!-- Register form -->
    <div id="welcome-form-register" style="display:none">
      <div id="welcome-google-wrap-reg" style="display:none">
        <button class="auth-google-btn">
          <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.566 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          Continue with Google
        </button>
        <div class="auth-divider"><span>or create a username account</span></div>
      </div>
      <input class="auth-field" type="text" id="welcome-reg-user" placeholder="Choose a username" autocomplete="username" maxlength="30">
      <input class="auth-field" type="password" id="welcome-reg-pw" placeholder="Password (8+ characters)" autocomplete="new-password">
      <input class="auth-field" type="password" id="welcome-reg-pw2" placeholder="Confirm password" autocomplete="new-password">
      <input class="auth-field" type="email" id="welcome-reg-email" placeholder="Email (optional)" autocomplete="email" style="margin-bottom:4px">
      <div style="color:#aaa;font-size:.72rem;margin-bottom:12px;line-height:1.4">Optional — helps identify your account if you ever need support. Never shared or used for marketing.</div>
      <div class="auth-err" id="welcome-reg-err"></div>
      <button class="auth-submit" id="welcome-register-submit">Create Account &amp; Start Scanning</button>
    </div>
    <!-- Guest option -->
    <div style="text-align:center;margin-top:16px">
      <button id="first-run-guest-btn" style="background:none;border:none;color:#aaa;font-size:.78rem;cursor:pointer;text-decoration:underline">Use as guest</button>
    </div>
  </div>
</div>

<div id="kw-modal" style="display:none;position:fixed;inset:0;z-index:100;align-items:center;justify-content:center">
  <div style="position:absolute;inset:0;background:rgba(0,0,0,.7)" id="kw-modal-backdrop"></div>
  <div style="position:relative;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:10px;width:420px;max-width:calc(100vw - 32px);max-height:80vh;display:flex;flex-direction:column;overflow:hidden;z-index:1">
    <!-- pinned header: title, instructions, add input -->
    <div style="padding:16px 20px 0;flex-shrink:0">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;position:relative;margin-bottom:4px">
        <h2 style="color:#fff;font-size:1.05rem;margin:0">🎯 Want List</h2>
        <button id="kw-info-btn" title="Keyword syntax help" style="background:none;border:1.5px solid #4ade80;border-radius:5px;color:#4ade80;font-weight:700;font-size:1rem;cursor:pointer;padding:4px 11px;line-height:1.4;flex-shrink:0">ⓘ</button>
        <div id="kw-info-popover">
          <b>Keyword syntax</b><br>
          <code>Allen</code> — whole word (not Allentown or McAllen)<br>
          <code>"Jam Pedals"</code> — exact phrase<br>
          <code>Thorpy, Dane</code> — comma = AND (both required)<br>
          <code>OD*</code> — wildcard, end of word only (OD808, OD-1…)<br>
          <code>Mesa, -combo</code> — minus = NOT (also <code>-"combo amp"</code>)<br>
          <code>Mesa, Mark*; Heartbreaker</code> — semicolon = OR (either side)<br>
          <code>Mesa, -combo: Angel; Blues; Trem</code> — colon = apply the prefix to every OR branch
        </div>
      </div>
      <p style="color:#aaa;font-size:.82rem;margin-bottom:12px;line-height:1.45">
        Highlights matches across all results. New matches sort to top after a scan.
      </p>
      <div style="display:flex;gap:6px;margin-bottom:12px">
        <input id="kw-input" type="text" placeholder="Add an item to your want list…"
               style="flex:1;padding:8px 12px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#eee;font-size:.9rem;outline:none"
               >
        <button id="kw-add-btn" style="padding:8px 16px;background:#0a5c2a;border:1px solid #2d6a2d;border-radius:5px;color:#4ade80;font-size:.85rem;cursor:pointer;white-space:nowrap">+ Add</button>
      </div>
    </div>
    <!-- scrollable keyword chips -->
    <div id="kw-list" style="overflow-y:auto;flex:1;min-height:0;padding:0 24px 16px"></div>
    <!-- pinned footer: always visible -->
    <div style="display:flex;gap:10px;justify-content:space-between;border-top:1px solid #2e2e2e;padding:14px 24px 20px;flex-shrink:0">
      <button id="kw-clear-btn" style="padding:6px 14px;background:#1a1a1a;border:1px solid #5a2a2a;border-radius:5px;color:#a05050;font-size:.78rem;cursor:pointer">Clear Want List</button>
      <button id="kw-alerts-btn" class="alerts-open-btn" style="display:none">✉ Email alerts</button>
      <button id="kw-done-btn" style="padding:6px 18px;background:#252525;border:1px solid #3a3a3a;border-radius:5px;color:#aaa;font-size:.85rem;cursor:pointer">Done</button>
    </div>
  </div>
</div>

<header>
  <h1>GC Used Inventory Tracker <span style="font-size:.65rem;font-weight:400;opacity:.6"><!-- __VER__ --></span></h1>
  <button id="stop-btn">⏹ Stop Running</button>
  <span id="hdr-status">Loading…</span>
  <div id="auth-widget">
    <span id="auth-sync-dot" title="Synced to account"></span>
    <div id="auth-user-info">
      <span id="auth-email"></span>
      <button id="alerts-open-btn" class="alerts-open-btn" style="display:none">✉ Email alerts</button>
      <button id="auth-logout-btn">Sign out</button>
    </div>
    <button id="auth-login-btn">Sign in</button>
  </div>
</header>

<!-- ── Auth modal ── -->
<div id="auth-modal">
  <div class="auth-box">
    <button class="auth-close">✕</button>
    <div class="auth-tabs">
      <button class="auth-tab active" id="auth-tab-login">Sign In</button>
      <button class="auth-tab" id="auth-tab-register">Create Account</button>
    </div>
    <!-- Login form -->
    <div id="auth-form-login">
      <div id="auth-google-wrap" style="display:none">
        <button class="auth-google-btn">
          <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.566 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          Sign in with Google
        </button>
        <div class="auth-divider"><span>or sign in with username</span></div>
      </div>
      <input class="auth-field" type="text" id="auth-login-user" placeholder="Username" autocomplete="username">
      <input class="auth-field" type="password" id="auth-login-pw" placeholder="Password" autocomplete="current-password">
      <div class="auth-err" id="auth-login-err"></div>
      <button class="auth-submit" id="auth-login-submit">Sign In</button>
      <div class="auth-note">Your watch list, want list &amp; favorites sync across all your devices.</div>
    </div>
    <!-- Register form -->
    <div id="auth-form-register" style="display:none">
      <div id="auth-google-wrap-reg" style="display:none">
        <button class="auth-google-btn">
          <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.566 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"/></svg>
          Continue with Google
        </button>
        <div class="auth-divider"><span>or create a username account</span></div>
      </div>
      <input class="auth-field" type="text" id="auth-reg-username" placeholder="Choose a username" autocomplete="username" maxlength="30">
      <input class="auth-field" type="password" id="auth-reg-pw" placeholder="Password (8+ characters)" autocomplete="new-password">
      <input class="auth-field" type="password" id="auth-reg-pw2" placeholder="Confirm password" autocomplete="new-password">
      <input class="auth-field" type="email" id="auth-reg-email" placeholder="Email (optional)" autocomplete="email" style="margin-bottom:4px">
      <div class="auth-note" style="margin-bottom:12px;margin-top:0;color:#aaa">Optional — helps identify your account if you ever need support. Never shared or used for marketing.</div>
      <div class="auth-err" id="auth-reg-err"></div>
      <button class="auth-submit" id="auth-register-submit">Create Account</button>
    </div>
  </div>
</div>

<!-- ── Google new-user welcome modal ── -->
<div id="google-welcome-modal">
  <div class="gw-backdrop"></div>
  <div class="gw-box">
    <h2>Welcome to GC Tracker! 👋</h2>
    <p>You're signed in with Google. Choose a username for your account — it's how you'll appear and sign in if you ever use a password instead.</p>
    <label class="gw-label" for="gw-username">Username</label>
    <input class="auth-field" type="text" id="gw-username" placeholder="Choose a username" autocomplete="username" maxlength="30">
    <div class="gw-msg" id="gw-msg"></div>
    <hr class="gw-divider">
    <div style="color:#aaa;font-size:.82rem;margin-bottom:10px">Already have a GC Tracker account? Import your watch list, want list, and favorites.</div>
    <button class="gw-import-toggle" id="gw-import-toggle">+ Import existing account</button>
    <div class="gw-import-section" id="gw-import-section">
      <div style="color:#888;font-size:.78rem;margin-bottom:10px;line-height:1.5">Enter your existing username above and the password for that account. Your saved data will be moved over and the old account will be removed.</div>
      <label class="gw-label" for="gw-import-pw">Password for existing account</label>
      <input class="auth-field" type="password" id="gw-import-pw" placeholder="Password" autocomplete="current-password">
    </div>
    <button class="auth-submit" id="gw-submit" style="margin-top:6px">Save &amp; Continue</button>
    <div style="text-align:center;margin-top:12px">
      <button id="gw-skip-btn" style="background:none;border:none;color:#555;font-size:.78rem;cursor:pointer;text-decoration:underline">Skip for now</button>
    </div>
  </div>
</div>

<!-- ── Google link nudge banner (for existing password users) ── -->
<div id="google-link-banner">
  🔒 <span>Link Google Sign-In to your account for added security — password-only login will be retired in a future update.</span>
  <button class="glib-link">Link Google Account</button>
  <button class="glib-dismiss">✕ Dismiss</button>
</div>

</div>

<!-- ══ GC PANEL ══ -->
<div class="mobile-title-bar"><button class="mtb-about">About</button><span class="mtb-title">GC Used Inventory Tracker</span><span class="mtb-ver"><!-- __VER__ --></span></div>
<div class="layout">

  <div class="left" id="gc-left">
    <button id="sidebar-collapse-btn" title="Collapse store panel">«</button>
    <div class="sheet-handle"></div>
    <button class="mobile-sidebar-toggle" id="gc-sidebar-toggle">
      <span class="toggle-arrow" id="gc-toggle-arrow">▶</span>
      Stores
      <span class="toggle-count" id="gc-toggle-count"></span>
    </button>
    <div class="search-wrap" id="search-wrap">
      <input id="search" type="text" placeholder="Filter by location name…" autocomplete="off">
      <div class="sel-btns">
        <button class="sel-btn" id="favs-btn">★ Favorites</button>
        <button class="sel-btn" id="sel-all-btn">Select All</button>
      </div>
      <div class="zip-sort-row">
        <button id="zip-sort-btn" title="Sort stores by distance from ZIP">📍 ZIP Sort</button>
        <input id="zip-input" type="text" maxlength="5" placeholder="ZIP code…"
          autocomplete="postal-code" inputmode="numeric" enterkeyhint="go"
>
      </div>
      <div class="zip-radius-row" id="zip-radius-row" style="display:none">
        <label for="zip-radius-select">Within</label>
        <select id="zip-radius-select" title="Only show and search stores within this distance of your ZIP. Stores without a map location — the (?) rows — are excluded by any distance limit.">
          <option value="">Any distance</option>
          <option value="5">5 mi</option>
          <option value="10">10 mi</option>
          <option value="25">25 mi</option>
          <option value="50">50 mi</option>
          <option value="100">100 mi</option>
        </select>
      </div>
    </div>

    <div id="store-list"></div>

    <div class="left-footer">
      <div id="sel-count">0 stores selected</div>
    </div>
  </div>

  <div class="right">
    <div class="status-bar">
      <span id="s-last-wrap">Last checked for new gear: <b id="s-last">—</b> <button id="check-now-btn" style="padding:2px 10px;background:#c00;color:#fff;border:none;border-radius:4px;font-size:.72rem;font-weight:700;cursor:pointer;margin-left:4px;display:none">Scan for New Listings</button> <button id="view-toggle-btn" class="view-toggle-btn" title="Switch card / list view"><span id="view-toggle-icon">⊞</span></button></span>
      <span>Items: <b id="s-known">—</b></span>
      <span>Stores: <b id="s-stores">—</b></span>
      <!-- global-search moved into filter sheet -->
      <span id="s-want-match" style="display:none;color:#4caf50;font-weight:600;font-size:.82rem;cursor:pointer" title="Click to view want list matches"></span>
    </div>
    <div id="log"><span class="log-dim">Ready</span></div>
    <div class="results" id="res-panel" style="display:none">
      <!-- ── Persistent view-toggle chips (always visible, not in filter sheet) ── -->
      <div id="results-top-bar">
      <div class="quick-filter-bar">
        <button id="view-toggle-chip"       class="qf-chip view-toggle-chip-btn" title="Switch list / card view">☰</button>
        <button id="desktop-thumb-toggle" class="qf-chip" title="Show thumbnail grid view">⊞</button>
        <button id="new-toggle" class="qf-chip" title="Show only listings tagged NEW — new on Guitar Center's site since your last scan">😮 Newly Listed</button>
        <button id="price-drop-toggle" class="qf-chip">↓ Price Drops</button>
        <button id="vintage-toggle" class="qf-chip" title="Show only genuine vintage gear (GC's own classification)">🎸 Vintage</button>
        <div id="ss-wrap" style="display:none;position:relative">
          <button id="saved-searches-btn" class="qf-chip" title="Your saved filter combinations">🔖 Saved Searches</button>
        </div>
        <button id="watchlist-toggle"      class="qf-chip">★ Watch List</button>
        <button id="want-list-toggle"         class="qf-chip">🎯 Want List</button>
        <a id="search-wl-link" class="qf-edit-link" style="display:none;font-size:.75rem">✏︎ Edit Want List</a>
      </div>
      <div class="results-hdr">
        <span id="res-title" style="display:none"></span>
        <span class="badge" id="res-badge" style="display:none!important"></span>
        <button class="mobile-filter-toggle" id="gc-filter-toggle">
          <span class="toggle-arrow" id="gc-filter-arrow">▶</span> Filters
          <span class="filter-active-dot" id="gc-filter-dot"></span>
        </button>
        <div id="gc-filter-collapsible" class="filter-collapsible">
          <!-- ── Mobile sheet header (hidden on desktop) ── -->
          <div class="filter-sheet-header">
            <div class="filter-sheet-handle"></div>
            <div class="filter-sheet-hdr-row">
              <span class="filter-sheet-title">Filters</span>
              <button class="filter-clear-all-btn" id="filter-clear-all-btn">Clear All</button>
            </div>
          </div>
          <!-- ── Scrollable filter content ── -->
          <div class="filter-scroll-body">
            <span id="filter-item-count" style="color:#888;font-size:.78rem;white-space:nowrap;margin-right:6px"></span>
            <!-- ── Mobile sort row (hidden on desktop) ── -->
            <div class="mobile-sort-row" id="mobile-sort-row">
              <span class="mobile-sort-label">Sort:</span>
              <button class="mobile-sort-btn active" data-sort-field="date" data-sort-dir="desc">Newest</button>
              <button class="mobile-sort-btn" data-sort-field="date" data-sort-dir="asc">Oldest</button>
              <button class="mobile-sort-btn" data-sort-field="price" data-sort-dir="asc">Price ↑</button>
              <button class="mobile-sort-btn" data-sort-field="price" data-sort-dir="desc">Price ↓</button>
            </div>
            <!-- Keyword search — at top of sheet, searches all stores globally -->
            <div id="res-search-wrap">
              <span class="res-search-icon">🔍</span>
              <input id="res-search" type="text" placeholder="Search all stores…" autocomplete="off">
              <button id="res-search-clear" title="Clear search" style="display:none;background:none;border:none;color:#888;font-size:.85rem;cursor:pointer;padding:0 4px;line-height:1">✕</button>
              <button id="search-info-btn" title="Search syntax help" style="background:none;border:1px solid #3a3a3a;border-radius:4px;color:#555;font-size:.78rem;cursor:pointer;padding:2px 6px;line-height:1.4;flex-shrink:0">ⓘ</button>
              <div id="search-info-popover">
                <b>Search syntax</b><br>
                <code>Allen</code> — exact word match<br>
                <code>"Jam Pedals"</code> — phrase match<br>
                <code>Thorpy, Dane</code> — must contain both<br>
                <code>OD*</code> — wildcard, end of word only (OD808, OD-1…)<br>
                <code>-combo</code> — NOT (exclude; also <code>-"combo amp"</code>)<br>
                <code>fuzz; octave</code> — OR (either matches)<br>
                <code>Mesa -combo: Angel; Trem</code> — colon = apply prefix to every OR branch
              </div>
              <span id="res-search-count"></span>
            </div>
            <!-- ── Price range (mobile: always visible; desktop: see #price-dropdown below) ── -->
            <div class="price-range-mobile">
              <span class="price-range-label">Price</span>
              <div class="price-inputs-row">
                <span class="price-sym">$</span>
                <input id="price-min" type="number" min="0" step="0.01" placeholder="Min"
                  class="price-inp" autocomplete="off" inputmode="decimal">
                <span class="price-sep">–</span>
                <span class="price-sym">$</span>
                <input id="price-max" type="number" min="0" step="0.01" placeholder="Max"
                  class="price-inp" autocomplete="off" inputmode="decimal">
              </div>
            </div>
            <!-- ── Mobile accordion sections (hidden on desktop) ── -->
            <div class="filter-accordion" id="acc-brand">
              <button class="acc-header" data-acc="brand">
                <span class="acc-title">Brand</span>
                <span class="acc-summary" id="acc-brand-summary"></span>
                <span class="acc-arrow" id="acc-brand-arrow">▾</span>
              </button>
              <div class="acc-body" id="acc-brand-body">
                <div class="acc-search-wrap">
                  <input id="acc-brand-search" type="text" placeholder="Search brands…" autocomplete="off">
                </div>
                <div class="acc-list" id="acc-brand-list"></div>
              </div>
            </div>
            <div class="filter-accordion" id="acc-cond">
              <button class="acc-header" data-acc="cond">
                <span class="acc-title">Condition</span>
                <span class="acc-summary" id="acc-cond-summary"></span>
                <span class="acc-arrow" id="acc-cond-arrow">▾</span>
              </button>
              <div class="acc-body" id="acc-cond-body">
                <div class="acc-list" id="acc-cond-list"></div>
              </div>
            </div>
            <div class="filter-accordion" id="acc-cat" style="display:none">
              <button class="acc-header" data-acc="cat">
                <span class="acc-title">Category</span>
                <span class="acc-summary" id="acc-cat-summary"></span>
                <span class="acc-arrow" id="acc-cat-arrow">▾</span>
              </button>
              <div class="acc-body" id="acc-cat-body">
                <div class="acc-list" id="acc-cat-list"></div>
              </div>
            </div>
            <div class="filter-accordion" id="acc-sub" style="display:none">
              <button class="acc-header" data-acc="sub">
                <span class="acc-title">Subcategory</span>
                <span class="acc-summary" id="acc-sub-summary"></span>
                <span class="acc-arrow" id="acc-sub-arrow">▾</span>
              </button>
              <div class="acc-body" id="acc-sub-body">
                <div class="acc-list" id="acc-sub-list"></div>
              </div>
            </div>
            <!-- ── Desktop dropdown filters (hidden on mobile) ── -->
            <div id="brand-dropdown" class="brand-dd" style="display:none;position:relative">
              <button id="brand-dd-btn" class="cat-sel" style="cursor:pointer;white-space:nowrap">All Brands ▾</button>
              <div id="brand-dd-panel" style="display:none;position:fixed;z-index:500;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;width:260px;max-height:320px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)">
                <div style="padding:6px">
                  <input id="brand-dd-search" type="text" placeholder="Search brands…"
                    style="width:100%;padding:6px 10px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#eee;font-size:.82rem;outline:none;box-sizing:border-box"
                    autocomplete="off">
                </div>
                <div id="brand-dd-list" style="overflow-y:auto;max-height:260px"></div>
              </div>
            </div>
            <div id="cond-dropdown" class="cond-dd" style="display:none;position:relative">
              <button id="cond-dd-btn" class="cat-sel" style="cursor:pointer;white-space:nowrap">All Conditions ▾</button>
              <div id="cond-dd-panel" style="display:none;position:fixed;z-index:500;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;width:220px;max-height:300px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)">
                <div style="overflow-y:auto;max-height:260px;padding:4px 0" id="cond-dd-inner"></div>
              </div>
            </div>
            <div id="cat-dropdown" class="cond-dd" style="display:none;position:relative">
              <button id="cat-dd-btn" class="cat-sel" style="cursor:pointer;white-space:nowrap">All Categories ▾</button>
              <div id="cat-dd-panel" style="display:none;position:fixed;z-index:500;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;width:240px;max-height:300px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)">
                <div style="overflow-y:auto;max-height:260px;padding:4px 0" id="cat-dd-inner"></div>
              </div>
            </div>
            <div id="subcat-dropdown" class="cond-dd" style="display:none;position:relative">
              <button id="subcat-dd-btn" class="cat-sel" style="cursor:pointer;white-space:nowrap">All Subcategories ▾</button>
              <div id="subcat-dd-panel" style="display:none;position:fixed;z-index:500;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;width:240px;max-height:300px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5)">
                <div style="overflow-y:auto;max-height:260px;padding:4px 0" id="subcat-dd-inner"></div>
              </div>
            </div>
            <!-- ── Price range — desktop dropdown (hidden on mobile via CSS) ── -->
            <div id="price-dropdown" style="display:none;position:relative">
              <button id="price-dd-btn" class="cat-sel" style="cursor:pointer;white-space:nowrap">Price ▾</button>
              <div id="price-dd-panel" style="display:none;position:fixed;z-index:500;background:#1a1a1a;border:1px solid #3a3a3a;border-radius:6px;padding:14px 14px 10px;width:236px;box-shadow:0 8px 24px rgba(0,0,0,.5)">
                <div style="font-size:.72rem;color:#aaa;margin-bottom:9px;text-transform:uppercase;letter-spacing:.05em">Price Range</div>
                <div style="display:flex;align-items:center;gap:6px">
                  <span style="color:#bbb;font-size:.82rem">$</span>
                  <input id="price-min-dd" type="number" min="0" step="0.01" placeholder="Min"
                    style="width:78px;padding:6px 8px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#eee;font-size:.85rem;outline:none;box-sizing:border-box"
                    autocomplete="off" inputmode="decimal">
                  <span style="color:#999;font-size:.85rem">–</span>
                  <span style="color:#bbb;font-size:.82rem">$</span>
                  <input id="price-max-dd" type="number" min="0" step="0.01" placeholder="Max"
                    style="width:78px;padding:6px 8px;background:#252525;border:1px solid #3a3a3a;border-radius:4px;color:#eee;font-size:.85rem;outline:none;box-sizing:border-box"
                    autocomplete="off" inputmode="decimal">
                </div>
                <button id="price-dd-clear" style="display:none;margin-top:10px;background:none;border:none;color:#f88;font-size:.78rem;cursor:pointer;padding:0;line-height:1.4">✕ Clear price filter</button>
              </div>
            </div>
            <!-- Action buttons row (side-by-side on mobile, inline on desktop) -->
            <div id="filter-action-btns" style="display:none;gap:8px">
              <button id="save-search-btn" title="Save current search + filters"
                style="padding:7px 10px;border-radius:4px;background:#1e2e1e;border:1px solid #4ade80;color:#4ade80;font-size:.78rem;cursor:pointer;white-space:nowrap">
                💾 Save Search
              </button>
              <button id="clear-filters-btn"
                style="padding:7px 10px;border-radius:4px;background:#1e1e1e;border:1px solid #c00;color:#f88;font-size:.78rem;cursor:pointer;white-space:nowrap">
                ✕ Clear All
              </button>
            </div>
          </div>
          <!-- ── Pinned Show Results (mobile only) ── -->
          <button class="filter-done-btn">Show Results</button>
        </div>
      </div>
      </div><!-- /results-top-bar -->
      <!-- ss-dropdown lives here (outside overflow-x:auto chip bar) so position:fixed works on iOS -->
      <div id="ss-dropdown" class="ss-dropdown"></div>
      <!-- Shared condition_note tooltip — one node reused for every row, positioned via JS
           (position:fixed escapes the table cell's overflow:hidden, same reason ss-dropdown lives here) -->
      <div id="cond-tooltip" class="cond-tooltip"></div>
      <div id="res-body"></div>
    </div>
  </div>

</div>

<!-- ══ CL PANEL (moved to /cl route) ══ -->
<!-- placeholder: cl-left, cl-sidebar-toggle etc kept for JS refs -->
<div id="cl-panel" style="display:none">
  <div class="cl-left" id="cl-left">
    <button class="mobile-sidebar-toggle" id="cl-sidebar-toggle" style="display:none">
      <span class="toggle-arrow" id="cl-toggle-arrow"></span>
      Cities
      <span class="toggle-count" id="cl-toggle-count"></span>
    </button>
    <div class="search-wrap cl-left">
      <input id="cl-city-search" type="text" placeholder="Search cities…" autocomplete="off">
      <div class="cl-sel-btns">
        <button class="cl-sel-btn" id="cl-favs-btn">★ Favorites</button>
        <button class="cl-sel-btn" id="cl-select-all-btn">Select All</button>
        <button class="cl-sel-btn" id="cl-clear-all-btn">Clear All</button>
      </div>
    </div>
    <div id="cl-city-list"></div>
  </div>

  <!-- Right content: search bar + results -->
  <div class="cl-right">
    <div class="cl-search-bar">
      <input id="cl-query" type="text" placeholder="e.g. telecaster, les paul, fender twin…" autocomplete="off"
>
      <span id="cl-status"></span>
      <button id="cl-search-btn">Search</button>
    </div>
    <div class="cl-results-hdr" id="cl-toolbar" style="display:flex;align-items:center;gap:8px">
      <button id="cl-watchlist-toggle"
        class="cat-sel" style="border-color:#3a3a3a;color:#aaa;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
        ★ Watch List
      </button>
      <button id="cl-stub-open-kw-btn"
        class="cat-sel" style="border-color:#2d6a2d;color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;padding:5px 10px">
        🎯 Want List
      </button>
      <a id="cl-search-wl-link" style="color:#4ade80;cursor:pointer;white-space:nowrap;font-size:.78rem;text-decoration:none;margin-left:2px">Search Want List</a>
    </div>
    <div class="cl-results-hdr" id="cl-results-hdr" style="display:none">
      <span id="cl-count"></span>
      <input id="cl-res-search" type="text" placeholder="Filter results…" autocomplete="off">
    </div>
    <div id="cl-body"><div class="cl-empty">Select cities on the left, enter a search term, and click Search.</div></div>
  </div>

</div>

<!-- ── Store sheet backdrop (mobile only) ── -->
<div class="store-sheet-backdrop" id="store-sheet-backdrop"></div>

<!-- ── Mobile bottom action bar (hidden on desktop via CSS) ── -->
<div class="mobile-bottom-bar" id="mobile-bottom-bar">
  <button class="mbb-btn mbb-check" id="mbb-check">
    <span class="mbb-icon" id="mbb-check-icon">▶</span>
    <span class="mbb-label" id="mbb-check-label">Scan For New</span>
  </button>
  <button class="mbb-btn" id="mbb-filters">
    <span class="mbb-icon">🔍</span>
    <span class="mbb-label">Filter & Sort</span>
    <span class="mbb-dot" id="mbb-filter-dot"></span>
  </button>
  <button class="mbb-btn" id="mbb-stores">
    <span class="mbb-icon">🏪</span>
    <span class="mbb-label">Stores</span>
  </button>
  <button class="mbb-btn" id="mbb-auth">
    <span class="mbb-icon" id="mbb-auth-icon">👤</span>
    <span class="mbb-label" id="mbb-auth-label">Sign In</span>
  </button>
</div>

<script src="/static/gc.js"></script>

<div id="dev-footer">
  <span>Buy the developer a pack of strings</span>
  <a href="https://paypal.me/smurfco" target="_blank" rel="noopener" title="PayPal">
    <svg width="18" height="18" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M19.5 8.5c.3-2-1.2-3.5-3.5-3.5H9.5L7 20h3l.7-4.5h2.3c3.5 0 6-2 6.5-5.5l.5-1.5z" fill="#009cde"/>
      <path d="M16 10.5c.2-1.5-.8-2.5-2.5-2.5H9l-1.5 9h2.5l.5-3h2c2.5 0 4-1.5 4.3-3.5l.2-.5z" fill="#003087"/>
    </svg>
  </a>
  <a href="https://account.venmo.com/u/charles-boehmig" target="_blank" rel="noopener" title="Venmo">
    <svg width="18" height="18" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <rect width="24" height="24" rx="4" fill="#3D95CE"/>
      <path d="M17 5.5c.5 1 .7 2 .7 3.3 0 4-3.4 9.2-6.2 12.7H7.3L5 6.3l4-.4 1.3 10.2C11.6 14 13 10.8 13 8.3c0-1.3-.2-2.3-.6-3L17 5.5z" fill="#fff"/>
    </svg>
  </a>
  <span style="margin-left:4px">·</span>
  <a href="https://animalsintrees.com" target="_blank" rel="noopener" title="Animals in Trees" style="gap:5px">
    My music
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M9 18V5l12-2v13" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="6" cy="18" r="3" stroke="#aaa" stroke-width="1.5"/>
      <circle cx="18" cy="16" r="3" stroke="#aaa" stroke-width="1.5"/>
    </svg>
  </a>
  <span style="margin-left:4px">·</span>
  <a href="#" data-action="open-about">About</a>
  <span style="margin-left:4px">·</span>
  <a href="/privacy">Privacy Policy</a>
  <span id="admin-footer-sep" style="display:none;margin-left:4px">·</span>
  <a id="admin-footer-link" href="/admin/users" style="display:none;margin-left:0;color:#888;font-size:11px">Admin</a>
</div>

<!-- ── About modal ── -->
<!-- ── Your account / delete account (v2.22.1; JS in static/gc.js) ── -->
<div id="acct-modal">
  <div id="acct-box">
    <button id="acct-close-x" class="alerts-x" aria-label="Close">✕</button>
    <div id="acct-info" class="al-state">
      <h3>Your account</h3>
      <div class="acct-row"><span>Username</span><b id="acct-username"></b></div>
      <div class="acct-row"><span>Sign-in</span><span id="acct-signin"></span></div>
      <button id="acct-delete-link" class="acct-delete-link">Delete my account…</button>
    </div>
    <div id="acct-confirm" class="al-state">
      <h3>Delete your account?</h3>
      <p>This permanently deletes, right away:</p>
      <ul class="acct-list">
        <li>Your account and sign-in</li>
        <li>Your Watch List, Want List, favorite stores and saved searches</li>
        <li>Your email-alert address and alert history</li>
      </ul>
      <div class="acct-warn">This can't be undone. Lists saved on this device are cleared too.</div>
      <div id="acct-pw-wrap">
        <label for="acct-pw">Enter your password to confirm</label>
        <input id="acct-pw" type="password" autocomplete="current-password" maxlength="200">
        <button id="acct-delete-pw-btn" class="acct-danger">Delete my account permanently</button>
      </div>
      <div id="acct-google-wrap">
        <p id="acct-google-text">To confirm it's you, sign in with Google again.</p>
        <button id="acct-google-btn" class="acct-google">Confirm with Google</button>
      </div>
      <button id="acct-cancel-btn" class="al-secondary acct-full">Cancel</button>
    </div>
    <div id="acct-last" class="al-state">
      <h3>Last step</h3>
      <p>Google confirmed it's you. Delete <b id="acct-last-name"></b> and everything in it?</p>
      <button id="acct-delete-final-btn" class="acct-danger">Delete my account permanently</button>
      <button id="acct-keep-btn" class="al-secondary acct-full">Cancel — keep my account</button>
    </div>
    <div id="acct-done" class="al-state">
      <div class="acct-done-msg">Your account has been deleted. Thanks for using GC Gear Tracker.</div>
      <button id="acct-done-btn" class="about-close-btn">Close</button>
    </div>
    <p id="acct-msg" class="al-msg"></p>
  </div>
</div>

<!-- ── Email alerts panel (v2.22.0, beta accounts + admin; JS in static/gc.js) ── -->
<div id="alerts-modal">
  <div id="alerts-box">
    <button id="alerts-close-x" class="alerts-x" aria-label="Close">✕</button>
    <h3>Email alerts</h3>
    <p class="alerts-intro">Once a day, around 10 AM Eastern, we'll email you new listings that match your
      Want List, plus price drops on Want List matches and on your Watch List. Nothing new that day means no email.</p>
    <div id="al-setup" class="al-state">
      <label for="al-email-input">Email address</label>
      <input id="al-email-input" type="email" autocomplete="email" placeholder="you@example.com" maxlength="254">
      <button id="al-send-code-btn" class="al-primary">Send code</button>
      <p class="alerts-fine">Your address is stored encrypted and used only for these alerts. It's never sold or
        used for marketing. You can pause or remove it any time.</p>
    </div>
    <div id="al-code" class="al-state">
      <p>Enter the 6-digit code we sent to <b id="al-code-addr"></b>.</p>
      <input id="al-code-input" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="123456">
      <button id="al-confirm-btn" class="al-primary">Confirm</button>
      <button id="al-restart-btn" class="al-link">Use a different address</button>
    </div>
    <div id="al-on" class="al-state">
      <p>Alerts go to <b id="al-on-addr"></b></p>
      <p id="al-on-status" class="al-status"></p>
      <button id="al-pause-btn" class="al-primary"></button>
      <div class="al-row">
        <button id="al-test-btn" class="al-secondary">Send a test email</button>
        <button id="al-change-btn" class="al-secondary">Change address</button>
      </div>
      <button id="al-remove-btn" class="al-danger">Remove my email</button>
    </div>
    <p id="al-msg" class="al-msg"></p>
    <button id="alerts-close-btn" class="about-close-btn">Close</button>
  </div>
</div>

<div id="about-modal">
  <div id="about-box">
    <h3>GC Used Inventory Tracker</h3>
    <div class="about-sub">Developed by CKB</div>
    <p style="font-size:.82rem;color:#aaa;line-height:1.55;margin:12px 0 4px;text-align:center">A free tool for tracking Guitar Center's used instrument inventory. Scan for new listings, build a watch list, set up a want list, and save searches — all synced across your devices.</p>
    <p style="font-size:.75rem;color:#666;line-height:1.45;margin:0 0 10px;text-align:center;font-style:italic">Independent tool — not affiliated with or endorsed by Guitar Center, Inc.</p>
    <div class="about-donate-row">
      <span class="about-donate-label">Donate</span>
      <a href="https://paypal.me/smurfco" target="_blank" rel="noopener" title="PayPal">
        <svg width="22" height="22" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path d="M19.5 8.5c.3-2-1.2-3.5-3.5-3.5H9.5L7 20h3l.7-4.5h2.3c3.5 0 6-2 6.5-5.5l.5-1.5z" fill="#009cde"/>
          <path d="M16 10.5c.2-1.5-.8-2.5-2.5-2.5H9l-1.5 9h2.5l.5-3h2c2.5 0 4-1.5 4.3-3.5l.2-.5z" fill="#003087"/>
        </svg>
      </a>
      <a href="https://account.venmo.com/u/charles-boehmig" target="_blank" rel="noopener" title="Venmo">
        <svg width="22" height="22" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <rect width="24" height="24" rx="4" fill="#3D95CE"/>
          <path d="M17 5.5c.5 1 .7 2 .7 3.3 0 4-3.4 9.2-6.2 12.7H7.3L5 6.3l4-.4 1.3 10.2C11.6 14 13 10.8 13 8.3c0-1.3-.2-2.3-.6-3L17 5.5z" fill="#fff"/>
        </svg>
      </a>
    </div>
    <a href="https://animalsintrees.com" target="_blank" rel="noopener" class="about-music-link">
      My music
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M9 18V5l12-2v13" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="6" cy="18" r="3" stroke="#aaa" stroke-width="1.5"/>
        <circle cx="18" cy="16" r="3" stroke="#aaa" stroke-width="1.5"/>
      </svg>
    </a>
    <button id="about-account-btn" class="about-account-btn" style="display:none">Your account</button>
    <a href="/privacy" target="_blank" rel="noopener" style="display:block;margin-top:8px;font-size:.78rem;color:#666;text-align:center;text-decoration:none">Privacy Policy</a>
    <button class="about-close-btn">Close</button>
  </div>
</div>

<!-- __STORES_NOSCRIPT__ -->
<footer class="seo-footer">
  <a href="/privacy">Privacy Policy</a> &middot; Not affiliated with Guitar Center, Inc.
</footer>

</body>
</html>"""

PRIVACY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — GC Used Inventory Tracker</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#ccc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;line-height:1.7;padding:40px 20px 80px}
.wrap{max-width:700px;margin:0 auto}
a{color:#f88;text-decoration:none}
a:hover{text-decoration:underline}
h1{color:#fff;font-size:1.5rem;margin-bottom:6px}
.subtitle{color:#888;font-size:.85rem;margin-bottom:36px}
h2{color:#eee;font-size:1rem;font-weight:700;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid #2a2a2a}
p{margin-bottom:14px}
ul{margin:0 0 14px 20px}
ul li{margin-bottom:6px}
.back{display:inline-block;margin-bottom:28px;color:#888;font-size:.85rem}
.back:hover{color:#ccc}
footer{margin-top:48px;padding-top:16px;border-top:1px solid #222;color:#555;font-size:.8rem}
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="back">← Back to GC Used Inventory Tracker</a>
  <h1>Privacy Policy</h1>
  <p class="subtitle">Last updated: October 2026</p>

  <p>GC Used Inventory Tracker ("the site", "we", "us") is an independent personal project that
  helps musicians track used gear listings at Guitar Center. It is not affiliated with, sponsored
  by, or endorsed by Guitar Center, Inc. This policy explains what information we collect, how
  we use it, and your rights regarding that information.</p>

  <h2>Information We Collect</h2>

  <p><strong style="color:#eee">Account information.</strong> If you create an account, we store
  your chosen username, an optional email address, and a hashed (never plain-text) version of your
  password. If you sign in with Google, we store your Google account ID and the display name Google
  provides. Your account email address is never required and is used only for account recovery if
  you choose to provide it. It is never used for Want List email alerts, which use a separate
  address you confirm yourself (see below).</p>

  <p><strong style="color:#eee">Preferences and scan history.</strong> To sync your data across
  devices, we store your watch list, want list keywords, favorited stores, saved searches, and the
  timestamp and item IDs from your most recent scan. This data lives on our server and is tied to
  your account.</p>

  <p><strong style="color:#eee">Technical data.</strong> When you use the site, our server
  receives your IP address. We use it only for rate-limiting (to prevent abuse) and do not log or
  store it persistently. We also set an anonymous device ID cookie (<code style="color:#aaa;font-size:.85em">gt_device_id</code>)
  to count unique devices in aggregate — it contains no personal information.</p>

  <p><strong style="color:#eee">Analytics.</strong> We use Google Analytics 4 to understand how
  visitors use the site in aggregate (page views, session counts, general geography). Google
  Analytics may set its own cookies in your browser. You can opt out using the
  <a href="https://tools.google.com/dlpage/gaoptout" target="_blank" rel="noopener">Google Analytics
  Opt-out Browser Add-on</a>.</p>

  <h2>Want List Email Alerts</h2>
  <p>Want List email alerts are an optional feature for registered users (currently being rolled
  out to a small number of users). If you turn them on, we check once a day for new Guitar Center
  used listings that match your Want List searches, and for price drops on those matches and on
  items in your Watch List. If there is something new, you get one email that day listing it; if
  there is nothing new, we send nothing.</p>
  <ul>
    <li><strong style="color:#eee">Opt-in only.</strong> Alerts are off unless you turn them on.
    You enter the address you want alerts sent to and confirm it with a one-time code we email to
    you. We never add addresses ourselves, and we never use your account email for alerts.</li>
    <li><strong style="color:#eee">Used only for your alerts.</strong> Your alert address is used
    only to send you the alerts and confirmation codes you asked for. It is never sold, rented or
    shared, and never used for marketing, newsletters or promotions.</li>
    <li><strong style="color:#eee">Stored encrypted.</strong> Your alert address is encrypted on our
    server and is not shown in the site's admin pages or logs.</li>
    <li><strong style="color:#eee">No tracking.</strong> Alert emails do not use open tracking or
    click tracking.</li>
    <li><strong style="color:#eee">Easy to stop.</strong> Every alert email has an unsubscribe link.
    You can also turn alerts off, or use "Remove my email" to delete your alert address, at any
    time. Deleting your account deletes it too.</li>
  </ul>
  <p>Alert emails are delivered by Postmark (see Third-Party Services), which receives your alert
  address and the content of each email only in order to deliver it.</p>

  <h2>How We Use Your Information</h2>
  <ul>
    <li>To provide the core tracker functionality (scan results, watch list, want list)</li>
    <li>To sync your preferences across your own devices when you are logged in</li>
    <li>To send the Want List email alerts and confirmation codes you have opted in to</li>
    <li>To prevent abuse via rate limiting on scan and login endpoints</li>
    <li>To understand aggregate site usage through analytics</li>
  </ul>
  <p>We do not sell, rent, or share your personal information with third parties for their
  marketing purposes.</p>

  <h2>Cookies</h2>
  <ul>
    <li><strong style="color:#eee">Session cookie</strong> — keeps you logged in across browser
    sessions. Set by Flask, signed with a server secret, HttpOnly and Secure.</li>
    <li><strong style="color:#eee">gt_device_id</strong> — anonymous device identifier for
    internal usage counting. Contains no personal information.</li>
    <li><strong style="color:#eee">Google Analytics cookies</strong> (_ga, _gid, and related)
    — set by Google's analytics script to measure aggregate traffic.</li>
  </ul>

  <h2>Third-Party Services</h2>
  <ul>
    <li><strong style="color:#eee">Google Analytics</strong> — aggregate usage data.
    <a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Google Privacy Policy</a>.</li>
    <li><strong style="color:#eee">Google Sign-In (OAuth)</strong> — optional login method.
    We receive your Google ID and display name only. We do not receive your Google contacts,
    Drive files, or any other Google data.</li>
    <li><strong style="color:#eee">Railway</strong> — the cloud platform that hosts the site.
    Your data is stored on Railway's infrastructure in the United States.</li>
    <li><strong style="color:#eee">Postmark</strong> — delivers Want List email alerts and
    confirmation codes, only for users who opt in.
    <a href="https://postmarkapp.com/privacy-policy" target="_blank" rel="noopener">Postmark Privacy Policy</a>.</li>
  </ul>

  <h2>Data Retention</h2>
  <p>Your account and associated data are retained until you delete your account. You can delete
  it yourself at any time: click your username (or open About) and choose “Delete my account”. You
  confirm with your password or by signing in with Google again, and the account and everything in
  it (Watch List, Want List, favorite stores, saved searches and any alert address) is deleted right
  away. You can also ask us to delete it by contacting us at the address below. Guest users
  (no account) have no data stored on our servers beyond the anonymous device ID cookie.
  When you remove your alert address or your account is deleted, it is deleted right away; copies
  can remain in our daily server backups for up to 7 days (alert addresses only in encrypted form)
  before those backups are replaced.</p>

  <h2>Children's Privacy</h2>
  <p>This site is not directed at children under 13. We do not knowingly collect personal
  information from children.</p>

  <h2>Changes to This Policy</h2>
  <p>If we make material changes to this policy, we will update the "Last updated" date at the
  top of this page. Continued use of the site after changes are posted constitutes acceptance
  of the updated policy.</p>

  <h2>Contact</h2>
  <p>Questions about this privacy policy or your data can be sent to:
  <a href="mailto:chuck@gcgeartracker.com">chuck@gcgeartracker.com</a></p>

  <footer>GC Used Inventory Tracker is an independent tool and is not affiliated with or
  endorsed by Guitar Center, Inc.</footer>
</div>
</body>
</html>"""

# ── Google Analytics ──────────────────────────────────────────────────────────
if GA_MEASUREMENT_ID:
    # Only the async src= loader — no inline script block.
    # The gtag('config', ...) init lives in static/gc.js and static/cl.js,
    # which read the GA ID from the <meta name="ga-id"> tag below.
    _ga_snippet = (
        f'<!-- Google tag (gtag.js) -->\n'
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>\n'
        f'<meta name="ga-id" content="{GA_MEASUREMENT_ID}">'
    )
else:
    _ga_snippet = ''
APP_VERSION = "2.22.4"
HTML_TEMPLATE    = HTML_TEMPLATE.replace('<!-- __GA__ -->', _ga_snippet)
HTML_TEMPLATE    = HTML_TEMPLATE.replace('<!-- __VER__ -->', f'v{APP_VERSION}')
CL_TEMPLATE      = CL_TEMPLATE.replace('<!-- __GA__ -->', _ga_snippet)
NEWDEALS_TEMPLATE = NEWDEALS_TEMPLATE.replace('<!-- __GA__ -->', _ga_snippet)

# Cache-busting: version every local static JS/CSS reference so the 1-year
# SEND_FILE_MAX_AGE_DEFAULT above can never serve a stale asset across deploys.
# (2026-07 audit S7)
def _version_static(tpl: str) -> str:
    return re.sub(r'(/static/[a-zA-Z0-9._-]+\.(?:js|css))', rf'\1?v={APP_VERSION}', tpl)

HTML_TEMPLATE     = _version_static(HTML_TEMPLATE)
CL_TEMPLATE       = _version_static(CL_TEMPLATE)
NEWDEALS_TEMPLATE = _version_static(NEWDEALS_TEMPLATE)

# __STORES_NOSCRIPT__ is replaced at request time in index() so it always reflects
# the live store cache — see the index() route handler below.




# ── Startup bootstrap ─────────────────────────────────────────────────────────
# Runs unconditionally at import time (not just under `if __name__ == "__main__"`)
# so it executes the same way whether the app is launched directly (`python
# gc_tracker_app.py`, dev server) or imported by a WSGI server like gunicorn
# (`gunicorn gc_tracker_app:app`, v2.16.10) — gunicorn never runs the module as
# __main__, so anything load-bearing has to live out here or it silently never
# runs in production. _load_cookies() has no other call site, so it MUST run
# here.
_load_cookies()
if not STORES_CACHE.exists():
    print("Building store list…")
    refresh_store_list()

# ── Periodic malloc_trim ──────────────────────────────────────────────────────
# Root cause (investigated 2026-08-31): Railway production memory climbs from
# ~2GB to 7-8GB+ between deploys and never comes back down. Confirmed via local
# load testing that this is NOT a Python-level leak — it's glibc's allocator
# holding onto pages at their high-water mark. Under concurrent /api/browse
# traffic (large per-request temporary lists built from the ~92K-item catalog,
# --threads=8), glibc grabs extra heap to serve simultaneous large allocations
# and, once freed, keeps those pages reserved rather than returning them to the
# OS — classic RSS high-water-mark behavior, not a growing live working set.
# Confirmed locally that calling libc's malloc_trim(0) reliably reclaims it
# (reproduced twice: ~450MB baseline → ~1.2GB after load → ~410MB after trim,
# both rounds). This runs a trim on a timer so RSS gets reclaimed periodically
# in production without needing a deploy to reset it. Linux-only (Railway) —
# no-ops harmlessly if libc.so.6 isn't available (e.g. local dev on macOS).
_MALLOC_TRIM_INTERVAL_SECS = 300  # 5 minutes

def _malloc_trim_loop():
    try:
        _libc = _ctypes.CDLL("libc.so.6")
    except OSError:
        return  # not on glibc/Linux (e.g. local macOS dev) — nothing to do
    while True:
        time.sleep(_MALLOC_TRIM_INTERVAL_SECS)
        try:
            _gc.collect()
            _libc.malloc_trim(0)
        except Exception:
            pass  # best-effort — never let this background thread take the app down

threading.Thread(target=_malloc_trim_loop, daemon=True).start()

# Daily Want List alert scheduler (v2.19.0). ALERTS_SCHEDULER=off disables it
# (local dev / tests); it also no-ops while alerts or Postgres aren't ready.
if _ALERTS_SCHEDULER_ON:
    threading.Thread(target=_alerts_scheduler_loop, daemon=True).start()

# Nightly scan removed — "Check for New" is manual only

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\n  Guitar Center Tracker v{APP_VERSION} is running!")
    print(f"  Open: {url}")
    print(f"  Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
