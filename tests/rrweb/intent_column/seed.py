"""Two persons for the intent (合作强度) dropdown column.

Intent starts unset (NULL) for both; the test exercises the dropdown,
asserts the PATCH lands, and verifies it survives a list reload.
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx Intent Target",
                    email="rrwebfx_intent1@stub.local", status="待联系")
    lib.make_person(name="RRWebFx Intent Other",
                    email="rrwebfx_intent2@stub.local", status="待联系")
