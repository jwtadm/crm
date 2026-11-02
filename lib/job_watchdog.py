"""Background watchdog that flips zombie running jobs to 'interrupted'.

Why this exists (2026-06-03 incident):
A bulk_hydrate job sat in status='running' for 1.5 hours with cursor frozen.
Root cause: gunicorn sync worker hit `--timeout 120` from a slow client
keepalive connection (the worker was stuck in `socket.recv()` parsing an
incoming HTTP body); master SIGKILL'd the worker, the bulk_hydrate daemon
thread inside that worker died silently. The boot-time sweep only fires at
process restart, so the DB row stayed 'running' forever and the UI never
offered a Resume button.

This watchdog runs forever in a daemon thread started at app module load.
Every WATCHDOG_INTERVAL seconds it scans for running jobs whose
last_checkpoint_at is older than STALE_THRESHOLD; those get flipped to
'interrupted' so the UI can show Resume.

The UPDATE is conditional (`WHERE id=? AND status='running'`) so it's safe
even if multiple workers run the watchdog in parallel — the first one wins,
the rest no-op.
"""
from __future__ import annotations

import json
import threading
import time

from . import db


WATCHDOG_INTERVAL = 60     # how often the loop ticks
STALE_THRESHOLD = 300      # >5 min without a checkpoint → declare zombie

# Tables and the column inside payload_json that tracks per-row "size" — used
# only for log lines, not for the staleness decision.
TABLES = (
    ("similarity_jobs", "seed_total"),
    ("hydrate_jobs",    "placeholder_total"),
)


def _tick(now: float | None = None) -> int:
    """One scan pass. Returns the number of rows flipped this tick."""
    now = time.time() if now is None else now
    flipped = 0
    with db.tx() as c:
        for table, total_key in TABLES:
            rows = c.execute(
                f"SELECT id, payload_json FROM {table} WHERE status='running'"
            ).fetchall()
            for r in rows:
                try:
                    p = json.loads(r["payload_json"])
                except Exception:
                    continue
                # Old rows from before this field existed → fall back to
                # started_at; that grace covers the resume-in-flight case.
                last = float(p.get("last_checkpoint_at") or p.get("started_at") or 0.0)
                if last <= 0 or (now - last) <= STALE_THRESHOLD:
                    continue
                p["status"] = "interrupted"
                p["interrupted_at"] = int(now)
                p["interrupted_reason"] = (
                    f"watchdog: no checkpoint in {int(now - last)}s "
                    "(worker thread likely died silently — see job_watchdog.py)"
                )
                cur = c.execute(
                    f"UPDATE {table} SET status='interrupted', payload_json=? "
                    f"WHERE id=? AND status='running'",
                    (json.dumps(p, ensure_ascii=False), r["id"]),
                )
                if cur.rowcount == 1:
                    flipped += 1
                    print(f"[watchdog] {table} {r['id']} → interrupted "
                          f"(stale {int(now - last)}s; cursor {p.get('cursor')}/"
                          f"{p.get(total_key)})", flush=True)
    return flipped


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"[watchdog] tick error: {e}", flush=True)
        time.sleep(WATCHDOG_INTERVAL)


_started = False
_lock = threading.Lock()


def start() -> None:
    """Idempotent. Safe to call from multiple places (e.g. app.py module load).
    Spawns a daemon thread that never returns. We start it from app.py so the
    watchdog runs in whichever process imports app — typically the gunicorn
    master under --preload."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="job_watchdog").start()
    print(f"[watchdog] started (tick every {WATCHDOG_INTERVAL}s, "
          f"stale threshold {STALE_THRESHOLD}s)", flush=True)
