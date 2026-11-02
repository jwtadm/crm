"""Platform registry. Each platform module exposes:
    fetch_user_info(handle: str) -> dict   # normalized
    fetch_similar_users(internal_id: str, *, secondary_id: str = "") -> list[dict]
    search_users(keyword: str, count: int = 20) -> list[dict]

Normalized dict shape (see lib/db.py upsert_account for full keys):
    platform, platform_user_id, secondary_id, username, full_name, biography,
    follower_count, following_count, post_count, video_count,
    is_verified, is_private, is_business, category,
    profile_pic_url, external_url, bio_links, email, phone,
    city, address, country, raw
"""
from . import instagram, tiktok, x

PLATFORMS = {
    "instagram": instagram,
    "tiktok": tiktok,
    "x": x,
}

# Capabilities — which platforms support which calls
SIMILAR_CAPABLE = {"instagram", "tiktok"}
SEARCH_CAPABLE = {"instagram", "tiktok", "x"}
INFO_CAPABLE = {"instagram", "tiktok", "x"}


def get(name: str):
    if name not in PLATFORMS:
        raise ValueError(f"Unknown platform: {name}")
    return PLATFORMS[name]
