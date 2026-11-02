"""Fixture for hard-delete: one disposable person with an IG handle."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    pid = lib.make_person(
        name="RRWebFx Delete Target",
        email="rrwebfx_delete@trash.local",
        status="待联系",
    )
    lib.attach_placeholder(pid, platform="instagram", handle="rrwebfx_to_delete")
