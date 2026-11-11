"""Runtime-mutable settings (API keys + provider config) backed by SQLite.

Reads go through a short-lived process-local cache so the UI can still update
values without a restart while hot paths (a 100-recipient email batch used to
open 600 connections just to read 6 keys) stop hammering the database.

Cache contract:
  * one query loads every key, refreshed at most every _CACHE_TTL seconds;
  * set_many() invalidates immediately in this process;
  * refresh() forces a re-read, for callers that must see the just-saved value
    (e.g. the /api/settings/test endpoint, whose request may land on a
    different gunicorn worker than the save).
So a save is visible everywhere within _CACHE_TTL seconds, and immediately to
anyone who calls refresh() first.
"""
import threading
import time

from . import db


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


# How long a loaded snapshot is reused. Short on purpose: saves must propagate
# across gunicorn workers (each has its own process cache) without a restart.
_CACHE_TTL = 2.0

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "values": {}}


def init() -> None:
    with db.tx() as c:
        # Statement literal kept at the call site (this repo does not allow
        # passing SQL text through a variable). Not executescript(): it would
        # implicitly COMMIT, breaking db.tx()'s explicit BEGIN.
        c.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "  key        TEXT PRIMARY KEY,"
            "  value      TEXT NOT NULL DEFAULT '',"
            "  updated_at INTEGER NOT NULL"
            ")"
        )
    refresh()


def refresh() -> None:
    """Drop the cached snapshot so the next read hits the database."""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["values"] = {}


def _load() -> dict[str, str]:
    """All stored keys, from cache when fresh. One query otherwise."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["values"]
        if cached and (now - _cache["at"]) < _CACHE_TTL:
            return cached
    with db.ro() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    values = {r["key"]: r["value"] for r in rows}
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["values"] = values
    return values


def get(key: str) -> str:
    """Return stored value, or empty/default. Never raises."""
    v = _load().get(key)
    if v:
        return v
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
    stored = _load()          # one query for all 15 keys (used to be 15)
    out: dict[str, str] = {}
    for k in DEFAULTS:
        v = stored.get(k) or DEFAULTS.get(k, "")
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
    with db.tx() as c:
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
    if written:
        refresh()   # this process sees the new values on its next read
    return written


def _mask(v: str) -> str:
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "•" * 6 + v[-4:]


def _looks_like_mask(v: str) -> bool:
    return "•" in v
