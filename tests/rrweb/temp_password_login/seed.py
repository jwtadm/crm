"""Seed one active staff user. The test will trigger an admin password reset
on this fixture user, capture the returned temp password, then verify the
staff can log in with it and walk through the must_change_pw → /change_password
flow.

CRUCIAL: this user is a fixture (rrwebfx_ prefix) so the temp-password reset
NEVER touches real staff. The runner-wired test DB makes that doubly safe.
"""
import sys
sys.path.insert(0, "/root/ig_scraper")

from lib import auth, db


FIXTURE_STAFF_USERNAME = "rrwebfx_pwtest_staff"
FIXTURE_STAFF_INIT_PW = "rrwebfx-init-pw"


def _drop() -> None:
    db.init_db()
    u = auth.get_user_by_username(FIXTURE_STAFF_USERNAME)
    if u:
        with db.tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (u["id"],))


def seed() -> None:
    _drop()
    auth.create_user(
        username=FIXTURE_STAFF_USERNAME,
        email="rrwebfx_pwtest@example.local",
        display_name="RRWebFx PwTest Staff",
        password=FIXTURE_STAFF_INIT_PW,
        role="staff",
        is_active=1,
        must_change_pw=0,
    )


teardown = _drop
