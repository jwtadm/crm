"""SSO signed-link security checks, run against a throwaway DB copy.

    SCOUT_DB_PATH points at tests/.sso.db, never the real scout.db.

Covers: app creation (secret shown once, salt-only in DB), signature, ts
window, one-time nonce, TTL=0, IP whitelist, account whitelist, admin gating,
simple-key mode, reset secret, validation errors, and that must_change_pw
still applies after SSO.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "tests" / ".sso.db"
os.environ["SCOUT_DB_PATH"] = str(DB)

src = sqlite3.connect(REPO / "scout.db")
dst = sqlite3.connect(DB)
src.backup(dst)
dst.close()
src.close()

import app as app_module  # noqa: E402
from lib import sso  # noqa: E402

app = app_module.app
app.config.update(TESTING=True)
FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def admin_client():
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
    return c


def j(resp):
    try:
        return json.loads(resp.data)
    except Exception:
        return {}


def url(app_id, username, secret, ts=None, nonce=None):
    ts = str(ts if ts is not None else int(time.time()))
    nonce = nonce or os.urandom(8).hex()
    sign = sso.sign(username, ts, nonce, secret)
    return f"/sso/login?app_id={app_id}&username={username}&ts={ts}&nonce={nonce}&sign={sign}"


def page(resp):
    return resp.data.decode("utf-8", "replace")


admin = admin_client()
anon = app.test_client()

# --- create ---
r = admin.post("/api/admin/sso_apps", json={
    "name": "t-echo", "app_id": "testapp", "allow_users": ["Echo"],
})
d = j(r)
secret = d.get("secret", "")
check("create app -> 201 + secret", r.status_code == 201 and len(secret) == 64)
row = sqlite3.connect(DB).execute(
    "SELECT secret_salt, allow_users FROM sso_apps WHERE app_id='testapp'").fetchone()
check("DB stores salt, not the secret",
      row is not None and row[0] != secret and len(row[0]) == 32)

for payload, err in (
    ({"name": "x", "app_id": "bad id!"}, "bad_app_id"),
    ({"name": "x", "app_id": "testapp"}, "app_id_taken"),
    ({"name": "x", "app_id": "unknownu", "allow_users": ["nobody-xyz"]}, "unknown_username"),
):
    r = admin.post("/api/admin/sso_apps", json=payload)
    check(f"validation: {err}", r.status_code == 400 and j(r).get("error") == err)

# --- signature mode ---
r = anon.get(url("testapp", "Echo", secret), follow_redirects=False)
check("valid signed URL -> 302 + cookie",
      r.status_code == 302 and bool(r.headers.get("Set-Cookie")))
check("session is Echo",
      j(anon.get("/api/auth/me")).get("user", {}).get("username") == "Echo")

same = url("testapp", "Echo", secret)
first = anon.get(same, follow_redirects=False)
replay = anon.get(same, follow_redirects=False)
check("same link can be reopened", first.status_code == 302 and replay.status_code == 302)

r = anon.get(url("testapp", "Echo", "0" * 64), follow_redirects=False)
check("bad signature -> 403", r.status_code == 403 and "签名校验失败" in page(r))

r = anon.get(url("testapp", "Echo", secret, ts=int(time.time()) - 86400), follow_redirects=False)
check("permanent link: old ts still accepted", r.status_code == 302)

r = anon.get(url("testapp", "Echo", secret).replace("&sign=", "&sign=ABCDEF"), follow_redirects=False)
check("uppercase sign rejected", r.status_code == 403 and "sign 必须是 64 位小写十六进制" in page(r))

r = anon.get(f"/sso/login?app_id=testapp&username=Echo&ts={int(time.time())}&nonce=abc&sign={'0'*64}",
             follow_redirects=False)
check("short nonce rejected", r.status_code == 403 and "nonce 必须是 8-64 位字母数字" in page(r))

r = anon.get("/sso/login?app_id=nope&username=Echo&ts=1&nonce=abcdefgh&sign=" + "0" * 64,
             follow_redirects=False)
check("unknown app -> 403", r.status_code == 403 and "应用不存在或已禁用" in page(r))

# --- no per-app expiry knobs at all ---
perm = j(admin.post("/api/admin/sso_apps", json={
    "name": "perm", "app_id": "permapp",
}))["secret"]

# --- IP whitelist ---
ipapp = j(admin.post("/api/admin/sso_apps", json={
    "name": "ip", "app_id": "ipapp", "allow_ips": "10.0.0.0/8",
}))["secret"]
r = anon.get(url("ipapp", "Echo", ipapp), follow_redirects=False)
check("IP allowlist enforced", r.status_code == 403 and "来源 IP 不被允许" in page(r))

# --- account scope ---
r = anon.get(url("testapp", "Talia", secret), follow_redirects=False)
check("account whitelist enforced",
      r.status_code == 403 and "该账号不在本应用的允许登录范围" in page(r))

wide = j(admin.post("/api/admin/sso_apps", json={
    "name": "wide", "app_id": "wideapp",
}))["secret"]
r = anon.get(url("wideapp", "admin", wide), follow_redirects=False)
check("empty allow list logs in admin", r.status_code == 302)

named = j(admin.post("/api/admin/sso_apps", json={
    "name": "named", "app_id": "namedapp", "allow_users": ["admin"],
}))["secret"]
r = anon.get(url("namedapp", "admin", named), follow_redirects=False)
check("admin can be named explicitly", r.status_code == 302)

# --- simple key ---
r = anon.get(f"/sso/login?app_id=wideapp&username=Echo&key={wide}", follow_redirects=False)
check("simple mode off by default",
      r.status_code == 403 and "该应用未开启简单 key 模式" in page(r))

simple = j(admin.post("/api/admin/sso_apps", json={
    "name": "simple", "app_id": "simpleapp", "allow_simple_key": True,
}))
surl = f"/sso/login?app_id=simpleapp&username=Echo&key={simple['secret']}"
r1 = anon.get(surl, follow_redirects=False)
r2 = anon.get(surl, follow_redirects=False)
check("simple mode works and has no anti-replay", r1.status_code == 302 and r2.status_code == 302)
r = anon.get(f"/sso/login?app_id=simpleapp&username=Echo&key={'0'*64}", follow_redirects=False)
check("simple mode bad key", r.status_code == 403 and "密钥错误" in page(r))

# --- disable + reset secret ---
pk = j(admin.get("/api/admin/sso_apps"))["apps"]
perm_pk = next(a["id"] for a in pk if a["app_id"] == "permapp")
admin.post(f"/api/admin/sso_apps/{perm_pk}/toggle", json={"is_active": False})
r = anon.get(url("permapp", "Echo", perm), follow_redirects=False)
check("disabled app rejected", r.status_code == 403 and "应用不存在或已禁用" in page(r))
admin.post(f"/api/admin/sso_apps/{perm_pk}/toggle", json={"is_active": True})

new_secret = j(admin.post(f"/api/admin/sso_apps/{perm_pk}/reset_secret"))["secret"]
check("reset returns a new secret", new_secret and new_secret != perm)
r = anon.get(url("permapp", "Echo", perm), follow_redirects=False)
check("old secret stops working", r.status_code == 403 and "签名校验失败" in page(r))

# --- must_change_pw ---
sqlite3.connect(DB).execute(
    "UPDATE users SET must_change_pw = 1 WHERE username = 'Echo'").connection.commit()
b = app.test_client()
b.get(url("testapp", "Echo", secret))
r = b.get("/people", follow_redirects=False)
check("must_change_pw still enforced",
      r.status_code == 302 and "/change_password" in (r.headers.get("Location") or ""))
sqlite3.connect(DB).execute(
    "UPDATE users SET must_change_pw = 0 WHERE username = 'Echo'").connection.commit()

# --- audit ---
actions = [r[0] for r in sqlite3.connect(DB).execute(
    "SELECT action FROM audit_log WHERE action LIKE 'sso%'").fetchall()]
check("audit written", "sso.login" in actions and "sso_app.create" in actions
      and "sso.denied" in actions)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} -> {FAILS}")
    sys.exit(len(FAILS))
print("all checks passed")
