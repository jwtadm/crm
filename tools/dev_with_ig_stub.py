"""Run the Flask app with ALL platform / AI / mailer side-effects stubbed.

This is the harness the rrweb regression suite (tests/rrweb/) drives — every
test must be replayable on an offline laptop with zero TikHub / OpenAI / SES
credentials. Each stub returns deterministic, hand-shaped data so assertions
in the steps files can be exact.

Stubbed:
  - platforms.instagram.fetch_user_info       (hydrate path; bio email seeded)
  - platforms.instagram.fetch_similar_users   (similarity discovery)
  - platforms.instagram.search_users          (query / AI search)
  - platforms.tiktok.{fetch_user_info, fetch_similar_users, search_users}
  - platforms.x.{fetch_user_info, search_users}  (x has no similar API upstream)
  - lib.ai.generate_search_queries            (deterministic queries; no LLM)
  - lib.ai.polish_email / generate_embedding  (echo / zero vector)
  - lib.mailer.send_one                       (noop; never hits SMTP)

We also pre-seed SMTP_* settings so the send_batch route's env check passes.

Usage: .venv/bin/python tools/dev_with_ig_stub.py
"""
import os
import sys
from pathlib import Path

# Repo root derived from this file's location, so the script runs from any
# checkout (Windows dev box, /opt/ig_scraper on the server) unedited.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import ai, db, mailer, settings
from platforms import instagram as ig, tiktok as tk, x as twi

db.init_db()
settings.init()  # ensure the settings table exists on a fresh test DB


# ---------------------------------------------------------------------------
# 1. Settings: pretend SMTP + AI keys are configured so the routes don't 400
# ---------------------------------------------------------------------------
settings.set_many({
    "SMTP_HOST": "stub.smtp.local",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": os.environ.get("SCOUT_STUB_SMTP_USER", "stub-user"),
    "SMTP_PASSWORD": os.environ.get("SCOUT_STUB_SMTP_PASS", "stub-pass"),
    "SMTP_FROM_EMAIL": "noreply@stub.local",
    "SMTP_FROM_NAME": "Scout (stub)",
    "OPENROUTER_API_KEY": os.environ.get("SCOUT_STUB_OR_KEY", "stub-or-key"),
})


# ---------------------------------------------------------------------------
# 2. Platform stubs
# ---------------------------------------------------------------------------
def _stub_ig_fetch_user_info(handle: str) -> dict:
    # Per-handle email so two parallel hydrates don't collide; for rrwebfx_*
    # fixtures we keep the rrwebfx_ prefix so wipe_fixtures catches them.
    is_fixture = handle.startswith("rrwebfx_")
    email = (f"rrwebfx_{handle}@demoshop.io" if is_fixture
             else f"demo_scraped_{handle}@demoshop.io")
    name = (f"RRWebFx Hydrated {handle}" if is_fixture
            else f"Demo Indie Maker ({handle})")
    # Vary bio per handle so the LLM-filter test can predict drops.
    # The similar stub returns the same 3 usernames (rrwebfx_sim_ig_{1,2,3});
    # _2 carries the fitness bio that the keyword judge always drops.
    h = handle.lower()
    if h.endswith("_sim_ig_2"):
        bio = "daily fitness coach, weight loss tips for moms"
    elif h.endswith("_sim_ig_1"):
        bio = "indie illustrator with original characters"
    elif h.endswith("_sim_ig_3"):
        bio = "handmade plushie maker, custom commissions"
    else:
        bio = "indie illustrator · plushies · biz"
    return {
        "platform": "instagram",
        "platform_user_id": f"demo-stub-{handle}",
        "secondary_id": f"demo-sec-{handle}",
        "username": handle,
        "full_name": name,
        "biography": bio,
        "follower_count": 12345,
        "following_count": 678,
        "post_count": 421,
        "video_count": 0,
        "is_verified": False, "is_private": False, "is_business": True,
        "category": "Visual Artist",
        "profile_pic_url": "",
        "external_url": "https://demoshop.io",
        "bio_links": [],
        "email": email,
        "phone": "", "city": "", "address": "", "country": "",
        "raw": {"stubbed": True},
    }


