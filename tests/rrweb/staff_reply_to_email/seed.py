"""One recipient so we can send a stub email and inspect the Reply-To header."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx ReplyTo Recipient",
                    email="rrwebfx_replyto@stub.local", status="待联系")
