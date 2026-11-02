"""One customer for the follow-up timeline + reminder banner test."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx FollowUp Target",
                    email="rrwebfx_followup@stub.local", status="待联系")
