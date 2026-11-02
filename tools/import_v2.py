"""Multi-file CRM importer (Tables 1/2/3, 2026-05-07 batch).

Processing order: 108 → 109 → 110. Later files overwrite non-empty person
fields seen earlier. Each file's structure is different — see parsers below.

Usage:
    python tools/import_v2.py --clear           # wipe DB first then import all
    python tools/import_v2.py --dry-run         # preview counts only

Person dedup: by email (lowercased). When no email, handle is the key.
Platform_account dedup: by (platform, "import:<username>") placeholder ID.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook

from lib import db


# ----------- helpers -----------

def _s(v) -> str:
    """Normalize cell to trimmed string."""
    if v is None:
        return ""
    return str(v).strip()


def _email(v) -> str:
    s = _s(v).lower().replace(" ", "")
    if "@" not in s or "." not in s:
        return ""
    s = s.split("\n")[0].split(",")[0].split(";")[0].strip()
    if "@" not in s or "." not in s:
        return ""
    return s


def _handle(v) -> str:
    s = _s(v).lstrip("@")
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    s = s.split()[0] if s else ""
    s = s.lstrip("@")
    return s


_PLAT_NORM = {
    "x": "x", "twitter": "x", "tw": "x",
    "ig": "instagram", "ins": "instagram", "instagram": "instagram",
    "tk": "tiktok", "tt": "tiktok", "tiktok": "tiktok",
    "douyin": "tiktok",  # close enough for our schema
}


def _platform(v) -> str:
    return _PLAT_NORM.get(_s(v).lower(), "")


# ----------- staged record -----------

class Record:
    """One unified person + handles record. Later parsers can merge into it."""
    def __init__(self, email: str = ""):
        self.email = email
        self.full_name = ""
        self.owner = ""
        self.status = ""
        self.notes_lines: list[str] = []
        self.tags: list[str] = []
        self.handles: list[tuple[str, str]] = []   # (platform, username)
        self.follower_hint: dict[str, str] = {}    # platform -> raw follower string

    def add_note(self, line: str):
        line = (line or "").strip()
        if line and line not in self.notes_lines:
            self.notes_lines.append(line)

    def add_handle(self, platform: str, username: str):
        if not platform or not username:
            return
        username = username.strip().lstrip("@")
        if not username:
            return
        if (platform, username.lower()) not in [(p, u.lower()) for p, u in self.handles]:
            self.handles.append((platform, username))


# ----------- parsers -----------

def parse_table1(path: Path) -> list[Record]:
    """file_108.xlsx → sheet 最全 (8086 rows).
    Cols: 日期 | 序号 | 平台 | 负责人 | 账号名称 | 粉丝数 | 邮箱 | 初次反馈备注 | 初次反馈时间 | 二次反馈 | 二次反馈时间
    """
    print(f"[t1] reading {path.name}")
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb["sheet 最全"]
    out: list[Record] = []
    rows = ws.iter_rows(values_only=True)
    next(rows, None)  # header
    for raw in rows:
        if not raw or not any(raw):
            continue
        raw = list(raw) + [None] * (11 - len(raw))
        platform = _platform(raw[2])
        owner = _s(raw[3])
        username = _handle(raw[4])
        followers = _s(raw[5])
        email_raw = _s(raw[6])
        email = _email(email_raw)
        feedback1 = _s(raw[7])
        feedback2 = _s(raw[9])

        r = Record(email=email)
        if platform and username:
            r.add_handle(platform, username)
            if followers:
                r.follower_hint[platform] = followers
        r.owner = owner
        # 邮箱列写"私信"等中文 = 当时只走 DM，没邮箱；当作 note 留下
        if email_raw and not email and email_raw not in ("私信", "无", "-"):
            r.add_note(f"原始邮箱字段: {email_raw}")
        if feedback1:
            r.add_note(f"初次反馈: {feedback1}")
        if feedback2:
            r.add_note(f"二次反馈: {feedback2}")
        out.append(r)
    wb.close()
    print(f"[t1] parsed {len(out)} records")
    return out


def parse_table2(path: Path) -> list[Record]:
    """file_109.xlsx — Email-艺术家 (14745) + DM (219). 竞家账号 / 优秀视频 ignored."""
    print(f"[t2] reading {path.name}")
    wb = load_workbook(path, data_only=True, read_only=True)
    out: list[Record] = []

    # Sheet "Email-艺术家":  _, email, Instagram, 联系人, platform, Follow
    ws = wb["Email-艺术家"]
    rows = ws.iter_rows(values_only=True)
    next(rows, None)
    for raw in rows:
        if not raw or not any(raw):
            continue
        raw = list(raw) + [None] * (6 - len(raw))
        email = _email(raw[1])
        ig = _handle(raw[2])
        contact = _s(raw[3])
        plat_raw = _platform(raw[4])
        follow_note = _s(raw[5])
        if not (email or ig):
            continue
        r = Record(email=email)
        r.full_name = contact
        if ig:
            r.add_handle("instagram", ig)
        if plat_raw and ig:
            # explicit platform col may say something other than instagram (rare)
            if plat_raw != "instagram":
                r.add_handle(plat_raw, ig)
        if follow_note:
            r.add_note(f"Follow: {follow_note}")
        out.append(r)

    # Sheet "DM": Platform, user_id, name, Follow, Date, Email, _, 中国, _
    ws = wb["DM"]
    rows = ws.iter_rows(values_only=True)
    next(rows, None)
    for raw in rows:
        if not raw or not any(raw):
            continue
        raw = list(raw) + [None] * (9 - len(raw))
        platform = _platform(raw[0])
        username = _handle(raw[1])
        name = _s(raw[2])
        follow = _s(raw[3])
        email = _email(raw[5])
        china_handle = _handle(raw[8])
        if not (email or username or china_handle):
            continue
        r = Record(email=email)
        r.full_name = name
        if platform and username:
            r.add_handle(platform, username)
        if china_handle:
            # column "中国" typically a domestic IG handle paired with the X account
            r.add_handle("instagram", china_handle)
        if follow:
            r.add_note(f"DM: {follow}")
        out.append(r)

    wb.close()
    print(f"[t2] parsed {len(out)} records")
    return out


def parse_table3(path: Path) -> list[Record]:
    """file_110.csv (35809 rows).
    Cols: 时间 | 联系人 | 电子邮箱 | 负责员工 | Twitter | Instagram | tk | 标签 | 创建日期 | 更新日期 | _
    """
    print(f"[t3] reading {path.name}")
    out: list[Record] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for raw in reader:
            if not raw or not any(raw):
                continue
            raw = (raw + [""] * 11)[:11]
            status = _s(raw[0])
            name = _s(raw[1])
            email = _email(raw[2])
            owner = _s(raw[3])
            tw = _handle(raw[4])
            ig = _handle(raw[5])
            tk = _handle(raw[6])
            tag = _s(raw[7])
            if not (email or tw or ig or tk):
                continue
            r = Record(email=email)
            r.full_name = name
            r.owner = owner
            if status:
                r.status = status
            if tw:
                r.add_handle("x", tw)
            if ig:
                r.add_handle("instagram", ig)
            if tk:
                r.add_handle("tiktok", tk)
            if tag:
                r.tags.append(tag)
            out.append(r)
    print(f"[t3] parsed {len(out)} records")
    return out


# ----------- merge & write -----------

_DEFAULT_STATUS = "待联系"
_STATUS_MAP = {  # normalize legacy/EN values to the new CN canonical set
    "lead": "待联系",
    "contacted": "已联系",
    "replied": "已回复",
    "dropped": "已放弃",
    "qualified": "已成交",
    "成交": "已成交",
}


def _norm_status(s: str) -> str:
    s = (s or "").strip()
    return _STATUS_MAP.get(s, s) if s else _DEFAULT_STATUS


def upsert_record(c: sqlite3.Connection, r: Record, source: str, now: int) -> None:
    """Insert/update one Record. Email is canonical when present; otherwise
    use the first handle as the dedup key (so multi-row same-handle merges).
    """
    pid: int | None = None
    fresh_insert = False
    status_norm = _norm_status(r.status)

    if r.email:
        row = c.execute("SELECT id FROM people WHERE email=?", (r.email,)).fetchone()
        if row:
            pid = row[0]
        else:
            cur = c.execute(
                "INSERT INTO people (email, full_name, status, owner, tags, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (r.email, r.full_name, status_norm, r.owner,
                 json.dumps(r.tags, ensure_ascii=False), "\n".join(r.notes_lines), now, now),
            )
            pid = cur.lastrowid
            fresh_insert = True

    # No email yet: see if any of the handles is already linked to a person
    if pid is None:
        for plat, uname in r.handles:
            placeholder = f"import:{uname.lower()}"
            row = c.execute(
                "SELECT person_id FROM platform_accounts WHERE platform=? AND platform_user_id=?",
                (plat, placeholder),
            ).fetchone()
            if row:
                pid = row[0]
                break

    # Still no person → create one (no email; full_name from record)
    if pid is None:
        cur = c.execute(
            "INSERT INTO people (email, full_name, status, owner, tags, notes, created_at, updated_at) VALUES (NULL,?,?,?,?,?,?,?)",
            (r.full_name, status_norm, r.owner,
             json.dumps(r.tags, ensure_ascii=False), "\n".join(r.notes_lines), now, now),
        )
        pid = cur.lastrowid
        fresh_insert = True

    # If existing person, merge non-empty fields from this record
    if not fresh_insert:
        sets, args = [], []
        if r.full_name:
            sets.append("full_name=?"); args.append(r.full_name)
        if r.status and status_norm != _DEFAULT_STATUS:
            sets.append("status=?"); args.append(status_norm)
        if r.owner:
            sets.append("owner=?"); args.append(r.owner)
        if r.tags:
            existing_tags_row = c.execute("SELECT tags FROM people WHERE id=?", (pid,)).fetchone()
            existing = []
            try:
                existing = json.loads(existing_tags_row[0] or "[]")
            except Exception:
                pass
            merged = list(dict.fromkeys([*existing, *r.tags]))
            sets.append("tags=?"); args.append(json.dumps(merged, ensure_ascii=False))
        if r.notes_lines:
            existing_notes_row = c.execute("SELECT notes FROM people WHERE id=?", (pid,)).fetchone()
            existing_notes = (existing_notes_row[0] or "").strip()
            new_notes = "\n".join(r.notes_lines)
            if existing_notes and new_notes not in existing_notes:
                merged = existing_notes + "\n" + new_notes
            elif not existing_notes:
                merged = new_notes
            else:
                merged = existing_notes
            sets.append("notes=?"); args.append(merged)
        if sets:
            sets.append("updated_at=?"); args.append(now)
            args.append(pid)
            c.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", args)

    # Always attach handles regardless of fresh/existing — this was the bug
    for plat, uname in r.handles:
        placeholder = f"import:{uname.lower()}"
        exists = c.execute(
            "SELECT 1 FROM platform_accounts WHERE platform=? AND platform_user_id=?",
            (plat, placeholder),
        ).fetchone()
        if exists:
            continue
        try:
            c.execute(
                """INSERT INTO platform_accounts (person_id, platform, platform_user_id, username, source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, plat, placeholder, uname, source, now, now),
            )
        except sqlite3.IntegrityError:
            pass


