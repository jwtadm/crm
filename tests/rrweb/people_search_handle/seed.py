"""Search-by-handle test:
  - Seed two 待联系 people with distinctive last names (避免名字撞到其他人)
    each carrying one IG handle that does NOT overlap with the person name
  - Test searches /people by handle (without @ and with @) and asserts the
    right rows surface — proves the SQL EXISTS subquery + @ stripping work.
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    a = lib.make_person(name="RRWebFx Search By Handle Alpha")
    lib.make_real_account(a, platform="instagram",
                          handle="rrwebfx_searchhandle_one", follower_count=12345)

    b = lib.make_person(name="RRWebFx Search By Handle Beta")
    lib.make_real_account(b, platform="instagram",
                          handle="rrwebfx_searchhandle_two", follower_count=23456)
