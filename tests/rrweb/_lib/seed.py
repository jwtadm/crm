"""Shared fixture helpers for rrweb tests.

Tests must be self-contained: each call to `wipe_fixtures()` removes ANY rows
created by previous test runs (matched by the conventional `FIXTURE_TAG` source
+ `rrwebfx_` username prefix + display-name prefix `RRWebFx`), so two consecutive
runs don't drift.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib import auth, db  # noqa: E402

from . import config

FIXTURE_TAG = "rrwebfx"
USERNAME_PREFIX = "rrwebfx_"
DISPLAY_PREFIX = "RRWebFx"


def _assert_test_db() -> None:
    """Refuse to wipe / mutate anything unless the runner has wired the
    harness DB. Without this, accidentally running tests/rrweb/run.py from
    a production shell would silently nuke admin's password (see
    ensure_admin) and any RRWebFx-tagged real data."""
    if not config.in_test_mode():
        raise RuntimeError(
            "rrweb test harness refusing to touch the DB: SCOUT_DB_PATH is not "
            f"pointed at the test DB ({config.TEST_DB_PATH}). "
            "Don't run tests/rrweb/run.py outside the runner — it sets this for you."
        )


def _conn() -> sqlite3.Connection:
    db.init_db()
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def wipe_fixtures() -> None:
    _assert_test_db()
    """Drop everything previous runs left behind. Matches by FIXTURE_TAG /
    prefix only — never touches data outside that convention.

    Strategy: delete *persons* whose any platform account, name, or email
    looks like a fixture — ON DELETE CASCADE then drops the rest. This is
    more robust than deleting platform_accounts in isolation, because
    upsert side-effects (email/full_name promotion) can drift the person
    row's name out of the RRWebFx prefix even when the handle stays.
    """
    c = _conn()
    try:
        c.execute(
            f"""DELETE FROM people WHERE id IN (
                SELECT DISTINCT person_id FROM platform_accounts
                WHERE username LIKE '{USERNAME_PREFIX}%'
                   OR source   LIKE '%{FIXTURE_TAG}%'
            )"""
        )
        c.execute(f"DELETE FROM people WHERE full_name LIKE '{DISPLAY_PREFIX}%'")
        c.execute(f"DELETE FROM people WHERE email LIKE '{FIXTURE_TAG}_%@%'")
        # Belt-and-suspenders: any orphan platform_accounts left by partial
        # writes (shouldn't happen with the CASCADE, but cheap insurance).
        c.execute(f"DELETE FROM platform_accounts WHERE username LIKE '{USERNAME_PREFIX}%'")
        c.execute(f"DELETE FROM platform_accounts WHERE source   LIKE '%{FIXTURE_TAG}%'")
        c.commit()
    finally:
        c.close()


def ensure_admin() -> None:
    """Idempotently (re)set admin password to the value the steps file types."""
    _assert_test_db()
    u = auth.get_user_by_username(config.ADMIN_USERNAME)
    if u:
        auth.set_password(u["id"], config.ADMIN_PASSWORD, must_change=False)
    else:
        auth.create_user(
            username=config.ADMIN_USERNAME,
            email=None,
            display_name="admin",
            password=config.ADMIN_PASSWORD,
            role="admin",
            is_active=1,
            must_change_pw=0,
        )


def make_person(*, name: str, email: str | None = None, owner: str = "",
                status: str = "待联系") -> int:
    _assert_test_db()
    """Insert one person row tagged with the fixture display-name prefix."""
    assert name.startswith(DISPLAY_PREFIX), \
        f"fixture person names must start with {DISPLAY_PREFIX!r} (got {name!r})"
    now = int(time.time())
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO people (full_name, email, owner, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, owner, status, now, now),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def make_real_account(person_id: int, *, platform: str, handle: str,
                      platform_user_id: str | None = None,
                      follower_count: int = 10000) -> int:
    _assert_test_db()
    """Insert a hydrated (non-placeholder) platform_account row, marked as a
    fixture via source=manual:rrwebfx. Used by similarity-seed tests."""
    assert handle.startswith(USERNAME_PREFIX), \
        f"fixture handles must start with {USERNAME_PREFIX!r} (got {handle!r})"
    now = int(time.time())
    pid = platform_user_id or f"{FIXTURE_TAG}-{platform}-{handle}"
    c = _conn()
    try:
        cur = c.execute(
            """INSERT INTO platform_accounts
                 (person_id, platform, platform_user_id, username,
                  full_name, follower_count, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (person_id, platform, pid, handle,
             f"{DISPLAY_PREFIX} Seed Account", follower_count,
             f"manual:{FIXTURE_TAG}", now, now),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def attach_placeholder(person_id: int, *, platform: str, handle: str) -> int:
    """Attach a `platform_user_id='import:<handle>'` placeholder row to a
    person. The username is forced to the rrwebfx_ prefix so wipe_fixtures
    can find it on the next run."""
    _assert_test_db()
    assert handle.startswith(USERNAME_PREFIX), \
        f"fixture handles must start with {USERNAME_PREFIX!r} (got {handle!r})"
    now = int(time.time())
    c = _conn()
    try:
        cur = c.execute(
            """INSERT INTO platform_accounts
                 (person_id, platform, platform_user_id, username, source,
                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (person_id, platform, f"import:{handle.lower()}", handle,
             f"manual:{FIXTURE_TAG}", now, now),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()
