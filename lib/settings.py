"""Runtime-mutable settings (API keys + provider config) backed by SQLite.

Keys are read on every call so the UI can update them without a restart.
Defaults cover non-secret provider URLs / model IDs, so the user only needs
to fill in API keys to get going.
"""
import sqlite3
import time
from contextlib import contextmanager

from .db import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
"""

DEFAULTS: dict[str, str] = {
    # OpenRouter (chat + embeddings, OpenAI-compatible)
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_BASE": "https://openrouter.ai/api/v1",
    "OPENROUTER_CHAT_MODEL": "deepseek/deepseek-chat-v3.1:free",
    "OPENROUTER_CHAT_FALLBACKS": "qwen/qwen3-235b-a22b:free,meta-llama/llama-3.3-70b-instruct:free",
    "OPENROUTER_EMBED_MODEL": "",

    # TikHub (TikTok / IG / X scraping)
    "TIKHUB_API_KEY": "",

    # Ops alert recipient — bulk jobs email this address when they auto-stop
    # (TikHub credit exhausted / sustained upstream errors). Empty = no alert.
    "BULK_ALERT_EMAIL": "",

    # SMTP (邮件群发出口；推荐 AWS SES SMTP，兼容任何 SMTP 服务)
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM_EMAIL": "",
    "SMTP_FROM_NAME": "",

    # LLM ICP filter — apply on every ingest.run() right before upsert.
    # OFF by default: avoids surprising new installs + keeps test runs
    # deterministic (the rrweb suite assumes stub candidates always ingest).
    "LLM_FILTER_ENABLED": "",  # "1" / "on" turns it on; anything else = off
    "LLM_FILTER_BRIEF": (
        "Target: independent visual creators who post their own original "
        "artwork and could plausibly want their characters or art turned "
        "into custom plushies / merch.\n\n"
        "KEEP (decide 'keep' for these):\n"
        "- Independent illustrators / digital artists (original work, "
        "consistent personal style)\n"
        "- Comic / manga / webtoon artists publishing their own series\n"
        "- Original character (OC) / mascot / sticker / pin designers\n"
        "- Plush sewists, amigurumi makers, fiber / textile artists\n"
        "- Indie game artists with original character work\n"
        "- Tattoo artists with cute / character-forward styles\n"
        "- Fan artists with a strong recurring original style + commission "
        "practice\n"
        "- Small art studios / indie art brands (≤ ~5 people)\n\n"
        "DROP (decide 'drop' for these):\n"
        "- Corporate brands, mass-market retailers, dropshippers, AliExpress-"
        "style storefronts\n"
        "- Generic lifestyle / fashion / fitness / food / travel / beauty / "
        "finance / tech influencers with no art portfolio\n"
        "- News, meme, repost or aggregator accounts\n"
        "- AI-only generators with no consistent human signature\n"
        "- Adult / NSFW-only accounts\n"
        "- Photographers / models / dancers (unless explicit illustration "
        "work is part of their feed)\n"
        "- Pure educational / coaching / business accounts\n\n"
        "When in doubt, default to KEEP — better to over-include and let a "
        "human reject later than miss a real creator."
    ),
}

# Which keys are secrets (masked in GET responses)
SECRET_KEYS = {
    "OPENROUTER_API_KEY",
    "TIKHUB_API_KEY",
    "SMTP_PASSWORD",
}


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get(key: str) -> str:
    """Return stored value, or empty/default. Never raises."""
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"]:
        return row["value"]
    return DEFAULTS.get(key, "")


def get_list(key: str) -> list[str]:
    raw = get(key)
    return [s.strip() for s in raw.split(",") if s.strip()]


def require(key: str) -> str:
    """Like get(), but raise a helpful error if blank."""
    val = get(key)
    if not val:
        raise RuntimeError(
            f"Missing setting: {key}. Open /settings in the browser and fill it in."
        )
    return val


def all_settings(unmasked: bool = False) -> dict[str, str]:
    """Snapshot of every known key. Secrets are masked unless unmasked=True."""
    out: dict[str, str] = {}
    for k in DEFAULTS:
        v = get(k)
        if not unmasked and k in SECRET_KEYS and v:
            out[k] = _mask(v)
        else:
            out[k] = v
    return out


def set_many(updates: dict[str, str]) -> dict[str, str]:
    """Store every recognized key. Empty strings clear a value (falls back to default).

    Special rule for secrets: if the incoming value looks like a mask
    (contains '••' or matches the current mask), we keep the existing value —
    so a user who edits one field won't accidentally wipe the others.
    """
    now = int(time.time())
    written: dict[str, str] = {}
    with _conn() as c:
        for k, v in updates.items():
            if k not in DEFAULTS:
                continue
            v = (v or "").strip()
            if k in SECRET_KEYS and v and ("•" in v or _looks_like_mask(v)):
                continue  # user left the mask alone — don't overwrite
            c.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (k, v, now),
            )
            written[k] = v
    return written


def _mask(v: str) -> str:
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "•" * 6 + v[-4:]


def _looks_like_mask(v: str) -> bool:
    return "•" in v
