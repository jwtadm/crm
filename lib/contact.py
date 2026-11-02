"""Email + contact extraction from free-form text (bios, captions, etc.)."""
import re

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

# Strip stray prose punctuation that the \b boundary may include
TRAIL_PUNCT_RE = re.compile(r"[.,;:!?)\]>\"'\u2019\u201d]+$")

# Common stray prefixes users paste with their email (caught 2026-05-22 when
# someone copied "Email:summerlili591@gmail.com" from somewhere and the
# whole string ended up stored as the canonical email, breaking dedup).
_PREFIX_RE = re.compile(r"^(?:e-?mail|邮箱|mail to|mailto)\s*[:：]\s*", re.IGNORECASE)


def normalize_email(raw: str | None) -> str | None:
    """Lowercase + strip + drop accidental `Email:` / `邮箱:` / `mailto:` prefixes.

    Returns None for empty input. Does NOT validate that the result is a
    well-formed email — caller's job (use `is_valid_email` for that)."""
    if not raw:
        return None
    s = str(raw).strip().replace("\u3000", " ")  # full-width → space
    s = _PREFIX_RE.sub("", s).strip()
    # Strip any `< >` wrappers some clients use ("RFC name <addr@host>")
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    return s.lower() or None


def is_valid_email(s: str | None) -> bool:
    """True iff `s` matches our address regex end-to-end. Use this AFTER
    normalize_email() to surface a user-friendly error before write."""
    if not s:
        return False
    # fullmatch — reject leading/trailing garbage entirely
    return EMAIL_RE.fullmatch(s) is not None


def extract_emails(*texts: str) -> list[str]:
    """Find all unique emails across the given strings, in order of first appearance."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for text in texts:
        if not text:
            continue
        for m in EMAIL_RE.findall(text):
            cleaned = TRAIL_PUNCT_RE.sub("", m).lower()
            if cleaned and cleaned not in seen_set:
                seen.append(cleaned)
                seen_set.add(cleaned)
    return seen


def extract_primary_email(*texts: str) -> str:
    emails = extract_emails(*texts)
    return emails[0] if emails else ""
