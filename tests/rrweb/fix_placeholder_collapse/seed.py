"""Fixtures for the placeholder-collapse + conflict-jump fix.

Layout:
  Person A "RRWebFx Bug1 Target"  — holds an instagram placeholder
                                    (platform_user_id='import:rrwebfx_target')
  Person B "RRWebFx Bug1 Self"    — empty drawer; will try to claim @rrwebfx_target
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    A = lib.make_person(name="RRWebFx Bug1 Target", owner="summerlily",
                        status="已成交")
    lib.attach_placeholder(A, platform="instagram", handle="rrwebfx_target")
    lib.make_person(name="RRWebFx Bug1 Self", status="待联系")
