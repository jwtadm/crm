"""TikTok via TikHub. Email is regex-extracted from `signature` (bio)."""
import time

from ._tikhub import get, unwrap
from lib.contact import extract_primary_email

PLATFORM = "tiktok"


def _pick_avatar(raw: dict) -> str:
    for key in ("avatar_larger", "avatar_medium", "avatar_thumb"):
        v = raw.get(key)
        if isinstance(v, dict):
            urls = v.get("url_list") or []
            if urls:
                return urls[0]
        elif isinstance(v, str) and v:
            return v
    return raw.get("avatar_url", "")


def _normalize(raw: dict) -> dict:
    bio = raw.get("signature", "") or ""
    bio_url = raw.get("bio_url") or raw.get("bio_secure_url") or ""
    email = extract_primary_email(bio, bio_url)
    verified = bool(raw.get("custom_verify") or raw.get("enterprise_verify_reason"))
    return {
        "platform": PLATFORM,
        "platform_user_id": str(raw.get("uid") or raw.get("user_id") or raw.get("id") or ""),
        "secondary_id": raw.get("sec_uid", ""),
        "username": raw.get("unique_id") or raw.get("uniqueId") or "",
        "full_name": raw.get("nickname", ""),
        "biography": bio,
        "follower_count": raw.get("follower_count") or raw.get("followerCount"),
        "following_count": raw.get("following_count") or raw.get("followingCount"),
        "post_count": raw.get("aweme_count") or raw.get("videoCount"),
        "video_count": raw.get("aweme_count") or raw.get("videoCount"),
        "is_verified": verified,
        "is_private": bool(raw.get("secret") or raw.get("privateAccount")),
        "is_business": bool(raw.get("commerce_user_info") or raw.get("commerceUserInfo")),
        "category": raw.get("category", ""),
        "profile_pic_url": _pick_avatar(raw),
        "external_url": bio_url,
        "bio_links": [bio_url] if bio_url else [],
        "email": email,
        "phone": "",
        "city": "",
        "address": "",
        "country": raw.get("region", ""),
        "raw": raw,
    }


def fetch_user_info(handle: str) -> dict:
    data = get(
        "/api/v1/tiktok/app/v3/handler_user_profile",
        {"unique_id": handle.lstrip("@")},
    )
    inner = unwrap(data)
    user = inner.get("user") if isinstance(inner, dict) else inner
    if not isinstance(user, dict):
        raise RuntimeError(f"TikTok info: unexpected payload for @{handle}")
    return _normalize(user)


def fetch_similar_users(internal_id: str, *, secondary_id: str = "") -> list[dict]:
    """TikTok similar API requires sec_uid (long string), not numeric uid.
    Retries up to 3 times — endpoint is upstream-flaky.
    """
    sec = secondary_id or internal_id
    if not sec:
        return []
    last_err = None
    for attempt in range(3):
        try:
            data = get(
                "/api/v1/tiktok/app/v3/fetch_similar_user_recommendations",
                {"sec_uid": sec},
            )
            inner = unwrap(data)
            users = []
            if isinstance(inner, dict):
                # Try common containers
                for k in ("user_list", "users", "items"):
                    if k in inner and isinstance(inner[k], list):
                        users = inner[k]
                        break
            elif isinstance(inner, list):
                users = inner
            out = []
            for u in users:
                if isinstance(u, dict) and "user_info" in u and isinstance(u["user_info"], dict):
                    u = u["user_info"]
                if isinstance(u, dict):
                    out.append(_normalize(u))
            return out
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"TikTok similar failed after retries: {last_err}")


def search_users(keyword: str, count: int = 20) -> list[dict]:
    data = get(
        "/api/v1/tiktok/app/v3/fetch_user_search_result",
        {"keyword": keyword, "offset": 0, "count": min(count, 20)},
    )
    inner = unwrap(data)
    users = []
    if isinstance(inner, dict):
        for k in ("user_list", "users"):
            if k in inner and isinstance(inner[k], list):
                users = inner[k]
                break
    elif isinstance(inner, list):
        users = inner
    out = []
    for u in users[:count]:
        if isinstance(u, dict) and "user_info" in u and isinstance(u["user_info"], dict):
            u = u["user_info"]
        if isinstance(u, dict):
            out.append(_normalize(u))
    return out
