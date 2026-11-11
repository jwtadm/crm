"""SQLite layer: people + platform_accounts with email-global / (platform,id) dedup.

A "person" is the global identity — keyed by email when known, otherwise pinned
to a single platform account. Each `platform_accounts` row links to one person;
discovering a shared email later collapses two persons into one.

All blob/list fields are stored as JSON text so the DB stays human-inspectable.
"""
import json
import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

# Test harness overrides this via SCOUT_DB_PATH env var so the rrweb suite
# never touches the production scout.db. See tests/rrweb/run.py.
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "scout.db"
DB_PATH = Path(os.environ["SCOUT_DB_PATH"]) if os.environ.get("SCOUT_DB_PATH") else _DEFAULT_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE,                    -- canonical key when known
    full_name   TEXT,
    -- CRM-friendly fields
    status      TEXT DEFAULT 'lead',            -- lead / contacted / replied / qualified / dropped
    tags        TEXT DEFAULT '[]',              -- JSON array
    notes       TEXT DEFAULT '',
    owner       TEXT DEFAULT '',
    last_contacted_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,            -- 'instagram' | 'tiktok' | 'x'
    platform_user_id  TEXT NOT NULL,            -- canonical numeric/string id from platform
    secondary_id      TEXT,                     -- e.g. tiktok sec_uid
    username          TEXT,                     -- @handle
    full_name         TEXT,
    biography         TEXT,
    follower_count    INTEGER,
    following_count   INTEGER,
    post_count        INTEGER,
    video_count       INTEGER,
    is_verified       INTEGER DEFAULT 0,
    is_private        INTEGER DEFAULT 0,
    is_business       INTEGER DEFAULT 0,
    category          TEXT,
    profile_pic_url   TEXT,
    external_url      TEXT,
    bio_links         TEXT DEFAULT '[]',        -- JSON array
    email             TEXT,                     -- email scraped/found at this account
    phone             TEXT,
    city              TEXT,
    address           TEXT,
    country           TEXT,
    bio_embedding     BLOB,                     -- packed float32 vector
    embedding_text    TEXT,                     -- text we embedded (for cache invalidation)
    raw               TEXT,                     -- full upstream JSON
    source            TEXT,                     -- e.g. 'similar:instagram:natgeo' / 'query:tiktok:fitness coach' / 'manual'
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    UNIQUE(platform, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_pa_username ON platform_accounts(platform, username);
-- idx_pa_person / idx_people_email are intentionally NOT created here.
--   * idx_pa_person: idx_pa_person_cover has person_id as its leading column,
--     so it is a strict superset.
--   * idx_people_email: duplicate of the UNIQUE(people.email) auto-index.
-- Both are dropped by _drop_redundant_indexes() for databases that still have
-- them. Creating them here just to drop them again would rebuild two indexes
-- on every single boot.
-- Performance indexes for common queries (filtering/sorting)
CREATE INDEX IF NOT EXISTS idx_people_status ON people(status);
-- idx_people_assigned is created in _apply_migrations: the column only exists
-- after the migration ALTERs, so this index cannot be built at SCHEMA time.
CREATE INDEX IF NOT EXISTS idx_people_created_at ON people(created_at);
CREATE INDEX IF NOT EXISTS idx_people_updated_at ON people(updated_at);
-- idx_people_last_contacted is not created: it was a prefix of
-- idx_people_list_sort below, so the default sort already had an index.
CREATE INDEX IF NOT EXISTS idx_pa_follower ON platform_accounts(follower_count);
-- Composite sort index: eliminates 29k-row temp B-tree sort on /api/db/list.
-- Matches ORDER BY follower_count DESC, updated_at DESC with platform IN (...) filter.
CREATE INDEX IF NOT EXISTS idx_pa_list_sort ON platform_accounts(platform, follower_count DESC, updated_at DESC);
-- Composite sort index for list_people default ORDER BY last_contacted_at DESC, updated_at DESC.
CREATE INDEX IF NOT EXISTS idx_people_list_sort ON people(last_contacted_at DESC, updated_at DESC);
-- idx_email_log_person / idx_followup_actor are created in _apply_migrations:
-- their tables are defined later in this script, so the indexes cannot be
-- built here on a fresh database.

CREATE TABLE IF NOT EXISTS email_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER,
    to_email    TEXT NOT NULL,
    subject     TEXT,
    sent_at     INTEGER NOT NULL,
    ok          INTEGER NOT NULL,        -- 1 = success, 0 = failure
    error       TEXT,
    job_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_log_sent_at ON email_log(sent_at);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    email           TEXT UNIQUE,
    display_name    TEXT,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'staff',  -- 'admin' | 'staff'
    is_active       INTEGER NOT NULL DEFAULT 0,
    must_change_pw  INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    last_login_at   INTEGER
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at);

