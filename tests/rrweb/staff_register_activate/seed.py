"""Wipe any prior fixture staff user; admin is ensured by the runner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib import auth, db


FIXTURE_STAFF_USERNAME = "rrwebfx_staff"


def seed() -> None:
    # Drop any prior staff user — the steps file will register a fresh one.
    db.init_db()
    u = auth.get_user_by_username(FIXTURE_STAFF_USERNAME)
    if u:
        with db.tx() as c:
            c.execute("DELETE FROM users WHERE id=?", (u["id"],))


teardown = seed  # idempotent: just clean up
