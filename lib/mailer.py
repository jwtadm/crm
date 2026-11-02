"""SMTP-based email sender with template placeholders.

Uses standard library smtplib so it works with any SMTP provider — AWS SES,
Gmail App Password, 阿里云邮件推送, Resend SMTP relay, etc. — by just changing
the SMTP_* settings in /settings.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import settings


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, person: dict, handles: list[dict] | None = None) -> str:
    """Replace `{{name}}`, `{{email}}`, `{{owner}}`, `{{handle_ig}}`, `{{handle_x}}`, etc.

    Unknown placeholders are left as-is so the user notices the typo.
    """
    handles = handles or []
    handle_map: dict[str, str] = {}
    for h in handles:
        plat = h.get("platform")
        user = h.get("username")
        if plat and user:
            handle_map[f"handle_{plat}"] = f"@{user}"

    values = {
        "name": person.get("full_name") or "",
        "email": person.get("email") or "",
        "owner": person.get("owner") or "",
        "status": person.get("status") or "",
        **handle_map,
    }

    def sub(match: re.Match) -> str:
        key = match.group(1)
        return str(values.get(key, match.group(0)))

    return _PLACEHOLDER_RE.sub(sub, template)


def send_one(to_email: str, subject: str, body: str,
             reply_to: str | None = None) -> None:
    """Send one plain-text email via SMTP. Raises on failure.

    Intentionally NO `List-Unsubscribe` / `Precedence: bulk` headers — this app
    sends low-volume (<500/day) personalised outreach where each message has a
    rendered name + handle, and we want it to look like a regular 1:1 email
    rather than a marketing blast. Gmail's bulk-sender requirements only kick
    in above 5k/day; below that, fewer "this is bulk" signals helps inbox
    placement (and is the path the user explicitly asked for).

    `reply_to` overrides the default (SMTP_FROM_EMAIL) so per-staff replies
    land in the sending staff's inbox even though we keep one global From.
    """
    host = settings.require("SMTP_HOST")
    port = int(settings.get("SMTP_PORT") or 587)
    username = settings.require("SMTP_USERNAME")
    password = settings.require("SMTP_PASSWORD")
    from_email = settings.require("SMTP_FROM_EMAIL")
    from_name = settings.get("SMTP_FROM_NAME") or from_email

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = (reply_to or "").strip() or from_email
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            s.login(username, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(username, password)
            s.send_message(msg)
