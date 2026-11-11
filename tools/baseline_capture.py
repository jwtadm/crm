#!/usr/bin/env python3
"""Capture a behaviour + performance baseline for the read-only API surface.

Why this exists
---------------
The refactor has one hard requirement: **现有逻辑不变**. The only way to prove
that is to snapshot exactly what every read endpoint returns *before* touching
anything, then diff byte-for-byte after each phase. This script produces that
snapshot, and at the same time records how long each endpoint took and how many
SQL statements / connections it burned -- which is the evidence for the
performance half of the work.

It runs the app in-process through Flask's test client, so no server is needed.

Usage
-----
    # capture
    SCOUT_DB_PATH=tests/.baseline.db python tools/baseline_capture.py --label before

    # capture again later and diff against the first run
    SCOUT_DB_PATH=tests/.baseline.db python tools/baseline_capture.py --label after
    python tools/baseline_capture.py --compare before after

Safety
------
Refuses to run unless SCOUT_DB_PATH is set to something other than the repo's
real ``scout.db``. Booting the app mutates the DB it points at (init_db runs
migrations + ANALYZE) and this script resets the admin password, so it must
only ever see a throwaway copy. Same convention as tests/rrweb/_lib/seed.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

REPORT_ROOT = REPO_ROOT / "reports" / "baseline"

# Baseline admin credential. Only ever applied to the throwaway copy.
BASELINE_ADMIN_PW = os.environ.get("SCOUT_BASELINE_ADMIN_PW", "baseline12345")

# Keys whose value moves on its own (login stamps, wall-clock echoes). Blanked
# before hashing so an unrelated field change can't mask a real regression.
VOLATILE_KEYS = {"last_login_at"}
VOLATILE_PLACEHOLDER = "<volatile>"

# ---------------------------------------------------------------------------
# Deterministic time windows.
#
# All four are exact Asia/Shanghai midnights (ts % 86400 == 57600), so they
# stay valid day buckets no matter when the script runs -- that is what makes
# these cases byte-comparable across runs. WIDE is the window from the real
# slow request observed in crm.log (GET /api/stats?start=1725120000&end=1793808000).
# ---------------------------------------------------------------------------
W_END = 1793808000          # 2026-11-05 00:00 +08
W_DAY_START = W_END - 86400          # 1 day
W_WEEK_START = W_END - 7 * 86400     # 7 days
W_MONTH_START = W_END - 30 * 86400   # 30 days
W_QUARTER_START = W_END - 90 * 86400  # 90 days
W_WIDE_START = 1725120000            # 795 days -- the observed slow case


def _fail(msg: str) -> None:
    sys.exit(f"baseline_capture: {msg}")


def _guard_db_path() -> Path:
    raw = os.environ.get("SCOUT_DB_PATH")
    if not raw:
        _fail(
            "SCOUT_DB_PATH is not set. Point it at a throwaway copy, e.g.\n"
            "  cp scout.db tests/.baseline.db\n"
            "  SCOUT_DB_PATH=tests/.baseline.db python tools/baseline_capture.py --label before"
        )
    target = Path(raw).resolve()
    if target == (REPO_ROOT / "scout.db").resolve():
        _fail("SCOUT_DB_PATH points at the real scout.db. Refusing -- use a copy.")
    if not target.exists():
        _fail(f"{target} does not exist. Copy scout.db there first.")
    return target


# ---------------------------------------------------------------------------
# SQL accounting
#
# Patching sqlite3.connect (rather than lib.db._conn) catches every connection
# in the process -- including lib/settings.py's separate, un-PRAGMA'd factory,
# which is precisely one of the things being measured.
# ---------------------------------------------------------------------------
_SQL = {"stmts": 0, "conns": 0}
_orig_connect = sqlite3.connect


def _count_stmt(_sql: str) -> None:
    _SQL["stmts"] += 1


def _patched_connect(*args, **kwargs):
    conn = _orig_connect(*args, **kwargs)
    _SQL["conns"] += 1
    conn.set_trace_callback(_count_stmt)
    return conn


def _reset_sql() -> None:
    _SQL["stmts"] = 0
    _SQL["conns"] = 0


def _normalise(obj):
    """Recursively blank volatile values so hashes compare meaningfully."""
    if isinstance(obj, dict):
        return {
            k: (VOLATILE_PLACEHOLDER if k in VOLATILE_KEYS and obj[k] is not None
                else _normalise(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


def _canonical(obj) -> str:
    return json.dumps(_normalise(obj), ensure_ascii=False, sort_keys=True, indent=2)


def build_cases() -> list[dict]:
    """The read-only surface worth pinning down.

    ``stable`` cases must be byte-identical across runs; they use explicit
    timestamps so the wall clock is irrelevant. ``stable: False`` cases depend
    on "now" (presets, due reminders) and are reported separately rather than
    failed.
    """
    # Deterministic sample rows, picked by id order so they never shift.
    p_plain = 76677      # lowest id
    p_followups = 76678  # has follow-up log rows
    p_multi = 76727      # has >1 platform account

    cases: list[dict] = [
        {"name": "auth_me", "path": "/api/auth/me"},
        {"name": "platforms", "path": "/api/platforms"},
        {"name": "db_stats", "path": "/api/db/stats"},

        # ---- stats: explicit windows (deterministic) ----
        {"name": "stats_win_day", "path": f"/api/stats?start={W_DAY_START}&end={W_END}"},
        {"name": "stats_win_week", "path": f"/api/stats?start={W_WEEK_START}&end={W_END}"},
        {"name": "stats_win_month", "path": f"/api/stats?start={W_MONTH_START}&end={W_END}"},
        {"name": "stats_win_wide", "path": f"/api/stats?start={W_WIDE_START}&end={W_END}"},
        {"name": "stats_win_month_user5",
         "path": f"/api/stats?start={W_MONTH_START}&end={W_END}&user_id=5"},
        {"name": "stats_win_month_userall",
         "path": f"/api/stats?start={W_MONTH_START}&end={W_END}&user_id=all"},

        # ---- stats: presets (time-dependent) ----
        {"name": "stats_preset_all", "path": "/api/stats?range=all", "stable": False},
        {"name": "stats_preset_today", "path": "/api/stats?range=today", "stable": False},
        {"name": "stats_preset_yesterday", "path": "/api/stats?range=yesterday", "stable": False},
        {"name": "stats_preset_week", "path": "/api/stats?range=week", "stable": False},
        {"name": "stats_preset_month", "path": "/api/stats?range=month", "stable": False},

        # ---- trends: explicit windows (deterministic; the N+1 hot spot) ----
        {"name": "trends_win_day", "path": f"/api/stats/trends?start={W_DAY_START}&end={W_END}"},
        {"name": "trends_win_week", "path": f"/api/stats/trends?start={W_WEEK_START}&end={W_END}"},
        {"name": "trends_win_month", "path": f"/api/stats/trends?start={W_MONTH_START}&end={W_END}"},
        {"name": "trends_win_quarter",
         "path": f"/api/stats/trends?start={W_QUARTER_START}&end={W_END}"},
        {"name": "trends_win_month_user5",
         "path": f"/api/stats/trends?start={W_MONTH_START}&end={W_END}&user_id=5"},

        # ---- trends: days/offset paths (time-dependent) ----
        {"name": "trends_days1", "path": "/api/stats/trends?days=1", "stable": False},
        {"name": "trends_days7", "path": "/api/stats/trends?days=7", "stable": False},
        {"name": "trends_days30", "path": "/api/stats/trends?days=30", "stable": False},
        {"name": "trends_days90", "path": "/api/stats/trends?days=90", "stable": False},
        {"name": "trends_yesterday", "path": "/api/stats/trends?days=1&offset=1", "stable": False},

        # ---- people list: every sort path + the filter combinations ----
        {"name": "people_default", "path": "/api/people/list?limit=50"},
        {"name": "people_page2", "path": "/api/people/list?limit=50&offset=50"},
        {"name": "people_order_updated", "path": "/api/people/list?limit=50&order=updated"},
        {"name": "people_order_created", "path": "/api/people/list?limit=50&order=created"},
        {"name": "people_order_created_asc", "path": "/api/people/list?limit=50&order=created_asc"},
        {"name": "people_order_name", "path": "/api/people/list?limit=50&order=name"},
        {"name": "people_order_followers", "path": "/api/people/list?limit=50&order=followers"},
        {"name": "people_search_text", "path": "/api/people/list?limit=50&q=anna"},
        {"name": "people_search_handle", "path": "/api/people/list?limit=50&q=%40anna"},
        {"name": "people_status", "path": "/api/people/list?limit=50&status=%E5%BE%85%E8%81%94%E7%B3%BB"},
        {"name": "people_has_email_yes", "path": "/api/people/list?limit=50&has_email=yes"},
        {"name": "people_has_email_no", "path": "/api/people/list?limit=50&has_email=no"},
        {"name": "people_platform_ig", "path": "/api/people/list?limit=50&has_platform=instagram"},
        {"name": "people_platform_none", "path": "/api/people/list?limit=50&has_platform=none"},
        {"name": "people_include_rejected", "path": "/api/people/list?limit=50&include_rejected=1"},
        {"name": "people_assigned_5", "path": "/api/people/list?limit=50&assigned_user_id=5"},
        {"name": "people_unassigned", "path": "/api/people/list?limit=50&assigned_user_id=unassigned"},

        # ---- match_ids (the limit=100000 path) ----
        {"name": "match_ids_status", "path": "/api/people/match_ids?status=%E5%BE%85%E8%81%94%E7%B3%BB"},
        {"name": "match_ids_search", "path": "/api/people/match_ids?q=anna"},

        # ---- single person detail ----
        {"name": "person_plain", "path": f"/api/people/{p_plain}"},
        {"name": "person_followups_row", "path": f"/api/people/{p_followups}"},
        {"name": "person_multi_account", "path": f"/api/people/{p_multi}"},
        {"name": "person_followups", "path": f"/api/people/{p_followups}/followups"},
        {"name": "person_reminders", "path": f"/api/people/{p_plain}/reminders"},

        # ---- admin surface ----
        {"name": "db_list", "path": "/api/db/list?limit=100"},
        {"name": "db_list_q", "path": "/api/db/list?limit=100&q=art"},
        {"name": "admin_users", "path": "/api/admin/users"},
        {"name": "admin_active_staff", "path": "/api/admin/active_staff"},
        {"name": "admin_audit", "path": "/api/admin/audit"},

        # ---- time-dependent ----
        {"name": "reminders_due", "path": "/api/reminders/due", "stable": False},
    ]
    for c in cases:
        c.setdefault("stable", True)
    return cases


def capture(label: str, include_stress: bool, repeat: int) -> Path:
    db_path = _guard_db_path()

    # Silence the watchdog before importing app: it starts a 60s daemon thread
    # at import time whose queries would pollute the per-case SQL counts.
    import lib.job_watchdog as job_watchdog
    job_watchdog.start = lambda *a, **k: None  # type: ignore[assignment]

    sqlite3.connect = _patched_connect  # type: ignore[assignment]

    import app as app_module
    from lib import auth

    admin = auth.get_user_by_username("admin")
    if not admin:
        _fail("no 'admin' user in the baseline DB")
    auth.set_password(admin["id"], BASELINE_ADMIN_PW, must_change=False)

    flask_app = app_module.app
    flask_app.config.update(TESTING=True)

    cases = build_cases()
    if include_stress:
        cases.append({
            "name": "trends_win_wide_STRESS",
            "path": f"/api/stats/trends?start={W_WIDE_START}&end={W_END}",
            "stable": True,
        })

    out_dir = REPORT_ROOT / label
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    with flask_app.test_client() as client:
        resp = client.post("/api/auth/login",
                           json={"username": "admin", "password": BASELINE_ADMIN_PW})
        if resp.status_code != 200:
            _fail(f"admin login failed: {resp.status_code} {resp.get_data(as_text=True)[:200]}")

        for idx, case in enumerate(cases, 1):
            name, path = case["name"], case["path"]
            best_ms = None
            body = None
            status = None
            stmts = conns = 0

            for attempt in range(repeat):
                _reset_sql()
                t0 = time.perf_counter()
                r = client.get(path)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if best_ms is None or elapsed_ms < best_ms:
                    best_ms = elapsed_ms
                    stmts, conns = _SQL["stmts"], _SQL["conns"]
                status = r.status_code
                body = r.get_data(as_text=True)

            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = {"__unparsed__": body[:2000]}

            canon = _canonical(parsed)
            (out_dir / "cases" / f"{name}.json").write_text(canon, encoding="utf-8")

            row = {
                "name": name,
                "path": path,
                "stable": case["stable"],
                "status": status,
                "ms": round(best_ms or 0.0, 1),
                "sql_statements": stmts,
                "connections": conns,
                "response_bytes": len(canon.encode("utf-8")),
                "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
            }
            summary.append(row)
            flag = "" if case["stable"] else "  (time-dependent)"
            print(f"[{idx:>2}/{len(cases)}] {name:<32} {status}  "
                  f"{row['ms']:>8.1f}ms  sql={stmts:<5} conn={conns:<4}{flag}",
                  flush=True)

    meta = {
        "label": label,
        "captured_at": int(time.time()),
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size,
        "repeat": repeat,
        "cases": summary,
        "totals": {
            "cases": len(summary),
            "sql_statements": sum(r["sql_statements"] for r in summary),
            "connections": sum(r["connections"] for r in summary),
            "ms": round(sum(r["ms"] for r in summary), 1),
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nwrote {out_dir}")
    print(f"totals: {meta['totals']['cases']} cases, "
          f"{meta['totals']['sql_statements']} SQL statements, "
          f"{meta['totals']['connections']} connections, "
          f"{meta['totals']['ms']:.0f}ms")
    return out_dir


def compare(before_label: str, after_label: str) -> int:
    def load(label: str) -> dict:
        f = REPORT_ROOT / label / "summary.json"
        if not f.exists():
            _fail(f"no baseline at {f}")
        return json.loads(f.read_text(encoding="utf-8"))

    before, after = load(before_label), load(after_label)
    b_by = {c["name"]: c for c in before["cases"]}
    a_by = {c["name"]: c for c in after["cases"]}

    changed_stable: list[str] = []
    changed_volatile: list[str] = []
    rows: list[tuple] = []

    for name in sorted(set(b_by) | set(a_by)):
        b, a = b_by.get(name), a_by.get(name)
        if b is None:
            print(f"  + NEW CASE      {name}")
            continue
        if a is None:
            print(f"  - MISSING CASE  {name}")
            changed_stable.append(name)
            continue
        same = b["sha256"] == a["sha256"]
        if not same:
            (changed_stable if b["stable"] else changed_volatile).append(name)
        rows.append((name, b, a, same))

    print(f"\n{'case':<34}{'sql before':>11}{'sql after':>11}"
          f"{'ms before':>11}{'ms after':>10}  body")
    print("-" * 94)
    for name, b, a, same in rows:
        mark = "same" if same else ("DIFF" if b["stable"] else "diff*")
        print(f"{name:<34}{b['sql_statements']:>11}{a['sql_statements']:>11}"
              f"{b['ms']:>11.1f}{a['ms']:>10.1f}  {mark}")

    bt, at = before["totals"], after["totals"]
    print("-" * 94)
    print(f"{'TOTAL':<34}{bt['sql_statements']:>11}{at['sql_statements']:>11}"
          f"{bt['ms']:>11.1f}{at['ms']:>10.1f}")

    print()
    if changed_volatile:
        print(f"time-dependent cases differing (expected): {len(changed_volatile)}")
        for n in changed_volatile:
            print(f"    * {n}")
    if changed_stable:
        print(f"\nFAIL: {len(changed_stable)} deterministic case(s) changed:")
        for n in changed_stable:
            print(f"    ! {n}")
            print(f"      diff: reports/baseline/{before_label}/cases/{n}.json"
                  f"  vs  reports/baseline/{after_label}/cases/{n}.json")
        return 1

    print("OK: every deterministic response is byte-identical.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", help="capture a baseline under this name")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="diff two captured baselines")
    ap.add_argument("--stress", action="store_true",
                    help="also capture the 795-day trends window (slow: ~5.5k queries today)")
    ap.add_argument("--repeat", type=int, default=2,
                    help="runs per case; the fastest is recorded (default 2)")
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare)
    if args.label:
        capture(args.label, args.stress, max(1, args.repeat))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
