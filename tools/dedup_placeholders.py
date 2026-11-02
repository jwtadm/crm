"""Merge person rows that ended up duplicated by the placeholder-collapse bug.

Symptom: same (platform, LOWER(username)) lives on two different person rows,
one as `import:<handle>` placeholder, the other as the real numeric id. The
placeholder row usually carries the CRM state (owner/status/email), while the
real-id row was created blindly by similarity/ingest.

Strategy: keep the side that has CRM signal (email > status != 待联系 > owner > name),
fold the other side in via db.merge_persons.

Default is dry-run. Pass --apply to write.
"""
from __future__ import annotations
import argparse

from lib import db


def _crm_score(p) -> int:
    s = 0
    if (p["email"] or "").strip():            s += 8
    if (p["status"] or "") not in ("", "待联系", "lead"):  s += 4
    if (p["owner"] or "").strip():            s += 2
    if (p["full_name"] or "").strip():        s += 1
    if p["assigned_user_id"] is not None:     s += 4
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db.init_db()

    pairs: list[tuple[int, int, str, str]] = []  # (target, source, platform, handle)
    with db.tx() as c:
        rows = c.execute("""
            SELECT platform, LOWER(username) AS uname,
                   GROUP_CONCAT(person_id || ":" || id || ":" || platform_user_id, '|') AS detail
            FROM platform_accounts
            WHERE username IS NOT NULL AND username != ''
            GROUP BY platform, LOWER(username)
            HAVING COUNT(DISTINCT person_id) > 1
        """).fetchall()
        print(f"duplicate (platform, handle) groups: {len(rows)}")
        for r in rows:
            parts = [p.split(":", 2) for p in r["detail"].split("|")]
            pids = sorted({int(p[0]) for p in parts})
            if len(pids) != 2:
                print(f"  skip (>{len(pids)}-way): {r['platform']} @{r['uname']}")
                continue
            a = c.execute("SELECT * FROM people WHERE id=?", (pids[0],)).fetchone()
            b = c.execute("SELECT * FROM people WHERE id=?", (pids[1],)).fetchone()
            if not a or not b:
                continue
            # Higher CRM score wins; tie → older id (smaller) wins
            target, source = (a, b) if _crm_score(a) >= _crm_score(b) else (b, a)
            pairs.append((target["id"], source["id"], r["platform"], r["uname"]))
            print(f"  @{r['uname']} ({r['platform']}): merge {source['id']} → {target['id']} "
                  f"(target CRM score={_crm_score(target)}, source={_crm_score(source)})")

    if not args.apply:
        print(f"\n(dry-run; {len(pairs)} merges queued; pass --apply to write)")
        return 0

    merged = 0
    with db.tx() as c:
        for target_id, source_id, _, _ in pairs:
            if db.merge_persons(c, target_id, source_id):
                merged += 1
    print(f"\napplied {merged} merges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
