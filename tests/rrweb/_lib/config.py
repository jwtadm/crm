"""Shared config for rrweb regression tests.

Tests run against a Flask instance booted by `run.py` from
`tools/dev_with_ig_stub.py`. Both processes inherit `SCOUT_DB_PATH` from the
runner, which points at a dedicated test DB (`tests/.scout-test.db`) — the
production `scout.db` is NEVER touched. The seed module enforces this with
a hard assert before any write.
"""
import os
from pathlib import Path

APP_URL = "http://localhost:5001"
APP_PORT = 5001

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("SCOUT_RRWEB_ADMIN_PW", "admin12345")  # test fixture pw; rotated each run

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEST_DB_PATH = (REPO_ROOT / "tests" / ".scout-test.db").resolve()


def in_test_mode() -> bool:
    """True iff the runner has wired the harness DB into the env."""
    cur = os.environ.get("SCOUT_DB_PATH")
    return bool(cur) and Path(cur).resolve() == TEST_DB_PATH
