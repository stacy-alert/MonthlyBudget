"""
Builds the daily watchlist: tickers reporting earnings after today's close
(AMC) or before tomorrow's open (BMO) - i.e. the events you'd actually be
entering a position for today, per the strategy (positions are opened ~15
minutes before the close on the day before the announcement).

Uses Nasdaq's public earnings-calendar endpoint. It's undocumented and can
change shape without notice; every field access here is defensive.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/market-activity/earnings",
}

_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class EarningsEvent:
    ticker: str
    name: Optional[str]
    session: str  # "before" | "after" | "unknown"
    market_cap: Optional[float]


def _parse_market_cap(raw) -> Optional[float]:
    if not raw:
        return None
    cleaned = str(raw).replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned == "N/A":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _session_from_time_field(raw: Optional[str]) -> str:
    if raw == "time-after-hours":
        return "after"
    if raw == "time-pre-market":
        return "before"
    return "unknown"


def fetch_earnings_for_date(d: date, use_cache: bool = True) -> list[EarningsEvent]:
    key = d.isoformat()
    if use_cache and key in _cache:
        ts, rows = _cache[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            return rows

    resp = requests.get(NASDAQ_EARNINGS_URL, params={"date": key}, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    rows_raw = ((payload or {}).get("data") or {}).get("rows") or []
    events = []
    for row in rows_raw:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        events.append(
            EarningsEvent(
                ticker=symbol,
                name=row.get("name"),
                session=_session_from_time_field(row.get("time")),
                market_cap=_parse_market_cap(row.get("marketCap")),
            )
        )

    _cache[key] = (time.time(), events)
    return events


def _next_calendar_day(d: date) -> date:
    return d + timedelta(days=1)


def build_watchlist(target_date: Optional[date] = None, min_market_cap: Optional[float] = None) -> list[EarningsEvent]:
    """Tickers to evaluate today: today's AMC reporters + tomorrow's BMO reporters.

    Both groups are events where a position would be opened today (15 min
    before the close, per the strategy). `min_market_cap` pre-filters out
    illiquid names before the (much more expensive) per-ticker options scan.
    """
    d = target_date or date.today()
    next_d = _next_calendar_day(d)

    today_events = [e for e in fetch_earnings_for_date(d) if e.session == "after"]
    tomorrow_events = [e for e in fetch_earnings_for_date(next_d) if e.session == "before"]

    combined: dict[str, EarningsEvent] = {}
    for e in today_events + tomorrow_events:
        combined[e.ticker] = e

    events = list(combined.values())

    if min_market_cap is not None:
        events = [e for e in events if e.market_cap is None or e.market_cap >= min_market_cap]

    events.sort(key=lambda e: (e.market_cap or 0), reverse=True)
    return events
