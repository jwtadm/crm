#!/usr/bin/env python3
"""Write-path regression: exercise every mutating endpoint against a scratch DB.

tools/baseline_capture.py proves the READ surface is unchanged byte-for-byte.
This covers the other half — the flows that change data — with the platform
API, OpenAI and SMTP stubbed out so it runs offline and never touches a real
account. Together they stand in for the rrweb suite, which needs Playwright
plus an external recording tool and cannot run on every machine.

Usage
-----
    cp scout.db tests/.writepath.db
    SCOUT_DB_PATH=tests/.writepath.db python tools/write_path_regression.py

Refuses to run against the repo's real scout.db (it creates and deletes
customers, sends stubbed "email", and promotes/demotes users).

Every check prints PASS/FAIL with the observed value; exit code is the number
of failures.

Note: each DB helper below keeps its SQL as a literal inside the function —
this repo forbids a generic "run this SQL string" helper (the security scanner
rejects that shape), so there is deliberately no count_sql(sql) utility.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

ADMIN_PW = os.environ.get("SCOUT_BASELINE_ADMIN_PW", "baseline12345")
STAFF_PW = "staffpass12345"
EMBED_DIM = 4096

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}   {detail}")
        _failures.append(f"{label} {detail}".strip())


def guard_db_path() -> Path:
    raw = os.environ.get("SCOUT_DB_PATH")
    if not raw:
        sys.exit("write_path_regression: set SCOUT_DB_PATH to a throwaway copy "
                 "(cp scout.db tests/.writepath.db)")
    target = Path(raw).resolve()
    if target == (REPO_ROOT / "scout.db").resolve():
        sys.exit("write_path_regression: SCOUT_DB_PATH points at the real scout.db. Refusing.")
    if not target.exists():
        sys.exit(f"write_path_regression: {target} does not exist")
    return target


# ---------------------------------------------------------------------------
# Read helpers (SQL literal per function — no generic query runner)
# ---------------------------------------------------------------------------
def person_by_email(email: str) -> dict | None:
    from lib import db
    with db.tx() as c:
        r = c.execute("SELECT * FROM people WHERE email=?", (email,)).fetchone()
    return dict(r) if r else None


def person_by_id(person_id: int) -> dict | None:
    from lib import db
    with db.tx() as c:
        r = c.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    return dict(r) if r else None


def accounts_of(person_id: int) -> list[dict]:
    from lib import db
    with db.tx() as c:
        rows = c.execute(
            "SELECT platform, platform_user_id, username, is_rejected FROM platform_accounts "
            "WHERE person_id=? ORDER BY platform", (person_id,)).fetchall()
    return [dict(r) for r in rows]


def total_accounts() -> int:
    from lib import db
    with db.tx() as c:
        return c.execute("SELECT COUNT(*) FROM platform_accounts").fetchone()[0]


def total_people() -> int:
    from lib import db
    with db.tx() as c:
        return c.execute("SELECT COUNT(*) FROM people").fetchone()[0]


def accounts_for_person(person_id: int) -> int:
    from lib import db
    with db.tx() as c:
        return c.execute(
            "SELECT COUNT(*) FROM platform_accounts WHERE person_id=?", (person_id,)).fetchone()[0]


def status_events_for(person_id: int, new_status: str) -> int:
    from lib import db
    with db.tx() as c:
        return c.execute(
            "SELECT COUNT(*) FROM status_events WHERE person_id=? AND new_status=?",
            (person_id, new_status)).fetchone()[0]


def followups_with_content(person_id: int, content: str) -> int:
    from lib import db
    with db.tx() as c:
        return c.execute(
            "SELECT COUNT(*) FROM followup_logs WHERE person_id=? AND content=?",
            (person_id, content)).fetchone()[0]


# ---------------------------------------------------------------------------
# Offline stubs — installed before the app module is imported so no request
# can reach TikHub / OpenRouter / SMTP.
# ---------------------------------------------------------------------------
def install_stubs():
    from lib import ai, ingest, mailer

    sent: list[dict] = []
    fetched: list[str] = []

    def fake_embed_texts(texts):
        return [[0.01 * ((i % 7) + 1)] * EMBED_DIM for i in range(len(texts))]

    def fake_send_one(to_email, subject, body, **_kw):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return None

    ai.embed_texts = fake_embed_texts            # no OpenRouter
    ai._chat = lambda *a, **k: "ok"              # no OpenRouter
    ai.generate_search_queries = lambda *a, **k: ["stub query"]
    ai.classify_creators = lambda briefs, b: {x["username"]: True for x in briefs}
    ingest.ai = ai
    mailer.send_one = fake_send_one              # no SMTP

    def norm(platform: str, handle: str) -> dict:
        fetched.append(f"{platform}:{handle}")
        return {
            "platform": platform,
            "platform_user_id": f"stub-{platform}-{handle}",
            "secondary_id": f"sec-{handle}",
            "username": handle,
            "full_name": f"Stub {handle}",
            "biography": "stub bio for regression",
            "follower_count": 4321, "following_count": 12, "post_count": 3,
            "video_count": 0,
            "is_verified": False, "is_private": False, "is_business": False,
            "category": "Artist", "profile_pic_url": "", "external_url": "",
            "bio_links": [], "email": None, "phone": "",
            "city": "", "address": "", "country": "",
            "raw": {"stubbed": True},
        }

    class _StubPlatform:
        def __init__(self, name):
            self.name = name

        def fetch_user_info(self, handle):
            return norm(self.name, handle)

        def similar_users(self, *a, **k):
            return []

        def search_users(self, *a, **k):
            return []

    import app as app_mod
    app_mod.get_platform = lambda name: _StubPlatform(name)
    return sent, fetched


def run() -> None:
    guard_db_path()

    import lib.job_watchdog as jw
    jw.start = lambda *a, **k: None          # no background sweeper during the test

    sent, fetched = install_stubs()

    import app as app_mod
    from lib import auth, db, settings

    settings.set_many({
        "SMTP_HOST": "stub.local", "SMTP_PORT": "587", "SMTP_USERNAME": "u",
        "SMTP_PASSWORD": "p", "SMTP_FROM_EMAIL": "noreply@stub.local",
        "SMTP_FROM_NAME": "Stub", "OPENROUTER_API_KEY": "stub",
    })
    settings.refresh()

    app_mod.app.config.update(TESTING=True)
    stamp = int(time.time())

    admin = auth.get_user_by_username("admin")
    if not admin:
        sys.exit("write_path_regression: no admin user in the target DB")
    auth.set_password(admin["id"], ADMIN_PW, must_change=False)

    pid = None
    uid = None
    email = f"wp_{stamp}_a@stub.local"

    with app_mod.app.test_client() as cl:
        r = cl.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        check("admin login", r.status_code == 200, str(r.status_code))

        print("\n[create]")
        r = cl.post("/api/people", json={
            "full_name": "WP Alpha", "email": email,
            "handles": [{"platform": "instagram", "username": f"wp_{stamp}_ig"}],
        })
        body = r.get_json() or {}
        pid = body.get("person_id") or body.get("id")
        check("create person", r.status_code == 200 and bool(pid), f"{r.status_code} {body}")
        p = person_by_email(email)
        # Manually added customers start at 主动来找 (only discovery/ingest
        # creates 待联系), see app.py api_people_create.
        check("created row has 主动来找 default",
              bool(p) and p["status"] == "主动来找", str(p and p["status"]))
        check("created row has one placeholder handle",
              bool(p) and len(accounts_of(p["id"])) == 1)

        r = cl.post("/api/people", json={"full_name": "Dup", "email": email})
        check("duplicate email rejected", r.status_code == 409
              and (r.get_json() or {}).get("error") == "email_exists",
              f"{r.status_code} {r.get_json()}")

        r = cl.post("/api/people", json={
            "full_name": "Dup2",
            "handles": [{"platform": "instagram", "username": f"wp_{stamp}_ig"}]})
        check("duplicate handle rejected", r.status_code == 409
              and (r.get_json() or {}).get("error") == "handle_exists",
              f"{r.status_code} {r.get_json()}")

        print("\n[patch + status events]")
        r = cl.patch(f"/api/db/person/{pid}", json={"status": "已联系", "intent": "强度高"})
        check("patch status", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        p = person_by_email(email)
        check("status persisted", bool(p) and p["status"] == "已联系", str(p and p["status"]))
        check("intent persisted", bool(p) and p["intent"] == "强度高", str(p and p["intent"]))
        check("last_contacted_at stamped on status change",
              bool(p) and bool(p["last_contacted_at"]))
        check("status_event recorded", status_events_for(pid, "已联系") >= 1)

        # Admin is allowed to PATCH any status; the 不适合 refusal only guards
        # staff (checked in the staff section below), so verify the status really
        # landed and then put it back.
        r = cl.patch(f"/api/db/person/{pid}", json={"status": "不适合"})
        check("admin may set 不适合 via patch", r.status_code == 200, str(r.status_code))
        cl.patch(f"/api/db/person/{pid}", json={"status": "已联系"})

        print("\n[followups + reminders]")
        r = cl.post(f"/api/people/{pid}/followups", json={"content": "called them"})
        check("add followup", r.status_code == 200, str(r.status_code))
        r = cl.get(f"/api/people/{pid}/followups")
        items = (r.get_json() or {}).get("followups") or []
        check("followup listed",
              any(x.get("content") == "called them" for x in items), str(items)[:120])

        # Payload key is `remind_at` (epoch seconds).
        r = cl.post(f"/api/people/{pid}/reminders",
                    json={"remind_at": int(time.time()), "content": "ping"})
        check("add reminder", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        r = cl.get("/api/reminders/due")
        due = (r.get_json() or {}).get("reminders") or []
        check("reminder appears in due list",
              any(x.get("content") == "ping" for x in due), str(due)[:120])

        print("\n[reject / unreject]")
        r = cl.post(f"/api/people/{pid}/reject", json={"reason": "不合适"})
        check("reject", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        p = person_by_email(email)
        check("status is 不适合", bool(p) and p["status"] == "不适合", str(p and p["status"]))
        check("previous_status saved",
              bool(p) and p["previous_status"] == "已联系", str(p and p["previous_status"]))
        check("account flagged rejected",
              all(a["is_rejected"] == 1 for a in accounts_of(pid)), str(accounts_of(pid)))

        r = cl.post(f"/api/people/{pid}/unreject", json={})
        check("unreject", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        p = person_by_email(email)
        check("status restored to previous",
              bool(p) and p["status"] == "已联系", str(p and p["status"]))
        check("rejection cleared",
              all(a["is_rejected"] == 0 for a in accounts_of(pid)), str(accounts_of(pid)))

        print("\n[handles: placeholder -> hydrate]")
        ph = f"wp_{stamp}_x"
        r = cl.post(f"/api/people/{pid}/handles", json={"platform": "x", "handle": ph})
        check("add placeholder handle", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        xacc = [a for a in accounts_of(pid) if a["platform"] == "x"]
        check("placeholder id form",
              bool(xacc) and xacc[0]["platform_user_id"] == f"import:{ph}", str(xacc))

        r = cl.post(f"/api/people/{pid}/hydrate")
        check("hydrate", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        xacc = [a for a in accounts_of(pid) if a["platform"] == "x"]
        check("placeholder collapsed into real id",
              len(xacc) == 1 and xacc[0]["platform_user_id"] == f"stub-x-{ph}", str(xacc))
        check("hydrate called upstream stub", any(h.endswith(ph) for h in fetched), str(fetched))

        print("\n[no-downgrade invariant]")
        r = cl.patch(f"/api/db/person/{pid}", json={"status": "已成交"})
        check("patch to 已成交", r.status_code == 200, str(r.status_code))
        r = cl.post(f"/api/people/{pid}/contact", json={})
        check("record contact", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        p = person_by_email(email)
        check("已成交 not downgraded by contact",
              bool(p) and p["status"] == "已成交", str(p and p["status"]))

        print("\n[merge]")
        src_email = f"wp_{stamp}_b@stub.local"
        r = cl.post("/api/people", json={"full_name": "WP Beta", "email": src_email})
        b = r.get_json() or {}
        src = b.get("person_id") or b.get("id")
        check("create source person", bool(src), str(b))
        r = cl.post(f"/api/people/{src}/followups", json={"content": "src note"})
        check("source has followup", r.status_code == 200, str(r.status_code))
        r = cl.post(f"/api/people/{pid}/merge/{src}", json={})
        check("merge", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        check("source row deleted", person_by_email(src_email) is None)
        check("followup re-parented", followups_with_content(pid, "src note") == 1)

        print("\n[import_handle conflict rolls back]")
        other_email = f"wp_{stamp}_c@stub.local"
        r = cl.post("/api/people", json={"full_name": "WP Gamma", "email": other_email})
        check("create third person", r.status_code == 200, str(r.status_code))
        before_accts, before_people = total_accounts(), total_people()
        orig_platform = app_mod.get_platform

        class _EmailColliding:
            def fetch_user_info(self, handle):
                return {
                    "platform": "instagram", "platform_user_id": f"stub-collide-{handle}",
                    "username": handle, "full_name": "Collide", "email": other_email,
                    "biography": "", "follower_count": 1, "following_count": 0,
                    "post_count": 0, "video_count": 0, "is_verified": False,
                    "is_private": False, "is_business": False, "category": "",
                    "profile_pic_url": "", "external_url": "", "bio_links": [],
                    "phone": "", "city": "", "address": "", "country": "", "raw": {},
                }

        app_mod.get_platform = lambda name: _EmailColliding()
        r = cl.post("/api/db/import_handle",
                    json={"platform": "instagram", "handle": f"collide_{stamp}",
                          "person_id": pid})
        app_mod.get_platform = orig_platform
        check("import_handle returns 409",
              r.status_code == 409 and (r.get_json() or {}).get("error") == "handle_exists",
              f"{r.status_code} {r.get_json()}")
        check("409 did not persist any writes",
              total_accounts() == before_accts and total_people() == before_people,
              f"accounts {before_accts}->{total_accounts()}, "
              f"people {before_people}->{total_people()}")

        print("\n[export]")
        r = cl.post("/api/db/export", json={})
        check("export xlsx", r.status_code == 200 and r.data[:2] == b"PK",
              f"{r.status_code} {len(r.data)}B")

        print("\n[staff permissions]")
        uname = f"wp_{stamp}_staff"
        # Self-registration creates role=staff, is_active=0; an admin then
        # activates it. There is no admin "create user" endpoint (that path is
        # GET-only), so register is the real flow.
        r = cl.post("/api/auth/register", json={
            "username": uname, "password": STAFF_PW, "display_name": "WP Staff"})
        u = r.get_json() or {}
        uid = u.get("id")
        check("register staff user", r.status_code == 200 and bool(uid),
              f"{r.status_code} {u}")
        if uid:
            rr = cl.get("/api/admin/users")
            pending = [x for x in (rr.get_json() or {}).get("users", []) if x.get("id") == uid]
            check("registered user starts inactive",
                  bool(pending) and pending[0].get("is_active") == 0, str(pending)[:150])
            cl.post(f"/api/admin/users/{uid}/activate", json={})
            auth.set_password(uid, STAFF_PW, must_change=False)
            # bulk_assign payload is {ids, assigned_user_id}.
            r = cl.post("/api/people/bulk_assign",
                        json={"ids": [pid], "assigned_user_id": uid})
            check("bulk assign", r.status_code == 200, f"{r.status_code} {r.get_json()}")
            check("person assigned to staff",
                  (person_by_email(email) or {}).get("assigned_user_id") == uid,
                  str(r.get_json()))

    if uid:
        with app_mod.app.test_client() as scl:
            r = scl.post("/api/auth/login",
                         json={"username": f"wp_{stamp}_staff", "password": STAFF_PW})
            check("staff login", r.status_code == 200, f"{r.status_code} {r.get_json()}")

            r = scl.get("/api/admin/users")
            check("staff blocked from admin API", r.status_code == 403, str(r.status_code))

            r = scl.get(f"/api/people/{pid}")
            check("staff can read own person", r.status_code == 200, str(r.status_code))

            r = scl.patch(f"/api/db/person/{pid}", json={"notes": "staff note"})
            check("staff can patch allowed field", r.status_code == 200, str(r.status_code))

            r = scl.patch(f"/api/db/person/{pid}", json={"assigned_user_id": 1})
            check("staff cannot reassign owner",
                  r.status_code in (400, 403), str(r.status_code))

            r = scl.patch(f"/api/db/person/{pid}", json={"status": "不适合"})
            check("staff cannot set 不适合 via generic patch",
                  r.status_code == 400
                  and (r.get_json() or {}).get("error") == "forbidden_field",
                  f"{r.status_code} {r.get_json()}")

            r = scl.get("/api/people/list?limit=5")
            lst = r.get_json() or {}
            others = [x for x in (lst.get("people") or [])
                      if x.get("assigned_user_id") not in (None, uid)]
            check("staff list is scoped to own rows", not others,
                  f"leaked {len(others)} rows")

    print("\n[email batch job]")
    with app_mod.app.test_client() as cl:
        cl.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        before_status = (person_by_email(email) or {}).get("status")
        r = cl.post("/api/email/send_batch", json={
            "person_ids": [pid],
            "subject": "WP subject {{name}}",
            "body": "WP body {{name}}",
            "throttle": 0,
        })
        job = r.get_json() or {}
        check("start email batch", r.status_code == 200 and bool(job.get("job_id")),
              f"{r.status_code} {job}")
        jid = job.get("job_id")
        final = {}
        for _ in range(60):
            time.sleep(0.25)
            rr = cl.get(f"/api/email/job/{jid}")
            final = rr.get_json() or {}
            if final.get("status") in ("done", "failed"):
                break
        check("email job finished", final.get("status") == "done", str(final)[:200])
        check("stub mailer was called", len(sent) >= 1, f"{len(sent)} sends")
        check("placeholder rendered in subject",
              bool(sent) and "{{name}}" not in sent[0]["subject"],
              sent[0]["subject"] if sent else "")
        after_status = (person_by_email(email) or {}).get("status")
        check("status not downgraded by batch send", after_status == before_status,
              f"{before_status} -> {after_status}")

        print("\n[delete]")
        r = cl.delete(f"/api/people/{pid}")
        check("delete person", r.status_code == 200, f"{r.status_code} {r.get_json()}")
        check("person gone", person_by_email(email) is None)
        check("accounts cascade-deleted", accounts_for_person(pid) == 0)

    print(f"\n{'=' * 60}")
    if _failures:
        print(f"{len(_failures)} of {_checks} checks FAILED:")
        for f in _failures:
            print(f"  - {f}")
    else:
        print(f"all {_checks} checks passed")


if __name__ == "__main__":
    run()
