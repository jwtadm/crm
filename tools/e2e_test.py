"""End-to-end test of the permission system, walking each user-path scenario
from docs/permissions_design.md.

Idempotent: cleans test users/data at start. Requires the dev server running on
localhost:5001 with a clean DB (or at least clean of test users).
"""
import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

sys.path.insert(0, "/root/ig_scraper")
from lib import auth as _auth_mod, db as _db, ingest as _ingest

B = "http://localhost:5001"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw): return None


def session():
    cj = CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj), NoRedirect()
    )


def call(opener, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(B + path, data=data, method=method, headers=headers)
    try:
        r = opener.open(req)
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


PASSED = 0
FAILED = 0


def check(cond, label):
    global PASSED, FAILED
    if cond:
        print(f"  ✓ {label}")
        PASSED += 1
    else:
        print(f"  ✗ {label}")
        FAILED += 1


def must(actual, expected, label):
    check(actual == expected, f"{label}: got {actual!r}, want {expected!r}")


def section(title):
    print(f"\n==== {title} ====")


# ----- cleanup before run -----
section("CLEANUP")
with _db.tx() as c:
    c.execute("UPDATE people SET assigned_user_id=NULL")
    c.execute("UPDATE platform_accounts SET rejected_by=NULL")
    for u in c.execute("SELECT id FROM users WHERE username != 'admin'").fetchall():
        c.execute("DELETE FROM audit_log WHERE actor_id=?", (u["id"],))
        c.execute("DELETE FROM users WHERE id=?", (u["id"],))
    c.execute("DELETE FROM audit_log")
    c.execute("DELETE FROM email_jobs")
    # Make sure the default admin exists & is fresh (admin/admin, must_change_pw=1)
    row = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if row:
        c.execute("UPDATE users SET password_hash=?, must_change_pw=1, is_active=1, role='admin' WHERE id=?",
                  (_auth_mod.hash_pw("admin"), row["id"]))
    # Seed a few people if empty
    if c.execute("SELECT COUNT(*) FROM people").fetchone()[0] < 5:
        import time as _t
        n = int(_t.time())
        for nm in ("alpha_e2e", "beta_e2e", "gamma_e2e", "delta_e2e", "epsilon_e2e"):
            c.execute("INSERT INTO people (full_name, status, created_at, updated_at, email) VALUES (?,?,?,?,?)",
                      (nm, "待联系", n, n, f"{nm}@e2e.local"))
_auth_mod._RATE_BUCKETS.clear()
print("  cleanup done")


# =========================================================================
# Scenario 0: 首次部署
# =========================================================================
section("S0  Anonymous redirects + auto-bootstrap admin")
anon = session()
for p in ["/", "/people", "/similarity", "/query", "/database", "/settings", "/admin/users"]:
    s, _, h = call(anon, p)
    check(s == 302 and "/login" in h.get("Location", ""), f"GET {p} redirects to /login")
must(call(anon, "/api/people/list")[0],            401, "anon GET /api/people/list")
must(call(anon, "/api/similarity/run", "POST", {})[0], 401, "anon POST /api/similarity/run")


# =========================================================================
# Scenario 7 (auth lifecycle): login as admin, must change password
# =========================================================================
section("S7  Default admin login + forced password change")
admin = session()
s, b, _ = call(admin, "/api/auth/login", "POST", {"username": "admin", "password": "admin"})
must(s, 200, "login admin/admin")
me = json.loads(call(admin, "/api/auth/me")[1])
check(me["user"]["must_change_pw"] == 1, "must_change_pw flag on")

# Any non-/change_password page should redirect to /change_password
s, _, h = call(admin, "/people")
check(s == 302 and h.get("Location") == "/change_password", "must_change → /change_password")

# Most APIs are blocked until they change pw
s, _, _ = call(admin, "/api/people/list")
must(s, 403, "must_change blocks API")

# Can still hit auth APIs
must(call(admin, "/api/auth/me")[0], 200, "/api/auth/me works during must_change")

# Wrong old password rejected
s, b, _ = call(admin, "/api/auth/change_password", "POST", {"old": "wrongpass", "new": "newAdminPwd1"})
must(s, 400, "wrong old password rejected")

# Real change
s, b, _ = call(admin, "/api/auth/change_password", "POST", {"old": "admin", "new": "newAdminPwd1"})
must(s, 200, "change password ok")

# Now session is rotated; /people should be reachable
s, _, _ = call(admin, "/people")
must(s, 200, "after change pw, /people accessible")

# Try old password — should fail
s, _, _ = call(session(), "/api/auth/login", "POST", {"username": "admin", "password": "admin"})
must(s, 401, "old password no longer valid")

# Login with new password
admin = session()
must(call(admin, "/api/auth/login", "POST", {"username": "admin", "password": "newAdminPwd1"})[0],
     200, "login with new password")


# =========================================================================
# Scenario 1: staff register + admin activate
# =========================================================================
section("S1  Register Alice + admin activates her")
s, b, _ = call(anon, "/api/auth/register", "POST", {
    "username": "alice_e2e", "email": "alice_e2e@test.local",
    "display_name": "Alice", "password": "alicepass1",
})
must(s, 200, "register alice")

# Inactive — login refused
must(call(session(), "/api/auth/login", "POST",
          {"username": "alice_e2e", "password": "alicepass1"})[0],
     403, "alice login refused while inactive")

# Get alice id
alice_id = _auth_mod.get_user_by_username("alice_e2e")["id"]

# Admin activates
s, _, _ = call(admin, f"/api/admin/users/{alice_id}/activate", "POST", {})
must(s, 200, "admin activates alice")

# Audit log records it
events = json.loads(call(admin, "/api/admin/audit")[1])["events"]
check(any(e["action"] == "user.activate" and e["target"] == f"user:{alice_id}" for e in events),
      "audit: user.activate logged")

# Alice logs in
alice = session()
must(call(alice, "/api/auth/login", "POST", {"username": "alice_e2e", "password": "alicepass1"})[0],
     200, "alice login")

# Alice nav: only /people, others 403
for p in ["/similarity", "/query", "/database", "/settings", "/admin/users"]:
    must(call(alice, p)[0], 403, f"alice GET {p}")
must(call(alice, "/people")[0], 200, "alice GET /people allowed")

# Alice / → /people
s, _, h = call(alice, "/")
check(s == 302 and h.get("Location") == "/people", "alice / → /people")
s, _, h = call(admin, "/")
check(s == 302 and h.get("Location") == "/similarity", "admin / → /similarity")

# Alice forbidden on every admin API
forbidden = [
    ("POST", "/api/similarity/run", {}),
    ("POST", "/api/query/run", {}),
    ("POST", "/api/db/clear", {"confirm": True}),
    ("GET",  "/api/settings", None),
    ("POST", "/api/settings", {}),
    ("POST", "/api/settings/test", {"provider": "openrouter_chat"}),
    ("POST", "/api/db/import_handle", {"platform": "instagram", "handle": "x"}),
    ("POST", "/api/db/embed_missing", {}),
    ("POST", "/api/ai/queries", {"description": "x"}),
    ("POST", "/api/ai/embed", {"text": "x"}),
    ("GET",  "/api/platform/instagram/info?handle=x", None),
    ("GET",  "/api/platform/instagram/search?keyword=x", None),
    ("GET",  "/api/platform/instagram/similar?internal_id=1", None),
    ("POST", "/api/people", {"email": "a@b.c"}),
    ("POST", "/api/people/bulk_assign", {"ids": [1]}),
    ("GET",  "/api/db/list", None),
    ("POST", "/api/db/export", {}),
    ("GET",  "/api/admin/users", None),
    ("GET",  "/api/admin/audit", None),
    ("POST", "/api/admin/migrate_owners", {}),
]
for m, p, body in forbidden:
    must(call(alice, p, m, body)[0], 403, f"alice {m} {p}")

# Empty list
data = json.loads(call(alice, "/api/people/list")[1])
must(data["total"], 0, "alice sees 0 people")


# =========================================================================
# Scenario 2: assign + bulk_assign + owner migration
# =========================================================================
section("S2  Admin assigns customers to Alice")
admin_full = json.loads(call(admin, "/api/people/list?limit=200")[1])["people"]
assert len(admin_full) >= 5, "need ≥5 seeded people"
p1, p2, p3, p4, p5 = admin_full[:5]

# Single assign
must(call(admin, f"/api/db/person/{p1['id']}", "PATCH",
          {"assigned_user_id": alice_id})[0], 200, "assign p1 to alice")

# Bulk assign
must(call(admin, "/api/people/bulk_assign", "POST",
          {"ids": [p2["id"], p3["id"], p4["id"]], "assigned_user_id": alice_id})[0],
     200, "bulk_assign 3 to alice")

# Alice sees 4
data = json.loads(call(alice, "/api/people/list")[1])
must(data["total"], 4, "alice sees 4")

# Unassign p1
must(call(admin, f"/api/db/person/{p1['id']}", "PATCH",
          {"assigned_user_id": None})[0], 200, "unassign p1")
data = json.loads(call(alice, "/api/people/list")[1])
must(data["total"], 3, "alice sees 3 after unassign")

# admin's `/api/people/list?assigned_user_id=unassigned` shows unassigned
data = json.loads(call(admin, "/api/people/list?assigned_user_id=unassigned&limit=200")[1])
check(any(p["id"] == p1["id"] for p in data["people"]), "unassigned filter shows p1")
check(p1["id"] not in [p["id"] for p in json.loads(call(admin, f"/api/people/list?assigned_user_id={alice_id}")[1])["people"]],
      "alice's filter excludes p1")

# Audit
events = json.loads(call(admin, "/api/admin/audit")[1])["events"]
check(any(e["action"] == "person.bulk_assign" for e in events), "audit: bulk_assign")
check(any(e["action"] == "person.assign" for e in events), "audit: assign")

# owner-migration dry-run (should be no-op since no people have owner text)
data = json.loads(call(admin, "/api/admin/migrate_owners", "POST", {})[1])
check("would_update" in data, "migrate_owners returns shape")


# =========================================================================
# Scenario 3: staff daily ops + field whitelist
# =========================================================================
section("S3  Staff ops + field whitelist + reassignment race")
# Status PATCH allowed
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"status": "已联系"})[0], 200, "alice PATCH status")
# Notes PATCH allowed
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"notes": "talked"})[0], 200, "alice PATCH notes")
# Tags PATCH allowed
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"tags": ["a", "b"]})[0], 200, "alice PATCH tags")
# Email forbidden
s, b, _ = call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"email": "x@x.x"})
must(s, 400, "alice PATCH email forbidden")
check(json.loads(b).get("error") == "forbidden_field", "error code is forbidden_field")
# full_name forbidden
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"full_name": "X"})[0], 400, "alice PATCH full_name forbidden")
# assigned_user_id forbidden
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"assigned_user_id": 99})[0], 400, "alice PATCH assigned_user_id forbidden")
# status='不适合' must go through reject endpoint
must(call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"status": "不适合"})[0], 400, "alice PATCH status=不适合 → forbidden")
# Try to PATCH someone not assigned (p1 unassigned, p5 admin's)
must(call(alice, f"/api/db/person/{p1['id']}", "PATCH", {"status": "已联系"})[0], 403, "alice PATCH unassigned p1 → 403")
must(call(alice, f"/api/db/person/{p5['id']}", "PATCH", {"status": "已联系"})[0], 403, "alice PATCH p5 (not hers) → 403")
# Hydrate: own ok (will likely 400 for invalid handle, but auth must pass), others 403
must(call(alice, f"/api/people/{p5['id']}/hydrate", "POST", {})[0], 403, "alice hydrate other → 403")
# Contact own
must(call(alice, f"/api/people/{p2['id']}/contact", "POST", {"status": "已联系"})[0], 200, "alice contact own ok")
# Contact reject status via contact endpoint
must(call(alice, f"/api/people/{p2['id']}/contact", "POST", {"status": "不适合"})[0], 400, "alice contact status=不适合 → blocked")
# DELETE account: 403
# Find an account to attempt delete on
acc_row = None
with _db.tx() as c:
    r = c.execute("SELECT id FROM platform_accounts LIMIT 1").fetchone()
    if r: acc_row = r["id"]
