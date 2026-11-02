"""One-shot importer for an existing CRM xlsx (sheet "客户联系总数据表0428").

Maps each row to a `people` row keyed by email; IG/Twitter handles become
placeholder `platform_accounts` rows (no follower counts, no embedding —
those come later when the account is hydrated via TikHub).

Usage:
    python tools/import_crm.py <path-to.xlsx>            # commit
    python tools/import_crm.py <path-to.xlsx> --dry-run  # preview only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook

from lib import db


SHEET_NAME = "客户联系总数据表0428"
COL_STATUS, COL_NAME, COL_EMAIL, COL_OWNER, COL_TWITTER, COL_IG, COL_TAGS, COL_CREATED, COL_UPDATED, COL_IP = range(10)


def _norm_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_email(v) -> str:
    s = _norm_str(v).lower().replace(" ", "")
    if "@" not in s or "." not in s:
        return ""
    return s.split("\n")[0]  # if multi-line, take the first


def _norm_handle(v) -> str:
    s = _norm_str(v).lstrip("@")
    # If a URL slipped in, take the last path segment
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    s = s.split()[0] if s else ""  # one handle per cell, take the first token
    return s


def _process(c: sqlite3.Connection, ws) -> dict:
    stats = {
        "rows": 0, "skipped_no_email": 0, "skipped_bad_email": 0,
        "people_created": 0, "people_updated": 0,
        "ig_added": 0, "x_added": 0, "handle_dupes": 0,
    }
    now = int(time.time())
    rows = ws.iter_rows(values_only=True)
    next(rows, None)  # header

    for raw in rows:
        stats["rows"] += 1
        if raw is None:
            continue
        # pad if shorter than expected
        raw = list(raw) + [None] * (10 - len(raw))

        email_raw = raw[COL_EMAIL]
        email = _norm_email(email_raw)
        if not email:
            if email_raw:
                stats["skipped_bad_email"] += 1
            else:
                stats["skipped_no_email"] += 1
            continue

        name = _norm_str(raw[COL_NAME])
        status = _norm_str(raw[COL_STATUS]) or "lead"
        owner = _norm_str(raw[COL_OWNER])
        tag_text = _norm_str(raw[COL_TAGS])
        ip = _norm_str(raw[COL_IP])
        created = raw[COL_CREATED]
        ig = _norm_handle(raw[COL_IG])
        tw = _norm_handle(raw[COL_TWITTER])

        notes_parts = []
        if ip:
            notes_parts.append(f"IP: {ip}")
        if created:
            notes_parts.append(f"导入创建于 {created}")
        notes = " · ".join(notes_parts)
        tags_json = json.dumps([tag_text], ensure_ascii=False) if tag_text else "[]"

        existing = c.execute("SELECT id FROM people WHERE email=?", (email,)).fetchone()
        if existing:
            pid = existing[0]
            sets, args = [], []
            if name:
                sets.append("full_name=?"); args.append(name)
            if status and status != "lead":
                sets.append("status=?"); args.append(status)
            if owner:
                sets.append("owner=?"); args.append(owner)
            if tag_text:
                sets.append("tags=?"); args.append(tags_json)
            if notes:
                sets.append("notes=?"); args.append(notes)
            if sets:
                sets.append("updated_at=?"); args.append(now)
                args.append(pid)
                c.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", args)
            stats["people_updated"] += 1
        else:
            cur = c.execute(
                """INSERT INTO people (email, full_name, status, owner, tags, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (email, name, status, owner, tags_json, notes, now, now),
            )
            pid = cur.lastrowid
            stats["people_created"] += 1

        for platform, handle, key in [("instagram", ig, "ig_added"), ("x", tw, "x_added")]:
            if not handle:
                continue
            placeholder = f"import:{handle.lower()}"
            exists = c.execute(
                "SELECT id FROM platform_accounts WHERE platform=? AND platform_user_id=?",
                (platform, placeholder),
            ).fetchone()
            if exists:
                stats["handle_dupes"] += 1
                continue
            try:
                c.execute(
                    """INSERT INTO platform_accounts
                       (person_id, platform, platform_user_id, username, source, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, platform, placeholder, handle, "import:crm-2026-04-28", now, now),
                )
                stats[key] += 1
            except sqlite3.IntegrityError:
                stats["handle_dupes"] += 1

    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    wb = load_workbook(args.path, data_only=True, read_only=True)
    if SHEET_NAME not in wb.sheetnames:
        sys.exit(f"sheet not found: {SHEET_NAME!r}; available: {wb.sheetnames}")
    ws = wb[SHEET_NAME]

    if args.dry_run:
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        try:
            stats = _process(c, ws)
        finally:
            c.close()
        print("DRY-RUN — nothing written")
    else:
        with db.tx() as c:
            stats = _process(c, ws)

    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
