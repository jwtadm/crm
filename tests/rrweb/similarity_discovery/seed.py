"""Similarity discovery needs at least one hydrated IG account in the DB to
serve as the seed the user picks on the /similarity page."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    pid = lib.make_person(name="RRWebFx Similarity Seed Owner",
                          status="待联系")
    lib.make_real_account(pid, platform="instagram",
                          handle="rrwebfx_simseed", follower_count=55555)
