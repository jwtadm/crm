"""One-shot script: run similarity discovery on every 已成交 customer's hydrated
IG account. Per-seed count=10, serial loop, picks up TikHub-rate-limit fix
from platforms/_tikhub.py.

Run from huawei-temp prod:
  PW=<han_pw> python3 /tmp/bulk_similarity_dealmade.py [--dry-run] [--limit N]

Output: live progress per seed, then a summary with top new finds.
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import time

import requests

BASE = "http://localhost:5001"
SESS = requests.Session()
DB_PATH = "/opt/ig_scraper/scout.db"


def login():
    pw = os.environ.get("PW")
    if not pw:
        sys.exit("set PW env var (han's password)")
    user = os.environ.get("USER_NAME", "han")
    r = SESS.post(f"{BASE}/api/auth/login",
                  json={"username": user, "password": pw}, timeout=10)
    if not r.json().get("ok"):
        sys.exit(f"login failed: {r.text}")
    print(f"  logged in as {user}")


def list_seeds():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT pa.id AS aid, pa.username, pa.follower_count, p.full_name
          FROM platform_accounts pa JOIN people p ON p.id = pa.person_id
         WHERE p.status = '已成交' AND pa.platform = 'instagram'
           AND pa.platform_user_id NOT LIKE 'import:%'
           AND pa.follower_count IS NOT NULL
         ORDER BY pa.follower_count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def tikhub_balance() -> float:
    sys.path.insert(0, "/opt/ig_scraper")
    from lib import settings
    r = requests.get("https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
                     headers={"Authorization": "Bearer " + settings.require("TIKHUB_API_KEY")},
                     timeout=10)
    return float(r.json()["user_data"]["balance"])


def people_max_id() -> int:
    c = sqlite3.connect(DB_PATH)
    r = c.execute("SELECT COALESCE(MAX(id), 0) FROM people").fetchone()
    return int(r[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="just list seeds + estimate cost, don't call TikHub")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap number of seeds (debug)")
    ap.add_argument("--count", type=int, default=10,
                    help="similar candidates per seed (default 10)")
    args = ap.parse_args()

    seeds = list_seeds()
    if args.limit:
        seeds = seeds[:args.limit]

    print(f"\n=== bulk similarity for 已成交 ===")
    print(f"seeds (hydrated IG under 已成交): {len(seeds)}")
    print(f"per-seed count target: {args.count}")
    est_cost = len(seeds) * (0.002 + args.count * 0.001)
    print(f"upper-bound budget: ${est_cost:.2f} (real spend likely 30-50% lower due to dedup)")
    print()

    if args.dry_run:
        print("seed list:")
        for s in seeds:
            print(f"  @{s['username']:<30}  {s['follower_count']:>8}  owner={s['full_name']!r}")
        print(f"\n(dry-run; pass without --dry-run to actually call TikHub)")
        return 0

    login()
    print(f"  TikHub balance BEFORE: ${tikhub_balance():.4f}")
    print(f"  MAX(people.id) BEFORE: {people_max_id()}")
    print()

    started = time.time()
    snap_pid = people_max_id()

    totals = {"considered": 0, "new": 0, "skipped_pre": 0, "skipped_email": 0,
              "cross_platform": 0, "hydrate_errors": 0, "seed_errors": 0}
    seed_errors_detail = []

    for i, s in enumerate(seeds, 1):
        t0 = time.time()
        try:
            r = SESS.post(f"{BASE}/api/similarity/run",
                          json={"seed_account_ids": [s["aid"]], "count": args.count},
                          timeout=120)
            d = r.json()
        except Exception as e:
            print(f"  [{i:3d}/{len(seeds)}] @{s['username']:<28}  ERROR: {e!s:.80}")
            totals["seed_errors"] += 1
            seed_errors_detail.append({"seed": s["username"], "error": str(e)[:200]})
            continue
        if d.get("error"):
            print(f"  [{i:3d}/{len(seeds)}] @{s['username']:<28}  ERROR: {d.get('error')} {d.get('detail','')[:80]}")
            totals["seed_errors"] += 1
            seed_errors_detail.append({"seed": s["username"], "error": d.get("error")})
            continue
        new_n = len(d.get("new_accounts", []))
        cross_n = len(d.get("cross_platform", []))
        h_err = len(d.get("hydrate_errors", []))
        totals["considered"]    += d.get("considered", 0)
        totals["new"]           += new_n
        totals["skipped_pre"]   += d.get("skipped_pre", 0)
        totals["skipped_email"] += d.get("skipped_email", 0)
        totals["cross_platform"] += cross_n
        totals["hydrate_errors"] += h_err
        dt = time.time() - t0
        print(f"  [{i:3d}/{len(seeds)}] @{s['username']:<28}  "
              f"new={new_n:<3} dedup_pre={d.get('skipped_pre',0):<3} dedup_email={d.get('skipped_email',0):<2} "
              f"hydrate_err={h_err}  {dt:.1f}s")

    elapsed = time.time() - started

    print()
    print("=== summary ===")
    print(f"  elapsed: {elapsed/60:.1f} min")
    print(f"  seeds:   {len(seeds)} (errors: {totals['seed_errors']})")
    print(f"  TOTAL new accounts ingested: {totals['new']}")
    print(f"  TOTAL skipped (already in DB):    {totals['skipped_pre']}")
    print(f"  TOTAL skipped (same person email): {totals['skipped_email']}")
    print(f"  cross_platform merges:        {totals['cross_platform']}")
    print(f"  hydrate errors (after retry): {totals['hydrate_errors']}")
    print()

    # New person rows
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row
    new_persons = c.execute(
        "SELECT COUNT(*) FROM people WHERE id > ?", (snap_pid,)
    ).fetchone()[0]
    print(f"  new people rows: {new_persons} (id > {snap_pid})")
    print()

    print("  顶部新发现（按粉丝数排）:")
    for r in c.execute("""
        SELECT pa.platform, pa.username, pa.follower_count, pa.source
          FROM platform_accounts pa JOIN people p ON p.id=pa.person_id
         WHERE p.id > ?
         ORDER BY pa.follower_count DESC NULLS LAST LIMIT 15""", (snap_pid,)):
        print(f"   @{r['username']:<28}  {(r['follower_count'] or 0):>10}  ← {r['source']}")

    print()
    print(f"  TikHub balance AFTER:  ${tikhub_balance():.4f}")
    print()
    print(f"  Review URL: 打开 /people → 排序选「创建时间」→ 顶部就是这批新发现 {new_persons} 人")


if __name__ == "__main__":
    raise SystemExit(main())
