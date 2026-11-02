"""Two persons for the blacklist (reject/unreject) flow."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    lib.make_person(name="RRWebFx Blacklist Victim", status="待联系")
