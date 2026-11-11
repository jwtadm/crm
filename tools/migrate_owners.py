"""Map legacy `people.owner` text to `people.assigned_user_id`.

Default is dry-run. Pass --apply to actually write.

Match order:
  1. LOWER(people.owner) == LOWER(users.username)
  2. else LOWER(people.owner) == LOWER(users.display_name) and unique
  3. else skip

Rows that already have `assigned_user_id` are never overwritten (idempotent).
"""
from __future__ import annotations
import argparse
import json
import time
from collections import Counter, defaultdict

from lib import audit, db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--actor-id", type=int, default=None, help="user id to attribute audit entries to")
    args = ap.parse_args()

    db.init_db()

    with db.tx() as c:
        users = c.execute("SELECT id, username, display_name FROM users").fetchall()
        people = c.execute(
            "SELECT id, owner, assigned_user_id FROM people WHERE assigned_user_id IS NULL AND owner IS NOT NULL AND owner != ''"
        ).fetchall()

    by_username: dict[str, int] = {}
    display_buckets: dict[str, list[int]] = defaultdict(list)
    for u in users:
        by_username[(u["username"] or "").strip().lower()] = u["id"]
        if u["display_name"]:
            display_buckets[u["display_name"].strip().lower()].append(u["id"])

    will_update: list[tuple[int, int]] = []  # (person_id, user_id)
    no_match: Counter = Counter()
    ambiguous: Counter = Counter()

    for p in people:
        owner = (p["owner"] or "").strip().lower()
        if not owner:
            continue
        uid = by_username.get(owner)
        if uid is None:
            candidates = display_buckets.get(owner, [])
            if len(candidates) == 1:
                uid = candidates[0]
            elif len(candidates) > 1:
                ambiguous[owner] += 1
                continue
        if uid is None:
            no_match[p["owner"]] += 1
            continue
        will_update.append((p["id"], uid))

    print(f"would update: {len(will_update)}")
    print(f"unmatched:   {sum(no_match.values())} (top 10: {no_match.most_common(10)})")
    print(f"ambiguous:   {sum(ambiguous.values())} (display_name multi-hit)")

    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
        return 0

    now = int(time.time())
    with db.tx() as c:
        for pid, uid in will_update:
            c.execute(
                "UPDATE people SET assigned_user_id=?, updated_at=? WHERE id=? AND assigned_user_id IS NULL",
                (uid, now, pid),
            )
    # Audit (one entry per assignment kept terse). Batched into a single
    # transaction — per-row audit.log() meant one connection + commit each.
    audit.log_many([
        (args.actor_id, "person.assign", f"person:{pid}",
         {"assigned_user_id": uid, "via": "owner_migration"})
        for pid, uid in will_update
    ])
    print(f"\napplied {len(will_update)} assignments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
