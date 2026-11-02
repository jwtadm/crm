"""Bulk-similarity test: seed two hydrated IG accounts under 已成交 so the
default-checked status filter on /similarity catches them, then the test
clicks 全量找相似 → 确认 → waits for the worker to mark the job done."""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    a = lib.make_person(name="RRWebFx Bulk Sim Seed A", status="已成交")
    lib.make_real_account(a, platform="instagram",
                          handle="rrwebfx_bulkseed_a", follower_count=88888)

    b = lib.make_person(name="RRWebFx Bulk Sim Seed B", status="已成交")
    lib.make_real_account(b, platform="instagram",
                          handle="rrwebfx_bulkseed_b", follower_count=77777)
