"""Group mailer needs ≥1 person with an email under admin scope."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx Mailer Recipient 1",
                    email="rrwebfx_recip1@stub.local", status="待联系")
    lib.make_person(name="RRWebFx Mailer Recipient 2",
                    email="rrwebfx_recip2@stub.local", status="待联系")
