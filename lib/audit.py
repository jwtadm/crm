"""Minimal audit log. Records security-sensitive actions for after-the-fact review."""
import json
import time

from . import db


def log(actor_id: int | None, action: str, target: str | None = None, detail: dict | None = None) -> None:
    """Best-effort: never raise out of an audit call."""
    try:
        with db.tx() as c:
            c.execute(
                "INSERT INTO audit_log (actor_id, action, target, detail, created_at) VALUES (?,?,?,?,?)",
                (
                    actor_id,
                    action,
                    target,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    int(time.time()),
                ),
            )
    except Exception:
        pass


def recent(limit: int = 200) -> list[dict]:
    with db.tx() as c:
        rows = c.execute(
            """SELECT a.id, a.actor_id, a.action, a.target, a.detail, a.created_at,
                      u.username AS actor_username
                 FROM audit_log a
            LEFT JOIN users u ON u.id = a.actor_id
                ORDER BY a.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except Exception:
                pass
        out.append(d)
    return out
