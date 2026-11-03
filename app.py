"""IG Scout — multi-platform creator discovery + CRM-staging DB.

Every UI action has a 1:1 backend API. See `tools/api_test.py` for the contract.
"""
import json as _json_mod
import os
import secrets as _secrets
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from flask import Flask, request, jsonify, send_file, render_template, redirect, g
from flask_caching import Cache
from flask_compress import Compress
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from lib import db, ai, contact, ingest, mailer, settings, auth, audit, bulk_similarity, bulk_hydrate, job_watchdog
from platforms import (
    get as get_platform,
    PLATFORMS, SIMILAR_CAPABLE, SEARCH_CAPABLE, INFO_CAPABLE,
)

app = Flask(__name__)
# Stable secret across restarts. Stored next to scout.db so we don't lose
# sessions on bounce. If the file isn't writeable we fall back to per-process.
_SECRET_PATH = db.DB_PATH.parent / ".session_secret"
try:
    if _SECRET_PATH.exists():
        app.secret_key = _SECRET_PATH.read_bytes()
    else:
        app.secret_key = _secrets.token_bytes(32)
        _SECRET_PATH.write_bytes(app.secret_key)
except Exception:
    app.secret_key = _secrets.token_bytes(32)
app.permanent_session_lifetime = timedelta(days=30)  # honored only when remember-me

# Enable gzip compression for all responses
Compress(app)

# Simple in-memory cache for expensive operations
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 60  # 60 seconds default
cache = Cache(app)

db.init_db()
settings.init()


# ---------- JSON error handlers for API routes ----------
# Without these, Flask/Werkzeug returns HTML error pages for any unhandled
# exception, causing "JSON.parse: unexpected character" in the browser.

@app.errorhandler(404)
def _err_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found"}), 404
    return e

@app.errorhandler(405)
def _err_405(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "method_not_allowed"}), 405
    return e

