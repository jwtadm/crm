"""Instagram via TikHub.

We use TikHub's `v1/fetch_user_info_by_username_v3` for user info — the v2
endpoint started returning 400 across all usernames in May 2026 (upstream
scraper change). v3 returns the same shape (pk, follower_count, biography,
bio_links, etc.) so the normalize function is unchanged.
"""
from ._tikhub import get, unwrap
from lib.contact import extract_primary_email

PLATFORM = "instagram"


def _normalize_v2(raw: dict) -> dict:
    bio = raw.get("biography", "") or ""
    email = (raw.get("public_email") or "").strip() or extract_primary_email(bio)
    return {
        "platform": PLATFORM,
        "platform_user_id": str(raw.get("pk") or raw.get("pk_id") or raw.get("id") or ""),
        "secondary_id": "",
        "username": raw.get("username", ""),
        "full_name": raw.get("full_name", ""),
        "biography": bio,
        "follower_count": raw.get("follower_count"),
        "following_count": raw.get("following_count"),
        "post_count": raw.get("media_count"),
        "video_count": raw.get("total_igtv_videos") or raw.get("total_video_count"),
        "is_verified": bool(raw.get("is_verified")),
        "is_private": bool(raw.get("is_private")),
        "is_business": bool(raw.get("is_business") or raw.get("is_business_account")),
        "category": raw.get("category") or raw.get("category_name") or "",
        "profile_pic_url": (raw.get("hd_profile_pic_url_info") or {}).get("url")
            or raw.get("profile_pic_url_hd") or raw.get("profile_pic_url", ""),
        "external_url": raw.get("external_url", ""),
        "bio_links": [l.get("url", "") for l in (raw.get("bio_links") or []) if l.get("url")],
        "email": email,
        "phone": (raw.get("contact_phone_number") or raw.get("public_phone_number") or "").strip(),
        "city": raw.get("city_name") or "",
        "address": raw.get("address_street") or "",
        "country": "",
        "raw": raw,
    }


def _normalize_similar(raw: dict) -> dict:
    """Compact item from similar-users feed; bio may be missing."""
    return {
        "platform": PLATFORM,
        "platform_user_id": str(raw.get("pk") or raw.get("pk_id") or raw.get("id") or ""),
        "secondary_id": "",
        "username": raw.get("username", ""),
        "full_name": raw.get("full_name", ""),
        "biography": raw.get("biography", ""),
        "follower_count": None,
        "following_count": None,
        "post_count": None,
        "video_count": None,
        "is_verified": bool(raw.get("is_verified")),
        "is_private": bool(raw.get("is_private")),
        "is_business": False,
        "category": "",
        "profile_pic_url": raw.get("profile_pic_url", ""),
        "external_url": "",
        "bio_links": [],
        "email": "",
        "phone": "",
        "city": "",
        "address": "",
        "country": "",
        "raw": raw,
    }


def fetch_user_info(handle: str) -> dict:
    data = get("/api/v1/instagram/v1/fetch_user_info_by_username_v3", {"username": handle})
    return _normalize_v2(unwrap(data))


def fetch_similar_users(internal_id: str, *, secondary_id: str = "") -> list[dict]:
    data = get("/api/v1/instagram/v2/fetch_similar_users", {"user_id": str(internal_id)})
    inner = unwrap(data)
    items = inner.get("items") if isinstance(inner, dict) else inner
    if not isinstance(items, list):
        return []
    return [_normalize_similar(u) for u in items]


def search_users(keyword: str, count: int = 20) -> list[dict]:
    """IG keyword user search via TikHub. Returns lightweight items (no follower counts)."""
    data = get("/api/v1/instagram/v2/search_users", {"keyword": keyword})
    inner = unwrap(data)
    items = inner.get("items") if isinstance(inner, dict) else inner
    if not isinstance(items, list):
        return []
    return [_normalize_similar(u) for u in items[:count] if isinstance(u, dict)]
