"""Bulk hydrate: take a snapshot of every placeholder platform_account
(platform_user_id LIKE 'import:%') and call fetch_user_info(handle) on each,
so the row picks up real pk_id + bio + category + email and can later be used
as a similarity seed.

Mirrors lib/bulk_similarity.py to the letter — same payload_json shape, same
single-job lock idea, same boot-sweep. Two separate jobs (one hydrate + one
similarity) can run concurrently because they hit different TikHub endpoints
and have independent locks; that's the whole reason the two job tables are
separate instead of folded into a generic `bulk_jobs`.
"""
from __future__ import annotations

import json as _json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from . import db, settings
from platforms import INFO_CAPABLE, get as get_platform


# Concurrency for run_job. The TikHub rate limiter in platforms/_tikhub.py
# caps to ~4 RPS globally; with avg fetch_user_info ~3.2s we want workers
# in-flight to keep the rate limiter saturated. 8 workers comfortably hits
# the 4-RPS ceiling without burning idle threads.
# Empirical: 1 worker (serial) gave ~0.27 item/s in prod, taking ~32h for
# 30K placeholders. 8 workers should target ~3-4 item/s, ~2h for 30K.
HYDRATE_WORKERS = 8


# Cost model — fetch_user_info is roughly $0.001 per call on TikHub. Less
# variance than similarity (no per-seed expansion), so the estimate is
# usually pretty close to actual.
COST_PER_HYDRATE_USD = 0.001


# ---------- Seed selection ----------

def list_eligible_placeholders(
    statuses: list[str] | None = None,
    platforms: list[str] | None = None,
) -> list[dict]:
    """Return placeholder platform_accounts that can be hydrated.

    Filters:
      - platform must have fetch_user_info (INFO_CAPABLE)
      - platform_user_id LIKE 'import:%' (the placeholder marker)
      - is_rejected != 1 (don't waste TikHub credit on blacklisted handles)
      - username is non-empty (can't hydrate without a handle)
      - optional people.status ∈ statuses (None = no status filter)
    Ordered by oldest-first so a partial run knocks out the longest-pending
    placeholders before the newest ones (which a manual /people action might
    upgrade in the meantime).
    """
    plats = [p for p in (platforms or list(INFO_CAPABLE)) if p in INFO_CAPABLE]
    if not plats:
        return []
    sql = (
        "SELECT pa.id AS aid, pa.platform, pa.username, "
        "       p.id AS person_id, p.full_name AS person_name, p.status AS person_status "
        "  FROM platform_accounts pa "
        "  JOIN people p ON p.id = pa.person_id "
        " WHERE pa.platform IN (" + ",".join(["?"] * len(plats)) + ") "
        "   AND pa.platform_user_id LIKE 'import:%' "
        "   AND COALESCE(pa.is_rejected, 0) = 0 "
        "   AND TRIM(COALESCE(pa.username, '')) != '' "
    )
    args: list = list(plats)
    if statuses:
        sql += " AND p.status IN (" + ",".join(["?"] * len(statuses)) + ") "
        args.extend(statuses)
    sql += " ORDER BY pa.created_at ASC, pa.id ASC"
    with db.tx() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def estimate_cost(rows: list[dict]) -> dict:
    """Hydrate cost is linear in row count — one fetch_user_info per row."""
    n = len(rows)
    by_plat: dict[str, int] = {}
    for r in rows:
        by_plat[r["platform"]] = by_plat.get(r["platform"], 0) + 1
    return {
        "placeholders": n,
        "by_platform": by_plat,
        "upper_cost_usd": round(COST_PER_HYDRATE_USD * n, 2),
    }


