"""Seed three persons across different statuses, each with a real IG account.
The test exercises /similarity's new status filter to scope the seed list.
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    # 已成交 (target of the filter)
    a = lib.make_person(name="RRWebFx Status Filter Deal", status="已成交")
    lib.make_real_account(a, platform="instagram",
                          handle="rrwebfx_dealaccount", follower_count=50000)

    # 待联系 (should disappear when 已成交 is checked)
    b = lib.make_person(name="RRWebFx Status Filter Lead", status="待联系")
    lib.make_real_account(b, platform="instagram",
                          handle="rrwebfx_leadaccount", follower_count=40000)

    # 已联系 (also should disappear)
    c = lib.make_person(name="RRWebFx Status Filter Contacted", status="已联系")
    lib.make_real_account(c, platform="instagram",
                          handle="rrwebfx_contactedaccount", follower_count=30000)
