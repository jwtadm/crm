"""LLM ICP filter test:
  - one 已成交 IG seed → 3 stub sims (1 fitness coach, 2 artists)
  - dev_with_ig_stub patches ai.classify_creators with a keyword judge,
    so the fitness one always drops
  - test enables LLM_FILTER_ENABLED via /settings, runs bulk, asserts:
      LLM 滤掉: 1
      新增:    2

teardown() restores LLM_FILTER_ENABLED='' so subsequent tests aren't
affected. Doesn't touch similarity_jobs — done rows don't block new
jobs (single-job lock only catches running/interrupted)."""
from __future__ import annotations
import sqlite3

from tests.rrweb._lib import seed as lib, config


def seed() -> None:
    p = lib.make_person(name="RRWebFx LLM Filter Seed", status="已成交")
    lib.make_real_account(p, platform="instagram",
                          handle="rrwebfx_llmseed", follower_count=99999)


def teardown() -> None:
    # Belt-and-braces: turn the filter back off in the test DB so the
    # next rrweb test (alphabetical: mailer_compose_ux) starts clean.
    c = sqlite3.connect(config.TEST_DB_PATH)
    try:
        c.execute("UPDATE settings SET value='' WHERE key='LLM_FILTER_ENABLED'")
        c.commit()
    finally:
        c.close()
