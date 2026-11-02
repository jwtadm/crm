"""Hydrate placeholder platform_accounts via TikHub fetch_user_info.

For a filtered subset of people (e.g. high-value status), upgrade their
`import:<handle>` placeholder rows to real data: replace platform_user_id
with the real numeric one, populate bio/follower_count/etc, and (best-effort)
embed the bio for similarity ranking.

Usage:
    python tools/hydrate.py --status 已成交,主动来找,感兴趣 [--platforms ig,x,tiktok] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, ai
from platforms import get as get_platform


def _probe_platform(plat: str) -> bool:
    """Single test call to verify the platform's fetch endpoint is up."""
    test_handles = {"instagram": "instagram", "x": "jack", "tiktok": "tiktok"}
    handle = test_handles.get(plat)
    if not handle:
        return False
    try:
        get_platform(plat).fetch_user_info(handle)
        return True
    except Exception as e:
        print(f"  [{plat}] preflight failed: {str(e)[:100]}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="", help="Comma-separated status values to filter people")
    ap.add_argument("--platforms", default="instagram,x,tiktok", help="Comma-separated platforms to attempt")
    ap.add_argument("--limit", type=int, default=0, help="Max accounts to hydrate (0 = no limit)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    requested_plats = [p.strip() for p in args.platforms.split(",") if p.strip()]
    statuses = [s.strip() for s in args.status.split(",") if s.strip()]

    # Preflight
    print("=== Preflight ===")
    available = []
    for plat in requested_plats:
        if _probe_platform(plat):
            print(f"  [{plat}] OK")
            available.append(plat)
        else:
            print(f"  [{plat}] DOWN — skipping")
    if not available:
        sys.exit("All requested platforms are down. Try again later.")

    # Pick targets
    print("\n=== Selecting targets ===")
    placeholders: list[tuple[int, int, str, str]] = []  # (account_id, person_id, platform, username)
    with db.tx() as c:
        plat_qmarks = ",".join("?" * len(available))
        sql = f"""
            SELECT pa.id, pa.person_id, pa.platform, pa.username
            FROM platform_accounts pa
            JOIN people p ON pa.person_id = p.id
            WHERE pa.platform_user_id LIKE 'import:%'
              AND pa.platform IN ({plat_qmarks})
        """
        sql_args = list(available)
        if statuses:
            sql += f" AND p.status IN ({','.join('?' * len(statuses))})"
            sql_args.extend(statuses)
        rows = c.execute(sql, sql_args).fetchall()
        for r in rows:
            placeholders.append((r["id"], r["person_id"], r["platform"], r["username"]))

    if args.limit:
        placeholders = placeholders[: args.limit]

    by_plat = {}
    for _, _, plat, _ in placeholders:
        by_plat[plat] = by_plat.get(plat, 0) + 1
    print(f"  total placeholders to hydrate: {len(placeholders)}")
    for p, n in by_plat.items():
        print(f"    {p}: {n}")

    if args.dry_run:
        print("\nDRY-RUN — no API calls made.")
        return

    # Hydrate
    print(f"\n=== Hydrating ({args.workers} workers) ===")
    t_start = time.time()
    success: list[int] = []  # account_ids that were successfully replaced
    failed: list[tuple[int, str, str]] = []  # (account_id, handle, error)

    def _one(item):
        acct_id, person_id, plat, username = item
        # Skip dirty data (Excel error literals etc.)
        if not username or username.startswith("#") or " " in username:
            return ("skip", acct_id, username, "invalid handle")
        try:
            full = get_platform(plat).fetch_user_info(username)
            return ("ok", acct_id, person_id, plat, full)
        except Exception as e:
            return ("err", acct_id, username, str(e)[:200])

    import sqlite3 as _sql
    import json as _json
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, result in enumerate(ex.map(_one, placeholders), 1):
            if result[0] == "ok":
                _, acct_id, person_id, plat, full = result
                real_pid = str(full.get("platform_user_id", ""))
                if not real_pid:
                    failed.append((acct_id, full.get("username", "?"), "no platform_user_id returned"))
                    continue
                bio_email = (full.get("email") or "").strip().lower() or None
                try:
                    with db.tx() as c:
                        c.execute(
                            """UPDATE platform_accounts
                                  SET platform_user_id=?, secondary_id=?, username=?, full_name=?,
                                      biography=?, follower_count=?, following_count=?, post_count=?,
                                      video_count=?, is_verified=?, is_private=?, is_business=?,
                                      category=?, profile_pic_url=?, external_url=?, bio_links=?,
                                      email=?, phone=?, city=?, address=?, country=?,
                                      raw=?, source=?, updated_at=?
                                WHERE id=?""",
                            (
                                real_pid, full.get("secondary_id"),
                                full.get("username", ""), full.get("full_name", ""),
                                full.get("biography", ""),
                                full.get("follower_count"), full.get("following_count"),
                                full.get("post_count"), full.get("video_count"),
                                1 if full.get("is_verified") else 0,
                                1 if full.get("is_private") else 0,
                                1 if full.get("is_business") else 0,
                                full.get("category", ""), full.get("profile_pic_url", ""),
                                full.get("external_url", ""),
                                _json.dumps(full.get("bio_links") or [], ensure_ascii=False),
                                bio_email, full.get("phone", ""),
                                full.get("city", ""), full.get("address", ""), full.get("country", ""),
                                _json.dumps(full.get("raw") or {}, ensure_ascii=False),
                                "hydrate:phase-b-2026-05-07", int(time.time()),
                                acct_id,
                            ),
                        )
                        # Backfill person.email when the person row has none yet.
                        # On collision (email belongs to another existing person), MERGE the
                        # two persons — same real human ended up split across two rows.
                        if bio_email:
                            try:
                                c.execute(
                                    "UPDATE people SET email=?, updated_at=? "
                                    "WHERE id=? AND (email IS NULL OR email='')",
                                    (bio_email, int(time.time()), person_id),
                                )
                            except _sql.IntegrityError:
                                owner = c.execute(
                                    "SELECT id FROM people WHERE LOWER(email) = LOWER(?)",
                                    (bio_email,),
                                ).fetchone()
                                if owner and owner[0] != person_id:
                                    db.merge_persons(c, owner[0], person_id)
                    success.append(acct_id)
                except _sql.IntegrityError as e:
                    failed.append((acct_id, full.get("username", "?"),
                                   f"已存在另一行同样的 platform_user_id（重复账号合并冲突）"))
            elif result[0] == "skip":
                failed.append((result[1], result[2], result[3]))
            else:
                failed.append((result[1], result[2], result[3]))

            if i % 25 == 0 or i == len(placeholders):
                elapsed = time.time() - t_start
                print(f"  progress {i}/{len(placeholders)}  ok={len(success)} fail={len(failed)}  ({elapsed:.0f}s)")

    # Embed all newly-real accounts
    if success:
        print(f"\n=== Embedding {len(success)} new accounts ===")
        try:
            from lib import ingest as _ingest
            _ingest._embed_accounts(success)
            print(f"  embedded OK")
        except Exception as e:
            print(f"  embedding error: {e}")

    # Summary
    print("\n" + "=" * 60)
    print(f"  hydrated: {len(success)}")
    print(f"  failed/skipped: {len(failed)}")
    if failed:
        # Show first 10 failures
        print(f"  sample failures:")
        for entry in failed[:10]:
            print(f"    acct={entry[0]} handle=@{entry[1]} → {entry[2][:80]}")
    print(f"  wall time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