# ----------- orchestrator -----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="Wipe people + platform_accounts before importing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--t1", default="/tmp/file_108.xlsx")
    ap.add_argument("--t2", default="/tmp/file_109.xlsx")
    ap.add_argument("--t3", default="/tmp/file_110.csv")
    args = ap.parse_args()

    db.init_db()
    paths = [Path(args.t1), Path(args.t2), Path(args.t3)]
    for p in paths:
        if not p.exists():
            sys.exit(f"missing file: {p}")

    print("=" * 60)
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'COMMIT'}{'  + clear' if args.clear else ''}")
    print("=" * 60)

    # Parse all three (in memory) to validate before touching DB
    recs1 = parse_table1(paths[0])
    recs2 = parse_table2(paths[1])
    recs3 = parse_table3(paths[2])

    print(f"\nTotals: t1={len(recs1)} t2={len(recs2)} t3={len(recs3)}  →  combined {len(recs1)+len(recs2)+len(recs3)}")

    # Open connection
    if args.dry_run:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
    else:
        conn = sqlite3.connect(db.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

    if args.clear:
        print("\n[clear] truncating people + platform_accounts ...")
        conn.execute("DELETE FROM platform_accounts")
        conn.execute("DELETE FROM people")
        conn.commit()
        print(f"[clear] done. people={conn.execute('SELECT COUNT(*) FROM people').fetchone()[0]}")

    now = int(time.time())
    sources = ["import:t1-2026-05-07", "import:t2-2026-05-07", "import:t3-2026-05-07"]
    all_recs = [recs1, recs2, recs3]

    for src, recs in zip(sources, all_recs):
        print(f"\n[{src}] applying {len(recs)} records ...")
        for r in recs:
            upsert_record(conn, r, src, now)
        conn.commit()
        ppl = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        pa = conn.execute("SELECT COUNT(*) FROM platform_accounts").fetchone()[0]
        print(f"  → DB now: people={ppl:,} platform_accounts={pa:,}")

    # Final summary
    print("\n" + "=" * 60)
    print("FINAL DB STATE")
    print("=" * 60)
    print(f"  people:            {conn.execute('SELECT COUNT(*) FROM people').fetchone()[0]:,}")
    print(f"  platform_accounts: {conn.execute('SELECT COUNT(*) FROM platform_accounts').fetchone()[0]:,}")
    by_plat = conn.execute("SELECT platform, COUNT(*) FROM platform_accounts GROUP BY platform").fetchall()
    for plat, n in by_plat:
        print(f"    └ {plat:<12} {n:,}")
    by_owner = conn.execute(
        "SELECT owner, COUNT(*) FROM people WHERE owner != '' GROUP BY owner ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
    print(f"  top owners:")
    for owner, n in by_owner:
        print(f"    └ {owner:<20} {n:,}")
    by_status = conn.execute(
        "SELECT status, COUNT(*) FROM people GROUP BY status ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
    print(f"  status distribution:")
    for status, n in by_status:
        print(f"    └ {status:<20} {n:,}")
    conn.close()


if __name__ == "__main__":
    main()
