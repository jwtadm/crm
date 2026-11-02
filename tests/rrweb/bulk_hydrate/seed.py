"""Bulk-hydrate test:
  - Seed two 已成交 people, each with an IG placeholder (import:<handle>)
  - dev_with_ig_stub patches platforms.instagram.fetch_user_info to return
    real-shaped data with a numeric pk_id → upsert_account collapses the
    placeholder in place
  - Test opens /people, clicks 批量补全, confirms, waits for done,
    asserts cursor 2/2 + hydrated=2
"""
from __future__ import annotations
from tests.rrweb._lib import seed as lib


def seed() -> None:
    a = lib.make_person(name="RRWebFx Hydrate Seed A", status="已成交")
    lib.attach_placeholder(a, platform="instagram", handle="rrwebfx_hydseed_a")
    b = lib.make_person(name="RRWebFx Hydrate Seed B", status="已成交")
    lib.attach_placeholder(b, platform="instagram", handle="rrwebfx_hydseed_b")