@app.errorhandler(500)
def _err_500(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal_error"}), 500
    return e

@app.errorhandler(Exception)
def _err_unhandled(e):
    """Catch-all: return JSON for API routes instead of HTML."""
    import traceback
    traceback.print_exc()
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal_error", "detail": str(e)}), 500
    # Let Flask handle non-API routes normally (HTML error page / redirect)
    raise e

# Auto-bootstrap: if there is no active admin, create admin/admin with
# must_change_pw=1 so the operator can log in immediately and is forced to
# pick a real password before doing anything else.
if auth.count_active_admins() == 0:
    if not auth.get_user_by_username("admin"):
        # Direct insert to bypass the 8-char min — the must_change_pw flag
        # will force them off this trivial password the moment they log in.
        with db.tx() as _c:
            _c.execute(
                """INSERT INTO users
                   (username, email, display_name, password_hash, role, is_active, must_change_pw, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("admin", None, "admin", auth.hash_pw("admin"), "admin", 1, 1, int(time.time())),
            )
        print("[bootstrap] created default admin (username=admin / password=admin) — you will be forced to change it on first login.")


@app.after_request
def _no_cache_html(response):
    """Disable browser caching for HTML pages so JS/UI changes go live the
    moment we deploy. This app is iterated on rapidly and stale-HTML bugs are
    expensive to diagnose ("the checkbox is gone!" turned out to be cache twice).
    Static API responses (JSON) are not affected.
    """
    ct = response.content_type or ""
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------- slow request logging ----------
# Anything over this threshold is printed to stdout (gunicorn error log /
# journalctl) with its wall time. Without this, a slow handler is invisible:
# gunicorn's access log records no duration, so "the page hung" reports have no
# server-side evidence to point at.
_SLOW_REQUEST_SECONDS = 1.0


@app.before_request
def _mark_request_start():
    g._req_t0 = time.perf_counter()


@app.after_request
def _log_slow_request(response):
    t0 = getattr(g, "_req_t0", None)
    if t0 is not None:
        elapsed = time.perf_counter() - t0
        if elapsed >= _SLOW_REQUEST_SECONDS:
            print(
                f"[slow] {request.method} {request.full_path.rstrip('?')} "
                f"{elapsed:.2f}s status={response.status_code}",
                flush=True,
            )
    return response


# ============================================================
#  Pages
# ============================================================

@app.route("/")
@auth.login_required
def index():
    return redirect("/stats")


@app.route("/similarity")
@auth.admin_required
def page_similarity():
    return render_template("similarity.html")


@app.route("/query")
@auth.admin_required
def page_query():
    return render_template("query.html")


@app.route("/database")
@auth.admin_required
def page_database():
    return render_template("database.html")


@app.route("/me")
@auth.login_required
def page_me():
    return render_template("me.html")


@app.route("/api/auth/global_from")
@auth.login_required
def api_auth_global_from():
    """Lets /me show the global From as a hint without leaking the rest of
    the admin SMTP settings."""
    return jsonify({"from_email": settings.get("SMTP_FROM_EMAIL") or ""})


@app.route("/settings")
@auth.admin_required
def page_settings():
    return render_template("settings.html")


@app.route("/people")
@auth.login_required
def page_people():
    return render_template("people.html")


@app.route("/stats")
@auth.login_required
def page_stats():
    return render_template("stats.html")


@app.route("/admin/users")
@auth.admin_required
def page_admin_users():
    return render_template("admin_users.html")


@app.route("/login")
def page_login():
    return render_template("login.html")


@app.route("/register")
def page_register():
    return render_template("register.html")


@app.route("/change_password")
@auth.login_required
def page_change_password():
    return render_template("change_password.html")


# ============================================================
#  Auth API
# ============================================================

@app.route("/api/auth/me")
def api_auth_me():
    u = auth.current_user()
    if not u:
        return jsonify({"user": None}), 200
    # Don't ship password_hash even though current_user already strips it.
    out = {k: u[k] for k in ("id", "username", "email", "display_name", "role",
                              "is_active", "must_change_pw", "last_login_at")}
    # Surface per-staff preferences (reply_to_email so /settings can preload).
    full = auth.get_user(u["id"]) or {}
    out["reply_to_email"] = full.get("reply_to_email") or ""
    return jsonify({"user": out})


_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/api/auth/preferences", methods=["PATCH"])
@auth.login_required
def api_auth_set_preferences():
    """Update per-user preferences. Currently: reply_to_email.

    Body: {"reply_to_email": "summerlily@example.com"} (empty string clears).
    """
    payload = request.json or {}
    u = auth.current_user()
    if "reply_to_email" in payload:
        raw = (payload.get("reply_to_email") or "").strip()
        if raw and not _EMAIL_RE.match(raw):
            return jsonify({"error": "invalid_email"}), 400
        with db.tx() as c:
            c.execute(
                "UPDATE users SET reply_to_email=? WHERE id=?",
                (raw or None, u["id"]),
            )
        audit.log(u["id"], "user.set_reply_to", f"user:{u['id']}",
                  {"reply_to_email": raw or None})
    return jsonify({"ok": True})


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    if not auth.rate_limit("register", ip, max_hits=10, window_seconds=60):
        return jsonify({"error": "rate_limited"}), 429
    payload = request.json or {}
    username = (payload.get("username") or "").strip()
    email = contact.normalize_email(payload.get("email"))
    display_name = (payload.get("display_name") or "").strip()
    password = payload.get("password") or ""

    if not username or not password:
        return jsonify({"error": "missing_fields"}), 400
    if len(password) < auth.MIN_PASSWORD_LEN:
        return jsonify({"error": "weak_password"}), 400
    if auth.get_user_by_username(username):
        return jsonify({"error": "username_taken"}), 409
    if email and auth.get_user_by_email(email):
        return jsonify({"error": "email_taken"}), 409
    try:
        uid = auth.create_user(
            username=username, email=email, display_name=display_name or username,
            password=password, role="staff", is_active=0,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": uid, "pending_activation": True})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    payload = request.json or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    remember = bool(payload.get("remember"))

    # Rate limit by ip AND username (whichever trips first)
    # IP limit is the floor: 30/min is generous for an internal tool but still
    # caps a stupid bot. Per-username 5/min is the real brute-force protection.
    if not auth.rate_limit("login_ip", ip, max_hits=30, window_seconds=60):
        return jsonify({"error": "rate_limited"}), 429
    if not auth.rate_limit("login_user", username.lower(), max_hits=5, window_seconds=60):
        return jsonify({"error": "rate_limited"}), 429

    user = auth.get_user_by_username(username) if username else None
    if not user or not auth.verify_pw(password, user["password_hash"]):
        return jsonify({"error": "invalid_credentials"}), 401
    if not user["is_active"]:
        return jsonify({"error": "account_disabled"}), 403
    auth.login_user(user, remember=remember)
    auth.stamp_login(user["id"])
    auth.reset_rate("login_user", username.lower())
    return jsonify({
        "ok": True,
        "user": {k: user[k] for k in ("id", "username", "role", "display_name", "must_change_pw")},
    })


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    auth.logout_user()
    return jsonify({"ok": True})


@app.route("/api/auth/change_password", methods=["POST"])
@auth.login_required
def api_auth_change_password():
    u = auth.current_user()
    payload = request.json or {}
    old = payload.get("old") or ""
    new = payload.get("new") or ""
    if len(new) < auth.MIN_PASSWORD_LEN:
        return jsonify({"error": "weak_password"}), 400
    full = auth.get_user(u["id"])
    if not auth.verify_pw(old, full["password_hash"]):
        return jsonify({"error": "wrong_password"}), 400
    auth.set_password(u["id"], new, must_change=False)
    # Re-issue session so old cookies elsewhere are invalidated.
    auth.login_user(full)
    return jsonify({"ok": True})


# ============================================================
#  CRM: people-grouped browse + contact tracking
# ============================================================

@app.route("/api/people", methods=["POST"])
@auth.login_required
def api_people_create():
    """Manually add a customer. Any logged-in user can call this.

    Body: {
        email?, full_name?, status? (default 主动来找), owner?, tags?: list, notes?,
        handles?: [{platform: 'instagram'|'x'|'tiktok', username: str}, ...]
    }

    Need at least email OR one handle. Returns 409 if email or any handle
    collides with an existing record.

    Auth note: staff users get assigned_user_id = themselves automatically.
    Admin can override via payload.assigned_user_id (not exposed in the
    default UI; for migration scripts).
    """
    import json as _json
    u = auth.current_user()
    payload = request.json or {}
    email = contact.normalize_email(payload.get("email"))
    full_name = (payload.get("full_name") or "").strip()
    status = (payload.get("status") or "主动来找").strip()
    owner = (payload.get("owner") or "").strip()
    notes = (payload.get("notes") or "").strip()
    # Staff: auto-claim. Admin: respect explicit payload value, else None.
    if u["role"] == "staff":
        assigned_user_id: int | None = u["id"]
    else:
        raw_aid = payload.get("assigned_user_id")
        assigned_user_id = int(raw_aid) if (raw_aid not in (None, "", 0)) else None
    tags_in = payload.get("tags") or []
    if isinstance(tags_in, str):
        tags_in = [s.strip() for s in tags_in.split(",") if s.strip()]
    handles_in = payload.get("handles") or []
    valid_handles: list[tuple[str, str]] = []
    for h in handles_in:
        plat = (h.get("platform") or "").strip().lower()
        uname = (h.get("username") or "").strip().lstrip("@")
        if plat in {"instagram", "x", "tiktok"} and uname:
            valid_handles.append((plat, uname))

    if email and not contact.is_valid_email(email):
        return jsonify({"error": "invalid_email",
                        "detail": f"邮箱格式不对：{email!r}。应该形如 name@example.com（系统已自动剥掉 Email:/邮箱:/mailto: 这种前缀和首尾空格）"}), 400
    if not email and not valid_handles:
        return jsonify({"error": "需要至少填邮箱或一个平台 handle"}), 400

    now = int(time.time())
    with db.tx() as c:
        # Email collision check
        if email:
            row = c.execute("SELECT id, full_name FROM people WHERE email=?", (email,)).fetchone()
            if row:
                return jsonify({
                    "error": "email_exists",
                    "detail": f"邮箱 {email} 已属于客户「{row['full_name'] or '(未命名)'}」(id={row['id']})。如要更新请直接编辑那一行。",
                    "person_id": row["id"],
                }), 409

        # Handle collision check
        for plat, uname in valid_handles:
            placeholder = f"import:{uname.lower()}"
            row = c.execute(
                "SELECT pa.person_id, p.full_name, p.email FROM platform_accounts pa "
                "JOIN people p ON p.id = pa.person_id "
                "WHERE pa.platform=? AND pa.platform_user_id=?",
                (plat, placeholder),
            ).fetchone()
            if row:
                return jsonify({
                    "error": "handle_exists",
                    "detail": f"@{uname} ({plat}) 已属于客户「{row['full_name'] or '(未命名)'}」(email={row['email'] or '无'})。",
                    "person_id": row["person_id"],
                }), 409

        # Insert person
        cur = c.execute(
            "INSERT INTO people (email, full_name, status, owner, tags, notes, "
            "assigned_user_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (email, full_name, status, owner,
             _json.dumps(tags_in, ensure_ascii=False), notes,
             assigned_user_id, now, now),
        )
        pid = cur.lastrowid

        # Attach handles as placeholders (no follower count, can lazy-hydrate later)
        for plat, uname in valid_handles:
            db.attach_placeholder_handle(c, pid, plat, uname)

        person = dict(c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone())
        # Record initial status event for statistics
        db.record_status_event(c, pid, u["id"], None, status)

    audit.log(u["id"], "person.create", f"person:{pid}",
              {"email": email, "by_role": u["role"]})
    return jsonify({"person": person, "id": pid})


@app.route("/api/people/list")
@auth.login_required
def api_people_list():
    args = request.args
    u = auth.current_user()
    # staff: forced filter; admin: optional ?assigned_user_id=<n>|unassigned
    if u["role"] == "admin":
        raw = (args.get("assigned_user_id") or "").strip()
        if raw == "unassigned":
            assigned_filter = "unassigned"
        elif raw.isdigit():
            assigned_filter = int(raw)
        else:
            assigned_filter = None
        owner_param = (args.get("owner") or "").strip()
    else:
        assigned_filter = u["id"]
        owner_param = ""  # ignore staff-supplied owner
    include_rejected = (args.get("include_rejected") or "").lower() in ("1", "true", "yes")
    with db.tx() as c:
        rows, total = db.list_people(
            c,
            q=(args.get("q") or "").strip(),
            status=(args.get("status") or "").strip(),
            owner=owner_param,
            has_platform=(args.get("has_platform") or "").strip(),
            has_email=(args.get("has_email") or "").strip(),
            order=(args.get("order") or "last_contacted").strip(),
            limit=int(args.get("limit") or 100),
            offset=int(args.get("offset") or 0),
            assigned_user_id=assigned_filter,
            include_rejected=include_rejected,
        )
        owners = db.list_owners(c)
        statuses = db.list_statuses(c)
    return jsonify({"people": rows, "total": total, "owners": owners, "statuses": statuses})


@app.route("/api/people/match_ids")
@auth.login_required
def api_people_match_ids():
    """Same filters as /api/people/list but returns ONLY ids (with email).

    Used by "select all matching" — frontend gets all IDs across pages without
    having to paginate. Returns only people that have an email (otherwise can't
    receive bulk email anyway).
    """
    args = request.args
    u = auth.current_user()
    if u["role"] == "admin":
        raw = (args.get("assigned_user_id") or "").strip()
        assigned_filter = "unassigned" if raw == "unassigned" else (int(raw) if raw.isdigit() else None)
        owner_param = (args.get("owner") or "").strip()
    else:
        assigned_filter = u["id"]
        owner_param = ""
    with db.tx() as c:
        rows, total = db.list_people(
            c,
            q=(args.get("q") or "").strip(),
            status=(args.get("status") or "").strip(),
            owner=owner_param,
            has_platform=(args.get("has_platform") or "").strip(),
            has_email=(args.get("has_email") or "").strip(),
            order=(args.get("order") or "last_contacted").strip(),
            limit=100000,
            offset=0,
            assigned_user_id=assigned_filter,
        )
    with_email = [{"id": r["id"], "email": r["email"], "full_name": r.get("full_name")} for r in rows if r.get("email")]
    return jsonify({"ids": [r["id"] for r in with_email], "total_matching": total,
                    "with_email": len(with_email), "sample": with_email[:5]})


@app.route("/api/people/<int:person_id>", methods=["DELETE"])
@auth.admin_required
def api_people_delete(person_id: int):
    """Hard-delete a customer and all attached platform_accounts.

    Admin only — destructive and irreversible. The soft path is to mark them
    `不适合` via /api/people/<id>/reject, which keeps the row + bio embedding
    around so AI discovery can avoid them in future. Use this hard delete only
    when the entry was straight-up wrong (typo, test row, duplicate).
    """
    with db.tx() as c:
        row = c.execute(
            "SELECT id, full_name, email FROM people WHERE id=?", (person_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        n_accts = c.execute(
            "SELECT COUNT(*) FROM platform_accounts WHERE person_id=?", (person_id,)
        ).fetchone()[0]
        c.execute("DELETE FROM platform_accounts WHERE person_id=?", (person_id,))
        c.execute("DELETE FROM people WHERE id=?", (person_id,))
    audit.log(
        auth.current_user()["id"], "person.delete", f"person:{person_id}",
        {"full_name": row["full_name"], "email": row["email"],
         "accounts_deleted": n_accts},
    )
    return jsonify({"deleted": True, "accounts_deleted": n_accts})


@app.route("/api/people/<int:target_id>/merge/<int:source_id>", methods=["POST"])
@auth.admin_required
def api_people_merge(target_id: int, source_id: int):
    """Fold `source_id` into `target_id`: move source's platform_accounts,
    follow-ups, reminders and email history onto target, merge person fields
    (target wins, source fills blanks), then delete source.

    Admin only — destructive and irreversible, like the hard delete. Used by
    the 'add handle → already belongs to someone' conflict, where the operator
    chooses to absorb the duplicate into the card they're curating.
    """
    if target_id == source_id:
        return jsonify({"error": "same_person",
                        "detail": "源和目标是同一个客户"}), 400
    with db.tx() as c:
        t = c.execute("SELECT id, full_name, email FROM people WHERE id=?", (target_id,)).fetchone()
        s = c.execute("SELECT id, full_name, email FROM people WHERE id=?", (source_id,)).fetchone()
        if not t or not s:
            return jsonify({"error": "not_found",
                            "detail": "源或目标客户不存在（可能已被合并）"}), 404
        ok = db.merge_persons(c, target_id, source_id)
    if not ok:
        return jsonify({"error": "merge_failed"}), 400
    audit.log(
        auth.current_user()["id"], "person.merge", f"person:{target_id}",
        {"source_id": source_id, "source_name": s["full_name"],
         "source_email": s["email"]},
    )
    return jsonify({"merged": True, "target_id": target_id, "source_id": source_id})


@app.route("/api/people/<int:person_id>", methods=["GET"])
@auth.login_required
def api_people_get(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    with db.tx() as c:
        row = db.get_person(c, person_id)
        if not row:
            return jsonify({"error": "not_found"}), 404
        person = dict(row)
        handles = [
            dict(r) for r in c.execute(
                "SELECT id, platform, platform_user_id, username, full_name, "
                "follower_count, follower_count AS followers, biography, source, "
                "is_rejected FROM platform_accounts WHERE person_id=? "
                "ORDER BY platform, id", (person_id,),
            ).fetchall()
        ]
    person["handles"] = handles
    return jsonify({"person": person})


@app.route("/api/people/<int:person_id>/hydrate", methods=["POST"])
@auth.login_required
def api_people_hydrate(person_id: int):
    if not auth.user_owns_person(auth.current_user(), person_id):
        return jsonify({"error": "forbidden"}), 403
    """Pull real data from each platform for this person's handles.

    Works on both placeholder rows (`import:*`) and already-real ones (refresh).
    On per-handle success: row updated in-place + bio embedding regenerated.
    Returns per-handle results so the UI can show partial-success.
    """
    import json as _json
    import sqlite3 as _sql
    with db.tx() as c:
        if not db.get_person(c, person_id):
            return jsonify({"error": "not found"}), 404
        rows = c.execute(
            "SELECT id, platform, username, platform_user_id FROM platform_accounts WHERE person_id=?",
            (person_id,),
        ).fetchall()

    if not rows:
        return jsonify({"error": "no_handles", "detail": "这个客户没有平台 handle，无法拉取"}), 400

    results = []
    fresh_ids: list[int] = []
    for r in rows:
        plat = r["platform"]
        username = (r["username"] or "").strip().lstrip("@")
        if not username or "#" in username or " " in username:
            results.append({"platform": plat, "username": r["username"], "ok": False, "error": "无效 handle"})
            continue
        if plat not in INFO_CAPABLE:
            results.append({"platform": plat, "username": username, "ok": False, "error": f"{plat} 不支持 fetch"})
            continue
        try:
            full = get_platform(plat).fetch_user_info(username)
        except Exception as e:
            results.append({"platform": plat, "username": username, "ok": False, "error": str(e)[:200]})
            continue
        real_pid = str(full.get("platform_user_id") or "")
        if not real_pid:
            results.append({"platform": plat, "username": username, "ok": False, "error": "上游未返回 user id"})
            continue

        bio_email = (full.get("email") or "").strip().lower() or None
        try:
            with db.tx() as c:
                c.execute(
                    """UPDATE platform_accounts
                          SET platform_user_id=?, secondary_id=?, username=?, full_name=?,
                              biography=?, follower_count=?, following_count=?, post_count=?,
                              video_count=?, is_verified=?, is_private=?, is_business=?,
                              category=?, profile_pic_url=?, external_url=?, bio_links=?,
                              email=?, phone=?, city=?, address=?, country=?,
                              raw=?, source=?, updated_at=?
                        WHERE id=?""",
                    (
                        real_pid, full.get("secondary_id"),
                        full.get("username", ""), full.get("full_name", ""),
                        full.get("biography", ""),
                        full.get("follower_count"), full.get("following_count"),
                        full.get("post_count"), full.get("video_count"),
                        1 if full.get("is_verified") else 0,
                        1 if full.get("is_private") else 0,
                        1 if full.get("is_business") else 0,
                        full.get("category", ""), full.get("profile_pic_url", ""),
                        full.get("external_url", ""),
                        _json.dumps(full.get("bio_links") or [], ensure_ascii=False),
                        bio_email, full.get("phone", ""),
                        full.get("city", ""), full.get("address", ""), full.get("country", ""),
                        _json.dumps(full.get("raw") or {}, ensure_ascii=False),
                        "manual:hydrate-on-demand", int(time.time()),
                        r["id"],
                    ),
                )
                # Propagate bio-email to person row when person currently has no email.
                # If bio_email already belongs to a DIFFERENT person → that means we found
                # the same real human under two person rows (e.g. CRM had email-only entry
                # and similarity later attached an IG handle to a separate person).
                # Auto-merge them: keep the email-owner as canonical, fold the just-hydrated
                # rows into them, delete the loser.
                if bio_email:
                    try:
                        c.execute(
                            "UPDATE people SET email=?, updated_at=? "
                            "WHERE id=? AND (email IS NULL OR email='')",
                            (bio_email, int(time.time()), person_id),
                        )
                    except _sql.IntegrityError:
                        owner_row = c.execute(
                            "SELECT id FROM people WHERE LOWER(email) = LOWER(?)", (bio_email,)
                        ).fetchone()
                        if owner_row and owner_row["id"] != person_id:
                            db.merge_persons(c, owner_row["id"], person_id)
                            person_id = owner_row["id"]
        except _sql.IntegrityError:
            # The real platform_user_id already lives on another row (i.e. another
            # person has this exact IG account). Same real artist, two person rows
            # → merge them: drop our placeholder, fold this person's other handles
            # into the canonical owner.
            with db.tx() as c2:
                other = c2.execute(
                    "SELECT person_id FROM platform_accounts WHERE platform=? AND platform_user_id=?",
                    (plat, real_pid),
                ).fetchone()
                if other and other["person_id"] != person_id:
                    c2.execute("DELETE FROM platform_accounts WHERE id=?", (r["id"],))
                    db.merge_persons(c2, other["person_id"], person_id)
                    results.append({
                        "platform": plat, "username": username, "ok": True,
                        "merged_into_person": other["person_id"],
                    })
                    person_id = other["person_id"]
                    continue
            results.append({
                "platform": plat, "username": username, "ok": False,
                "error": "另一条记录已用同一个 platform_user_id（可能是重复账号）",
            })
            continue

        fresh_ids.append(r["id"])
        results.append({
            "platform": plat, "username": username, "ok": True,
            "follower_count": full.get("follower_count"),
            "is_verified": bool(full.get("is_verified")),
            "full_name": full.get("full_name"),
        })

    if fresh_ids:
        try:
            ingest._embed_accounts(fresh_ids)
        except Exception:
            pass

    ok = sum(1 for r in results if r["ok"])
    return jsonify({"results": results, "ok": ok, "total": len(results)})


@app.route("/api/people/<int:person_id>/contact", methods=["POST"])
@auth.login_required
def api_people_contact(person_id: int):
    """Stamp last_contacted_at and (optionally) status. Body: {"status": "contacted"}"""
    if not auth.user_owns_person(auth.current_user(), person_id):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    set_status = payload.get("status") or None
    if set_status == "不适合":
        return jsonify({"error": "forbidden_field", "field": "status",
                        "detail": "use /api/people/<id>/reject"}), 400
    with db.tx() as c:
        person = db.record_contact(c, person_id, set_status=set_status)
        if person.get("_old_status") and set_status:
            db.record_status_event(c, person_id, auth.current_user()["id"],
                                   person["_old_status"], set_status)
        person.pop("_old_status", None)
    return jsonify({"person": person})


# ============================================================
# Follow-up logs — free-text "what did we say" entries per customer.
# Surfaced as a timeline in the customer drawer.
# ============================================================

@app.route("/api/people/<int:person_id>/followups", methods=["GET"])
@auth.login_required
def api_people_followups_list(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    with db.tx() as c:
        rows = c.execute(
            """SELECT f.id, f.person_id, f.actor_id, f.content, f.created_at,
                      u.username AS actor_username, u.display_name AS actor_display_name
                 FROM followup_logs f
                 LEFT JOIN users u ON u.id = f.actor_id
                WHERE f.person_id = ?
             ORDER BY f.created_at DESC""",
            (person_id,),
        ).fetchall()
    return jsonify({"followups": [dict(r) for r in rows]})


@app.route("/api/people/<int:person_id>/followups", methods=["POST"])
@auth.login_required
def api_people_followups_add(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    content = (payload.get("content") or "").strip()
    if not content:
        return jsonify({"error": "empty_content"}), 400
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO followup_logs (person_id, actor_id, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (person_id, u["id"], content, now),
        )
        # Also stamp last_contacted_at — logging a follow-up == we talked to them
        c.execute(
            "UPDATE people SET last_contacted_at=?, updated_at=? WHERE id=?",
            (now, now, person_id),
        )
        fid = cur.lastrowid
    audit.log(u["id"], "followup.add", f"person:{person_id}", {"followup_id": fid})
    return jsonify({"ok": True, "followup_id": fid, "created_at": now})


@app.route("/api/people/followups/<int:followup_id>", methods=["DELETE"])
@auth.login_required
def api_people_followup_delete(followup_id: int):
    u = auth.current_user()
    with db.tx() as c:
        row = c.execute(
            "SELECT person_id, actor_id FROM followup_logs WHERE id=?",
            (followup_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        # Only the original author OR admin can delete
        if u["role"] != "admin" and row["actor_id"] != u["id"]:
            return jsonify({"error": "forbidden"}), 403
        c.execute("DELETE FROM followup_logs WHERE id=?", (followup_id,))
    audit.log(u["id"], "followup.delete", f"followup:{followup_id}",
              {"person_id": row["person_id"]})
    return jsonify({"ok": True})


# ============================================================
# Reminders — scheduled "ping me to follow up" entries.
# A banner on every page polls /api/reminders/due for the current user.
# ============================================================

@app.route("/api/people/<int:person_id>/reminders", methods=["POST"])
@auth.login_required
def api_people_reminder_add(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    remind_at = payload.get("remind_at")  # epoch seconds
    content = (payload.get("content") or "").strip()
    try:
        remind_at = int(remind_at)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_remind_at"}), 400
    if remind_at < int(time.time()) - 60:
        return jsonify({"error": "past_time", "detail": "提醒时间不能早于现在"}), 400
    now = int(time.time())
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO reminders (person_id, actor_id, remind_at, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, u["id"], remind_at, content, now),
        )
        rid = cur.lastrowid
    audit.log(u["id"], "reminder.add", f"person:{person_id}",
              {"reminder_id": rid, "remind_at": remind_at})
    return jsonify({"ok": True, "reminder_id": rid})


@app.route("/api/people/<int:person_id>/reminders", methods=["GET"])
@auth.login_required
def api_people_reminders_for_person(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    with db.tx() as c:
        rows = c.execute(
            "SELECT id, person_id, actor_id, remind_at, content, dismissed_at, created_at "
            "FROM reminders WHERE person_id=? ORDER BY remind_at DESC",
            (person_id,),
        ).fetchall()
    return jsonify({"reminders": [dict(r) for r in rows]})


@app.route("/api/reminders/due")
@auth.login_required
def api_reminders_due():
    """Return all due-and-not-dismissed reminders the current user owns."""
    u = auth.current_user()
    now = int(time.time())
    with db.tx() as c:
        rows = c.execute(
            """SELECT r.id, r.person_id, r.actor_id, r.remind_at, r.content,
                      r.created_at, p.full_name AS person_name, p.email AS person_email
                 FROM reminders r
                 JOIN people  p ON p.id = r.person_id
                WHERE r.dismissed_at IS NULL
                  AND r.remind_at <= ?
                  AND r.actor_id = ?
             ORDER BY r.remind_at ASC""",
            (now, u["id"]),
        ).fetchall()
    return jsonify({"reminders": [dict(r) for r in rows], "as_of": now})


@app.route("/api/reminders/<int:reminder_id>/dismiss", methods=["POST"])
@auth.login_required
def api_reminder_dismiss(reminder_id: int):
    u = auth.current_user()
    with db.tx() as c:
        row = c.execute("SELECT actor_id, person_id FROM reminders WHERE id=?",
                        (reminder_id,)).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        if u["role"] != "admin" and row["actor_id"] != u["id"]:
            return jsonify({"error": "forbidden"}), 403
        c.execute("UPDATE reminders SET dismissed_at=? WHERE id=?",
                  (int(time.time()), reminder_id))
    audit.log(u["id"], "reminder.dismiss", f"reminder:{reminder_id}",
              {"person_id": row["person_id"]})
    return jsonify({"ok": True})


@app.route("/api/people/<int:person_id>/reject", methods=["POST"])
@auth.login_required
def api_people_reject(person_id: int):
    """Mark a person + all their platform_accounts as rejected (the negative-sample pool)."""
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    reason = (payload.get("reason") or "").strip() or None
    now = int(time.time())
    with db.tx() as c:
        p = db.get_person(c, person_id)
        if not p:
            return jsonify({"error": "forbidden"}), 403
        old_status = p["status"]
        prev = p["status"] if p["status"] != "不适合" else (p["previous_status"] or "待联系")
        c.execute(
            "UPDATE people SET previous_status=?, status='不适合', updated_at=? WHERE id=?",
            (prev, now, person_id),
        )
        c.execute(
            """UPDATE platform_accounts SET is_rejected=1, rejection_reason=?, rejected_by=?, rejected_at=?, updated_at=?
                WHERE person_id=?""",
            (reason, u["id"], now, now, person_id),
        )
        # Record status change event
        if old_status != "不适合":
            db.record_status_event(c, person_id, u["id"], old_status, "不适合")
    audit.log(u["id"], "person.reject", f"person:{person_id}", {"reason": reason})
    return jsonify({"ok": True})


@app.route("/api/people/<int:person_id>/unreject", methods=["POST"])
@auth.login_required
def api_people_unreject(person_id: int):
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    now = int(time.time())
    with db.tx() as c:
        p = db.get_person(c, person_id)
        if not p:
            return jsonify({"error": "forbidden"}), 403
        restore_status = p["previous_status"] or "待联系"
        c.execute(
            "UPDATE people SET status=?, previous_status=NULL, updated_at=? WHERE id=?",
            (restore_status, now, person_id),
        )
        c.execute(
            """UPDATE platform_accounts SET is_rejected=0, rejection_reason=NULL,
                      rejected_by=NULL, rejected_at=NULL, updated_at=?
                WHERE person_id=?""",
            (now, person_id),
        )
        # Record status change event (from 不适合 back to previous status)
        db.record_status_event(c, person_id, u["id"], "不适合", restore_status)
    audit.log(u["id"], "person.unreject", f"person:{person_id}")
    return jsonify({"ok": True})


@app.route("/api/people/bulk_assign", methods=["POST"])
@auth.admin_required
def api_people_bulk_assign():
    """Body: {ids: [int], assigned_user_id: int|null}"""
    payload = request.json or {}
    ids = list(payload.get("ids") or [])
    target = payload.get("assigned_user_id")
    if target in ("", 0):
        target = None
    if not ids:
        return jsonify({"error": "no_ids"}), 400
    if target is not None:
        u = auth.get_user(int(target))
        if not u or not u["is_active"]:
            return jsonify({"error": "invalid_user"}), 400
    now = int(time.time())
    with db.tx() as c:
        qmarks = ",".join("?" * len(ids))
        cur = c.execute(
            f"UPDATE people SET assigned_user_id=?, updated_at=? WHERE id IN ({qmarks})",
            [target, now, *ids],
        )
        updated = cur.rowcount
    audit.log(auth.current_user()["id"], "person.bulk_assign", None,
              {"count": updated, "assigned_user_id": target})
    return jsonify({"updated": updated})


# ============================================================
#  Bulk email
# ============================================================

EMAIL_JOBS: dict[str, dict] = {}    # job_id -> live job dict (in-memory mirror of DB row)
EMAIL_BATCH_MAX = 100


def _save_email_job(job: dict, created_by: int) -> None:
    with db.tx() as c:
        c.execute(
            """INSERT INTO email_jobs (id, created_by, created_at, finished_at, status, payload_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 finished_at=excluded.finished_at,
                 status=excluded.status,
                 payload_json=excluded.payload_json""",
            (job["id"], created_by, int(job.get("started_at") or time.time()),
             int(job["finished_at"]) if job.get("finished_at") else None,
             job["status"], _json_mod.dumps(job, ensure_ascii=False)),
        )


def _load_email_job(job_id: str) -> tuple[dict | None, int | None]:
    """Return (job_dict, created_by) or (None, None) if missing."""
    if job_id in EMAIL_JOBS:
        # We don't reliably know created_by from memory — reach into DB to be safe.
        with db.tx() as c:
            r = c.execute("SELECT created_by FROM email_jobs WHERE id=?", (job_id,)).fetchone()
        return EMAIL_JOBS[job_id], (r["created_by"] if r else None)
    with db.tx() as c:
        r = c.execute("SELECT created_by, payload_json FROM email_jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None, None
    try:
        return _json_mod.loads(r["payload_json"]), r["created_by"]
    except Exception:
        return None, r["created_by"]


def _gather_recipients(person_ids: list[int]) -> list[dict]:
    """Fetch person + handles in one shot, preserving input order."""
    if not person_ids:
        return []
    with db.tx() as c:
        rows = db.list_people(c, limit=len(person_ids), offset=0)
        # list_people doesn't filter by id; do it ourselves
        qmarks = ",".join("?" * len(person_ids))
        people = c.execute(
            f"SELECT id, email, full_name, status, owner FROM people WHERE id IN ({qmarks})",
            person_ids,
        ).fetchall()
        people_by_id = {p["id"]: dict(p) for p in people}
        handle_rows = c.execute(
            f"SELECT person_id, platform, username FROM platform_accounts WHERE person_id IN ({qmarks})",
            person_ids,
        ).fetchall()
    handles_by_pid: dict[int, list[dict]] = {}
    for h in handle_rows:
        handles_by_pid.setdefault(h["person_id"], []).append(
            {"platform": h["platform"], "username": h["username"]}
        )
    out = []
    for pid in person_ids:
        p = people_by_id.get(pid)
        if not p:
            continue
        p["handles"] = handles_by_pid.get(pid, [])
        out.append(p)
    return out


@app.route("/api/email/polish", methods=["POST"])
@auth.login_required
def api_email_polish():
    """Body: {subject, body}. Returns {subject, body} polished by LLM.

    Preserves {{name}} / {{email}} / {{owner}} / {{handle_*}} placeholders intact.
    Tuned for MyCraftZone-style creator outreach (custom plushie services).
    """
    payload = request.json or {}
    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()
    if not subject and not body:
        return jsonify({"error": "subject or body required"}), 400

    system_prompt = """You polish cold-outreach emails for MyCraftZone, a company that turns artists' artwork into custom plushies (no MOQ, free extras like keychains/badges, worldwide shipping). The audience is independent illustrators, comic artists, and indie creators.

Tone goals:
- Warm, specific, professional. Sound like a real person, not a template.
- Briefly compliment the recipient's work (referencing their handle if available via {{handle_instagram}} or {{handle_x}}).
- One concrete value prop: turn artwork into plushies, no MOQ, free extras.
- One light social proof line if natural (e.g. "we've worked with 2,000+ creators").
- One clear, low-pressure CTA inviting reply.
- Keep it tight: 100–160 words.

CRITICAL RULES:
1. Keep these placeholders EXACTLY as written, do not modify or remove: {{name}} {{email}} {{owner}} {{handle_instagram}} {{handle_x}}
2. Output strict JSON, no commentary: {"subject": "...", "body": "..."}
3. Body should be plain text with line breaks (\\n). No HTML, no markdown.
4. End with a short signature: "Echo Fan / MyCraftZone" unless the user clearly wrote a different signature."""

    user_prompt = f"Polish this email. Keep all {{...}} placeholders unchanged.\n\nSubject: {subject}\n\nBody:\n{body}"

    try:
        raw = ai._chat(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.5,
        )
        raw = ai._strip_codefence(raw)
        import json as _json
        data = _json.loads(raw)
        new_subject = (data.get("subject") or "").strip() or subject
        new_body = (data.get("body") or "").strip() or body
    except Exception as e:
        return jsonify({"error": f"polish failed: {e}"}), 500

    return jsonify({"subject": new_subject, "body": new_body})


@app.route("/api/email/preview", methods=["POST"])
@auth.login_required
def api_email_preview():
    """Body: {person_ids:[int], subject, body}. Returns first 3 rendered messages.

    Soft-filters not-owned ids: silently drops them rather than returning 400,
    so a batch that mixes owned + not-owned doesn't make the whole preview
    pane go empty (the actual send_batch still enforces ownership strictly).
    Returns notice.not_owned so the UI can warn if needed.
    """
    payload = request.json or {}
    person_ids = list(payload.get("person_ids") or [])
    subject_t = payload.get("subject") or ""
    body_t = payload.get("body") or ""
    owned, not_owned = auth.filter_owned_person_ids(auth.current_user(), person_ids)
    # Only render the first 3 owned recipients (preview shouldn't be expensive).
    recipients = _gather_recipients(owned[:3])
    out = []
    for r in recipients:
        out.append({
            "person_id": r["id"],
            "to": r.get("email"),
            "name": r.get("full_name"),
            "subject": mailer.render(subject_t, r, r.get("handles")),
            "body": mailer.render(body_t, r, r.get("handles")),
        })
    return jsonify({"previews": out, "not_owned": not_owned})


@app.route("/api/email/send_batch", methods=["POST"])
@auth.login_required
def api_email_send_batch():
    """Body: {person_ids:[int], subject, body, throttle_seconds?}.
    Kicks off a background job; returns job_id immediately. Poll /api/email/job/<id>.
    """
    payload = request.json or {}
    person_ids = list(payload.get("person_ids") or [])
    subject_t = (payload.get("subject") or "").strip()
    body_t = (payload.get("body") or "").strip()
    throttle = max(0.0, float(payload.get("throttle_seconds") or 3.0))

    if not person_ids:
        return jsonify({"error": "no recipients"}), 400
    if len(person_ids) > EMAIL_BATCH_MAX:
        return jsonify({"error": f"single batch capped at {EMAIL_BATCH_MAX}"}), 400
    if not subject_t or not body_t:
        return jsonify({"error": "subject and body required"}), 400

    u = auth.current_user()
    owned, not_owned = auth.filter_owned_person_ids(u, person_ids)
    if not_owned:
        return jsonify({"error": "forbidden_ids", "ids": not_owned}), 400

    # Verify SMTP is configured before queuing
    for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"):
        if not settings.get(k):
            return jsonify({"error": f"missing setting: {k} — fill it on /settings"}), 400

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "running",
        "total": len(owned),
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "log": [],
        "started_at": time.time(),
        "finished_at": None,
        "throttle": throttle,
        "created_by": u["id"],
        # Persisted inputs so a gunicorn restart doesn't leave us with
        # an irrecoverable job (caught on 2026-05-22: deploy at minute 2
        # of a 100-recipient batch killed the worker; payload_json had
        # no person_ids/subject/body so we couldn't resume). These three
        # fields are write-once and treated as immutable for the job's life.
        "person_ids": list(owned),
        "subject_t": subject_t,
        "body_t": body_t,
    }
    EMAIL_JOBS[job_id] = job
    _save_email_job(job, u["id"])

    # Resolve sender's per-staff Reply-To so the recipients' replies land in
    # the actual sender's inbox rather than the shared SMTP_FROM_EMAIL one.
    sender_full = auth.get_user(u["id"]) or {}
    sender_reply_to = (sender_full.get("reply_to_email") or "").strip() or None

    threading.Thread(
        target=_run_email_job,
        args=(job, owned, subject_t, body_t, throttle, u["id"], sender_reply_to),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "total": len(owned)})


def _run_email_job(job: dict, person_ids: list[int], subject_t: str, body_t: str,
                   throttle: float, created_by: int,
                   sender_reply_to: str | None = None) -> None:
    recipients = _gather_recipients(person_ids)
    rec_by_id = {r["id"]: r for r in recipients}
    for i, pid in enumerate(person_ids):
        r = rec_by_id.get(pid)
        if not r:
            job["skipped"] += 1
            job["log"].append({"person_id": pid, "ok": False, "error": "person not found"})
        elif not r.get("email"):
            job["skipped"] += 1
            job["log"].append({"person_id": pid, "ok": False, "error": "无邮箱"})
        else:
            try:
                subject = mailer.render(subject_t, r, r.get("handles"))
                body = mailer.render(body_t, r, r.get("handles"))
                mailer.send_one(r["email"], subject, body, reply_to=sender_reply_to)
                job["sent"] += 1
                job["log"].append({"person_id": pid, "email": r["email"], "ok": True})
                with db.tx() as c:
                    result = db.record_contact(c, pid, set_status="已联系")
                    if result.get("_old_status"):
                        db.record_status_event(c, pid, created_by,
                                               result["_old_status"], "已联系")
            except Exception as e:
                job["failed"] += 1
                job["log"].append({"person_id": pid, "email": r.get("email"), "ok": False, "error": str(e)[:300]})
        if i + 1 < len(person_ids) and throttle > 0:
            time.sleep(throttle)
        # Periodic checkpoint so polling sees progress even after restart.
        if i % 5 == 0:
            _save_email_job(job, created_by)
    job["status"] = "done"
    job["finished_at"] = time.time()
    _save_email_job(job, created_by)


@app.route("/api/email/job/<job_id>")
@auth.login_required
def api_email_job(job_id: str):
    u = auth.current_user()
    job, created_by = _load_email_job(job_id)
    if not job:
        return jsonify({"error": "forbidden"}), 403
    if u["role"] != "admin" and created_by != u["id"]:
        return jsonify({"error": "forbidden"}), 403
    # Tell the UI whether resume is possible (interrupted + we have the
    # full inputs saved). Old jobs from before 2026-05-22 are interrupted
    # but irrecoverable because person_ids/subject_t/body_t weren't saved.
    if job.get("status") == "interrupted":
        sent_ids = {e.get("person_id") for e in job.get("log", []) if e.get("ok")}
        remaining = [pid for pid in (job.get("person_ids") or []) if pid not in sent_ids]
        job["resume_possible"] = bool(job.get("person_ids")) and bool(job.get("subject_t")) and bool(job.get("body_t"))
        job["remaining_count"] = len(remaining)
    return jsonify(job)


@app.route("/api/email/job/<job_id>/resume", methods=["POST"])
@auth.login_required
def api_email_job_resume(job_id: str):
    """Re-spawn an interrupted send: skip recipients already in the log
    with ok=True, send to the rest using the originally-saved subject/body
    templates. Atomic CAS prevents two concurrent resumes."""
    u = auth.current_user()
    job, created_by = _load_email_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if u["role"] != "admin" and created_by != u["id"]:
        return jsonify({"error": "forbidden"}), 403
    if job.get("status") != "interrupted":
        return jsonify({"error": "not_interrupted", "detail": f"job status is {job.get('status')}"}), 400
    if not (job.get("person_ids") and job.get("subject_t") and job.get("body_t")):
        return jsonify({"error": "not_resumable",
                        "detail": "老 job 没保存原始 person_ids/subject/body，不能 resume；只能新建一个 batch"}), 400

    # Atomic CAS: only one thread can claim. Re-read row after winning.
    with db.tx() as c:
        cur = c.execute(
            "UPDATE email_jobs SET status='running' WHERE id=? AND status='interrupted'",
            (job_id,),
        )
        if cur.rowcount != 1:
            return jsonify({"error": "race", "detail": "another resume already in flight"}), 409

    # Re-read fresh state from DB to be sure
    job, created_by = _load_email_job(job_id)
    # CAS only updated the `status` column; payload_json still has the old
    # 'interrupted' value baked in. Sync them: set status='running' in the
    # dict AND persist back so the next poll (which reads payload_json) sees
    # 'running' even before the worker thread's first checkpoint.
    job["status"] = "running"
    job["finished_at"] = None
    job.pop("interrupted_at", None)
    job.pop("interrupted_reason", None)
    _save_email_job(job, created_by)
    sent_ids = {e.get("person_id") for e in job.get("log", []) if e.get("ok")}
    remaining = [pid for pid in (job.get("person_ids") or []) if pid not in sent_ids]
    if not remaining:
        job["status"] = "done"
        job["finished_at"] = time.time()
        _save_email_job(job, created_by)
        return jsonify({"ok": True, "remaining": 0})

    # Re-filter remaining against the resumer's auth scope (don't let staff
    # cross-mail people that got reassigned away after the original send).
    owned, _ = auth.filter_owned_person_ids(u, remaining)
    if not owned:
        job["status"] = "interrupted"
        _save_email_job(job, created_by)
        return jsonify({"error": "no_owned", "detail": "剩余收件人都不在你的分配下了"}), 400

    sender_full = auth.get_user(u["id"]) or {}
    sender_reply_to = (sender_full.get("reply_to_email") or "").strip() or None
    EMAIL_JOBS[job_id] = job
    threading.Thread(
        target=_run_email_job,
        args=(job, owned, job["subject_t"], job["body_t"],
              float(job.get("throttle") or 3.0), created_by, sender_reply_to),
        daemon=True,
    ).start()
    audit.log(u["id"], "email_job.resume", f"job:{job_id}",
              {"remaining": len(owned)})
    return jsonify({"ok": True, "remaining": len(owned)})


def _sweep_stale_running_email_jobs() -> int:
    """Called once at module load (before gunicorn forks workers). Any job
    sitting in 'running' status from a previous process is a zombie — its
    worker thread died with the old master. Mark them 'interrupted' so the
    UI stops lying and the user can /resume."""
    try:
        with db.tx() as c:
            rows = c.execute(
                "SELECT id, payload_json FROM email_jobs WHERE status='running'"
            ).fetchall()
            if not rows:
                return 0
            n = 0
            for r in rows:
                try:
                    p = _json_mod.loads(r["payload_json"])
                except Exception:
                    p = {}
                p["status"] = "interrupted"
                # NOTE: we do NOT set finished_at — the job is paused, not done
                p["interrupted_at"] = int(time.time())
                p["interrupted_reason"] = "process restart"
                c.execute(
                    "UPDATE email_jobs SET status='interrupted', payload_json=? WHERE id=?",
                    (_json_mod.dumps(p, ensure_ascii=False), r["id"]),
                )
                n += 1
            return n
    except Exception as e:
        # Don't block app startup; just log to stderr
        print(f"[email_job sweep] error: {e}", flush=True)
        return 0


# Run the sweep at module load, BEFORE gunicorn workers fork (we use
# --preload, so this runs once in the master process). Safe for our
# single-node deploy; would need extra coordination for HUP-style reloads.
_swept = _sweep_stale_running_email_jobs()
if _swept:
    print(f"[boot] swept {_swept} zombie email_jobs → status='interrupted'", flush=True)


# ============================================================
#  Settings (API keys etc.)
# ============================================================

@app.route("/api/settings", methods=["GET"])
@auth.admin_required
def api_settings_get():
    """Return all settings. Secrets are masked."""
    return jsonify({
        "settings": settings.all_settings(unmasked=False),
        "configured": {
            k: bool(settings.get(k))
            for k in settings.SECRET_KEYS
        },
    })


@app.route("/api/settings", methods=["POST"])
@auth.admin_required
def api_settings_set():
    """Body: {KEY: value, ...}. Masked secret values are ignored (kept as-is)."""
    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "expected object"}), 400
    written = settings.set_many(payload)
    audit.log(auth.current_user()["id"], "settings.write", None,
              {"keys": list(written.keys())})
    return jsonify({"written_keys": list(written.keys())})


@app.route("/api/settings/test", methods=["POST"])
@auth.admin_required
def api_settings_test():
    """Run a real, minimal call against the configured provider to validate it.

    Body: {"provider": "openrouter_chat" | "openrouter_embed" | "tikhub"}
    Tests use whatever's currently *saved* — POST settings first if you want
    to test a freshly-typed value.
    """
    payload = request.json or {}
    provider = (payload.get("provider") or "").strip()
    t0 = time.perf_counter()
    try:
        if provider == "openrouter_chat":
            out = ai._chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                temperature=0.0,
            )
            detail = f"chat ok — model said: {out[:80]!r}"
        elif provider == "openrouter_embed":
            vec = ai.embed_text("hello world")
            detail = f"embedding ok — got {len(vec)}-dim vector"
        elif provider == "tikhub":
            info = get_platform("instagram").fetch_user_info("instagram")
            followers = info.get("follower_count")
            detail = f"tikhub ok — fetched @instagram ({followers:,} followers)" if followers else "tikhub ok"
        elif provider == "smtp":
            to = (payload.get("to") or "").strip()
            if not to:
                return jsonify({"ok": False, "detail": "测试目标邮箱必填（建议填你自己的邮箱）"}), 400
            mailer.send_one(
                to,
                "Scout SMTP 测试邮件",
                "如果你收到这封邮件，说明 SMTP 配置正确，可以开始群发了。",
            )
            detail = f"测试邮件已发往 {to}"
        else:
            return jsonify({"ok": False, "detail": f"unknown provider: {provider!r}"}), 400
    except Exception as e:
        return jsonify({
            "ok": False,
            "detail": str(e)[:400],
            "ms": int((time.perf_counter() - t0) * 1000),
        })
    return jsonify({
        "ok": True,
        "detail": detail,
        "ms": int((time.perf_counter() - t0) * 1000),
    })


# ============================================================
#  DB browse / list / single
# ============================================================

@cache.cached(timeout=30, key_prefix='db_stats')
def _stats() -> dict:
    with db.tx() as c:
        return db.count_accounts(c)


@app.route("/api/db/stats")
@auth.login_required
def api_db_stats():
    return jsonify(_stats())


# ============================================================
# Statistics / Dashboard API
# ============================================================

@app.route("/api/stats")
@auth.login_required
def api_stats():
    """Comprehensive stats for the dashboard.

    Query params:
      range: today | yesterday | week | month (default: today)
      start, end: custom unix timestamps (override range)
      user_id: filter to specific user (admin only; staff always sees own)
    """
    import datetime
    u = auth.current_user()
    args = request.args

    # Parse date range
    range_preset = (args.get("range") or "today").strip()
    now = datetime.datetime.now()

    if "start" in args and "end" in args:
        start_ts = int(args["start"])
        end_ts = int(args["end"])
    else:
        if range_preset == "today":
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(day_start.timestamp())
            end_ts = start_ts + 86400
        elif range_preset == "yesterday":
            yesterday = now - datetime.timedelta(days=1)
            day_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(day_start.timestamp())
            end_ts = start_ts + 86400
        elif range_preset == "week":
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(day_start.timestamp()) - 6 * 86400
            end_ts = int(day_start.timestamp()) + 86400
        elif range_preset == "month":
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(day_start.timestamp()) - 29 * 86400
            end_ts = int(day_start.timestamp()) + 86400
        else:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(day_start.timestamp())
            end_ts = start_ts + 86400

    # Determine user filter
    target_user_id = None
    if u["role"] != "admin":
        target_user_id = u["id"]  # staff always sees own data only
    else:
        raw_uid = (args.get("user_id") or "").strip()
        if raw_uid and raw_uid != "all":
            target_user_id = int(raw_uid)

    with db.tx() as c:
        data = db.get_daily_overview(c, start_ts, end_ts, user_id=target_user_id)

    data["range"] = {"start": start_ts, "end": end_ts, "preset": range_preset}
    return jsonify(data)


@app.route("/api/stats/trends")
@auth.login_required
def api_stats_trends():
    """Daily trend data for charts.

    Query params:
      days: number of days to look back (default 7, max 90)
      user_id: filter to specific user (admin only)
    """
    u = auth.current_user()
    args = request.args
    days = min(int(args.get("days") or 7), 90)

    target_user_id = None
    if u["role"] != "admin":
        target_user_id = u["id"]
    else:
        raw_uid = (args.get("user_id") or "").strip()
        if raw_uid and raw_uid != "all":
            target_user_id = int(raw_uid)

    with db.tx() as c:
        trends = db.get_trends(c, days, user_id=target_user_id)

    return jsonify({"trends": trends, "days": days})


@app.route("/api/platforms")
@auth.login_required
def api_platforms():
    """Capability map — frontends consult this to gray out unsupported actions."""
    return jsonify({
        "platforms": list(PLATFORMS.keys()),
        "similar_capable": sorted(SIMILAR_CAPABLE),
        "search_capable": sorted(SEARCH_CAPABLE),
        "info_capable": sorted(INFO_CAPABLE),
    })


@app.route("/api/db/list")
@auth.admin_required
def api_db_list():
    """Filter platform_accounts. Optional `description` triggers embedding rerank.

    Query params:
        text=str             substring filter on name/bio/category
        description=str      embed + cosine sort all matched rows
        platforms=ig,tiktok  CSV
        limit=int            default 200
    """
    text = (request.args.get("text") or "").strip()
    description = (request.args.get("description") or "").strip()
    plats_raw = request.args.get("platforms") or ""
    platforms = [p for p in plats_raw.split(",") if p] or None
    statuses_raw = request.args.get("statuses") or ""
    statuses = [s for s in statuses_raw.split(",") if s] or None
    limit = int(request.args.get("limit") or 200)

    # Only load heavy columns (raw JSON + embeddings) when description rerank needs them
    need_embeddings = bool(description)

    with db.tx() as c:
        rows = db.list_accounts(c, platforms=platforms, text=text,
                                limit=limit, statuses=statuses,
                                include_heavy=need_embeddings)
        accounts = [db.row_to_dict(r) for r in rows]
        id_to_emb = {}
        if need_embeddings:
            for r in rows:
                if r["bio_embedding"]:
                    id_to_emb[r["id"]] = db.unpack_embedding(r["bio_embedding"])

    if need_embeddings and id_to_emb:
        try:
            qvec = ai.embed_text(description)
        except Exception as e:
            return jsonify({"error": f"embedding failed: {e}"}), 500
        for a in accounts:
            v = id_to_emb.get(a["id"])
            a["score"] = ai.cosine(qvec, v) if v else 0.0
        accounts.sort(key=lambda a: a.get("score", 0.0), reverse=True)
    return jsonify({"accounts": accounts, "stats": _stats()})


@app.route("/api/db/account/<int:account_id>")
@auth.login_required
def api_db_account(account_id: int):
    if not auth.user_owns_account(auth.current_user(), account_id):
        return jsonify({"error": "forbidden"}), 403
    with db.tx() as c:
        row = db.get_account(c, account_id)
        person = db.get_person(c, row["person_id"])
        siblings = [
            db.row_to_dict(r) for r in db.list_accounts_for_person(c, row["person_id"])
            if r["id"] != account_id
        ]
    return jsonify({
        "account": db.row_to_dict(row),
        "person": dict(person) if person else None,
        "other_accounts_for_person": siblings,
    })


@app.route("/api/db/account/<int:account_id>", methods=["DELETE"])
@auth.login_required
def api_db_delete_account(account_id: int):
    u = auth.current_user()
    if not auth.user_owns_account(u, account_id):
        return jsonify({"error": "forbidden"}), 403
    with db.tx() as c:
        ok = db.delete_account(c, account_id)
    return jsonify({"deleted": ok})


# Fields a staff user is allowed to change. Admin retains the full allow-set
# from `db.update_person_fields`.
_STAFF_PATCH_ALLOWED = {"status", "tags", "notes", "last_contacted_at",
                        "full_name", "email", "intent"}


@app.route("/api/db/person/<int:person_id>", methods=["PATCH"])
@auth.login_required
def api_db_update_person(person_id: int):
    """Update CRM fields on a person. Email collisions return 409."""
    import sqlite3 as _sql
    u = auth.current_user()
    payload = request.json or {}
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    if u["role"] != "admin":
        for k in payload.keys():
            if k not in _STAFF_PATCH_ALLOWED:
                return jsonify({"error": "forbidden_field", "field": k}), 400
        if payload.get("status") == "不适合":
            return jsonify({"error": "forbidden_field", "field": "status",
                            "detail": "use /api/people/<id>/reject"}), 400
    with db.tx() as c:
        # Read old status before update for status event tracking
        old_status = None
        if "status" in payload:
            old_row = c.execute("SELECT status FROM people WHERE id=?", (person_id,)).fetchone()
            old_status = old_row["status"] if old_row else None
        try:
            db.update_person_fields(c, person_id, **payload)
        except ValueError as e:
            # Currently the only ValueError we raise is invalid_email
            msg = str(e)
            if msg.startswith("invalid_email"):
                return jsonify({"error": "invalid_email",
                                "detail": "邮箱格式不对，应该形如 name@example.com"}), 400
            return jsonify({"error": "bad_input", "detail": msg}), 400
        except _sql.IntegrityError:
            return jsonify({
                "error": "email_conflict",
                "detail": "该邮箱已属于另一个人。如要合并请先删除另一边或换个邮箱。",
            }), 409
        # Record status change event if status was updated
        if "status" in payload and old_status and old_status != payload["status"]:
            db.record_status_event(c, person_id, u["id"], old_status, payload["status"])
        person = db.get_person(c, person_id)
    if u["role"] == "admin" and "assigned_user_id" in payload:
        audit.log(u["id"], "person.assign", f"person:{person_id}",
                  {"assigned_user_id": payload.get("assigned_user_id")})
    return jsonify({"person": dict(person)})


@app.route("/api/db/import_handle", methods=["POST"])
@auth.login_required
def api_db_import_handle():
    """Body: {platform, handle, person_id?: int}

    Fetches one platform handle from the real upstream API and upserts it.

    Without `person_id`: legacy seed-import path used by the /similarity
    bootstrap UI. Admin-only. The fetched account is routed to a person
    automatically (email match → existing person; otherwise → new person).

    With `person_id`: attach this handle to the named customer. Owning
    user only (admin or assigned staff). If the fetched account routes to
    a different person (because the bio email matches another customer in
    the DB), we return 409 with the existing person's id so the UI can
    surface a "merge with that customer?" prompt rather than silently
    folding two real humans together.
    """
    payload = request.json or {}
    platform = payload.get("platform")
    handle = (payload.get("handle") or "").strip().lstrip("@")
    target_pid = payload.get("person_id")
    if platform not in INFO_CAPABLE or not handle:
        return jsonify({"error": "invalid platform or handle"}), 400

    u = auth.current_user()
    if target_pid is None:
        if u["role"] != "admin":
            return jsonify({"error": "forbidden"}), 403
    else:
        try:
            target_pid = int(target_pid)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid person_id"}), 400
        if not auth.user_owns_person(u, target_pid):
            return jsonify({"error": "forbidden"}), 403

    try:
        norm = get_platform(platform).fetch_user_info(handle)
    except Exception as e:
        return jsonify({"error": "platform_fetch_failed", "detail": str(e)[:300]}), 502

    with db.tx() as c:
        source = "manual:add-to-person" if target_pid is not None else "manual"
        acct_id, returned_pid, created = db.upsert_account(
            c, norm, source=source, prefer_person_id=target_pid
        )

        if target_pid is not None and returned_pid != target_pid:
            other = db.get_person(c, returned_pid)
            return jsonify({
                "error": "handle_exists",
                "existing_person_id": returned_pid,
                "existing_full_name": (other["full_name"] if other else "") or "",
                "existing_email": (other["email"] if other else "") or "",
                "detail": f"@{handle} 已属于另一个客户",
            }), 409

        # Same-person placeholder cleanup: if this person already had a
        # `import:<handle>` placeholder row for the same platform/username
        # before we hydrated, drop it so we don't show two rows for one handle.
        if target_pid is not None:
            c.execute(
                "DELETE FROM platform_accounts WHERE person_id=? AND platform=? "
                "AND id != ? AND platform_user_id LIKE 'import:%' "
                "AND LOWER(username)=LOWER(?)",
                (target_pid, platform, acct_id, handle),
            )
        row = db.get_account(c, acct_id)
    try:
        ingest._embed_accounts([acct_id])
    except Exception:
        pass
    return jsonify({"account": db.row_to_dict(row), "created": created})


@app.route("/api/people/<int:person_id>/handles", methods=["POST"])
@auth.login_required
def api_people_add_handle(person_id: int):
    """Attach a handle to an existing customer WITHOUT touching the platform
    API — inserts a placeholder row only. Use this when you want to record a
    handle you saw somewhere and let the user lazy-hydrate it later via 🔄.

    Body: {platform, handle}
    """
    u = auth.current_user()
    if not auth.user_owns_person(u, person_id):
        return jsonify({"error": "forbidden"}), 403
    payload = request.json or {}
    platform = (payload.get("platform") or "").strip().lower()
    handle = (payload.get("handle") or "").strip().lstrip("@")
    if platform not in {"instagram", "x", "tiktok"} or not handle:
        return jsonify({"error": "invalid platform or handle"}), 400

    import sqlite3 as _sql
    with db.tx() as c:
        # Refuse if same (person, platform, username) already exists
        already = c.execute(
            "SELECT id FROM platform_accounts "
            "WHERE person_id=? AND platform=? AND LOWER(username)=LOWER(?)",
            (person_id, platform, handle),
        ).fetchone()
        if already:
            return jsonify({
                "error": "already_attached",
                "account_id": already["id"],
                "detail": f"@{handle} 已经在这个客户名下",
            }), 409
        try:
            acct_id = db.attach_placeholder_handle(c, person_id, platform, handle)
        except _sql.IntegrityError:
            # Placeholder id `import:<handle>` already exists on a different person.
            owner_row = c.execute(
                "SELECT pa.person_id, p.full_name, p.email FROM platform_accounts pa "
                "JOIN people p ON p.id = pa.person_id "
                "WHERE pa.platform=? AND pa.platform_user_id=?",
                (platform, f"import:{handle.lower()}"),
            ).fetchone()
            return jsonify({
                "error": "handle_exists",
                "existing_person_id": owner_row["person_id"] if owner_row else None,
                "existing_full_name": (owner_row["full_name"] if owner_row else "") or "",
                "existing_email": (owner_row["email"] if owner_row else "") or "",
                "detail": f"@{handle} 已属于另一个客户",
            }), 409
        row = db.get_account(c, acct_id)
    return jsonify({"account": db.row_to_dict(row), "created": True})


@app.route("/api/db/embed_missing", methods=["POST"])
@auth.admin_required
def api_embed_missing():
    return jsonify({"updated": ingest.embed_missing()})


@app.route("/api/db/clear", methods=["POST"])
@auth.admin_required
def api_db_clear():
    """Drop all data — useful for re-running tests. Requires {confirm: true}."""
    payload = request.json or {}
    if not payload.get("confirm"):
        return jsonify({"error": "set confirm=true to clear DB"}), 400
    with db.tx() as c:
        c.execute("DELETE FROM platform_accounts")
        c.execute("DELETE FROM people")
    audit.log(auth.current_user()["id"], "db.clear", None)
    return jsonify({"cleared": True, "stats": _stats()})


# ============================================================
#  AI helpers (exposed so backend tests can verify each step)
# ============================================================

@app.route("/api/ai/queries", methods=["POST"])
@auth.admin_required
def api_ai_queries():
    """Body: {description, n?, platforms?} → {queries: [...]}"""
    payload = request.json or {}
    description = (payload.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description required"}), 400
    n = int(payload.get("n") or 8)
    platforms = payload.get("platforms")
    try:
        queries = ai.generate_search_queries(description, n=n, platforms=platforms)
        return jsonify({"queries": queries})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/embed", methods=["POST"])
@auth.admin_required
def api_ai_embed():
    """Body: {text} or {texts: [...]}"""
    payload = request.json or {}
    if payload.get("texts"):
        try:
            return jsonify({"vectors": ai.embed_texts(payload["texts"])})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text or texts required"}), 400
    try:
        return jsonify({"vector": ai.embed_text(text)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#  Direct platform calls (raw, no DB write — for debugging / testing)
# ============================================================

@app.route("/api/platform/<platform>/info")
@auth.admin_required
def api_platform_info(platform: str):
    if platform not in INFO_CAPABLE:
        return jsonify({"error": "unsupported"}), 400
    handle = (request.args.get("handle") or "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "handle required"}), 400
    try:
        return jsonify({"normalized": get_platform(platform).fetch_user_info(handle)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/platform/<platform>/search")
@auth.admin_required
def api_platform_search(platform: str):
    if platform not in SEARCH_CAPABLE:
        return jsonify({"error": "unsupported"}), 400
    keyword = (request.args.get("keyword") or "").strip()
    count = int(request.args.get("count") or 10)
    if not keyword:
        return jsonify({"error": "keyword required"}), 400
    try:
        return jsonify({"results": get_platform(platform).search_users(keyword, count=count)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/platform/<platform>/similar")
@auth.admin_required
def api_platform_similar(platform: str):
    if platform not in SIMILAR_CAPABLE:
        return jsonify({"error": "unsupported"}), 400
    internal_id = (request.args.get("internal_id") or "").strip()
    secondary_id = (request.args.get("secondary_id") or "").strip()
    if not internal_id and not secondary_id:
        return jsonify({"error": "internal_id or secondary_id required"}), 400
    try:
        results = get_platform(platform).fetch_similar_users(internal_id, secondary_id=secondary_id)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
#  Discovery: similarity (DB seed → similar API)
# ============================================================

@app.route("/api/similarity/run", methods=["POST"])
@auth.admin_required
def api_similarity_run():
    """Body: {seed_account_ids: [int], count: int}"""
    payload = request.json or {}
    seed_ids = payload.get("seed_account_ids") or []
    target_count = int(payload.get("count") or 20)
    if not seed_ids:
        return jsonify({"error": "no seeds selected"}), 400

    with db.tx() as c:
        seeds = db.get_accounts_by_ids(c, seed_ids)
    if not seeds:
        return jsonify({"error": "seed accounts not found in DB"}), 404

    # Fetch similar lists per seed in parallel
    def _one(seed):
        plat = seed["platform"]
        if plat not in SIMILAR_CAPABLE:
            return seed["username"], plat, [], f"{plat} has no similar API"
        try:
            sims = get_platform(plat).fetch_similar_users(
                seed["platform_user_id"],
                secondary_id=seed["secondary_id"] or "",
            )
            return seed["username"], plat, sims, None
        except Exception as e:
            return seed["username"], plat, [], str(e)

    seed_errors: list[dict] = []
    candidates: list[tuple[dict, str]] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for uname, plat, sims, err in ex.map(_one, seeds):
            if err:
                seed_errors.append({"seed": uname, "platform": plat, "error": err})
                continue
            for s in sims:
                candidates.append((s, f"similar:{plat}:{uname}"))

    report = ingest.run(candidates, target_count=target_count, hydrate=True)
    report["seed_errors"] = seed_errors
    report["seeds_used"] = [{"username": s["username"], "platform": s["platform"]} for s in seeds]
    report["stats"] = _stats()
    return jsonify(report)


# ============================================================
#  Discovery: bulk similarity ("以所有客户做一次")
# ============================================================
#
# Pattern mirrors email_jobs: one row per job, status ∈
# running/interrupted/done/failed, payload_json carries the full state.
# Only one job may be active at a time — enforced by find_active_job() check
# at /bulk/start. Process restart sweeps 'running' rows → 'interrupted' so the
# UI surfaces a Resume button instead of a hung spinner.

# In-memory mirror of the live job dict to keep polling cheap. The DB row is
# the source of truth across restarts; this is purely a hot-path cache.
SIMILARITY_JOBS: dict[str, dict] = {}

# Statuses we prompt for by default in the "全量找相似" modal. Excludes
# 已放弃 / 不适合 / 已拒绝-on-the-account-row — see seed SQL for the latter.
DEFAULT_BULK_STATUSES = ["已成交", "已回复", "已联系"]


def _scope_seeds_to_user(rows: list[dict], user: dict) -> list[dict]:
    """Staff can only seed off people they're assigned to; admin sees all.
    Currently bulk-start is admin-only (per Sir), so this is a guard rather
    than a real filter, but it keeps the path symmetric with email_send_batch
    if we later relax to staff."""
    if user["role"] == "admin":
        return rows
    uid = user["id"]
    out = []
    with db.tx() as c:
        for r in rows:
            p = c.execute("SELECT assigned_user_id FROM people WHERE id=?",
                          (r["person_id"],)).fetchone()
            if p and (p["assigned_user_id"] == uid):
                out.append(r)
    return out


@app.route("/api/similarity/bulk/estimate", methods=["GET"])
@auth.admin_required
def api_similarity_bulk_estimate():
    """Preview the modal: how many seeds, how much it could cost, what's the
    TikHub balance. Cheap — only hits SQLite + one optional HTTP call to
    TikHub. Default statuses match DEFAULT_BULK_STATUSES; pass ?statuses=
    (comma-CSV, empty string = no filter) to override."""
    statuses_raw = request.args.get("statuses")
    if statuses_raw is None:
        statuses = list(DEFAULT_BULK_STATUSES)
    else:
        statuses = [s for s in statuses_raw.split(",") if s.strip()] or None
    plats_raw = request.args.get("platforms")
    platforms = [p for p in (plats_raw or "").split(",") if p.strip()] or None
    seeds = bulk_similarity.list_eligible_seeds(statuses=statuses, platforms=platforms)
    est = bulk_similarity.estimate_cost(seeds)
    return jsonify({
        "statuses": statuses,
        "platforms": platforms or sorted(bulk_similarity.SIMILAR_CAPABLE),
        **est,
        "tikhub_balance_usd": bulk_similarity.fetch_tikhub_balance(),
        "active_job": bulk_similarity.find_active_job(),
    })


@app.route("/api/similarity/bulk/active", methods=["GET"])
@auth.admin_required
def api_similarity_bulk_active():
    """Page-load query: if there's a running/interrupted job, show its card
    instead of the launcher. Null when nothing's pending."""
    job = bulk_similarity.find_active_job()
    return jsonify({"job": job})


@app.route("/api/similarity/bulk/start", methods=["POST"])
@auth.admin_required
def api_similarity_bulk_start():
    """Body: {statuses?: [str], platforms?: [str]}. Default statuses =
    DEFAULT_BULK_STATUSES, default platforms = all SIMILAR_CAPABLE.

    Single-job lock: if any row is currently running OR interrupted, return
    409 with the offending job_id so the UI can pivot to its progress card."""
    payload = request.json or {}
    statuses = payload.get("statuses")
    if statuses is None:
        statuses = list(DEFAULT_BULK_STATUSES)
    elif statuses == []:
        statuses = None  # explicit "no filter"
    platforms = payload.get("platforms") or None

    u = auth.current_user()
    seeds = bulk_similarity.list_eligible_seeds(statuses=statuses, platforms=platforms)
    seeds = _scope_seeds_to_user(seeds, u)
    # Optional cap — used for small-scale canary/test runs over a large pool.
    max_seeds = payload.get("max_seeds")
    if max_seeds:
        try:
            seeds = seeds[: int(max_seeds)]
        except (TypeError, ValueError):
            pass
    if not seeds:
        return jsonify({"error": "no_seeds",
                        "detail": "按当前筛选条件，数据库里没有可用的种子账号"}), 400

    job = bulk_similarity.build_job(seeds, {
        "statuses": statuses,
        "platforms": platforms or sorted(bulk_similarity.SIMILAR_CAPABLE),
    }, created_by=u["id"])

    # Atomic acquire-or-fail. Folds the lock check + INSERT into one tx so
    # two concurrent /bulk/start calls can't both squeak past a separate
    # find_active_job() probe.
    acquired, active = bulk_similarity.try_create_locked(job, u["id"])
    if not acquired:
        return jsonify({
            "error": "another_job_active",
            "detail": "另有相似任务在跑（或被打断未恢复），同一时间只允许一个",
            "job_id": (active or {}).get("id"),
            "job_status": (active or {}).get("status"),
        }), 409

    SIMILARITY_JOBS[job["id"]] = job
    audit.log(u["id"], "similarity_bulk.start", f"job:{job['id']}",
              {"seed_count": len(seeds), "statuses": statuses})

    threading.Thread(
        target=bulk_similarity.run_job, args=(job, u["id"]), daemon=True,
    ).start()
    return jsonify({"job_id": job["id"], "seed_count": len(seeds)})


@app.route("/api/similarity/bulk/<job_id>", methods=["GET"])
@auth.admin_required
def api_similarity_bulk_get(job_id: str):
    """Polling endpoint. UI hits this every ~2s while the card is open.
    Returns the full job dict; the modal cares about cursor/seed_total/
    progress/per_seed (recent tail)."""
    job = SIMILARITY_JOBS.get(job_id)
    if not job:
        loaded, _ = bulk_similarity.load_job(job_id)
        job = loaded
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job)


@app.route("/api/similarity/bulk/<job_id>/resume", methods=["POST"])
@auth.admin_required
def api_similarity_bulk_resume(job_id: str):
    """Restart an interrupted job from its last checkpoint. Atomic CAS flips
    'interrupted' → 'running'; loses the race → 409. Worker picks up at
    job.cursor, so the in-flight seed at crash time re-runs (costs one extra
    similar API call; cheaper than tracking sub-seed progress)."""
    job, created_by = bulk_similarity.load_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job.get("status") != "interrupted":
        return jsonify({"error": "not_interrupted",
                        "detail": f"job status is {job.get('status')}"}), 400

    with db.tx() as c:
        cur = c.execute(
            "UPDATE similarity_jobs SET status='running' WHERE id=? AND status='interrupted'",
            (job_id,),
        )
        if cur.rowcount != 1:
            return jsonify({"error": "race",
                            "detail": "another resume already in flight"}), 409

    job["status"] = "running"
    job["finished_at"] = None
    job.pop("interrupted_at", None)
    job.pop("interrupted_reason", None)
    bulk_similarity.save_job(job, created_by)
    SIMILARITY_JOBS[job_id] = job

    u = auth.current_user()
    audit.log(u["id"], "similarity_bulk.resume", f"job:{job_id}",
              {"cursor": job.get("cursor"), "seed_total": job.get("seed_total")})

    threading.Thread(
        target=bulk_similarity.run_job, args=(job, created_by), daemon=True,
    ).start()
    return jsonify({"ok": True, "remaining": max(0, job.get("seed_total", 0) - job.get("cursor", 0))})


@app.route("/api/similarity/bulk/<job_id>/cancel", methods=["POST"])
@auth.admin_required
def api_similarity_bulk_cancel(job_id: str):
    """Abandon a running or interrupted job. Sets status='cancelled' so the
    worker loop (which checks each iteration) stops, and find_active_job
    ignores it."""
    job, _ = bulk_similarity.load_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job.get("status") not in ("interrupted", "running"):
        return jsonify({"error": "not_cancellable",
                        "detail": f"only running/interrupted jobs can be cancelled (this one is {job.get('status')})"}), 400
    # Signal the in-memory worker thread FIRST — instant, no DB race.
    bulk_similarity.mark_cancelled(job_id)
    with db.tx() as c:
        cur = c.execute(
            "UPDATE similarity_jobs SET status='cancelled' WHERE id=? AND status IN ('running','interrupted')",
            (job_id,),
        )
        if cur.rowcount != 1:
            return jsonify({"error": "race"}), 409
    job["status"] = "cancelled"
    job["finished_at"] = time.time()
    bulk_similarity.save_job(job, job.get("created_by") or auth.current_user()["id"])
    SIMILARITY_JOBS.pop(job_id, None)
    audit.log(auth.current_user()["id"], "similarity_bulk.cancel", f"job:{job_id}",
              {"cursor": job.get("cursor"), "seed_total": job.get("seed_total")})
    return jsonify({"ok": True})


# Boot-time sweep: any 'running' row is a zombie from the previous process
# (its worker thread died with that process). Flip to 'interrupted' so the
# UI shows a Resume button instead of a forever-spinner.
_swept_sim = bulk_similarity.sweep_stale_running()
if _swept_sim:
    print(f"[boot] swept {_swept_sim} zombie similarity_jobs → status='interrupted'", flush=True)


# ============================================================
#  Discovery: bulk hydrate ("批量补全 placeholder")
# ============================================================
#
# Twin of bulk similarity but for fetch_user_info — turn 'import:<handle>'
# placeholder rows into real platform_accounts with pk_id, bio, email, etc.
# Lock is independent: a hydrate job and a similarity job can coexist (they
# hit different TikHub endpoints, separate rate-limit buckets).

HYDRATE_JOBS: dict[str, dict] = {}

# Defaults match bulk_similarity so an admin clicking through both wizards
# sees the same status presets.
DEFAULT_HYDRATE_STATUSES = ["已成交", "已回复", "已联系"]


@app.route("/api/hydrate/bulk/estimate", methods=["GET"])
@auth.admin_required
def api_hydrate_bulk_estimate():
    statuses_raw = request.args.get("statuses")
    if statuses_raw is None:
        statuses = list(DEFAULT_HYDRATE_STATUSES)
    else:
        statuses = [s for s in statuses_raw.split(",") if s.strip()] or None
    plats_raw = request.args.get("platforms")
    platforms = [p for p in (plats_raw or "").split(",") if p.strip()] or None
    rows = bulk_hydrate.list_eligible_placeholders(statuses=statuses, platforms=platforms)
    est = bulk_hydrate.estimate_cost(rows)
    return jsonify({
        "statuses": statuses,
        "platforms": platforms or sorted(bulk_hydrate.INFO_CAPABLE),
        **est,
        "tikhub_balance_usd": bulk_hydrate.fetch_tikhub_balance(),
        "active_job": bulk_hydrate.find_active_job(),
    })


@app.route("/api/hydrate/bulk/active", methods=["GET"])
@auth.admin_required
def api_hydrate_bulk_active():
    return jsonify({"job": bulk_hydrate.find_active_job()})


@app.route("/api/hydrate/bulk/start", methods=["POST"])
@auth.admin_required
def api_hydrate_bulk_start():
    """Body: {statuses?, platforms?}. Same conventions as similarity bulk."""
    payload = request.json or {}
    statuses = payload.get("statuses")
    if statuses is None:
        statuses = list(DEFAULT_HYDRATE_STATUSES)
    elif statuses == []:
        statuses = None
    platforms = payload.get("platforms") or None

    u = auth.current_user()
    rows = bulk_hydrate.list_eligible_placeholders(statuses=statuses, platforms=platforms)
    if not rows:
        return jsonify({"error": "no_placeholders",
                        "detail": "按当前筛选条件，没有可补全的 placeholder"}), 400

    job = bulk_hydrate.build_job(rows, {
        "statuses": statuses,
        "platforms": platforms or sorted(bulk_hydrate.INFO_CAPABLE),
    }, created_by=u["id"])

    acquired, active = bulk_hydrate.try_create_locked(job, u["id"])
    if not acquired:
        return jsonify({
            "error": "another_job_active",
            "detail": "另有 hydrate 任务在跑（或被打断未恢复），同一时间只允许一个",
            "job_id": (active or {}).get("id"),
            "job_status": (active or {}).get("status"),
        }), 409

    HYDRATE_JOBS[job["id"]] = job
    audit.log(u["id"], "hydrate_bulk.start", f"job:{job['id']}",
              {"count": len(rows), "statuses": statuses})

    threading.Thread(
        target=bulk_hydrate.run_job, args=(job, u["id"]), daemon=True,
    ).start()
    return jsonify({"job_id": job["id"], "count": len(rows)})


@app.route("/api/hydrate/bulk/<job_id>", methods=["GET"])
@auth.admin_required
def api_hydrate_bulk_get(job_id: str):
    job = HYDRATE_JOBS.get(job_id)
    if not job:
        loaded, _ = bulk_hydrate.load_job(job_id)
        job = loaded
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job)


@app.route("/api/hydrate/bulk/<job_id>/resume", methods=["POST"])
@auth.admin_required
def api_hydrate_bulk_resume(job_id: str):
    job, created_by = bulk_hydrate.load_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job.get("status") != "interrupted":
        return jsonify({"error": "not_interrupted",
                        "detail": f"job status is {job.get('status')}"}), 400

    with db.tx() as c:
        cur = c.execute(
            "UPDATE hydrate_jobs SET status='running' WHERE id=? AND status='interrupted'",
            (job_id,),
        )
        if cur.rowcount != 1:
            return jsonify({"error": "race"}), 409

    job["status"] = "running"
    job["finished_at"] = None
    job.pop("interrupted_at", None)
    job.pop("interrupted_reason", None)
    bulk_hydrate.save_job(job, created_by)
    HYDRATE_JOBS[job_id] = job

    u = auth.current_user()
    audit.log(u["id"], "hydrate_bulk.resume", f"job:{job_id}",
              {"cursor": job.get("cursor"), "placeholder_total": job.get("placeholder_total")})

    threading.Thread(
        target=bulk_hydrate.run_job, args=(job, created_by), daemon=True,
    ).start()
    return jsonify({"ok": True,
                    "remaining": max(0, job.get("placeholder_total", 0) - job.get("cursor", 0))})


@app.route("/api/hydrate/bulk/<job_id>/cancel", methods=["POST"])
@auth.admin_required
def api_hydrate_bulk_cancel(job_id: str):
    job, _ = bulk_hydrate.load_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    if job.get("status") not in ("interrupted", "running"):
        return jsonify({"error": "not_cancellable",
                        "detail": f"only running/interrupted jobs can be cancelled (this one is {job.get('status')})"}), 400
    # Signal the in-memory worker thread FIRST — instant, no DB race.
    bulk_hydrate.mark_cancelled(job_id)
    with db.tx() as c:
        cur = c.execute(
            "UPDATE hydrate_jobs SET status='cancelled' WHERE id=? AND status IN ('running','interrupted')",
            (job_id,),
        )
        if cur.rowcount != 1:
            return jsonify({"error": "race"}), 409
    job["status"] = "cancelled"
    job["finished_at"] = time.time()
    bulk_hydrate.save_job(job, job.get("created_by") or auth.current_user()["id"])
    HYDRATE_JOBS.pop(job_id, None)
    audit.log(auth.current_user()["id"], "hydrate_bulk.cancel", f"job:{job_id}",
              {"cursor": job.get("cursor"), "placeholder_total": job.get("placeholder_total")})
    return jsonify({"ok": True})


_swept_hyd = bulk_hydrate.sweep_stale_running()
if _swept_hyd:
    print(f"[boot] swept {_swept_hyd} zombie hydrate_jobs → status='interrupted'", flush=True)


# Live watchdog — boot sweep only catches process-restart zombies; a worker
# SIGKILL'd mid-run (e.g. slow-client gunicorn timeout) needs the watchdog.
job_watchdog.start()


# ============================================================
#  Discovery: AI query search
# ============================================================

@app.route("/api/query/run", methods=["POST"])
@auth.admin_required
def api_query_run():
    """Body: {description, count, platforms?, num_queries?, queries?: [str]}
    If `queries` is provided, AI generation is skipped (useful for tests).
    """
    payload = request.json or {}
    description = (payload.get("description") or "").strip()
    target_count = int(payload.get("count") or 20)
    platforms = payload.get("platforms") or list(SEARCH_CAPABLE)
    platforms = [p for p in platforms if p in SEARCH_CAPABLE]
    num_queries = int(payload.get("num_queries") or 8)

    if not platforms:
        return jsonify({"error": "no enabled platforms"}), 400

    queries = payload.get("queries")
    if queries:
        queries = [q.strip() for q in queries if q.strip()][:num_queries]
    else:
        if not description:
            return jsonify({"error": "description or queries required"}), 400
        try:
            queries = ai.generate_search_queries(description, n=num_queries, platforms=platforms)
        except Exception as e:
            return jsonify({"error": f"query generation failed: {e}"}), 500
    if not queries:
        return jsonify({"error": "no queries to run"}), 500

    jobs = [(p, q) for p in platforms for q in queries]
    search_errors: list[dict] = []
    candidates: list[tuple[dict, str]] = []

    def run_search(job):
        plat, q = job
        try:
            results = get_platform(plat).search_users(q, count=10)
            return plat, q, results, None
        except Exception as e:
            return plat, q, [], str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for plat, q, results, err in ex.map(run_search, jobs):
            if err:
                search_errors.append({"platform": plat, "query": q, "error": err})
                continue
            for r in results:
                candidates.append((r, f"query:{plat}:{q}"))

    report = ingest.run(candidates, target_count=target_count, hydrate=True)
    report["queries"] = queries
    report["platforms_used"] = platforms
    report["search_errors"] = search_errors
    report["stats"] = _stats()
    return jsonify(report)


# ============================================================
#  Excel export (accounts)
# ============================================================

@app.route("/api/db/export", methods=["POST"])
@auth.admin_required
def api_db_export():
    payload = request.json or {}
    ids = payload.get("account_ids")
    with db.tx() as c:
        rows = db.get_accounts_by_ids(c, ids) if ids else db.list_accounts(c, limit=100000)
    rows = [db.row_to_dict(r) for r in rows]

    wb = Workbook()
    ws = wb.active
    ws.title = "Creators"
    headers = [
        "Platform", "Username", "Full Name", "Email", "Phone",
        "Followers", "Following", "Posts", "Verified", "Category",
        "Biography", "External URL", "City", "Source", "Updated",
    ]
    header_fill = PatternFill(start_color="1a1a2e", end_color="1a1a2e", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    thin_border = Border(bottom=Side(style="thin", color="E0E0E0"))
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for r_idx, a in enumerate(rows, 2):
        vals = [
            a.get("platform", ""), a.get("username", ""), a.get("full_name", ""),
            a.get("email", ""), a.get("phone", ""),
            a.get("follower_count") or "", a.get("following_count") or "", a.get("post_count") or "",
            "Yes" if a.get("is_verified") else "No",
            a.get("category", ""), a.get("biography", ""), a.get("external_url", ""),
            a.get("city", ""), a.get("source", ""), a.get("updated_at", ""),
        ]
        for c_idx, val in enumerate(vals, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center", wrap_text=(c_idx == 11))
    widths = [12, 22, 22, 26, 16, 12, 12, 10, 10, 18, 50, 30, 14, 30, 14]
    for i, w in enumerate(widths):
        ws.column_dimensions[ws.cell(row=1, column=i + 1).column_letter].width = w
    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return send_file(
        tmp.name, as_attachment=True, download_name="creators.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================
#  Admin: user management
# ============================================================

@app.route("/api/admin/users")
@auth.admin_required
def api_admin_users_list():
    return jsonify({"users": auth.list_users()})


@app.route("/api/admin/active_staff")
@auth.login_required
def api_active_staff():
    """Used by admin's "assign to" dropdown. Returns minimal info; no PII."""
    if auth.current_user()["role"] != "admin":
        return jsonify({"users": []})
    return jsonify({"users": auth.list_active_staff_and_admins()})


@app.route("/api/admin/users/<int:user_id>/activate", methods=["POST"])
@auth.admin_required
def api_admin_user_activate(user_id: int):
    target = auth.get_user(user_id)
    if not target:
        return jsonify({"error": "not_found"}), 404
    auth.set_active(user_id, True)
    audit.log(auth.current_user()["id"], "user.activate", f"user:{user_id}")
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:user_id>/deactivate", methods=["POST"])
@auth.admin_required
def api_admin_user_deactivate(user_id: int):
    actor = auth.current_user()
    target = auth.get_user(user_id)
    if not target:
        return jsonify({"error": "not_found"}), 404
    if target["role"] == "admin" and auth.count_active_admins(exclude_id=user_id) == 0:
        return jsonify({"error": "last_admin"}), 400
    payload = request.json or {}
    unassign = bool(payload.get("unassign_customers"))
    if unassign:
        with db.tx() as c:
            c.execute("UPDATE people SET assigned_user_id=NULL, updated_at=? WHERE assigned_user_id=?",
                      (int(time.time()), user_id))
    auth.set_active(user_id, False)
    audit.log(actor["id"], "user.deactivate", f"user:{user_id}",
              {"unassign_customers": unassign})
    return jsonify({"ok": True})


# Temp-password alphabet: 62 chars minus the ones humans confuse visually
# (capital I, lowercase l, digit 1; capital O, digit 0). Don't introduce
# punctuation — staff will be typing this from a chat message and shifted
# symbols invite their own typos.
_TEMP_PW_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TEMP_PW_LEN = 12


@app.route("/api/admin/users/<int:user_id>/reset_password", methods=["POST"])
@auth.admin_required
def api_admin_user_reset_password(user_id: int):
    target = auth.get_user(user_id)
    if not target:
        return jsonify({"error": "not_found"}), 404
    temp = "".join(_secrets.choice(_TEMP_PW_ALPHABET) for _ in range(_TEMP_PW_LEN))
    auth.set_password(user_id, temp, must_change=True)
    audit.log(auth.current_user()["id"], "user.password_reset", f"user:{user_id}")
    return jsonify({"ok": True, "temp_password": temp})


@app.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@auth.admin_required
def api_admin_user_role(user_id: int):
    payload = request.json or {}
    new_role = payload.get("role")
    if new_role not in ("admin", "staff"):
        return jsonify({"error": "bad_role"}), 400
    target = auth.get_user(user_id)
    if not target:
        return jsonify({"error": "not_found"}), 404
    if target["role"] == "admin" and new_role == "staff" \
            and auth.count_active_admins(exclude_id=user_id) == 0:
        return jsonify({"error": "last_admin"}), 400
    auth.set_role(user_id, new_role)
    audit.log(auth.current_user()["id"], "user.role_change", f"user:{user_id}",
              {"role": new_role})
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@auth.admin_required
def api_admin_user_delete(user_id: int):
    target = auth.get_user(user_id)
    if not target:
        return jsonify({"error": "not_found"}), 404
    with db.tx() as c:
        n_assigned = c.execute(
            "SELECT COUNT(*) AS n FROM people WHERE assigned_user_id=?", (user_id,)
        ).fetchone()["n"]
    if n_assigned > 0 or target["last_login_at"]:
        return jsonify({"error": "user_in_use",
                        "detail": "请先转派客户并停用"}), 400
    if target["role"] == "admin" and auth.count_active_admins(exclude_id=user_id) == 0:
        return jsonify({"error": "last_admin"}), 400
    auth.delete_user(user_id)
    audit.log(auth.current_user()["id"], "user.delete", f"user:{user_id}")
    return jsonify({"ok": True})


@app.route("/api/admin/audit")
@auth.admin_required
def api_admin_audit():
    return jsonify({"events": audit.recent(limit=200)})


@app.route("/api/admin/migrate_owners", methods=["POST"])
@auth.admin_required
def api_admin_migrate_owners():
    """Run the owner-text → assigned_user_id migration. Body: {apply: bool}."""
    from collections import defaultdict, Counter
    payload = request.json or {}
    apply = bool(payload.get("apply"))
    actor = auth.current_user()
    with db.tx() as c:
        users = c.execute("SELECT id, username, display_name FROM users").fetchall()
        people = c.execute(
            "SELECT id, owner FROM people "
            "WHERE assigned_user_id IS NULL AND owner IS NOT NULL AND owner != ''"
        ).fetchall()
    by_username: dict[str, int] = {}
    display_buckets: dict[str, list[int]] = defaultdict(list)
    for u in users:
        by_username[(u["username"] or "").strip().lower()] = u["id"]
        if u["display_name"]:
            display_buckets[u["display_name"].strip().lower()].append(u["id"])
    will_update: list[tuple[int, int]] = []
    no_match: Counter = Counter()
    ambiguous: Counter = Counter()
    for p in people:
        key = (p["owner"] or "").strip().lower()
        if not key:
            continue
        uid = by_username.get(key)
        if uid is None:
            cands = display_buckets.get(key, [])
            if len(cands) == 1:
                uid = cands[0]
            elif len(cands) > 1:
                ambiguous[key] += 1
                continue
        if uid is None:
            no_match[p["owner"]] += 1
            continue
        will_update.append((p["id"], uid))
    if apply:
        now = int(time.time())
        with db.tx() as c:
            for pid, uid in will_update:
                c.execute(
                    "UPDATE people SET assigned_user_id=?, updated_at=? "
                    "WHERE id=? AND assigned_user_id IS NULL",
                    (uid, now, pid),
                )
        audit.log(actor["id"], "person.bulk_assign", None,
                  {"count": len(will_update), "via": "owner_migration"})
    return jsonify({
        "would_update": len(will_update),
        "applied": apply,
        "unmatched": sum(no_match.values()),
        "ambiguous": sum(ambiguous.values()),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