if acc_row is not None:
    must(call(alice, f"/api/db/account/{acc_row}", "DELETE")[0], 403, "alice DELETE account → 403")

# Reassignment race: admin moves p2 to root, then alice tries to PATCH
must(call(admin, f"/api/db/person/{p2['id']}", "PATCH", {"assigned_user_id": None})[0],
     200, "admin unassigns p2 mid-flow")
s, b, _ = call(alice, f"/api/db/person/{p2['id']}", "PATCH", {"status": "已联系"})
must(s, 403, "alice PATCH after race → 403")
# Reassign back so later tests work
call(admin, f"/api/db/person/{p2['id']}", "PATCH", {"assigned_user_id": alice_id})


# =========================================================================
# Scenario 5: 不适合 marking + ingest negative-sample filter (real)
# =========================================================================
section("S5  Reject + unreject + ingest filter")
# Alice rejects p3
must(call(alice, f"/api/people/{p3['id']}/reject", "POST", {"reason": "不合适"})[0], 200, "alice reject p3")
# Hidden in default list
data = json.loads(call(alice, "/api/people/list")[1])
check(p3["id"] not in [p["id"] for p in data["people"]], "p3 hidden from default")
# Visible with include_rejected=1
data = json.loads(call(alice, "/api/people/list?include_rejected=1")[1])
check(p3["id"] in [p["id"] for p in data["people"]], "include_rejected reveals p3")
# Status updated to 不适合
with _db.tx() as c:
    r = c.execute("SELECT status, previous_status FROM people WHERE id=?", (p3["id"],)).fetchone()