def _stub_ig_similar_users(internal_id, *, secondary_id=""):
    """Pretend the seed has 3 visually-similar IG creators with mixed
    bios — two ICP-aligned (illustrator + plushie maker), one obvious
    non-fit (fitness coach). The LLM-filter rrweb test relies on this
    split to assert that drops actually happen; existing tests only
    look at usernames so the change is backward-compatible."""
    profiles = [
        ("indie illustrator with original characters", "Stub IG Illustrator"),
        ("daily fitness coach, weight loss tips for moms", "Stub IG Fitness"),
        ("handmade plushie maker, custom commissions", "Stub IG Plushie"),
    ]
    return [
        {
            "platform": "instagram",
            "platform_user_id": f"sim-{internal_id}-{i}",
            "username": f"rrwebfx_sim_ig_{i}",
            "full_name": full_name,
            "biography": bio,
            "follower_count": 9000 + i * 1000,
            "email": None,
            "raw": {"stub_similar": True},
        }
        for i, (bio, full_name) in enumerate(profiles, start=1)
    ]


def _stub_ig_search_users(keyword: str, count: int = 20):
    """Pretend a keyword search returns 3 IG creators."""
    safe = keyword.replace(' ', '_').replace('/', '_').lower()[:24]
    return [
        {
            "platform": "instagram",
            "platform_user_id": f"q-ig-{safe}-{i}",
            "username": f"rrwebfx_qig_{safe}_{i}",
            "full_name": f"Stub IG match {i}",
            "biography": f"matches {keyword}",
            "follower_count": 5000 + i * 500,
            "email": None,
            "raw": {"stub_search": True},
        }
        for i in range(1, 4)
    ]


def _stub_tk_fetch_user_info(handle):
    return {
        "platform": "tiktok",
        "platform_user_id": f"tk-stub-{handle}",
        "secondary_id": f"tk-sec-{handle}",
        "username": handle, "full_name": f"Stub TikTok {handle}",
        "biography": "stubbed tiktok bio",
        "follower_count": 22222, "following_count": 100,
        "post_count": 0, "video_count": 50,
        "is_verified": False, "is_private": False, "is_business": False,
        "category": "", "profile_pic_url": "", "external_url": "",
        "bio_links": [], "email": None, "phone": "",
        "city": "", "address": "", "country": "", "raw": {"stub": True},
    }


def _stub_tk_similar_users(internal_id, *, secondary_id=""):
    return [
        {
            "platform": "tiktok",
            "platform_user_id": f"tk-sim-{internal_id}-{i}",
            "username": f"rrwebfx_sim_tk_{i}",
            "full_name": f"Stub Similar TT {i}",
            "follower_count": 8000 + i * 500,
            "raw": {"stub_similar": True},
        }
        for i in range(1, 3)
    ]


def _stub_tk_search_users(keyword, count=20):
    safe = keyword.replace(' ', '_').lower()[:24]
    return [
        {
            "platform": "tiktok",
            "platform_user_id": f"q-tk-{safe}-{i}",
            "username": f"rrwebfx_qtk_{safe}_{i}",
            "full_name": f"Stub TT match {i}",
            "follower_count": 4000 + i * 200,
            "raw": {"stub_search": True},
        }
        for i in range(1, 3)
    ]


def _stub_x_fetch_user_info(handle):
    return {
        "platform": "x",
        "platform_user_id": f"x-stub-{handle}",
        "username": handle, "full_name": f"Stub X {handle}",
        "biography": "stubbed x bio",
        "follower_count": 3333, "following_count": 200,
        "raw": {"stub": True},
    }


