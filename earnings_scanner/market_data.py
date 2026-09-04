"""
Shared HTTP session used for every yfinance call.

Yahoo Finance blocks the plain `requests` TLS fingerprint fairly aggressively,
which is the #1 cause of "Error: Unable to retrieve underlying stock price"
style failures in the original calculator. yfinance's own maintainers now
recommend routing through curl_cffi with a browser TLS impersonation profile
instead. We fall back to a plain requests session (with a real User-Agent) if
curl_cffi isn't installed, so the app still runs, just less reliably.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_session = None

T = TypeVar("T")

# Yahoo rate-limits cloud-hosting IP ranges (Render, Heroku, AWS, etc.) far
# more aggressively than residential IPs, so transient 429/"too many
# requests" style failures are expected in production, not a sign anything
# is broken. Retry a handful of times with jittered backoff before giving up.
_MAX_ATTEMPTS = 4
_BASE_DELAY_SECONDS = 1.5


def with_retry(fn: Callable[[], T], description: str = "request") -> T:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - yfinance raises assorted types
            last_error = e
            if attempt == _MAX_ATTEMPTS:
                break
            delay = _BASE_DELAY_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                description, attempt, _MAX_ATTEMPTS, e, delay,
            )
            time.sleep(delay)
    raise last_error  # type: ignore[misc]


def get_session():
    """Return a process-wide shared session for yfinance calls."""
    global _session
    with _lock:
        if _session is not None:
            return _session

        try:
            from curl_cffi import requests as curl_requests

            _session = curl_requests.Session(impersonate="chrome")
        except ImportError:
            import requests

            _session = requests.Session()
            _session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                }
            )

        return _session