def fetch_tikhub_balance() -> float | None:
    """Same shape as bulk_similarity.fetch_tikhub_balance; duplicated rather
    than imported across to keep the modules independent."""
    try:
        key = settings.get("TIKHUB_API_KEY")
        if not key:
            return None
        r = requests.get(
            "https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        return float(r.json()["user_data"]["balance"])
    except Exception:
        return None


# ---------- Job persistence ----------

# In-memory set of cancelled job IDs — instant cancel signal for the worker
# thread (same process). DB is the durable backup; this set is the fast path.
_CANCELLED_JOBS: set[str] = set()


def mark_cancelled(job_id: str) -> None:
    _CANCELLED_JOBS.add(job_id)


def is_cancelled(job_id: str) -> bool:
    return job_id in _CANCELLED_JOBS


def save_job(job: dict, created_by: int, *, update_status: bool = True) -> None:
    # Watchdog uses last_checkpoint_at to detect zombie jobs (worker died
    # silently — e.g. gunicorn sync worker SIGKILL'd by slow-client timeout
    # — leaving status='running' but no thread advancing the cursor).
    job["last_checkpoint_at"] = time.time()
    with db.tx() as c:
        if update_status:
            c.execute(
                """INSERT INTO hydrate_jobs (id, created_by, created_at, finished_at, status, payload_json)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     finished_at=excluded.finished_at,
                     status=excluded.status,
                     payload_json=excluded.payload_json""",
                (job["id"], created_by,
                 int(job.get("started_at") or time.time()),
                 int(job["finished_at"]) if job.get("finished_at") else None,
                 job["status"], _json.dumps(job, ensure_ascii=False)),
            )
        else:
            # Periodic checkpoint: don't overwrite status column
            c.execute(
                """UPDATE hydrate_jobs
                   SET payload_json=?, finished_at=?, last_checkpoint_at=?
                   WHERE id=?""",
                (_json.dumps(job, ensure_ascii=False),
                 int(job["finished_at"]) if job.get("finished_at") else None,
                 job["last_checkpoint_at"],
                 job["id"]),
            )
        # Sync back cancelled status from DB if set externally
        row = c.execute("SELECT status FROM hydrate_jobs WHERE id=?",
                        (job["id"],)).fetchone()
        if row and row["status"] == "cancelled":
            job["status"] = "cancelled"


def load_job(job_id: str) -> tuple[dict | None, int | None]:
    with db.tx() as c:
        r = c.execute(
            "SELECT created_by, payload_json FROM hydrate_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if not r:
        return None, None
    try:
        return _json.loads(r["payload_json"]), r["created_by"]
    except Exception:
        return None, r["created_by"]


def try_create_locked(job: dict, created_by: int) -> tuple[bool, dict | None]:
    """Acquire-or-fail: lock check + INSERT in one tx so two concurrent
    starts can't both squeak past. Independent from bulk_similarity's lock —
    two TikHub endpoints, separate rate-limit buckets, can run side by side."""
    job["last_checkpoint_at"] = time.time()
    with db.tx() as c:
        r = c.execute(
            "SELECT id, status, payload_json FROM hydrate_jobs "
            " WHERE status IN ('running','interrupted') "
            " ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if r:
            try:
                active = _json.loads(r["payload_json"])
            except Exception:
                active = {"id": r["id"], "status": r["status"]}
            return False, active
        c.execute(
            """INSERT INTO hydrate_jobs (id, created_by, created_at, finished_at, status, payload_json)
               VALUES (?,?,?,?,?,?)""",
            (job["id"], created_by,
             int(job.get("started_at") or time.time()),
             int(job["finished_at"]) if job.get("finished_at") else None,
             job["status"], _json.dumps(job, ensure_ascii=False)),
        )
    return True, None


def find_active_job() -> dict | None:
    with db.tx() as c:
        r = c.execute(
            "SELECT id, created_by, payload_json, status FROM hydrate_jobs "
            " WHERE status IN ('running','interrupted') "
            " ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    try:
        job = _json.loads(r["payload_json"])
    except Exception:
        job = {"id": r["id"], "status": r["status"]}
    job["created_by"] = r["created_by"]
    return job


def sweep_stale_running() -> int:
    """Boot-time sweep — same pattern as bulk_similarity. Any 'running' row
    in the DB at module load is a zombie from the previous process; flip it
    to 'interrupted' so the UI shows a Resume button."""
    try:
        with db.tx() as c:
            rows = c.execute(
                "SELECT id, payload_json FROM hydrate_jobs WHERE status='running'"
            ).fetchall()
            if not rows:
                return 0
            n = 0
            for r in rows:
                try:
                    p = _json.loads(r["payload_json"])
                except Exception:
                    p = {}
                p["status"] = "interrupted"
                p["interrupted_at"] = int(time.time())
                p["interrupted_reason"] = "process restart"
                c.execute(
                    "UPDATE hydrate_jobs SET status='interrupted', payload_json=? WHERE id=?",
                    (_json.dumps(p, ensure_ascii=False), r["id"]),
                )
                n += 1
            return n
    except Exception as e:
        print(f"[hydrate_job sweep] error: {e}", flush=True)
        return 0


# ---------- Worker ----------

def _blank_progress() -> dict:
    return {
        "hydrated": 0,       # successfully upgraded placeholder → real account
        "upserted_new": 0,   # rare: hydrate returned an account that wasn't a placeholder
        "errors": 0,         # fetch_user_info raised
        "missing": 0,        # API returned but no usable pk_id
    }


def build_job(rows: list[dict], params: dict, created_by: int) -> dict:
    return {
        "id": _short_id(),
        "status": "running",
        "params": params,
        "account_ids": [r["aid"] for r in rows],
        "placeholder_total": len(rows),
        "cursor": 0,
        "started_at": time.time(),
        "finished_at": None,
        "progress": _blank_progress(),
        "per_item": [],  # tail of recent results, for UI
        "created_by": created_by,
    }


def _short_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def run_job(job: dict, created_by: int) -> None:
    """Worker loop with batched parallelism. Reads cursor from job dict,
    resumes from there. Each batch:
      - kick off HYDRATE_WORKERS calls to fetch_user_info concurrently
      - wait for all to complete (TikHub rate-limiter in _tikhub.py
        throttles the global RPS so we don't trip 429)
      - merge progress in the main thread (single-threaded, race-free)
      - advance cursor to batch_end, checkpoint
    On crash mid-batch, cursor stays at the last batch boundary, so resume
    re-runs at most HYDRATE_WORKERS in-flight items (cheap; idempotent at
    the upsert layer since placeholder-collapse re-finds the row by handle).
    """
    account_ids = job.get("account_ids") or []
    start_idx = int(job.get("cursor") or 0)
    progress = job["progress"]

    # Re-fetch rows fresh from DB. We need username + platform; an account
    # deleted between job creation and execution is skipped with an error.
    with db.tx() as c:
        rows = db.get_accounts_by_ids(c, account_ids)
    by_id = {r["id"]: dict(r) for r in rows}

    def _worker(item: tuple[int, int, dict | None]) -> dict:
        i, aid, row = item
        entry: dict[str, Any] = {
            "account_id": aid, "idx": i,
            "username": (row or {}).get("username", "?"),
            "platform": (row or {}).get("platform", "?"),
            "ok": False, "error": None, "duration_ms": 0,
            # _delta gets merged into progress in main thread to keep counter
            # updates lock-free.
            "_delta": {},
        }
        t0 = time.time()
        if not row:
            entry["error"] = "deleted"
            entry["_delta"] = {"errors": 1}
        else:
            try:
                entry["_delta"] = _hydrate_one(row, entry)
            except Exception as e:
                entry["error"] = str(e)[:300]
                entry["_delta"] = {"errors": 1}
        entry["duration_ms"] = int((time.time() - t0) * 1000)
        return entry

    try:
        # Lazily create the pool — saves us paying its startup cost when
        # the job was already complete (cursor == total) on resume.
        with ThreadPoolExecutor(max_workers=HYDRATE_WORKERS) as ex:
            for batch_start in range(start_idx, len(account_ids), HYDRATE_WORKERS):
                # Check cancellation each batch — in-memory first (instant)
                if is_cancelled(job["id"]):
                    job["status"] = "cancelled"
                    save_job(job, created_by)
                    return
                with db.tx() as c:
                    current = c.execute("SELECT status FROM hydrate_jobs WHERE id=?", (job["id"],)).fetchone()
                if current and current["status"] == "cancelled":
                    job["status"] = "cancelled"
                    save_job(job, created_by)
                    return

                batch_end = min(batch_start + HYDRATE_WORKERS, len(account_ids))
                items = [(i, account_ids[i], by_id.get(account_ids[i]))
                         for i in range(batch_start, batch_end)]
                results = list(ex.map(_worker, items))

                # Merge in main thread — no race on progress / per_item / cursor
                for entry in results:
                    delta = entry.pop("_delta", {})
                    for k, v in delta.items():
                        progress[k] = progress.get(k, 0) + v
                    job["per_item"].append(entry)
                if len(job["per_item"]) > 200:
                    job["per_item"] = job["per_item"][-200:]
                job["cursor"] = batch_end
                save_job(job, created_by, update_status=False)

        job["status"] = "done"
        job["finished_at"] = time.time()
    except Exception as e:
        job["status"] = "failed"
        job["finished_at"] = time.time()
        job["fatal_error"] = str(e)[:500]
    finally:
        save_job(job, created_by)


def _hydrate_one(row: dict, entry: dict) -> dict:
    """fetch_user_info → upsert. Returns a {progress_key: +1} delta dict
    (caller merges in single-threaded section) so this is safe to call from
    multiple threads concurrently. Doesn't catch — caller wraps for the
    per-item error path."""
    plat = row["platform"]
    handle = (row.get("username") or "").strip().lstrip("@")
    if not handle:
        entry["error"] = "no handle"
        return {"errors": 1}
    if plat not in INFO_CAPABLE:
        entry["error"] = f"{plat} has no fetch_user_info"
        return {"errors": 1}
    norm = get_platform(plat).fetch_user_info(handle)
    if not norm or not norm.get("platform_user_id") or str(norm["platform_user_id"]).startswith("import:"):
        # API returned but no usable pk_id — leave the placeholder as-is
        entry["error"] = "no_pk_id_returned"
        return {"missing": 1}
    with db.tx() as c:
        _aid, _pid, created = db.upsert_account(c, norm, source=f"hydrate:bulk")
    entry["ok"] = True
    if created:
        # Rare: the existing placeholder didn't match (e.g. handle changed
        # case mid-flight), so a new row got inserted. Still a win.
        return {"upserted_new": 1}
    return {"hydrated": 1}
