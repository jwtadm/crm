"""Verify a non-admin staff can use the new add-customer entry. Seed creates
a dedicated staff user; the test logs in as them, adds a customer through
the modal, asserts the new row appears + is auto-assigned to that staff.
"""
import os
import sys
sys.path.insert(0, "/root/ig_scraper")
from lib import auth, db


FIXTURE_USERNAME = "rrwebfx_addcust_staff"


def _drop() -> None:
    db.init_db()
    u = auth.get_user_by_username(FIXTURE_USERNAME)
    if u:
        uid = u["id"]
        with db.tx() as c:
            # NULL every FK that points at this user so the users-row
            # delete doesn't trip an ON DELETE NO ACTION constraint.
            # Five tables reference users(id): people, audit_log,
            # email_jobs, platform_accounts.rejected_by, reminders,
            # followup_logs. email_jobs.created_by is NOT NULL so we
            # delete rather than NULL.
            c.execute("UPDATE people SET assigned_user_id=NULL WHERE assigned_user_id=?", (uid,))
            c.execute("UPDATE audit_log SET actor_id=NULL WHERE actor_id=?", (uid,))
            c.execute("UPDATE platform_accounts SET rejected_by=NULL WHERE rejected_by=?", (uid,))
            c.execute("UPDATE reminders SET actor_id=NULL WHERE actor_id=?", (uid,))
            c.execute("UPDATE followup_logs SET actor_id=NULL WHERE actor_id=?", (uid,))
            c.execute("DELETE FROM email_jobs WHERE created_by=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))


def seed() -> None:
    _drop()
    auth.create_user(
        username=FIXTURE_USERNAME,
        email="rrwebfx_addcust@stub.local",
        display_name="RRWebFx AddCust Staff",
        password=os.environ.get("SCOUT_RRWEB_STAFF_PW", "rrwebfx-addcust-pw"),
        role="staff",
        is_active=1,
        must_change_pw=0,
    )


teardown = _drop
