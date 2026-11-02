"""Bulk similarity discovery: take a snapshot of every hydrated platform_account
under a chosen status filter, then call fetch_similar_users + ingest serially,
checkpointing progress to similarity_jobs after each seed.

Why serial + checkpoint? TikHub has rate limits (parallel bulk reliably trips
429), one job at a time gives us deterministic cost, and the email_jobs pattern
already proved that checkpointing + sweep-on-boot survives gunicorn restarts.

This module owns the worker loop and the SQL for picking seeds. The HTTP layer
(app.py) owns: the single-job lock (409 if another job is running), the boot-
time sweep, route plumbing, and threading the worker.
"""
from __future__ import annotations

import json as _json
import sqlite3
import time
from typing import Any

import requests

from . import db, ingest, mailer, settings
from platforms import SIMILAR_CAPABLE, get as get_platform


# Cost model — matches tools/bulk_similarity_dealmade.py so the estimate the
# user sees in the modal lines up with the CLI we already trust. Both numbers
# are intentionally pessimistic upper bounds: real dedup typically lops 30-50%.
COST_PER_SEED_USD = 0.002      # similar-users API call
COST_PER_HYDRATE_USD = 0.001   # per-candidate hydrate (fetch_user_info)
# Per-seed candidate ceiling for the upper-bound estimate. IG returns ~80,
# TikTok ~30; using one number keeps the math legible in the modal.
EST_CANDIDATES_PER_SEED = 80

# Circuit breaker: stop the run (mark interrupted, don't burn the rest of the
# seeds) when TikHub credit is exhausted or the upstream is sustained-down.
# A 402 (Payment Required) stops immediately — every later call would 402 too.
# Otherwise we tolerate up to this many *consecutive* seed errors before
# assuming the run is wedged. Resume picks up at the seed that broke it.
CONSECUTIVE_ERROR_LIMIT = 5


# ---------- Seed selection ----------

def list_eligible_seeds(
    statuses: list[str] | None = None,
    platforms: list[str] | None = None,
) -> list[dict]:
    """Return hydrated platform_accounts that can be used as similarity seeds.

    Filters:
      - platform must support similar-users API (SIMILAR_CAPABLE)
      - platform_user_id NOT LIKE 'import:%' (placeholder rows can't be seeded)
      - is_rejected != 1 (the user has blacklisted this account — don't expand)
      - optional people.status ∈ statuses (None = no status filter)
    Ordered by follower_count DESC NULLS LAST so the modal preview shows the
    biggest seeds first.
    """
    plats = [p for p in (platforms or list(SIMILAR_CAPABLE)) if p in SIMILAR_CAPABLE]
    if not plats:
        return []
    sql = (
        "SELECT pa.id AS aid, pa.platform, pa.username, pa.platform_user_id, "
        "       pa.secondary_id, pa.follower_count, p.id AS person_id, "
        "       p.full_name AS person_name, p.status AS person_status "
        "  FROM platform_accounts pa "
        "  JOIN people p ON p.id = pa.person_id "
        " WHERE pa.platform IN (" + ",".join(["?"] * len(plats)) + ") "
        "   AND pa.platform_user_id NOT LIKE 'import:%' "
        "   AND COALESCE(pa.is_rejected, 0) = 0 "
    )
    args: list = list(plats)
    if statuses:
        sql += " AND p.status IN (" + ",".join(["?"] * len(statuses)) + ") "
        args.extend(statuses)
    sql += " ORDER BY pa.follower_count DESC NULLS LAST, pa.id ASC"
    with db.tx() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def estimate_cost(seeds: list[dict]) -> dict:
    """Upper-bound estimate. The 'upper' qualifier is doing real work here:
    we assume every seed returns the full candidate cap AND every candidate
    survives dedup AND gets hydrated. Real spend is consistently 30-50% lower
    because previous seeds' ingest dedups the next ones."""
    n = len(seeds)
    upper_hydrate = n * EST_CANDIDATES_PER_SEED
    cost = COST_PER_SEED_USD * n + COST_PER_HYDRATE_USD * upper_hydrate
    by_plat: dict[str, int] = {}
    for s in seeds:
        by_plat[s["platform"]] = by_plat.get(s["platform"], 0) + 1
    return {
        "seeds": n,
        "by_platform": by_plat,
        "upper_cost_usd": round(cost, 2),
    }


