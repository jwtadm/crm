"""Two recipients with email for the new compose UX test."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx Compose Recipient 1",
                    email="rrwebfx_compose1@stub.local", status="待联系")
    lib.make_person(name="RRWebFx Compose Recipient 2",
                    email="rrwebfx_compose2@stub.local", status="待联系")
