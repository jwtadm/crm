"""Auto-login / API-key security checks, run against a throwaway DB copy.

    SCOUT_DB_PATH is set here (tests/.autologin.db), so the real scout.db is
    never touched.

Covers: key creation (hashed at rest, plaintext returned once), username
whitelist, IP whitelist, revocation, expiry, single-use ticket, ticket replay,
open-redirect `next`, and that must_change_pw still applies after auto-login.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ["SCOUT_DB_PATH"] = str(REPO / "tests" / ".autologin.db")

src = sqlite3.connect(REPO / "tests" / ".shot.db")
dst = sqlite3.connect(REPO / "tests" / ".autologin.db")
src.backup(dst)
dst.close()
src.close()

import app as app_module  # noqa: E402

app = app_module.app
app.config.update(TESTING=True)
FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def j(resp):
    try:
        return json.loads(resp.data)
    except Exception:
        return {}


def admin_client():
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
    return c


admin = admin_client()
anon = app.test_client()
db_path = REPO / "tests" / ".autologin.db"

# --- key creation ---
r = admin.post("/api/admin/api_keys", json={"name": "t", "allowed_usernames": ["Echo"]})
key = j(r).get("key", "")
check("create key returns 201 + plaintext once", r.status_code == 201 and key.startswith("crm_"))
row = sqlite3.connect(db_path).execute(
    "SELECT key_hash, key_prefix FROM api_keys ORDER BY id DESC LIMIT 1").fetchone()
check("DB stores hash, not plaintext", row and row[0] != key and len(row[0]) == 64)
listed = j(admin.get("/api/admin/api_keys"))["keys"][0]
check("list never exposes plaintext/hash", "key" not in listed and "key_hash" not in listed)

r = admin.post("/api/admin/api_keys", json={"name": "bad", "allowed_usernames": ["nobody-xyz"]})
check("unknown username rejected", r.status_code == 400 and j(r)["error"] == "unknown_username")
r = admin.post("/api/admin/api_keys", json={"name": "bad2", "allowed_usernames": []})
check("empty whitelist rejected", r.status_code == 400)
r = admin.post("/api/admin/api_keys", json={"name": "bad3", "allowed_usernames": "*",
                                            "expires_days": 0})
check("permanent key rejected (0 days)", r.status_code == 400 and j(r)["error"] == "bad_expires_days")

# --- issue + redeem ---
r = anon.post("/api/auth/auto_login", json={"username": "Echo", "next": "/people"},
              headers={"Authorization": "Bearer " + key})
d = j(r)
check("issue login_url", r.status_code == 200 and d.get("expires_in") == 60)
path = d["login_url"].split("localhost", 1)[-1]

browser = app.test_client()
r = browser.get(path, follow_redirects=False)
check("ticket sets session cookie", r.status_code == 302 and bool(r.headers.get("Set-Cookie")))
check("session belongs to target user",
      j(browser.get("/api/auth/me")).get("user", {}).get("username") == "Echo")
r = browser.get(path, follow_redirects=False)
check("ticket cannot be replayed", "invalid_ticket" in (r.headers.get("Location") or ""))

# --- policy ---
r = anon.post("/api/auth/auto_login", json={"username": "Talia"},
              headers={"Authorization": "Bearer " + key})
check("username whitelist enforced", r.status_code == 403 and j(r)["error"] == "user_not_in_whitelist")
r = anon.post("/api/auth/auto_login", json={"username": "admin"},
              headers={"Authorization": "Bearer " + key})
check("whitelisted key still cannot log in admin",
      r.status_code == 403 and j(r)["error"] == "user_not_in_whitelist")

# a key that allows everyone must still refuse the admin account
wide = j(admin.post("/api/admin/api_keys", json={"name": "wide", "allowed_usernames": "*"}))["key"]
r = anon.post("/api/auth/auto_login", json={"username": "admin"},
              headers={"Authorization": "Bearer " + wide})
check("wildcard key cannot log in admin",
      r.status_code == 403 and j(r)["error"] == "admin_not_allowed")
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer crm_bogus"})
check("unknown key rejected", r.status_code == 401)
r = anon.post("/api/auth/auto_login", json={"username": "Echo"})
check("missing key rejected", r.status_code == 401)

made = j(admin.post("/api/admin/api_keys", json={"name": "ip", "allowed_usernames": "*",
                                                "allowed_ips": "10.0.0.0/8"}))
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + made["key"]})
check("IP allowlist enforced", r.status_code == 403 and j(r)["error"] == "ip_not_allowed")

made = j(admin.post("/api/admin/api_keys", json={"name": "rev", "allowed_usernames": "*"}))
check("revoke returns 200", admin.post(f"/api/admin/api_keys/{made['id']}/revoke").status_code == 200)
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + made["key"]})
check("revoked key rejected", r.status_code == 401)

made = j(admin.post("/api/admin/api_keys", json={"name": "exp", "allowed_usernames": "*"}))
sqlite3.connect(db_path).execute(
    "UPDATE api_keys SET expires_at = 1000 WHERE id = ?", (made["id"],)).connection.commit()
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + made["key"]})
check("expired key rejected", r.status_code == 401 and j(r)["error"] == "key_expired")

# --- redirect + must_change_pw ---
r = anon.post("/api/auth/auto_login", json={"username": "Echo", "next": "//evil.example/x"},
              headers={"Authorization": "Bearer " + key})
b2 = app.test_client()
loc = b2.get(j(r)["login_url"].split("localhost", 1)[-1], follow_redirects=False).headers.get("Location")
check("open redirect dropped", loc == "/")

sqlite3.connect(db_path).execute(
    "UPDATE users SET must_change_pw = 1 WHERE username = 'Echo'").connection.commit()
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + key})
b3 = app.test_client()
b3.get(j(r)["login_url"].split("localhost", 1)[-1])
r = b3.get("/people", follow_redirects=False)
check("must_change_pw still enforced", r.status_code == 302 and "/change_password" in (r.headers.get("Location") or ""))
sqlite3.connect(db_path).execute(
    "UPDATE users SET must_change_pw = 0 WHERE username = 'Echo'").connection.commit()

# --- expired ticket + key without expiry ---
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + key})
ticket_path = j(r)["login_url"].split("localhost", 1)[-1]
sqlite3.connect(db_path).execute(
    "UPDATE login_tickets SET expires_at = 1000").connection.commit()
r = app.test_client().get(ticket_path, follow_redirects=False)
check("expired ticket rejected", "invalid_ticket" in (r.headers.get("Location") or ""))

legacy = j(admin.post("/api/admin/api_keys", json={"name": "legacy", "allowed_usernames": "*"}))
sqlite3.connect(db_path).execute(
    "UPDATE api_keys SET expires_at = NULL WHERE id = ?", (legacy["id"],)).connection.commit()
r = anon.post("/api/auth/auto_login", json={"username": "Echo"},
              headers={"Authorization": "Bearer " + legacy["key"]})
check("key with no expiry rejected", r.status_code == 401 and j(r)["error"] == "key_expired")

# --- audit ---
actions = [r[0] for r in sqlite3.connect(db_path).execute(
    "SELECT action FROM audit_log WHERE action LIKE 'auto_login%' OR action LIKE 'api_key%'").fetchall()]
check("audit written", "auto_login.grant" in actions and "api_key.create" in actions
      and "api_key.revoke" in actions)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} -> {FAILS}")
    sys.exit(len(FAILS))
print("all checks passed")
