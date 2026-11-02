"""Filter-change-resets-page test:
  - Seed 4 待联系 + 2 已联系 people (cheap; no need to actually fill a page)
  - The test artificially bumps _offset to 200 to simulate "user navigated
    deep through 待联系", then switches status to 已联系 — without the fix,
    offset stays at 200 and the 已联系 table appears empty ("没有匹配的客户");
    with the fix, reloadFiltered() snaps offset back to 0 and the 2 rows show.
"""
from tests.rrweb._lib import seed as lib


def seed() -> None:
    for i in range(4):
        lib.make_person(name=f"RRWebFx Filter Reset Lead {i+1}", status="待联系")
    for i in range(2):
        lib.make_person(name=f"RRWebFx Filter Reset Contacted {i+1}", status="已联系")