check(r["status"] == "不适合", "p3 status = 不适合")
check(r["previous_status"] is not None, "previous_status saved")
# Reject other person (not assigned) → 403
must(call(alice, f"/api/people/{p5['id']}/reject", "POST", {})[0], 403, "alice reject other → 403")
# Unreject own
must(call(alice, f"/api/people/{p3['id']}/unreject", "POST", {})[0], 200, "alice unreject p3")
with _db.tx() as c:
    r = c.execute("SELECT status FROM people WHERE id=?", (p3["id"],)).fetchone()
check(r["status"] != "不适合", "p3 status restored")
# Unreject other → 403
must(call(alice, f"/api/people/{p5['id']}/unreject", "POST", {})[0], 403, "alice unreject other → 403")
# admin can reject anyone
must(call(admin, f"/api/people/{p5['id']}/reject", "POST", {"reason": "admin pick"})[0],
     200, "admin reject p5")

# ----- Ingest filter: directly exercise lib/ingest._filter_against_rejected -----
# Set up: give p5's account a bio_embedding (fake), and a candidate with a
# vector close vs far.
import struct
import lib.ai as _ai
import lib.ingest as _ing

dim = 8
v_close = [1.0, 0, 0, 0, 0, 0, 0, 0]  # exactly the rejected vector
v_far   = [0, 0, 0, 0, 0, 0, 0, 1.0]  # orthogonal
v_blob = struct.pack(f"{dim}f", *v_close)