CREATE TABLE IF NOT EXISTS email_jobs (
    id           TEXT PRIMARY KEY,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_jobs_created ON email_jobs(created_by, created_at);

-- "Bulk similarity" jobs: run fetch_similar_users + ingest serially over a
-- snapshot of seed accounts. status ∈ running / interrupted / done / failed.
-- payload_json carries params, seed_ids, cursor, progress, per-seed log, totals.
CREATE TABLE IF NOT EXISTS similarity_jobs (
    id           TEXT PRIMARY KEY,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_similarity_jobs_created ON similarity_jobs(created_by, created_at);

-- "Bulk hydrate" jobs: take a snapshot of placeholder accounts (rows whose
-- platform_user_id starts with 'import:') and call fetch_user_info on each
-- so they pick up a real pk_id + bio + email. Same shape as similarity_jobs;
-- runs are independent (different TikHub endpoint, separate locks).
CREATE TABLE IF NOT EXISTS hydrate_jobs (
    id           TEXT PRIMARY KEY,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hydrate_jobs_created ON hydrate_jobs(created_by, created_at);

-- Free-text follow-up records. One per "conversation point" with a customer
-- (a call, a reply, a meeting). Surfaced as a timeline in the customer drawer.
CREATE TABLE IF NOT EXISTS followup_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    actor_id   INTEGER REFERENCES users(id),
    content    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followup_person ON followup_logs(person_id, created_at);

-- Reminders for follow-up. A poll endpoint reads any row whose remind_at <=
-- now() and dismissed_at IS NULL; the UI renders pending ones as a banner.
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    actor_id     INTEGER REFERENCES users(id),
    remind_at    INTEGER NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    dismissed_at INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(remind_at, dismissed_at);
CREATE INDEX IF NOT EXISTS idx_rem_actor ON reminders(actor_id, dismissed_at);

-- Status change events for statistics / daily report.
-- Every time a person's status changes, a row is inserted here so we can
-- answer "how many became 已回复 today?" accurately.
CREATE TABLE IF NOT EXISTS status_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    actor_id    INTEGER REFERENCES users(id),
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_events_actor ON status_events(actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_status_events_status ON status_events(new_status, created_at);
"""


# Idempotent ALTERs for columns added after the initial schema. SQLite has no
# IF NOT EXISTS for ADD COLUMN, so we probe the table's columns first. Every
# ALTER is a literal statement below — SQLite cannot bind identifiers in DDL,
# so we hardcode them and only pass *values* via parameters elsewhere.


def _columns_of(c: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def _apply_migrations(c: sqlite3.Connection) -> None:
    people = _columns_of(c, "people")
    if "assigned_user_id" not in people:
        c.execute("ALTER TABLE people ADD COLUMN assigned_user_id INTEGER REFERENCES users(id)")
    if "previous_status" not in people:
        c.execute("ALTER TABLE people ADD COLUMN previous_status TEXT")
    if "intent" not in people:  # 合作强度高/中/低/无意愿; NULL = 未评估
        c.execute("ALTER TABLE people ADD COLUMN intent TEXT")
    # Index on a migrated column — cannot live in SCHEMA because on a fresh
    # database the column does not exist until the ALTERs above have run.
    c.execute("CREATE INDEX IF NOT EXISTS idx_people_assigned ON people(assigned_user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_people_intent ON people(intent)")

    users = _columns_of(c, "users")
    if "reply_to_email" not in users:  # per-staff "客户回信应该回到这" 邮箱
        c.execute("ALTER TABLE users ADD COLUMN reply_to_email TEXT")

    accounts = _columns_of(c, "platform_accounts")
    if "is_rejected" not in accounts:
        c.execute("ALTER TABLE platform_accounts ADD COLUMN is_rejected INTEGER DEFAULT 0")
    if "rejection_reason" not in accounts:
        c.execute("ALTER TABLE platform_accounts ADD COLUMN rejection_reason TEXT")
    if "rejected_by" not in accounts:
        c.execute("ALTER TABLE platform_accounts ADD COLUMN rejected_by INTEGER REFERENCES users(id)")
    if "rejected_at" not in accounts:
        c.execute("ALTER TABLE platform_accounts ADD COLUMN rejected_at INTEGER")
    # Performance indexes for migrated columns (safe to run multiple times)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_is_rejected ON platform_accounts(is_rejected, rejected_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_country ON platform_accounts(country)")

    # status_events table — create if missing (for existing databases)
    existing_tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "status_events" not in existing_tables:
        c.execute("""
            CREATE TABLE status_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                actor_id    INTEGER REFERENCES users(id),
                old_status  TEXT,
                new_status  TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX idx_status_events_actor ON status_events(actor_id, created_at)")
        c.execute("CREATE INDEX idx_status_events_status ON status_events(new_status, created_at)")

    # Indexes whose tables are defined later in the SCHEMA script (email_log,
    # followup_logs) — on a fresh database they cannot be created there. Safe
    # to run on every boot.
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_log_person ON email_log(person_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_followup_actor ON followup_logs(actor_id, created_at)")

    # --- covering indexes for the dashboard / list hot paths ---
    # Benchmarked 2026-11 on a byte-identical copy of the 820MB production DB:
    # the platform/verified/business distributions went 217ms -> 5ms combined,
    # order=followers 127ms -> 53ms, list_owners 8.7ms -> 1.9ms.
    # Covering indexes are preferred over caching here: always fresh, no
    # per-worker duplication across gunicorn workers, and the queries stay
    # plain COUNT(DISTINCT person_id) instead of a new cache layer to invalidate.
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_plat_person ON platform_accounts(platform, person_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_verified_person ON platform_accounts(is_verified, person_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_business_person ON platform_accounts(is_business, person_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pa_person_cover ON platform_accounts(person_id, platform, username, follower_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_people_owner ON people(owner)")

    # --- drop indexes now covered by something else ---
    # Runs after the replacements above exist, so no query is ever left without
    # a usable index. Verified on a production-sized copy: every affected plan
    # moved to the superset index with no slowdown, and LOWER(email) got
    # slightly faster (4.91ms -> 4.37ms) on the UNIQUE auto-index.
    # Removing them cuts write amplification on the two hottest write tables.
    c.execute("DROP INDEX IF EXISTS idx_people_email")          # UNIQUE(email) auto-index covers it
    c.execute("DROP INDEX IF EXISTS idx_people_last_contacted")  # prefix of idx_people_list_sort
    c.execute("DROP INDEX IF EXISTS idx_pa_person")              # prefix of idx_pa_person_cover


# ---------- connection ----------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")        # concurrent reads while writing
    c.execute("PRAGMA busy_timeout = 30000")      # wait up to 30s for locks
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA cache_size = -64000")       # 64MB page cache (default is 2MB)
    c.execute("PRAGMA temp_store = MEMORY")
    return c


def _index_names(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def init_db() -> None:
    with _conn() as c:
        # Snapshot the index set first so we can tell whether this boot
        # actually changed the schema.
        indexes_before = _index_names(c)
        c.executescript(SCHEMA)
        _apply_migrations(c)
        if _index_names(c) != indexes_before:
            # A new index was created: refresh optimizer statistics, otherwise
            # SQLite keeps using the old plan (e.g. idx_pa_username + temp
            # B-tree instead of the new covering index).
            c.execute("ANALYZE")
        else:
            # Steady-state boot. Full ANALYZE costs ~62ms on the 820MB
            # production DB and rewrites sqlite_stat1 every single process
            # start (and every dev-server reload) even when nothing changed.
            # PRAGMA optimize is ~0.7ms: it only re-analyses tables whose
            # statistics this connection actually found wanting.
            c.execute("PRAGMA optimize")
        # Non-blocking WAL checkpoint: keeps the -wal file from growing unbounded
        # without waiting on concurrent readers (PASSIVE never blocks).
        try:
            c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass


@contextmanager
def tx():
    """Write transaction context. Uses deferred BEGIN so read-only callers
    (like get_user) don't grab the write lock. Autocommit mode (isolation_level=None)
    means we must BEGIN/COMMIT explicitly."""
    c = _conn()
    begun = False
    try:
        c.execute("BEGIN")
        begun = True
        yield c
        c.execute("COMMIT")
    except Exception:
        if begun:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        c.close()


# ---------- embedding pack/unpack ----------

def pack_embedding(vec: list[float] | None) -> bytes | None:
    if not vec:
        return None
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ---------- upsert ----------

def _now() -> int:
    return int(time.time())


def upsert_account(c: sqlite3.Connection, normalized: dict, source: str = "", prefer_person_id: int = None) -> tuple[int, int, bool]:
    """Insert-or-update one platform account. Returns (account_id, person_id, created).

    Dedup rules:
      1. Email present → person row keyed by email (create if missing). Move account
         to that person if it currently belongs to a different one (merging stragglers).
      2. No email → use existing (platform, platform_user_id) row's person; create a
         fresh person if this account is new.

    When *prefer_person_id* is set and the caller has already decided which person
    this account should belong to (e.g. the user clicked "Add handle" on that
    customer's drawer), we honour that choice:
      - no-email + no existing account → attach to prefer_person_id instead of
        creating a new empty person.
      - email matches prefer_person_id → no change (already correct).
      - email matches a DIFFERENT person → prefer_person_id is ignored, the
        email-keyed person wins (so the UI can show a merge prompt).
    """
    now = _now()
    platform = normalized["platform"]
    pid = str(normalized["platform_user_id"])
    username = (normalized.get("username") or "").strip().lstrip("@")
    email = (normalized.get("email") or "").lower().strip() or None

    # Find existing account first
    existing = c.execute(
        "SELECT id, person_id, email FROM platform_accounts WHERE platform=? AND platform_user_id=?",
        (platform, pid),
    ).fetchone()

    # Placeholder-collapse: a manual import created (platform, "import:<handle>")
    # before we knew the real platform_user_id. When the real account comes in via
    # similarity/hydrate, treat the placeholder row as the existing row so we
    # upgrade it in place — preserving its person_id, owner, status, etc — instead
    # of inserting a duplicate person.
    if not existing and username and not pid.startswith("import:"):
        existing = c.execute(
            "SELECT id, person_id, email FROM platform_accounts "
            "WHERE platform=? AND platform_user_id=? ",
            (platform, f"import:{username.lower()}"),
        ).fetchone()

    # Resolve target person
    person_id: int
    if email:
        row = c.execute("SELECT id FROM people WHERE email=?", (email,)).fetchone()
        if row:
            person_id = row["id"]
        else:
            # Promote the existing account's person if it has no email yet, else create new
            if existing and not existing["email"]:
                person_id = existing["person_id"]
                c.execute(
                    "UPDATE people SET email=?, updated_at=? WHERE id=?",
                    (email, now, person_id),
                )
            else:
                cur = c.execute(
                    "INSERT INTO people (email, full_name, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (email, normalized.get("full_name", ""), "待联系", now, now),
                )
                person_id = cur.lastrowid
    else:
        if existing:
            person_id = existing["person_id"]
        elif prefer_person_id is not None:
            # Caller already told us which person this account belongs to — use
            # it instead of creating a brand-new empty person row.
            person_id = prefer_person_id
        else:
            cur = c.execute(
                "INSERT INTO people (email, full_name, status, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?)",
                (normalized.get("full_name", ""), "待联系", now, now),
            )
            person_id = cur.lastrowid

    # If account moved persons (email match brought an orphan to a known person),
    # delete the orphan person if it now has no accounts.
    if existing and existing["person_id"] != person_id:
        old_pid = existing["person_id"]
        c.execute(
            "UPDATE platform_accounts SET person_id=?, updated_at=? WHERE id=?",
            (person_id, now, existing["id"]),
        )
        # garbage-collect orphaned person row
        n_left = c.execute(
            "SELECT COUNT(*) AS n FROM platform_accounts WHERE person_id=?", (old_pid,)
        ).fetchone()["n"]
        if n_left == 0:
            c.execute("DELETE FROM people WHERE id=?", (old_pid,))

    # Build the row payload
    payload = {
        "person_id": person_id,
        "platform": platform,
        "platform_user_id": pid,
        "secondary_id": normalized.get("secondary_id"),
        "username": normalized.get("username", ""),
        "full_name": normalized.get("full_name", ""),
        "biography": normalized.get("biography", ""),
        "follower_count": normalized.get("follower_count"),
        "following_count": normalized.get("following_count"),
        "post_count": normalized.get("post_count"),
        "video_count": normalized.get("video_count"),
        "is_verified": 1 if normalized.get("is_verified") else 0,
        "is_private": 1 if normalized.get("is_private") else 0,
        "is_business": 1 if normalized.get("is_business") else 0,
        "category": normalized.get("category", ""),
        "profile_pic_url": normalized.get("profile_pic_url", ""),
        "external_url": normalized.get("external_url", ""),
        "bio_links": json.dumps(normalized.get("bio_links") or [], ensure_ascii=False),
        "email": email,
        "phone": normalized.get("phone", ""),
        "city": normalized.get("city", ""),
        "address": normalized.get("address", ""),
        "country": normalized.get("country", ""),
        "raw": json.dumps(normalized.get("raw") or {}, ensure_ascii=False),
        "source": source,
        "updated_at": now,
    }

    if existing:
        cols = ", ".join(f"{k}=:{k}" for k in payload)
        c.execute(f"UPDATE platform_accounts SET {cols} WHERE id={existing['id']}", payload)
        return existing["id"], person_id, False
    else:
        payload["created_at"] = now
        cols = ", ".join(payload.keys())
        placeholders = ", ".join(f":{k}" for k in payload)
        cur = c.execute(
            f"INSERT INTO platform_accounts ({cols}) VALUES ({placeholders})", payload
        )
        return cur.lastrowid, person_id, True


def update_embedding(c: sqlite3.Connection, account_id: int, embedding: list[float], source_text: str) -> None:
    c.execute(
        "UPDATE platform_accounts SET bio_embedding=?, embedding_text=?, updated_at=? WHERE id=?",
        (pack_embedding(embedding), source_text, _now(), account_id),
    )


# ---------- queries ----------

# Largest number of ids bound into one `IN (...)` list. Must stay under
# SQLITE_MAX_VARIABLE_NUMBER, which is 32766 on modern SQLite but only 999 on
# older builds, so 900 is the portable ceiling. Also well clear of MySQL's
# max_allowed_packet for the 45k-row platform_accounts table.
_IN_CHUNK = 900

# Columns excluded from default list queries to avoid loading ~19KB/row
# of raw JSON + embedding blobs. Only loaded when explicitly needed (e.g. embedding rerank).
_ACCOUNT_LIGHT_COLS = (
    "id, person_id, platform, platform_user_id, secondary_id, username, full_name, "
    "biography, follower_count, following_count, post_count, video_count, "
    "is_verified, is_private, is_business, category, profile_pic_url, external_url, "
    "bio_links, email, phone, city, address, country, embedding_text, source, "
    "created_at, updated_at, is_rejected, rejection_reason, rejected_by, rejected_at"
)


def list_accounts(
    c: sqlite3.Connection,
    platforms: list[str] | None = None,
    text: str = "",
    limit: int = 200,
    statuses: list[str] | None = None,
    include_heavy: bool = False,
) -> list[sqlite3.Row]:
    """List platform_accounts, optionally filtered by their owner-person's
    `people.status`. The status filter joins through people; `statuses=['已成交']`
    is the usual way to scope the similarity-seed picker to "已成交 客户名下
    的真实账号"。

    `include_heavy=False` (default) skips `raw` and `bio_embedding` columns,
    reducing per-row payload from ~19KB to ~1KB.
    """
    cols = "pa.*" if (include_heavy and statuses) else \
           "*" if include_heavy else \
           f"pa.{_ACCOUNT_LIGHT_COLS}" if statuses else _ACCOUNT_LIGHT_COLS
    if statuses:
        sql = f"SELECT {cols} FROM platform_accounts pa JOIN people p ON p.id = pa.person_id"
        col = lambda name: f"pa.{name}"
    else:
        sql = f"SELECT {cols} FROM platform_accounts"
        col = lambda name: name
    where = []
    args: list = []
    if platforms:
        where.append(f"{col('platform')} IN ({','.join(['?']*len(platforms))})")
        args.extend(platforms)
    if text:
        like = f"%{text}%"
        where.append(f"({col('username')} LIKE ? OR {col('full_name')} LIKE ? OR "
                     f"{col('biography')} LIKE ? OR {col('category')} LIKE ?)")
        args.extend([like, like, like, like])
    if statuses:
        where.append(f"p.status IN ({','.join(['?']*len(statuses))})")
        args.extend(statuses)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (f" ORDER BY {col('follower_count')} DESC, "
            f"{col('updated_at')} DESC LIMIT ?")
    args.append(limit)
    return c.execute(sql, args).fetchall()


def get_account(c: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM platform_accounts WHERE id=?", (account_id,)).fetchone()


def get_accounts_by_ids(c: sqlite3.Connection, ids: Iterable[int]) -> list[sqlite3.Row]:
    ids = list(ids)
    if not ids:
        return []
    qmarks = ",".join("?" * len(ids))
    return c.execute(f"SELECT * FROM platform_accounts WHERE id IN ({qmarks})", ids).fetchall()


def has_account(c: sqlite3.Connection, platform: str, platform_user_id: str) -> bool:
    return c.execute(
        "SELECT 1 FROM platform_accounts WHERE platform=? AND platform_user_id=?",
        (platform, str(platform_user_id)),
    ).fetchone() is not None


def find_person_by_email(c: sqlite3.Connection, email: str) -> int | None:
    from .contact import normalize_email
    e = normalize_email(email)
    if not e:
        return None
    row = c.execute("SELECT id FROM people WHERE email=?", (e,)).fetchone()
    return row["id"] if row else None


def get_person(c: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()


def list_accounts_for_person(c: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT * FROM platform_accounts WHERE person_id=? ORDER BY platform",
        (person_id,),
    ).fetchall()


def update_person_fields(c: sqlite3.Connection, person_id: int, **fields) -> None:
    """CRM-friendly mutation: update status/tags/notes/owner/last_contacted_at/full_name/email.

    Email has special semantics: it's the canonical key. Setting an email that
    already belongs to another person raises sqlite3.IntegrityError.
    """
    allowed = {
        "status", "tags", "notes", "owner", "last_contacted_at",
        "full_name", "email", "assigned_user_id", "previous_status",
        "intent",
    }
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "tags" and isinstance(v, list):
            v = json.dumps(v, ensure_ascii=False)
        if k == "email":
            from .contact import normalize_email, is_valid_email
            v = normalize_email(v)
            if v is not None and not is_valid_email(v):
                raise ValueError(f"invalid_email:{v}")
        if k == "assigned_user_id" and v in ("", 0):
            v = None
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(_now())
    args.append(person_id)
    c.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", args)


def attach_placeholder_handle(
    c: sqlite3.Connection, person_id: int, platform: str, username: str
) -> int:
    """Insert a placeholder platform_account row for a person without hitting
    the real platform API. Caller is expected to pre-validate (platform,
    username) uniqueness on the person; raises sqlite3.IntegrityError if the
    placeholder ID collides with an existing row globally."""
    now = _now()
    uname = username.strip().lstrip("@")
    cur = c.execute(
        """INSERT INTO platform_accounts
           (person_id, platform, platform_user_id, username, source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (person_id, platform, f"import:{uname.lower()}", uname,
         "manual:add-to-person", now, now),
    )
    return cur.lastrowid


def delete_account(c: sqlite3.Connection, account_id: int) -> bool:
    row = c.execute(
        "SELECT person_id FROM platform_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if not row:
        return False
    pid = row["person_id"]
    c.execute("DELETE FROM platform_accounts WHERE id=?", (account_id,))
    n_left = c.execute(
        "SELECT COUNT(*) AS n FROM platform_accounts WHERE person_id=?", (pid,)
    ).fetchone()["n"]
    if n_left == 0:
        c.execute("DELETE FROM people WHERE id=?", (pid,))
    return True


def merge_persons(c: sqlite3.Connection, target_id: int, source_id: int) -> bool:
    """Move all platform_accounts from `source_id` → `target_id`, merge person
    fields (target wins for already-set fields; source fills the gaps), then
    delete source. Returns True if a merge happened.

    On (platform, platform_user_id) collisions while moving, the source's row
    is dropped — the target already has the canonical record.

    Used by:
      - /tmp/dedup_persons.py (one-shot historical sweep)
      - hydrate flows when bio email matches another existing person
    """
    if target_id == source_id:
        return False
    now = _now()

    src_accounts = c.execute(
        "SELECT id, platform, platform_user_id FROM platform_accounts WHERE person_id=?",
        (source_id,),
    ).fetchall()
    for sa in src_accounts:
        existing = c.execute(
            "SELECT id FROM platform_accounts WHERE person_id=? AND platform=? AND platform_user_id=?",
            (target_id, sa["platform"], sa["platform_user_id"]),
        ).fetchone()
        if existing:
            c.execute("DELETE FROM platform_accounts WHERE id=?", (sa["id"],))
        else:
            try:
                c.execute(
                    "UPDATE platform_accounts SET person_id=?, updated_at=? WHERE id=?",
                    (target_id, now, sa["id"]),
                )
            except sqlite3.IntegrityError:
                c.execute("DELETE FROM platform_accounts WHERE id=?", (sa["id"],))

    # Snapshot both rows; target wins existing fields, source fills blanks.
    t = c.execute("SELECT * FROM people WHERE id=?", (target_id,)).fetchone()
    s = c.execute("SELECT * FROM people WHERE id=?", (source_id,)).fetchone()

    # Re-parent child records before deleting source. followup_logs, reminders
    # and email_log is_rejected is ON DELETE CASCADE, so without this their rows
    # would vanish with the source person — silent history loss on a manual
    # merge.  status_events is also CASCADE'd, so migrate it explicitly (RC-8).
    for tbl in ("followup_logs", "reminders", "email_log", "status_events"):
        c.execute(f"UPDATE {tbl} SET person_id=? WHERE person_id=?", (target_id, source_id))

    # Delete source BEFORE writing inherited fields onto target. people.email is
    # UNIQUE, so copying source's email onto target while source still holds it
    # would trip the constraint — free it first.
    c.execute("DELETE FROM people WHERE id=?", (source_id,))

    if t and s:
        sets, args = [], []
        for fld in ("email", "full_name", "owner"):
            if not (t[fld] or "").strip() and (s[fld] or "").strip():
                sets.append(f"{fld}=?")
                args.append(s[fld])
        # Status: prefer source's non-default if target is default 待联系
        if t["status"] == "待联系" and s["status"] and s["status"] not in ("待联系", "lead"):
            sets.append("status=?")
            args.append(s["status"])
        if (s["last_contacted_at"] or 0) > (t["last_contacted_at"] or 0):
            sets.append("last_contacted_at=?")
            args.append(s["last_contacted_at"])
        try:
            ttags = json.loads(t["tags"] or "[]")
            stags = json.loads(s["tags"] or "[]")
            merged = list(dict.fromkeys([*ttags, *stags]))
            if merged != ttags:
                sets.append("tags=?")
                args.append(json.dumps(merged, ensure_ascii=False))
        except Exception:
            pass
        sn = (s["notes"] or "").strip()
        tn = (t["notes"] or "").strip()
        if sn and sn not in tn:
            merged_notes = (tn + "\n" + sn) if tn else sn
            sets.append("notes=?")
            args.append(merged_notes)
        if sets:
            sets.append("updated_at=?")
            args.append(now)
            args.append(target_id)
            c.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", args)

    return True


def all_account_ids_with_embeddings(c: sqlite3.Connection) -> list[tuple[int, bytes]]:
    return [
        (r["id"], r["bio_embedding"])
        for r in c.execute("SELECT id, bio_embedding FROM platform_accounts WHERE bio_embedding IS NOT NULL")
    ]


def _people_where(
    *,
    q: str = "",
    status: str = "",
    owner: str = "",
    has_platform: str = "",
    has_email: str = "",
    assigned_user_id: int | str | None = None,
    include_rejected: bool = False,
) -> tuple[str, list]:
    """Shared WHERE clause for the people list / match_ids queries.

    Extracted so the two callers cannot drift apart. They already had: match_ids
    forgot to pass include_rejected, so "select all matching" silently dropped
    the 不适合 rows the table was showing.
    """
    where, args = ["1=1"], []
    if q:
        like = f"%{q}%"
        # Users often paste "@handle" — store doesn't keep the @, so strip
        # leading @s for the platform-handle subquery while leaving the
        # other clauses alone (email naturally contains @).
        handle_like = f"%{q.lstrip('@')}%"
        where.append(
            "(p.full_name LIKE ? OR p.email LIKE ? OR p.notes LIKE ? OR p.tags LIKE ? "
            " OR EXISTS (SELECT 1 FROM platform_accounts pa "
            "             WHERE pa.person_id = p.id AND pa.username LIKE ?))"
        )
        args.extend([like, like, like, like, handle_like])
    if status:
        where.append("p.status = ?")
        args.append(status)
    if owner:
        where.append("p.owner = ?")
        args.append(owner)
    if assigned_user_id == "unassigned":
        where.append("p.assigned_user_id IS NULL")
    elif isinstance(assigned_user_id, int):
        where.append("p.assigned_user_id = ?")
        args.append(assigned_user_id)
    if not include_rejected:
        where.append("(p.status IS NULL OR p.status != '不适合')")
    if has_email == "yes":
        where.append("p.email IS NOT NULL AND p.email != ''")
    elif has_email == "no":
        where.append("(p.email IS NULL OR p.email = '')")
    if has_platform == "none":
        where.append("NOT EXISTS (SELECT 1 FROM platform_accounts pa WHERE pa.person_id = p.id)")
    elif has_platform == "any":
        where.append("EXISTS (SELECT 1 FROM platform_accounts pa WHERE pa.person_id = p.id)")
    elif has_platform in ("instagram", "x", "tiktok"):
        where.append("EXISTS (SELECT 1 FROM platform_accounts pa WHERE pa.person_id = p.id AND pa.platform = ?)")
        args.append(has_platform)
    return " AND ".join(where), args


def _people_order_sql(order: str) -> str:
    """Shared ORDER BY for the people list queries (identical in both paths,
    so 'select all matching' returns the same rows in the same order)."""
    return {
        "last_contacted": "p.last_contacted_at DESC, p.updated_at DESC",
        "updated":        "p.updated_at DESC",
        "created":        "p.created_at DESC",
        # 「创建时间·正序」—— 给"挨个人工核对"用的稳定排序：行不会因为你点了
        # 一下「不适合」或填了邮箱就跳到顶上去。次序键用 p.id 而不是
        # created_at，因为后者可能撞秒。
        "created_asc":    "p.id ASC",
        "name":           "p.full_name COLLATE NOCASE ASC",
        "followers":      "agg.max_followers DESC, p.updated_at DESC",
    }.get(order, "p.last_contacted_at DESC, p.updated_at DESC")


def list_people(
    c: sqlite3.Connection,
    *,
    q: str = "",
    status: str = "",
    owner: str = "",
    has_platform: str = "",     # 'instagram' | 'x' | 'tiktok' | 'any' | 'none' | ''
    has_email: str = "",        # 'yes' | 'no' | ''
    order: str = "last_contacted",   # 'last_contacted' | 'updated' | 'created' | 'name'
    limit: int = 100,
    offset: int = 0,
    assigned_user_id: int | str | None = None,  # int = filter to that user; 'unassigned' = NULL; None = no filter
    include_rejected: bool = False,
) -> tuple[list[dict], int]:
    """Return (rows, total). Each row has aggregated `handles` & `account_count`.

    `assigned_user_id` is the canonical scoping filter. Caller is responsible for
    setting it to the current staff user's id (or 'unassigned'/None for admin
    views). The legacy `owner` text filter is kept as-is.
    """
    where_sql, args = _people_where(
        q=q, status=status, owner=owner, has_platform=has_platform,
        has_email=has_email, assigned_user_id=assigned_user_id,
        include_rejected=include_rejected,
    )
    total = c.execute(f"SELECT COUNT(*) FROM people p WHERE {where_sql}", args).fetchone()[0]

    order_sql = _people_order_sql(order)

    # Two-phase query: first get the paginated people, then aggregate only for those IDs.
    # This avoids scanning the entire platform_accounts table on every page load.
    if order == "followers":
        # For followers sort, we need the aggregation in the ORDER BY, so use the old approach
        # but still limit it to the paginated result
        rows = c.execute(
            f"""
            SELECT p.id, p.email, p.full_name, p.status, p.tags, p.notes, p.owner,
                   p.assigned_user_id, p.previous_status, p.intent,
                   p.last_contacted_at, p.created_at, p.updated_at,
                   u.username AS assigned_username, u.display_name AS assigned_display_name,
                   agg.handles_blob,
                   COALESCE(agg.account_count, 0) AS account_count,
                   agg.max_followers
              FROM people p
              LEFT JOIN users u ON u.id = p.assigned_user_id
              LEFT JOIN (
                  SELECT person_id,
                         GROUP_CONCAT(platform || ':' || username, '|') AS handles_blob,
                         COUNT(*) AS account_count,
                         MAX(follower_count) AS max_followers
                    FROM platform_accounts
                   GROUP BY person_id
              ) agg ON agg.person_id = p.id
             WHERE {where_sql}
             ORDER BY {order_sql}
             LIMIT ? OFFSET ?
            """,
            [*args, limit, offset],
        ).fetchall()
    else:
        # Fast path: paginate first, then aggregate only for the result set
        people_rows = c.execute(
            f"""
            SELECT p.id, p.email, p.full_name, p.status, p.tags, p.notes, p.owner,
                   p.assigned_user_id, p.previous_status, p.intent,
                   p.last_contacted_at, p.created_at, p.updated_at,
                   u.username AS assigned_username, u.display_name AS assigned_display_name
              FROM people p
              LEFT JOIN users u ON u.id = p.assigned_user_id
             WHERE {where_sql}
             ORDER BY {order_sql}
             LIMIT ? OFFSET ?
            """,
            [*args, limit, offset],
        ).fetchall()

        if not people_rows:
            return [], total

        # Now aggregate only for the people in this page.
        # Chunked: SQLite rejects a statement with more bound variables than
        # SQLITE_MAX_VARIABLE_NUMBER (32766 on modern builds, 999 on old ones),
        # and /api/people/match_ids requests the full match set at once
        # (limit=100000). 43k rows meant 43k placeholders -> OperationalError
        # "too many SQL variables" -> 500 on the most common filter.
        # 900 keeps us under the legacy 999 ceiling and is MySQL-safe too.
        person_ids = [r["id"] for r in people_rows]
        agg_map: dict[int, dict] = {}
        for start in range(0, len(person_ids), _IN_CHUNK):
            chunk = person_ids[start:start + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            agg_rows = c.execute(
                f"""
                SELECT person_id,
                       GROUP_CONCAT(platform || ':' || username, '|') AS handles_blob,
                       COUNT(*) AS account_count,
                       MAX(follower_count) AS max_followers
                  FROM platform_accounts
                 WHERE person_id IN ({placeholders})
                 GROUP BY person_id
                """,
                chunk,
            ).fetchall()
            for r in agg_rows:
                agg_map[r["person_id"]] = dict(r)

        # Merge aggregation into people rows
        rows = []
        for r in people_rows:
            d = dict(r)
            agg = agg_map.get(d["id"], {})
            d["handles_blob"] = agg.get("handles_blob", "")
            d["account_count"] = agg.get("account_count", 0)
            d["max_followers"] = agg.get("max_followers")
            rows.append(d)

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        handles_blob = d.pop("handles_blob") or ""
        # GROUP_CONCAT has no ORDER BY, so the concatenation order follows
        # whichever index the planner happens to pick — the chip order in the
        # table flipped purely from adding an index. Sort here so the output is
        # stable regardless of the plan (and independent of the SQLite version,
        # which matters because production's bundled SQLite may predate 3.44's
        # aggregate ORDER BY support).
        d["handles"] = sorted(
            (
                {"platform": p, "username": u}
                for chunk in handles_blob.split("|") if chunk and ":" in chunk
                for p, u in [chunk.split(":", 1)]
            ),
            key=lambda h: (h["platform"], h["username"] or ""),
        )
        out.append(d)
    return out, total


def list_people_brief(
    c: sqlite3.Connection,
    *,
    q: str = "",
    status: str = "",
    owner: str = "",
    has_platform: str = "",
    has_email: str = "",
    order: str = "last_contacted",
    limit: int = 100000,
    offset: int = 0,
    assigned_user_id: int | str | None = None,
    include_rejected: bool = False,
) -> tuple[list[dict], int]:
    """Same rows/order as list_people, but only id / email / full_name.

    For "select all matching" (up to 43k rows). list_people parses 43k tag
    JSON strings, sorts 43k handle lists and runs ~48 chunked aggregation
    queries — all discarded by this caller. Measured 540ms -> ~120ms.
    Filters and ordering come from the shared builders above, so the selection
    can never disagree with what the table shows.
    """
    where_sql, args = _people_where(
        q=q, status=status, owner=owner, has_platform=has_platform,
        has_email=has_email, assigned_user_id=assigned_user_id,
        include_rejected=include_rejected,
    )
    total = c.execute(f"SELECT COUNT(*) FROM people p WHERE {where_sql}", args).fetchone()[0]
    order_sql = _people_order_sql(order)

    if order == "followers":
        # Same join shape as list_people: ORDER BY needs the aggregate.
        rows = c.execute(
            f"""SELECT p.id, p.email, p.full_name
                  FROM people p
                  LEFT JOIN (
                      SELECT person_id, MAX(follower_count) AS max_followers
                        FROM platform_accounts GROUP BY person_id
                  ) agg ON agg.person_id = p.id
                 WHERE {where_sql}
                 ORDER BY {order_sql}
                 LIMIT ? OFFSET ?""",
            [*args, limit, offset],
        ).fetchall()
    else:
        rows = c.execute(
            f"""SELECT p.id, p.email, p.full_name
                  FROM people p
                 WHERE {where_sql}
                 ORDER BY {order_sql}
                 LIMIT ? OFFSET ?""",
            [*args, limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


# Statuses that are "ahead in the funnel" of 已联系 (or terminal). Sending
# a group email or stamping a contact should NEVER downgrade these to
# 已联系 — it would lose information the staff already recorded. We caught
# this on 2026-05-22 when a 100-person batch downgraded 21 已成交 customers.
_NO_DOWNGRADE_STATUSES = frozenset({"已回复", "已成交", "不适合", "已放弃"})


def record_contact(c: sqlite3.Connection, person_id: int,
                   *, set_status: str | None = None) -> dict:
    """Stamp last_contacted_at = now. Bump status only if the new status is
    a progression (no-downgrade). Specifically: if the row's current status
    is in {'已回复', '已成交', '不适合', '已放弃'}, we keep it; otherwise we
    accept the requested set_status.

    Pass set_status=None to only stamp the timestamp (no status change).
    Returns dict with person data plus '_old_status' for status event tracking."""
    now = _now()
    cur_status = None
    if set_status:
        cur = c.execute("SELECT status FROM people WHERE id=?", (person_id,)).fetchone()
        cur_status = cur["status"] if cur else None
        if cur_status in _NO_DOWNGRADE_STATUSES:
            # Keep status; just stamp the contact timestamp
            c.execute(
                "UPDATE people SET last_contacted_at=?, updated_at=? WHERE id=?",
                (now, now, person_id),
            )
            cur_status = None  # no actual status change
        else:
            c.execute(
                "UPDATE people SET last_contacted_at=?, status=?, updated_at=? WHERE id=?",
                (now, set_status, now, person_id),
            )
    else:
        c.execute(
            "UPDATE people SET last_contacted_at=?, updated_at=? WHERE id=?",
            (now, now, person_id),
        )
    row = c.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    result = dict(row) if row else {}
    result["_old_status"] = cur_status  # only set when status actually changed
    return result


def list_owners(c: sqlite3.Connection) -> list[dict]:
    """Distinct non-empty owner values + their counts, sorted by count desc.

    Returns dicts so the frontend can show "Echo (27,334)" and sink data-noise
    owners (status values misplaced in the column) to the bottom.
    """
    return [
        {"owner": r[0], "count": r[1]}
        for r in c.execute(
            "SELECT owner, COUNT(*) FROM people WHERE owner IS NOT NULL AND owner != '' "
            "GROUP BY owner ORDER BY COUNT(*) DESC, owner ASC"
        ).fetchall()
    ]


def list_statuses(c: sqlite3.Connection) -> list[dict]:
    """Distinct non-empty status values + counts, sorted by count desc.

    Used to populate the filter dropdown dynamically — covers legacy/dirty
    values like '感兴趣' alongside the standard 6.
    """
    return [
        {"status": r[0], "count": r[1]}
        for r in c.execute(
            "SELECT status, COUNT(*) FROM people WHERE status IS NOT NULL AND status != '' "
            "GROUP BY status ORDER BY COUNT(*) DESC, status ASC"
        ).fetchall()
    ]


def count_accounts(c: sqlite3.Connection) -> dict:
    rows = c.execute(
        "SELECT platform, COUNT(*) AS n FROM platform_accounts GROUP BY platform"
    ).fetchall()
    total = c.execute("SELECT COUNT(*) AS n FROM platform_accounts").fetchone()["n"]
    people = c.execute("SELECT COUNT(*) AS n FROM people").fetchone()["n"]
    return {"total": total, "people": people, "by_platform": {r["platform"]: r["n"] for r in rows}}


def row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k in ("bio_links",):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = []
    # Don't ship the binary embedding or raw JSON to the client by default
    d.pop("bio_embedding", None)
    if d.get("raw"):
        try:
            d["raw"] = json.loads(d["raw"])
        except Exception:
            d["raw"] = {}
    return d


# ---------- status change events ----------

def record_status_event(c: sqlite3.Connection, person_id: int, actor_id: int | None,
                        old_status: str | None, new_status: str) -> None:
    """Record a status change for statistics tracking."""
    now = _now()
    c.execute(
        "INSERT INTO status_events (person_id, actor_id, old_status, new_status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (person_id, actor_id, old_status, new_status, now),
    )


# ---------- statistics queries ----------

# SQLite's JSON1 extension is compiled in by default only on newer builds.
# Probe once per process and remember the answer; get_daily_overview uses a
# json_each() aggregation for the tag histogram when it is available (4.5x
# faster than parsing 30k JSON arrays in Python) and falls back otherwise.
_JSON1_AVAILABLE: bool | None = None


def _json1_available(c: sqlite3.Connection) -> bool:
    global _JSON1_AVAILABLE
    if _JSON1_AVAILABLE is None:
        try:
            c.execute("SELECT json_valid('[]')").fetchone()
            _JSON1_AVAILABLE = True
        except sqlite3.OperationalError:
            _JSON1_AVAILABLE = False
    return _JSON1_AVAILABLE


def _day_bounds(ts: int) -> tuple[int, int]:
    """Return (start_of_day, end_of_day) as unix timestamps for the given timestamp."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def get_daily_overview(c: sqlite3.Connection, start_ts: int, end_ts: int,
                       user_id: int | None = None) -> dict:
    """Get comprehensive stats for a date range.

    Returns summary KPIs, per-user breakdown, pipeline distribution,
    platform distribution, country distribution, intent distribution,
    email stats, and rejection stats.
    """
    user_filter = ""
    user_param = []
    if user_id:
        user_filter = "AND assigned_user_id = ?"
        user_param = [user_id]

    # Summary KPIs
    new_customers = c.execute(
        f"SELECT COUNT(*) AS n FROM people WHERE created_at >= ? AND created_at < ? {user_filter}",
        [start_ts, end_ts] + user_param,
    ).fetchone()["n"]

    # "联系人数" 使用 last_contacted_at 来判断联系时间
    # 因为历史数据可能没有写入 status_events
    contacted = c.execute(
        f"""SELECT COUNT(*) AS n FROM people
           WHERE last_contacted_at >= ? AND last_contacted_at < ?
           {user_filter}""",
        [start_ts, end_ts] + user_param,
    ).fetchone()["n"]

    emails_sent = c.execute(
        f"""SELECT COUNT(*) AS n, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_count
           FROM email_log el
           JOIN people p ON p.id = el.person_id
           WHERE el.sent_at >= ? AND el.sent_at < ? {user_filter}""",
        [start_ts, end_ts] + user_param,
    ).fetchone()
    emails_sent_count = emails_sent["n"] or 0
    emails_ok_count = emails_sent["ok_count"] or 0

    followup_count = c.execute(
        f"""SELECT COUNT(*) AS n FROM followup_logs f
            JOIN people p ON p.id = f.person_id
            WHERE f.created_at >= ? AND f.created_at < ? {user_filter}""",
        [start_ts, end_ts] + user_param,
    ).fetchone()["n"]

    # Status changes from status_events — COUNT(DISTINCT person_id) so the same
    # person entering a stage twice in the range counts once (RC-1/RC-6).
    # One GROUP BY query for all four funnel statuses instead of a 4-iteration
    # Python loop; missing statuses still report 0 below.
    status_changes = {s: 0 for s in ["已联系", "已回复", "已成交", "不适合"]}
    for row in c.execute(
        f"""SELECT se.new_status AS s, COUNT(DISTINCT se.person_id) AS n
              FROM status_events se
              LEFT JOIN people p ON p.id = se.person_id
             WHERE se.new_status IN ('已联系', '已回复', '已成交', '不适合')
               AND se.created_at >= ? AND se.created_at < ?
               {user_filter}
             GROUP BY se.new_status""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        status_changes[row["s"]] = row["n"]

    # Per-user breakdown — use GROUP BY queries instead of per-user loops (N+1 fix)
    per_user = []
    users = c.execute(
        "SELECT id, username, display_name FROM users WHERE is_active = 1 ORDER BY display_name"
    ).fetchall()
    user_map = {u["id"]: u for u in users}

    # Batch all per-user counts into single GROUP BY queries.
    # All queries include user_filter so when admin selects a specific employee,
    # the per-user table only shows that employee and totals match the KPI (RC-3).

    u_new_map = {}
    for r in c.execute(
        f"""SELECT assigned_user_id, COUNT(*) AS n FROM people
           WHERE created_at >= ? AND created_at < ? {user_filter}
           GROUP BY assigned_user_id""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        u_new_map[r["assigned_user_id"]] = r["n"]

    # "联系" per-user: 使用 last_contacted_at 来判断联系时间
    # 因为历史数据可能没有写入 status_events
    u_contacted_map = {}
    for r in c.execute(
        f"""SELECT assigned_user_id, COUNT(*) AS n FROM people
           WHERE last_contacted_at >= ? AND last_contacted_at < ?
           {user_filter}
           GROUP BY assigned_user_id""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        u_contacted_map[r["assigned_user_id"]] = r["n"]

    u_emails_map = {}
    for r in c.execute(
        f"""SELECT p.assigned_user_id, COUNT(*) AS n FROM email_log el
           JOIN people p ON p.id = el.person_id
           WHERE el.sent_at >= ? AND el.sent_at < ? {user_filter}
           GROUP BY p.assigned_user_id""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        u_emails_map[r["assigned_user_id"]] = r["n"]

    u_followups_map = {}
    for r in c.execute(
        f"""SELECT p.assigned_user_id, COUNT(*) AS n FROM followup_logs f
           JOIN people p ON p.id = f.person_id
           WHERE f.created_at >= ? AND f.created_at < ? {user_filter}
           GROUP BY p.assigned_user_id""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        u_followups_map[r["assigned_user_id"]] = r["n"]

    # Per-user status changes attributed by customer owner (assigned_user_id),
    # not by actor_id, so the per-user totals match the summary KPI (RC-4).
    u_status_map = {}  # {(user_id, status): count}
    for r in c.execute(
        f"""SELECT p.assigned_user_id, se.new_status, COUNT(DISTINCT se.person_id) AS n
           FROM status_events se
           LEFT JOIN people p ON p.id = se.person_id
           WHERE se.created_at >= ? AND se.created_at < ?
           AND se.new_status IN ('已联系','已回复','已成交','不适合')
           {user_filter}
           GROUP BY p.assigned_user_id, se.new_status""",
        [start_ts, end_ts] + user_param,
    ).fetchall():
        u_status_map[(r["assigned_user_id"], r["new_status"])] = r["n"]

    u_total_map = {}
    # "负责客户" 始终统计全量（不受时间范围影响），因为这是员工当前负责的客户总数
    for r in c.execute(
        f"SELECT assigned_user_id, COUNT(*) AS n FROM people WHERE 1=1 {user_filter} GROUP BY assigned_user_id",
        user_param,
    ).fetchall():
        u_total_map[r["assigned_user_id"]] = r["n"]

    for u in users:
        uid = u["id"]
        u_status_changes = {
            s: u_status_map.get((uid, s), 0)
            for s in ["已联系", "已回复", "已成交", "不适合"]
        }
        per_user.append({
            "user_id": uid,
            "username": u["username"],
            "display_name": u["display_name"] or u["username"],
            "total_customers": u_total_map.get(uid, 0),
            "new_customers": u_new_map.get(uid, 0),
            "contacted": u_contacted_map.get(uid, 0),
            "emails_sent": u_emails_map.get(uid, 0),
            "followups": u_followups_map.get(uid, 0),
            "status_changes": u_status_changes,
        })

    # Unassigned customers (assigned_user_id IS NULL) — shown as a synthetic
    # row so the per-user table total matches the pipeline total. Only makes
    # sense when NOT filtering to a specific user.
    if user_id is None:
        unassigned = u_total_map.get(None, 0)
        if unassigned > 0:
            ua_status_changes = {
                s: u_status_map.get((None, s), 0)
                for s in ["已联系", "已回复", "已成交", "不适合"]
            }
            per_user.append({
                "user_id": None,
                "username": "",
                "display_name": "未分配",
                "total_customers": unassigned,
                "new_customers": u_new_map.get(None, 0),
                "contacted": u_contacted_map.get(None, 0),
                "emails_sent": u_emails_map.get(None, 0),
                "followups": u_followups_map.get(None, 0),
                "status_changes": ua_status_changes,
            })

    # Pipeline distribution (all customers, not just today)
    # All distribution ORDER BYs carry a deterministic tie-break so an index
    # change can never reshuffle rows that share a count (which also keeps
    # baseline diffs meaningful).
    pipeline = []
    pipeline_rows = c.execute(
        f"SELECT status, COUNT(*) AS n FROM people WHERE 1=1 {user_filter} GROUP BY status ORDER BY n DESC, status ASC",
        user_param,
    ).fetchall()
    for r in pipeline_rows:
        pipeline.append({"status": r["status"] or "未设置", "count": r["n"]})

    # Platform distribution — ORDER BY platform ASC pins the current observed
    # order (the query previously relied on scan order).
    platform_dist = []
    plat_rows = c.execute(
        f"""SELECT pa.platform, COUNT(DISTINCT pa.person_id) AS n
            FROM platform_accounts pa
            JOIN people p ON p.id = pa.person_id
            WHERE 1=1 {user_filter}
            GROUP BY pa.platform ORDER BY pa.platform ASC""",
        user_param,
    ).fetchall()
    for r in plat_rows:
        platform_dist.append({"platform": r["platform"], "count": r["n"]})

    # Country distribution (top 15)
    country_dist = []
    country_rows = c.execute(
        f"""SELECT pa.country, COUNT(DISTINCT pa.person_id) AS n
            FROM platform_accounts pa
            JOIN people p ON p.id = pa.person_id
            WHERE pa.country IS NOT NULL AND pa.country != '' {user_filter}
            GROUP BY pa.country ORDER BY n DESC, pa.country ASC LIMIT 15""",
        user_param,
    ).fetchall()
    for r in country_rows:
        country_dist.append({"country": r["country"], "count": r["n"]})

    # Intent distribution
    # Grouping on the raw column lets idx_people_intent serve it as a covering
    # index scan (measured 11.8ms -> 1.4ms); COALESCE(intent, '未评估') in the
    # SQL forced a full table scan every request. The label merge and the
    # deterministic ordering happen here instead. No row currently stores the
    # literal '未评估', and even if one did the dict merge below folds it in.
    intent_counts: dict[str, int] = {}
    for r in c.execute(
        f"SELECT intent, COUNT(*) AS n FROM people WHERE 1=1 {user_filter} GROUP BY intent",
        user_param,
    ).fetchall():
        label = r["intent"] or "未评估"
        intent_counts[label] = intent_counts.get(label, 0) + r["n"]
    intent_dist = [
        {"intent": k, "count": v}
        for k, v in sorted(intent_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    # Rejection stats
    rejection_today = c.execute(
        f"""SELECT COUNT(*) AS n FROM platform_accounts pa
            JOIN people p ON p.id = pa.person_id
            WHERE pa.is_rejected = 1 AND pa.rejected_at >= ? AND pa.rejected_at < ?
            {user_filter}""",
        [start_ts, end_ts] + user_param,
    ).fetchone()["n"]

    rejection_total = c.execute(
        f"""SELECT COUNT(*) AS n FROM people
            WHERE status = '不适合' {user_filter}""",
        user_param,
    ).fetchone()["n"]

    # Rejection reasons — include user_filter so admin's employee selection
    # is reflected here too (RC-9).
    rejection_reasons = []
    reason_rows = c.execute(
        f"""SELECT pa.rejection_reason, COUNT(*) AS n FROM platform_accounts pa
           JOIN people p ON p.id = pa.person_id
           WHERE pa.is_rejected = 1 AND pa.rejection_reason IS NOT NULL AND pa.rejection_reason != ''
           {user_filter}
           GROUP BY pa.rejection_reason ORDER BY n DESC, pa.rejection_reason ASC LIMIT 10""",
        user_param,
    ).fetchall()
    for r in reason_rows:
        rejection_reasons.append({"reason": r["rejection_reason"], "count": r["n"]})

    # Tags distribution (top 15)
    # people.tags holds a JSON array per row (~30k non-empty rows). Parsing all
    # of them in Python measured ~56ms per /api/stats call — cProfile showed
    # 152,740 json.loads calls across 5 requests, the single largest remaining
    # cost. SQLite's JSON1 json_each counts the same thing in ~12ms with
    # identical output, so use it when available and fall back to the Python
    # scan otherwise (older SQLite builds may not have JSON1 compiled in).
    tag_dist = []
    if _json1_available(c):
        for r in c.execute(
            f"""SELECT je.value AS tag, COUNT(*) AS n
                  FROM people, json_each(people.tags) AS je
                 WHERE people.tags IS NOT NULL AND people.tags != '[]'
                   AND json_valid(people.tags)
                   {user_filter}
                 GROUP BY je.value ORDER BY n DESC, tag ASC LIMIT 15""",
            user_param,
        ).fetchall():
            tag_dist.append({"tag": r["tag"], "count": r["n"]})
    else:
        all_tags = c.execute(
            f"SELECT tags FROM people WHERE tags IS NOT NULL AND tags != '[]' {user_filter}",
            user_param,
        ).fetchall()
        tag_counts = {}
        for row in all_tags:
            try:
                tags_list = json.loads(row["tags"])
                for tag in tags_list:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except Exception:
                pass
        tag_dist = [{"tag": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))[:15]]

    # Verified / business account ratios
    verified_count = c.execute(
        f"""SELECT COUNT(DISTINCT pa.person_id) AS n FROM platform_accounts pa
            JOIN people p ON p.id = pa.person_id
            WHERE pa.is_verified = 1 {user_filter}""",
        user_param,
    ).fetchone()["n"]
    business_count = c.execute(
        f"""SELECT COUNT(DISTINCT pa.person_id) AS n FROM platform_accounts pa
            JOIN people p ON p.id = pa.person_id
            WHERE pa.is_business = 1 {user_filter}""",
        user_param,
    ).fetchone()["n"]
    total_people = c.execute(
        f"SELECT COUNT(*) AS n FROM people WHERE 1=1 {user_filter}",
        user_param,
    ).fetchone()["n"]

    return {
        "summary": {
            "new_customers": new_customers,
            "contacted": contacted,
            "emails_sent": emails_sent_count,
            "emails_ok": emails_ok_count,
            "followups": followup_count,
            "replies": status_changes.get("已回复", 0),
            "deals": status_changes.get("已成交", 0),
            # Use status_events for "不合适" so KPI / employee detail / pipeline
            # all share the same source (RC-6).
            "rejected_today": status_changes.get("不适合", 0),
        },
        "per_user": per_user,
        "pipeline": pipeline,
        "platform": platform_dist,
        "country": country_dist,
        "intent": intent_dist,
        "rejection_total": rejection_total,
        "rejection_reasons": rejection_reasons,
        "tags": tag_dist,
        "verified_count": verified_count,
        "business_count": business_count,
        "total_people": total_people,
    }


def get_trends(c: sqlite3.Connection, days: int = 7,
               user_id: int | None = None,
               start_ts: int | None = None,
               end_ts: int | None = None,
               offset: int = 0) -> list[dict]:
    """Get daily trend data.

    By default covers the last ``days`` days.  When *start_ts* and *end_ts*
    are both given, iterates over that custom range instead (RC-7b).
    When *offset* is given, shifts the start date back by that many days
    (e.g., offset=1 means start from yesterday instead of today).
    All counting uses the same source as get_daily_overview so trend bars
    match the KPI cards.
    """
    import datetime
    import pytz
    
    # Use China timezone (UTC+8) for consistent date calculations
    china_tz = pytz.timezone('Asia/Shanghai')

    user_filter = ""
    user_param: list = []
    if user_id:
        user_filter = "AND assigned_user_id = ?"
        user_param = [user_id]

    # Build list of (day_start_dt, day_start_ts, day_end_ts) tuples
    if start_ts is not None and end_ts is not None:
        # Custom range: iterate day by day from start to end
        day_dt = datetime.datetime.fromtimestamp(start_ts, tz=china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = datetime.datetime.fromtimestamp(end_ts, tz=china_tz)
        day_list = []
        while day_dt < end_dt:
            day_end = day_dt + datetime.timedelta(days=1)
            day_list.append((
                day_dt,
                int(day_dt.timestamp()),
                int(day_end.timestamp()),
            ))
            day_dt = day_end
    else:
        now = datetime.datetime.now(china_tz)
        # Apply offset: shift start date back by 'offset' days
        if offset > 0:
            now = now - datetime.timedelta(days=offset)
        day_list = []
        for i in range(days - 1, -1, -1):
            dt = now - datetime.timedelta(days=i)
            day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + datetime.timedelta(days=1)
            day_list.append((day_start, int(day_start.timestamp()), int(day_end.timestamp())))

    result = []
    if not day_list:
        return result

    # --- bucketed aggregation -------------------------------------------------
    # One query per metric over the whole window, bucketed by day, instead of
    # 7 queries per day (which meant 630 round-trips for a 90-day chart).
    # Bucket index = (ts - window_start) / 86400. Exact because every day
    # boundary in day_list is a whole Asia/Shanghai day and the zone has had no
    # DST since 1991, so consecutive boundaries are exactly 86400s apart.
    #
    # Each SQL statement stays a literal at its own call site (this repo does
    # not allow a generic SQL-forwarding helper — see AGENTS/Mimosa rules);
    # only the *rows* are handed to _bucket_map below.
    window_start = day_list[0][1]
    window_end = day_list[-1][2]
    n_days = len(day_list)

    def _bucket_map(rows) -> dict[int, int]:
        out: dict[int, int] = {}
        for row in rows:
            b = row[0]
            # Guard against a row landing outside the window (impossible given
            # the WHERE clause) rather than silently growing the chart.
            if b is not None and 0 <= b < n_days:
                out[b] = row[1]
        return out

    new_c = _bucket_map(c.execute(
        f"""SELECT (created_at - ?) / 86400 AS b, COUNT(*) AS n FROM people
             WHERE created_at >= ? AND created_at < ? {user_filter}
             GROUP BY b""",
        [window_start, window_start, window_end] + user_param,
    ).fetchall())

    emails_c = _bucket_map(c.execute(
        f"""SELECT (el.sent_at - ?) / 86400 AS b, COUNT(*) AS n FROM email_log el
             JOIN people p ON p.id = el.person_id
             WHERE el.sent_at >= ? AND el.sent_at < ? {user_filter}
             GROUP BY b""",
        [window_start, window_start, window_end] + user_param,
    ).fetchall())

    followups_c = _bucket_map(c.execute(
        f"""SELECT (f.created_at - ?) / 86400 AS b, COUNT(*) AS n FROM followup_logs f
             JOIN people p ON p.id = f.person_id
             WHERE f.created_at >= ? AND f.created_at < ? {user_filter}
             GROUP BY b""",
        [window_start, window_start, window_end] + user_param,
    ).fetchall())

    # Funnel statuses: one query yields all four series.
    # COUNT(DISTINCT person_id) per day, matching the previous per-day queries.
    funnel: dict[tuple[int, str], int] = {}
    for row in c.execute(
        f"""SELECT (se.created_at - ?) / 86400 AS b, se.new_status AS s,
                   COUNT(DISTINCT se.person_id) AS n
              FROM status_events se
              LEFT JOIN people p ON p.id = se.person_id
             WHERE se.new_status IN ('已联系', '已回复', '已成交', '不适合')
               AND se.created_at >= ? AND se.created_at < ? {user_filter}
             GROUP BY b, s""",
        [window_start, window_start, window_end] + user_param,
    ).fetchall():
        b = row[0]
        if b is not None and 0 <= b < n_days:
            funnel[(b, row[1])] = row[2]

    for i, (day_start, _sts, _ets) in enumerate(day_list):
        result.append({
            "date": day_start.strftime("%m-%d"),
            "new_customers": new_c.get(i, 0),
            "contacted": funnel.get((i, "已联系"), 0),
            "emails_sent": emails_c.get(i, 0),
            "followups": followups_c.get(i, 0),
            "replies": funnel.get((i, "已回复"), 0),
            "deals": funnel.get((i, "已成交"), 0),
            "rejected": funnel.get((i, "不适合"), 0),
        })
    return result
