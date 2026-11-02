"""One customer with one IG placeholder; we'll exercise add/delete in the drawer."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    pid = lib.make_person(
        name="RRWebFx Handles Customer",
        email="rrwebfx_handles@example.local",
        status="待联系",
    )
    lib.attach_placeholder(pid, platform="instagram", handle="rrwebfx_seedhandle")