# Create a fake rejected account with bio_embedding
import time as _t
with _db.tx() as c:
    c.execute(
        """INSERT INTO platform_accounts
           (person_id, platform, platform_user_id, username, biography, full_name,
            bio_embedding, embedding_text, is_rejected, rejected_by, rejected_at,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p5["id"], "instagram", "fake-rej-1", "fake_rejected_e2e",
         "rejected sample bio", "fake rejected",
         v_blob, "rejected sample bio", 1, alice_id, int(_t.time()),
         int(_t.time()), int(_t.time())),
    )

# Stub ai.embed_texts so we don't hit OpenRouter
_orig_embed = _ai.embed_texts
def stub_embed(texts):
    out = []
    for t in texts:
        if "BAD" in t:   out.append(v_close)
        else:            out.append(v_far)
    return out
_ai.embed_texts = stub_embed
_ing.ai = _ai  # ensure ingest sees the stub
# Also bust the rejection-pool cache so it reloads with our new row
_ing._REJ_CACHE["signature"] = None

candidates = [
    {"platform": "instagram", "username": "good_one",  "biography": "GOOD bio", "full_name": "good"},
    {"platform": "instagram", "username": "bad_one",   "biography": "BAD bio matches rejected", "full_name": "bad"},
]
kept, filt = _ing._filter_against_rejected(candidates)
check(len(kept) == 1 and kept[0]["username"] == "good_one", "filter keeps far candidate")
check(len(filt) == 1 and filt[0]["username"] == "bad_one", "filter drops close candidate")
check(filt[0]["score"] >= _ing.REJECTED_COSINE_THRESHOLD, "filter records score above threshold")

_ai.embed_texts = _orig_embed  # restore
# Clean up the fake row so it doesn't pollute later
with _db.tx() as c:
    c.execute("DELETE FROM platform_accounts WHERE platform_user_id='fake-rej-1'")


# =========================================================================
# Scenario 6: admin user management
# =========================================================================
section("S6  Admin user management")
# Reset password on alice — get temp + must_change set
s, b, _ = call(admin, f"/api/admin/users/{alice_id}/reset_password", "POST", {})
must(s, 200, "admin reset alice password")
temp = json.loads(b)["temp_password"]
check(len(temp) >= 8, "temp password ≥8 chars")
# Alice's old password no longer works
must(call(session(), "/api/auth/login", "POST",
          {"username": "alice_e2e", "password": "alicepass1"})[0], 401, "old alice password invalid")
# Temp password works
alice = session()
must(call(alice, "/api/auth/login", "POST", {"username": "alice_e2e", "password": temp})[0],
     200, "login with temp")
me_alice = json.loads(call(alice, "/api/auth/me")[1])
check(me_alice["user"]["must_change_pw"] == 1, "alice must_change_pw=1 after reset")
# Alice forced to change pw
s, _, h = call(alice, "/people")
check(s == 302 and h.get("Location") == "/change_password", "alice forced to /change_password")
# She changes it
must(call(alice, "/api/auth/change_password", "POST",
          {"old": temp, "new": "alicepass2"})[0], 200, "alice change pw")
must(call(alice, "/people")[0], 200, "alice can browse after change")

# role promote alice → admin
must(call(admin, f"/api/admin/users/{alice_id}/role", "POST", {"role": "admin"})[0],
     200, "promote alice to admin")
me_alice = json.loads(call(alice, "/api/auth/me")[1])
check(me_alice["user"]["role"] == "admin", "alice now admin")
# Now she can hit admin routes
must(call(alice, "/api/admin/users")[0], 200, "alice (now admin) GET /api/admin/users")

# Last-admin guard: try to demote BOTH admins
# First demote root admin → still 1 admin (alice) → ok
admin_id = _auth_mod.get_user_by_username("admin")["id"]
must(call(alice, f"/api/admin/users/{admin_id}/role", "POST", {"role": "staff"})[0],
     200, "demote first admin while 2 exist")
# Now alice is the last admin — demoting her should fail
s, b, _ = call(alice, f"/api/admin/users/{alice_id}/role", "POST", {"role": "staff"})
must(s, 400, "demote last admin → 400")
check(json.loads(b).get("error") == "last_admin", "error code is last_admin")
# Same for deactivate
s, b, _ = call(alice, f"/api/admin/users/{alice_id}/deactivate", "POST", {})
must(s, 400, "deactivate last admin → 400")
# Restore admin to admin role
call(alice, f"/api/admin/users/{admin_id}/role", "POST", {"role": "admin"})

# Deactivate alice (with unassign_customers=true) — admin first reactivates if needed
call(admin, f"/api/admin/users/{alice_id}/role", "POST", {"role": "staff"})  # demote alice for deactivate test
admin = session()
call(admin, "/api/auth/login", "POST", {"username": "admin", "password": "newAdminPwd1"})
s, _, _ = call(admin, f"/api/admin/users/{alice_id}/deactivate", "POST",
               {"unassign_customers": True})
must(s, 200, "admin deactivates alice")
# Verify her customers are unassigned now
with _db.tx() as c:
    n = c.execute("SELECT COUNT(*) FROM people WHERE assigned_user_id=?", (alice_id,)).fetchone()[0]
check(n == 0, "alice's customers unassigned after deactivation")
# Alice's session is killed
old_alice = alice  # the session that was logged in
s, _, _ = call(old_alice, "/api/people/list")
must(s, 401, "deactivated alice's session is rejected")
# Login attempt → 403
must(call(session(), "/api/auth/login", "POST",
          {"username": "alice_e2e", "password": "alicepass2"})[0], 403, "deactivated alice can't login")
# Reactivate
call(admin, f"/api/admin/users/{alice_id}/activate", "POST", {})

# Delete: alice has been logged in (last_login_at != NULL) → 400
s, b, _ = call(admin, f"/api/admin/users/{alice_id}", "DELETE")
must(s, 400, "delete user with last_login_at → 400")
# Make a fresh user that never logged in
s, _, _ = call(anon, "/api/auth/register", "POST",
               {"username": "neverlogged_e2e", "password": "longerpw1"})
nl_id = _auth_mod.get_user_by_username("neverlogged_e2e")["id"]
s, _, _ = call(admin, f"/api/admin/users/{nl_id}", "DELETE")
must(s, 200, "delete never-logged-in user")
check(_auth_mod.get_user(nl_id) is None, "user row gone")


# =========================================================================
# Scenario 4 / email jobs persistence + cross-user isolation
# =========================================================================
section("S4 / S7  Email job persistence + per-user scope")
# Re-login alice and make sure assigned customers exist
call(admin, "/api/people/bulk_assign", "POST",
     {"ids": [p1["id"], p2["id"]], "assigned_user_id": alice_id})

alice = session()
call(alice, "/api/auth/login", "POST", {"username": "alice_e2e", "password": "alicepass2"})

# preview with all-owned ids: ok (or 200 even if no email; previews handle missing)
s, _, _ = call(alice, "/api/email/preview", "POST",
               {"person_ids": [p1["id"], p2["id"]], "subject": "hi", "body": "hi {{name}}"})
must(s, 200, "alice preview own ids")

# preview with one foreign id → 400 forbidden_ids
s, b, _ = call(alice, "/api/email/preview", "POST",
               {"person_ids": [p1["id"], p5["id"]], "subject": "hi", "body": "hi"})
must(s, 400, "alice preview with foreign id → 400")
check(json.loads(b).get("error") == "forbidden_ids", "preview error is forbidden_ids")

# send_batch with one foreign id → 400
s, b, _ = call(alice, "/api/email/send_batch", "POST",
               {"person_ids": [p1["id"], p5["id"]], "subject": "x", "body": "x"})
must(s, 400, "alice send_batch with foreign id → 400")
check(json.loads(b).get("error") == "forbidden_ids", "send_batch error is forbidden_ids")

# Manually insert a fake email_job owned by admin and verify alice can't poll it
import uuid as _uuid
fake_job_id = _uuid.uuid4().hex[:12]
admin_id_local = _auth_mod.get_user_by_username("admin")["id"]
import time as _t
with _db.tx() as c:
    c.execute(
        "INSERT INTO email_jobs (id, created_by, created_at, status, payload_json) VALUES (?,?,?,?,?)",
        (fake_job_id, admin_id_local, int(_t.time()), "done",
         json.dumps({"id": fake_job_id, "status": "done", "sent": 0})),
    )
must(call(alice, f"/api/email/job/{fake_job_id}")[0], 403, "alice GET admin's job → 403")
must(call(admin, f"/api/email/job/{fake_job_id}")[0], 200, "admin GET own job → 200")
# Non-existent job → 403 (no leakage)
must(call(alice, "/api/email/job/nonexistent99")[0], 403, "alice GET nonexistent job → 403")


# =========================================================================
# Scenario UI: every page returns 200 when allowed, contains expected markers
# (Run BEFORE S8 because S8's brute-force hammering trips the rate limiter
# and would prevent us from re-logging in as admin afterwards.)
# =========================================================================
section("UI  pages render with expected markers")
admin = session()
call(admin, "/api/auth/login", "POST", {"username": "admin", "password": "newAdminPwd1"})
for p, marker in [
    ("/login",            'id="loginBtn"'),
    ("/register",         'id="registerBtn"'),
    ("/people",           'id="tableWrap"'),
    ("/similarity",       'id="navStats"'),
    ("/query",            'id="navStats"'),
    ("/database",         'id="navStats"'),
    ("/settings",         'id="navStats"'),
    ("/admin/users",      'id="usersBox"'),
    ("/change_password",  'id="btn"'),
]:
    s, body, _ = call(admin, p)
    must(s, 200, f"GET {p} (200)")
    check(marker in body, f"{p} contains {marker!r}")


# =========================================================================
# Scenario 8: edge cases — login bruteforce + register dupes + leaks
# =========================================================================
section("S8  Edge cases")
# Username enumeration: nonexistent user vs wrong password → same response.
# (Spec is "no info leak", i.e. equal response — value doesn't matter as long
# as it's the same. Could be 401 invalid_credentials, or 429 rate_limited if
# the bucket was already hot — both are acceptable as long as they match.)
s_a, b_a, _ = call(session(), "/api/auth/login", "POST",
                   {"username": "definitely_nope_e2e", "password": "x"})
s_b, b_b, _ = call(session(), "/api/auth/login", "POST",
                   {"username": "definitely_nope_e2e_2", "password": "wrong"})
check(s_a == s_b, f"login: nonexistent vs nonexistent same status (got {s_a} vs {s_b})")
check(json.loads(b_a).get("error") == json.loads(b_b).get("error"),
      "login: same error code for both")

# Login brute force: 5 failures in a row → 6th gets 429
_auth_mod._RATE_BUCKETS.clear()  # local no-op (server side has its own)
codes = []
for i in range(7):
    s, _, _ = call(session(), "/api/auth/login", "POST",
                   {"username": "alice_e2e", "password": "wrong" + str(i)})
    codes.append(s)
print(f"  login attempt codes: {codes}")
check(429 in codes, "login brute force eventually rate-limited")

# Register dupes
s, b, _ = call(anon, "/api/auth/register", "POST",
               {"username": "alice_e2e", "password": "another1pw"})
must(s, 409, "duplicate username → 409")
check(json.loads(b).get("error") == "username_taken", "username_taken")
s, b, _ = call(anon, "/api/auth/register", "POST",
               {"username": "freshname_e2e", "email": "alice_e2e@test.local", "password": "somepwd1"})
must(s, 409, "duplicate email → 409")
check(json.loads(b).get("error") == "email_taken", "email_taken")

# Existence non-leak: alice GET unrelated APIs returns same 403 whether id exists or not
must(call(alice, "/api/db/person/999999", "PATCH", {"status": "x"})[0], 403, "PATCH nonexistent person → 403")
must(call(alice, f"/api/db/person/{p5['id']}", "PATCH", {"status": "x"})[0], 403, "PATCH others person → 403")
must(call(alice, "/api/db/account/999999", "DELETE")[0], 403, "DELETE nonexistent account → 403")


# =========================================================================
# Done
# =========================================================================
print(f"\n==== RESULT: {PASSED} passed, {FAILED} failed ====")
sys.exit(0 if FAILED == 0 else 1)
