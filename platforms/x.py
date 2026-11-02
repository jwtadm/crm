"""X (Twitter) via TikHub. No similar-users API — info + search only.

Search returns tweets with embedded user_info; we dedupe by rest_id.
TikHub's Twitter search endpoint is upstream-flaky (sporadic 400s) — we retry.
"""
import time

from ._tikhub import get, unwrap
from lib.contact import extract_primary_email

PLATFORM = "x"


def _normalize(raw: dict) -> dict:
    bio = raw.get("desc") or raw.get("description", "") or ""
    email = extract_primary_email(bio)
    return {
        "platform": PLATFORM,
        "platform_user_id": str(raw.get("rest_id") or raw.get("id") or ""),
        "secondary_id": "",
        "username": raw.get("profile") or raw.get("screen_name", ""),
        "full_name": raw.get("name", ""),
        "biography": bio,
        "follower_count": raw.get("sub_count") or raw.get("followers_count"),
        "following_count": raw.get("friends") or raw.get("following_count"),
        "post_count": raw.get("statuses_count"),
        "video_count": raw.get("media_count"),
        "is_verified": bool(raw.get("blue_verified") or raw.get("verified")),
        "is_private": bool(raw.get("protected")),
        "is_business": bool(raw.get("business_account")),
        "category": ((raw.get("affiliates") or {}).get("label") or {}).get("description", ""),
        "profile_pic_url": raw.get("avatar", ""),
        "external_url": "",
        "bio_links": [],
        "email": email,
        "phone": "",
        "city": "",
        "address": raw.get("location", ""),
        "country": "",
        "raw": raw,
    }


def fetch_user_info(handle: str) -> dict:
    data = get(
        "/api/v1/twitter/web/fetch_user_profile",
        {"screen_name": handle.lstrip("@")},
    )
    return _normalize(unwrap(data) or {})


def fetch_similar_users(internal_id: str, *, secondary_id: str = "") -> list[dict]:
    # Not supported on X via TikHub.
    return []


def search_users(keyword: str, count: int = 20) -> list[dict]:
    """Search People timeline; dedupe embedded user_info by rest_id."""
    last_err = None
    data = None
    for attempt in range(3):
        try:
            data = get(
                "/api/v1/twitter/web/fetch_search_timeline",
                {"keyword": keyword, "search_type": "People"},
            )
            break
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"X search failed after retries: {last_err}")
    inner = unwrap(data) or {}
    timeline = inner.get("timeline") if isinstance(inner, dict) else None
    if not isinstance(timeline, list):
        return []
    seen_ids: set[str] = set()
    out: list[dict] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        u = item.get("user_info") or item.get("user")
        if not isinstance(u, dict):
            continue
        rid = str(u.get("rest_id") or u.get("id") or "")
        if not rid or rid in seen_ids:
            continue
        seen_ids.add(rid)
        out.append(_normalize(u))
        if len(out) >= count:
            break
    return out
