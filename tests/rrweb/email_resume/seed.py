"""Seed an interrupted email job + 2 recipients, mimicking the production
incident (gunicorn restart killed worker mid-flight). The test will then
log in, find the job via the compose modal, click 「重发剩余 N 个」 and
verify the remaining recipient actually gets a stub email."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib import auth, db, audit  # noqa: F401  audit kept for parity with prod path
from tests.rrweb._lib import seed as lib


SEEDED_JOB_ID = "rrwebfx-resume"


def seed() -> None:
    # Two recipients
    p1 = lib.make_person(name="RRWebFx Resume Recipient A",
                         email="rrwebfx_resume_a@stub.local", status="待联系")
    p2 = lib.make_person(name="RRWebFx Resume Recipient B",
                         email="rrwebfx_resume_b@stub.local", status="待联系")

    # The test will be driven by the admin user (ensure_admin already seeded
    # admin in the runner); resolve admin's id for the job's created_by.
    admin = auth.get_user_by_username("admin")
    if admin is None:
        raise RuntimeError("admin user missing — runner should have created it")
    admin_id = admin["id"]

    # Build a paused job: recipient A already sent, recipient B never tried.
    now = int(time.time())
    payload = {
        "id": SEEDED_JOB_ID,
        "status": "interrupted",
        "total": 2,
        "sent": 1,
        "failed": 0,
        "skipped": 0,
        "log": [
            {"person_id": p1, "email": "rrwebfx_resume_a@stub.local", "ok": True},
        ],
        "started_at": now - 600,           # 10 min ago
        "finished_at": None,
        "throttle": 0.0,
        "created_by": admin_id,
        "person_ids": [p1, p2],
        "subject_t": "RRWebFx resume test",
        "body_t": "hi {{name}} — this is the resume smoke test",
        "interrupted_at": now - 540,
        "interrupted_reason": "test fixture",
    }
    c = sqlite3.connect(db.DB_PATH)
    try:
        c.execute(
            "INSERT OR REPLACE INTO email_jobs "
            "(id, created_by, created_at, finished_at, status, payload_json) "
            "VALUES (?, ?, ?, ?, 'interrupted', ?)",
            (SEEDED_JOB_ID, admin_id, payload["started_at"], None,
             json.dumps(payload, ensure_ascii=False)),
        )
        c.commit()
    finally:
        c.close()


def teardown() -> None:
    c = sqlite3.connect(db.DB_PATH)
    try:
        c.execute("DELETE FROM email_jobs WHERE id=?", (SEEDED_JOB_ID,))
        c.commit()
    finally:
        c.close()
