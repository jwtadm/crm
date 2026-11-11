"""Signed-link SSO for external systems (OA / ERP / WeCom workbench).

The caller holds an app_id + secret (server side only) and builds a URL:

    /sso/login?app_id=..&username=..&ts=..&nonce=..&sign=..

    sign = hex( HMAC-SHA256(username|ts|nonce, secret) )   # lowercase

The secret is never stored: it is derived from the app's random salt and the
Flask secret key, so a database leak does not expose usable secrets, and
"reset secret" simply rotates the salt.

Simple-key mode (?key=<secret>) exists for trusted internal callers; it has no
timestamp/nonce and therefore no replay protection.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets

APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def new_salt() -> str:
    return secrets.token_hex(16)


def _master_bytes(master) -> bytes:
    return master.encode("utf-8") if isinstance(master, str) else bytes(master)


def app_secret(master, app_id: str, salt: str) -> str:
    """Deterministic per-app secret. Rotating `salt` invalidates the old one."""
    return hmac.new(
        _master_bytes(master),
        f"{app_id}:{salt}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign(username: str, ts: str, nonce: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{username}|{ts}|{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_sign(username: str, ts: str, nonce: str, sign_hex: str, secret: str) -> bool:
    return hmac.compare_digest(sign(username, ts, nonce, secret), sign_hex)


def valid_app_id(value: str) -> bool:
    return bool(APP_ID_RE.match(value or ""))


def valid_nonce(value: str) -> bool:
    return bool(NONCE_RE.match(value or ""))


def valid_sign_format(value: str) -> bool:
    return bool(HEX64_RE.match(value or ""))


def ip_allowed(app: dict, ip: str) -> bool:
    raw = (app.get("allow_ips") or "").strip()
    if not raw:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").split(",")[0].strip())
    except ValueError:
        return False
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                if addr in ipaddress.ip_network(part, strict=False):
                    return True
            elif addr == ipaddress.ip_address(part):
                return True
        except ValueError:
            continue
    return False


def allowed_users(app: dict) -> list[str]:
    try:
        return [str(u).strip().lower()
                for u in json.loads(app.get("allow_users") or "[]") if str(u).strip()]
    except (TypeError, ValueError):
        return []


def user_allowed(app: dict, username: str, user: dict) -> bool:
    """Empty list = every account, including admins (deployment decision:
    the app owner explicitly grants that when they leave the field blank)."""
    allowed = allowed_users(app)
    if not allowed:
        return True
    return username.strip().lower() in allowed


def check_sign_params(ts: str, nonce: str, sign_hex: str) -> str | None:
    """Return an error message, or None when the parameters look well formed."""
    if not ts.isdigit() or len(ts) != 10:
        return "ts 必须是 10 位 Unix 时间戳"
    if not valid_nonce(nonce):
        return "nonce 必须是 8-64 位字母数字"
    if not valid_sign_format(sign_hex):
        return "sign 必须是 64 位小写十六进制"
    return None