def fetch_tikhub_balance() -> float | None:
    """Read TikHub's account balance so the modal can warn before launch.
    Returns None on any error — the modal renders 'unknown' instead of
    blocking. Don't surface the exception to the user; a transient network
    blip shouldn't prevent them from starting a job."""
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

def save_job(job: dict, created_by: int) -> None:
    """Mirror to DB. payload_json is the source of truth across restarts;
    callers (app.py) keep an in-memory dict to avoid hitting SQLite on every
    progress tick, but we still checkpoint after each seed.

    Stamps `last_checkpoint_at` so the watchdog can spot zombie jobs whose
    worker thread died (e.g. gunicorn worker SIGKILL'd by slow-client
    timeout) — without this, the row sits in status='running' forever and
    the UI never offers a Resume button."""
    job["last_checkpoint_at"] = time.time()
    with db.tx() as c:
        c.execute(
            """INSERT INTO similarity_jobs (id, created_by, created_at, finished_at, status, payload_json)
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


def load_job(job_id: str) -> tuple[dict | None, int | None]:
    with db.tx() as c:
        r = c.execute(
            "SELECT created_by, payload_json FROM similarity_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if not r:
        return None, None
    try:
        return _json.loads(r["payload_json"]), r["created_by"]
    except Exception:
        return None, r["created_by"]


def try_create_locked(job: dict, created_by: int) -> tuple[bool, dict | None]:
    """Atomic acquire-or-fail: check for any active row AND insert the new
    job inside a single transaction. Returns (True, None) on success or
    (False, active_job_snapshot) if some other row already holds the lock.

    Why this exists: a naive find_active_job() → save_job() pair has a race
    window — two browser tabs can both see 'no active' and both INSERT.
    Folding both into one tx() block closes it (SQLite serializes writers)."""
    job["last_checkpoint_at"] = time.time()
    with db.tx() as c:
        r = c.execute(
            "SELECT id, status, payload_json FROM similarity_jobs "
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
            """INSERT INTO similarity_jobs (id, created_by, created_at, finished_at, status, payload_json)
               VALUES (?,?,?,?,?,?)""",
            (job["id"], created_by,
             int(job.get("started_at") or time.time()),
             int(job["finished_at"]) if job.get("finished_at") else None,
             job["status"], _json.dumps(job, ensure_ascii=False)),
        )
    return True, None


def find_active_job() -> dict | None:
    """Return the most recent running/interrupted job (any creator). Used by
    the single-job lock at /bulk/start and by the page-load 'is something
    pending?' query. 'interrupted' counts as active for the lock so a stale
    zombie doesn't deadlock the system — but the UI surfaces a Resume button
    that calls /resume, which CAS-flips status='interrupted' → 'running'."""
    with db.tx() as c:
        r = c.execute(
            "SELECT id, created_by, payload_json, status FROM similarity_jobs "
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
    """At process boot, flip any 'running' rows to 'interrupted' because their
    worker thread died with the previous process. Identical pattern to
    _sweep_stale_running_email_jobs in app.py. Safe to call multiple times."""
    try:
        with db.tx() as c:
            rows = c.execute(
                "SELECT id, payload_json FROM similarity_jobs WHERE status='running'"
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
                    "UPDATE similarity_jobs SET status='interrupted', payload_json=? WHERE id=?",
                    (_json.dumps(p, ensure_ascii=False), r["id"]),
                )
                n += 1
            return n
    except Exception as e:
        print(f"[similarity_job sweep] error: {e}", flush=True)
        return 0


# ---------- Worker ----------

def _blank_progress() -> dict:
    return {
        "considered": 0, "new": 0,
        "skipped_pre": 0, "skipped_email": 0,
        "cross_platform": 0, "hydrate_errors": 0,
        "seed_errors": 0,
        # LLM ICP filter — only ever > 0 when settings.LLM_FILTER_ENABLED
        # is on. Kept in the schema even when off so the UI doesn't need
        # special-case rendering.
        "llm_filtered": 0,
    }


def build_job(seed_rows: list[dict], params: dict, created_by: int) -> dict:
    """Construct the initial job dict (not yet persisted). Caller decides when
    to save_job() so the lock check + insert can be atomic."""
    return {
        "id": _short_id(),
        "status": "running",
        "params": params,
        "seed_ids": [s["aid"] for s in seed_rows],
        "seed_total": len(seed_rows),
        "cursor": 0,
        "started_at": time.time(),
        "finished_at": None,
        "progress": _blank_progress(),
        "per_seed": [],  # appended after each seed finishes
        "created_by": created_by,
    }


def _short_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _is_payment_error(exc: Exception) -> bool:
    """True when the failure is a TikHub 402 (out of credit). For IG the raw
    requests.HTTPError carries .response; for TikTok the 402 gets wrapped in a
    RuntimeError string after the retry loop, so we also string-match."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 402:
        return True
    s = str(exc).lower()
    return "payment required" in s or " 402" in f" {s}"


def _send_balance_alert(job: dict, reason: str, last_error: str) -> None:
    """Best-effort ops alert when a run trips the circuit breaker. Recipient is
    settings BULK_ALERT_EMAIL; silently no-ops if unset. Never raises — an
    alert failure must not mask the interrupt it's reporting."""
    try:
        to = (settings.get("BULK_ALERT_EMAIL") or "").strip()
        if not to:
            return
        bal = fetch_tikhub_balance()
        bal_str = f"${bal:.4f}" if bal is not None else "unknown"
        p = job.get("progress") or {}
        done = job.get("cursor") or 0
        total = job.get("seed_total") or 0
        kind = ("TikHub 余额耗尽 (402 Payment Required)"
                if reason == "tikhub_payment_required"
                else f"连续 {CONSECUTIVE_ERROR_LIMIT} 个种子失败")
        subject = f"[Scout] 全量找相似已中断 — {kind}"
        body = (
            f"找相似任务 {job.get('id')} 已自动中断并保存进度。\n\n"
            f"原因：{kind}\n"
            f"TikHub 余额：{bal_str}\n"
            f"进度：{done}/{total} 个种子（新增 {p.get('new', 0)}，"
            f"已在库去重 {p.get('skipped_pre', 0)}）\n"
            f"最后错误：{last_error[:200]}\n\n"
            f"充值后到 /similarity 页面点「续跑」即可从断点继续（或开着页面会自动续跑）。\n"
        )
        mailer.send_one(to, subject, body)
    except Exception:
        pass


def run_job(job: dict, created_by: int) -> None:
    """Worker loop. Reads cursor from job dict, resumes from there. Updates job
    dict in place and checkpoints to DB after every seed so a crash mid-loop
    leaves us at the last completed seed (we re-run the in-flight one on
    resume, which costs ~1 extra similar API call — acceptable).

    Circuit breaker: on a TikHub 402 (out of credit) or CONSECUTIVE_ERROR_LIMIT
    consecutive seed failures, the loop stops WITHOUT advancing past the broken
    seed, marks the job 'interrupted', and fires an ops email. Resume then picks
    up exactly at the seed that broke — nothing is silently skipped.

    Caller is expected to launch this in a daemon thread."""
    seed_ids = job.get("seed_ids") or []
    start_idx = int(job.get("cursor") or 0)
    progress = job["progress"]
    consecutive_errors = 0

    # Re-fetch seed rows fresh (the snapshot is by ID; if a seed got deleted
    # between job creation and execution, skip it).
    with db.tx() as c:
        seed_rows = db.get_accounts_by_ids(c, seed_ids)
    seeds_by_id = {r["id"]: dict(r) for r in seed_rows}

    try:
        for i in range(start_idx, len(seed_ids)):
            aid = seed_ids[i]
            seed = seeds_by_id.get(aid)
            t0 = time.time()
            entry: dict[str, Any] = {
                "seed_id": aid, "idx": i,
                "username": (seed or {}).get("username", "?"),
                "platform": (seed or {}).get("platform", "?"),
                "new": 0, "considered": 0,
                "error": None, "duration_ms": 0,
            }
            tripped = None  # set to a reason string if the breaker fires
            if not seed:
                entry["error"] = "seed_deleted"
                progress["seed_errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    tripped = "too_many_consecutive_errors"
            else:
                try:
                    _process_one_seed(seed, progress, entry)
                    consecutive_errors = 0
                except Exception as e:
                    entry["error"] = str(e)[:300]
                    progress["seed_errors"] += 1
                    consecutive_errors += 1
                    if _is_payment_error(e):
                        tripped = "tikhub_payment_required"
                    elif consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                        tripped = "too_many_consecutive_errors"
            entry["duration_ms"] = int((time.time() - t0) * 1000)
            job["per_seed"].append(entry)

            if tripped:
                # Stop here. cursor stays at i (the broken seed) so resume
                # re-runs it once credit is restored — no seeds skipped.
                job["cursor"] = i
                job["status"] = "interrupted"
                job["interrupted_at"] = time.time()
                job["interrupted_reason"] = tripped
                save_job(job, created_by)
                _send_balance_alert(job, tripped, entry.get("error") or "")
                return

            job["cursor"] = i + 1
            save_job(job, created_by)

        job["status"] = "done"
        job["finished_at"] = time.time()
    except Exception as e:
        # Catastrophic loop-level failure (unlikely; _process_one_seed
        # already catches its own). Mark failed so the UI shows red, not
        # an indefinite spinner.
        job["status"] = "failed"
        job["finished_at"] = time.time()
        job["fatal_error"] = str(e)[:500]
    finally:
        save_job(job, created_by)


def _process_one_seed(seed: dict, progress: dict, entry: dict) -> None:
    """Run fetch_similar_users + ingest for a single seed and roll the result
    into `progress` / `entry`. Per Sir's spec: target_count == len(candidates)
    so we ingest every account the API returned, no per-seed cap.

    Note: ingest.run handles its own DB transaction, so each seed's new rows
    are visible to the next seed's pre-dedup. That's the win over a single
    fan-out ingest at the end."""
    plat = seed["platform"]
    if plat not in SIMILAR_CAPABLE:
        entry["error"] = f"{plat} has no similar API"
        progress["seed_errors"] += 1
        return
    sims = get_platform(plat).fetch_similar_users(
        str(seed["platform_user_id"]),
        secondary_id=str(seed.get("secondary_id") or ""),
    )
    candidates = [(s, f"similar:{plat}:{seed['username']}") for s in (sims or [])]
    entry["considered"] = len(candidates)
    if not candidates:
        return
    # target_count = len(candidates) → "ingest everything the API returned"
    report = ingest.run(candidates, target_count=len(candidates), hydrate=True)
    progress["considered"]    += report.get("considered", 0)
    progress["skipped_pre"]   += report.get("skipped_pre", 0)
    progress["skipped_email"] += report.get("skipped_email", 0)
    new_n = len(report.get("new_accounts") or [])
    cross_n = len(report.get("cross_platform") or [])
    h_err = len(report.get("hydrate_errors") or [])
    progress["new"]             += new_n
    progress["cross_platform"]  += cross_n
    progress["hydrate_errors"]  += h_err
    progress["llm_filtered"]    += len(report.get("llm_filtered") or [])
    entry["new"] = new_n
