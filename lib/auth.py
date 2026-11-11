"""Authentication, authorization, and user CRUD.

Session model: Flask's signed cookie session stores `user_id` and a session
fingerprint (`sid`). Every request we re-load the user from DB to enforce
`is_active` and `must_change_pw`. No external dep beyond Werkzeug (already
installed via Flask).
"""
from __future__ import annotations
import secrets
import time
from collections import deque
from functools import wraps
from typing import Callable

from flask import g, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db


# ---------- password rules ----------

MIN_PASSWORD_LEN = 8


def hash_pw(plain: str) -> str:
    return generate_password_hash(plain)


def verify_pw(plain: str, hashed: str) -> bool:
    try:
        return check_password_hash(hashed, plain)
    except Exception:
        return False


# ---------- user CRUD ----------

def get_user(user_id: int) -> dict | None:
    with db.ro() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with db.ro() as c:
        row = c.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with db.ro() as c:
        row = c.execute("SELECT * FROM users WHERE LOWER(email)=LOWER(?)", (email,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with db.ro() as c:
        rows = c.execute(
            """SELECT u.*,
                  (SELECT COUNT(*) FROM people p WHERE p.assigned_user_id = u.id) AS assigned_count
                FROM users u
            ORDER BY u.role DESC, u.username ASC"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password_hash", None)
        out.append(d)
    return out


def list_active_staff_and_admins() -> list[dict]:
    """For the admin's "assign to" dropdown."""
    with db.ro() as c:
        rows = c.execute(
            "SELECT id, username, display_name, role FROM users WHERE is_active=1 ORDER BY role DESC, username ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_user(
    *,
    username: str,
    email: str | None,
    display_name: str,
    password: str,
    role: str = "staff",
    is_active: int = 0,
    must_change_pw: int = 0,
) -> int:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError("weak_password")
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO users
               (username, email, display_name, password_hash, role, is_active, must_change_pw, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (username.strip(), (email or "").strip().lower() or None,
             (display_name or "").strip() or username.strip(),
             hash_pw(password), role, is_active, must_change_pw, now),
        )
        return cur.lastrowid


def set_password(user_id: int, new_password: str, must_change: bool = False) -> None:
    if len(new_password) < MIN_PASSWORD_LEN:
        raise ValueError("weak_password")
    with db.tx() as c:
        c.execute(
            "UPDATE users SET password_hash=?, must_change_pw=? WHERE id=?",
            (hash_pw(new_password), 1 if must_change else 0, user_id),
        )


def set_active(user_id: int, active: bool) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET is_active=? WHERE id=?", (1 if active else 0, user_id))


def set_role(user_id: int, role: str) -> None:
    if role not in ("admin", "staff"):
        raise ValueError("bad_role")
    with db.tx() as c:
        c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def delete_user(user_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def count_active_admins(exclude_id: int | None = None) -> int:
    with db.ro() as c:
        if exclude_id is not None:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1 AND id != ?",
                (exclude_id,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active=1"
            ).fetchone()
    return row["n"]


def stamp_login(user_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (int(time.time()), user_id))


# ---------- session ----------

def login_user(user: dict, remember: bool = False) -> None:
    session.clear()
    session["user_id"] = user["id"]
    session["sid"] = secrets.token_hex(8)
    session.permanent = bool(remember)


def logout_user() -> None:
    session.clear()


def current_user() -> dict | None:
    """Cached on `g` for the duration of a request.

    Re-checks `is_active` every request so a deactivation takes effect immediately.
    Returns None if session invalid OR account disabled OR row missing.
    """
    if "user" in g.__dict__:
        return g.user
    uid = session.get("user_id")
    if not uid:
        g.user = None
        return None
    u = get_user(uid)
    if not u or not u["is_active"]:
        # Cookie present but account gone or disabled — clear cookie.
        session.clear()
        g.user = None
        return None
    u.pop("password_hash", None)
    g.user = u
    return u


# ---------- decorators ----------

def login_required(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(f"/login?next={request.path}")
        if u.get("must_change_pw") and request.path not in ("/change_password",) \
                and not request.path.startswith("/api/auth/"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "must_change_password"}), 403
            return redirect("/change_password")
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(f"/login?next={request.path}")
        if u["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "forbidden"}), 403
            return ("forbidden", 403)
        if u.get("must_change_pw") and request.path != "/change_password" \
                and not request.path.startswith("/api/auth/"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "must_change_password"}), 403
            return redirect("/change_password")
        return fn(*args, **kwargs)
    return wrapper


def is_admin(u: dict | None = None) -> bool:
    u = u if u is not None else current_user()
    return bool(u and u["role"] == "admin")


# ---------- ownership check ----------

def user_owns_person(user: dict, person_id: int) -> bool:
    """admin owns everything; staff owns rows where assigned_user_id = self."""
    if not user:
        return False
    if user["role"] == "admin":
        # admin "owns" any existing row, but we still want to return True only if it exists
        with db.ro() as c:
            r = c.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone()
        return r is not None
    with db.ro() as c:
        r = c.execute(
            "SELECT 1 FROM people WHERE id=? AND assigned_user_id=?", (person_id, user["id"])
        ).fetchone()
    return r is not None


def filter_owned_person_ids(user: dict, ids: list[int]) -> tuple[list[int], list[int]]:
    """Return (owned, not_owned). Empty input → ([], [])."""
    if not ids:
        return [], []
    if user and user["role"] == "admin":
        with db.ro() as c:
            qmarks = ",".join("?" * len(ids))
            rows = c.execute(
                f"SELECT id FROM people WHERE id IN ({qmarks})", ids
            ).fetchall()
        existing = {r["id"] for r in rows}
        owned = [i for i in ids if i in existing]
        not_owned = [i for i in ids if i not in existing]
        return owned, not_owned
    with db.ro() as c:
        qmarks = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT id FROM people WHERE id IN ({qmarks}) AND assigned_user_id=?",
            [*ids, user["id"]],
        ).fetchall()
    owned_set = {r["id"] for r in rows}
    owned = [i for i in ids if i in owned_set]
    not_owned = [i for i in ids if i not in owned_set]
    return owned, not_owned


def user_owns_account(user: dict, account_id: int) -> bool:
    """Account belongs to a person; check ownership at the person level."""
    if not user:
        return False
    with db.ro() as c:
        r = c.execute(
            "SELECT person_id FROM platform_accounts WHERE id=?", (account_id,)
        ).fetchone()
    if not r:
        return False
    return user_owns_person(user, r["person_id"])


# ---------- rate limit (in-memory; sliding window) ----------

_RATE_BUCKETS: dict[tuple[str, str], deque] = {}
# Each distinct (bucket, key) leaves a dict entry behind forever — the deque is
# pruned on use but the key is not. Normal use adds a handful (one per staff
# username, one per office IP); a scanner hitting /login or /register from many
# IPs would grow it without bound. Sweep the dead keys once the dict gets big.
_RATE_SWEEP_AT = 2000


def _sweep_rate_buckets(now: float) -> None:
    """Drop entries that can no longer affect any decision.

    Every bucket in use has a 60-second window, so once an entry's newest hit
    is older than that it is dead weight. Called only when the dict has grown
    past _RATE_SWEEP_AT, so it stays off the hot path.
    """
    horizon = now - 65          # widest window (60s) + slack
    for k, dq in list(_RATE_BUCKETS.items()):
        if not dq or dq[-1] < horizon:
            _RATE_BUCKETS.pop(k, None)


def rate_limit(bucket: str, key: str, max_hits: int, window_seconds: int) -> bool:
    """Return True if the call should be allowed; False if it should be 429-rejected."""
    now = time.time()
    k = (bucket, key)
    dq = _RATE_BUCKETS.setdefault(k, deque())
    while dq and dq[0] < now - window_seconds:
        dq.popleft()
    if len(dq) >= max_hits:
        return False
    dq.append(now)
    if len(_RATE_BUCKETS) > _RATE_SWEEP_AT:
        _sweep_rate_buckets(now)
    return True


def reset_rate(bucket: str, key: str) -> None:
    _RATE_BUCKETS.pop((bucket, key), None)
