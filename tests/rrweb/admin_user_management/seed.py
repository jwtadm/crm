"""Seed one inactive staff user that we'll drive through the admin panel:
activate → reset password → role toggle → deactivate → delete.

Done via auth.create_user so the user starts in a known state independent of
the UI registration flow (which has its own test).
"""
import os
import sys
sys.path.insert(0, "/root/ig_scraper")

from lib import auth, db


FIXTURE_STAFF_USERNAME = "rrwebfx_managed_staff"


def _drop() -> None:
    db.init_db()
    u = auth.get_user_by_username(FIXTURE_STAFF_USERNAME)
    if u:
        with db.tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (u["id"],))


def seed() -> None:
    _drop()  # idempotent
    auth.create_user(
        username=FIXTURE_STAFF_USERNAME,
        email="rrwebfx_managed_staff@example.local",
        display_name="RRWebFx Managed Staff",
        password=os.environ.get("SCOUT_RRWEB_MANAGED_PW", "rrwebfx-temp-pw-1"),
        role="staff",
        is_active=0,           # starts inactive so we can hit the 激活 button
        must_change_pw=0,
    )


teardown = _drop
