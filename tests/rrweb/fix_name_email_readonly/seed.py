"""Fixtures for the read-only-name/email regression.

We just need at least one person with a non-empty name + email so we can
click into the drawer and edit there. The wipe step drops any prior fixture
rows automatically.
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(
        name="RRWebFx Readonly Target",
        email="rrwebfx_readonly@example.local",
        status="待联系",
    )
