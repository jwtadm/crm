"""Shared TikHub HTTP helpers.

Includes a thread-safe rate limiter + 429-aware retry so concurrent ingest
(`lib.ingest.run` runs hydrate via ThreadPoolExecutor) doesn't burst past
TikHub's per-account QPS cap. The first real smoke test at $9.21 lost 9 of
10 hydrates to 429 — fixed here.
"""
import random
import threading
import time

import requests

from lib import settings

TIKHUB_BASE = "https://api.tikhub.io"

# Soft cap of N requests per second across all threads. TikHub's documented
# free-tier ceiling is 10 RPS; we run a safety margin so a fan-out of 8
# worker threads never blocks each other on rate-limit retries.
_MIN_SPACING_SEC = 0.25   # ≈ 4 RPS sustained
_lock = threading.Lock()
_last_call_at = 0.0


def _wait_slot() -> None:
    global _last_call_at
    with _lock:
        now = time.monotonic()
        gap = now - _last_call_at
        if gap < _MIN_SPACING_SEC:
            time.sleep(_MIN_SPACING_SEC - gap)
            now = time.monotonic()
        _last_call_at = now


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.require('TIKHUB_API_KEY')}"}


def get(path: str, params: dict, timeout: int = 30,
        *, max_retries: int = 4) -> dict:
    """GET with rate-limit pacing + 429 retry (honors Retry-After when present).

    Retries on 429 / 502 / 503 / 504 with exponential backoff + jitter. Other
    errors raise immediately. Each attempt counts against the global QPS slot.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        _wait_slot()
        try:
            resp = requests.get(f"{TIKHUB_BASE}{path}", params=params,
                                headers=_headers(), timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            # Retry on transient network errors too
            time.sleep(_backoff(attempt))
            continue

        if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
            # Prefer server-supplied Retry-After if it's a small number
            ra = resp.headers.get("Retry-After")
            try:
                ra_sec = float(ra) if ra else None
            except ValueError:
                ra_sec = None
            time.sleep(ra_sec if (ra_sec and ra_sec <= 30) else _backoff(attempt))
            continue

        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise RuntimeError(f"TikHub {path} error: {data.get('message', 'unknown')}")
        return data

    # Out of retries
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"TikHub {path}: gave up after {max_retries} retries")


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter: 0.5s, 1s, 2s, 4s + 0-0.5s jitter."""
    return 0.5 * (2 ** attempt) + random.random() * 0.5


def unwrap(data: dict):
    inner = data.get("data", {})
    if isinstance(inner, dict) and "data" in inner:
        return inner["data"]
    return inner