def _stub_x_search_users(keyword, count=20):
    safe = keyword.replace(' ', '_').lower()[:24]
    return [
        {
            "platform": "x",
            "platform_user_id": f"q-x-{safe}-{i}",
            "username": f"rrwebfx_qx_{safe}_{i}",
            "full_name": f"Stub X match {i}",
            "follower_count": 1000 + i * 100,
            "raw": {"stub_search": True},
        }
        for i in range(1, 3)
    ]


ig.fetch_user_info = _stub_ig_fetch_user_info
ig.fetch_similar_users = _stub_ig_similar_users
ig.search_users = _stub_ig_search_users
tk.fetch_user_info = _stub_tk_fetch_user_info
tk.fetch_similar_users = _stub_tk_similar_users
tk.search_users = _stub_tk_search_users
twi.fetch_user_info = _stub_x_fetch_user_info
twi.search_users = _stub_x_search_users


# ---------------------------------------------------------------------------
# 3. AI stubs (no LLM calls)
# ---------------------------------------------------------------------------
def _stub_generate_search_queries(profile_description, n=10, platforms=None):
    base = [
        "plushie maker", "indie illustrator", "art-toy designer",
        "kawaii sticker shop", "etsy seller", "vinyl figure artist",
        "softie crafter", "zine illustrator",
    ]
    return base[:max(1, n)]


def _stub_polish_email(subject, body, *args, **kwargs):
    return {"subject": subject, "body": body, "diff": ""}


def _stub_embed(text):
    return [0.0] * 32


# Deterministic ICP gatekeeper — mirrors the real ai.classify_creators
# contract ({username: keep_bool}) but decides by keyword match on the
# bio + full_name. Lets the rrweb llm_filter test predict verdicts
# without standing up OpenRouter in the test environment.
_ICP_KEEP_WORDS = {"illustrator", "artist", "comic", "webtoon", "plush",
                   "sticker", "manga", "indie", "character", "maker"}
_ICP_DROP_WORDS = {"news", "fitness", "coach", "fashion", "luxury",
                   "gym", "finance", "stocks", "meme", "aggregator"}


def _stub_classify_creators(briefs, icp_brief):
    out: dict = {}
    for b in briefs:
        text = ((b.get("bio") or "") + " " + (b.get("full_name") or "")).lower()
        drop = any(k in text for k in _ICP_DROP_WORDS)
        keep = any(k in text for k in _ICP_KEEP_WORDS)
        # mirror real default-keep policy: only drop when DROP wins outright
        out[b.get("username", "")] = not (drop and not keep)
    return out


ai.generate_search_queries = _stub_generate_search_queries
ai.classify_creators = _stub_classify_creators
for name in ("polish_email", "polish"):
    if hasattr(ai, name):
        setattr(ai, name, _stub_polish_email)
for name in ("generate_embedding", "embed", "embed_text"):
    if hasattr(ai, name):
        setattr(ai, name, _stub_embed)


# ---------------------------------------------------------------------------
# 4. Mailer stub — record in memory instead of hitting SMTP.
# ---------------------------------------------------------------------------
_sent_messages: list[dict] = []


def _stub_send_one(to_email, subject, body, reply_to=None):
    _sent_messages.append({"to": to_email, "subject": subject, "body": body,
                           "reply_to": reply_to})
    print(f"[stub-mail] -> {to_email!r} subject={subject!r} reply_to={reply_to!r}", flush=True)


mailer.send_one = _stub_send_one


# ---------------------------------------------------------------------------
# 5. Boot the app + introspection routes.
# ---------------------------------------------------------------------------
from app import app  # noqa: E402


@app.route("/__stub__/sent")
def _stub_sent_log():
    from flask import jsonify
    return jsonify({"count": len(_sent_messages), "messages": _sent_messages[-50:]})


@app.route("/__stub__/reset", methods=["POST"])
def _stub_reset():
    _sent_messages.clear()
    return ("", 204)


if __name__ == "__main__":
    print("[stub] all platform / AI / mailer side-effects stubbed; "
          "introspection at /__stub__/sent", flush=True)
    app.run(host="0.0.0.0", port=5001, debug=False)
