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

import threading

_lock = threading.Lock()
_session = None


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
