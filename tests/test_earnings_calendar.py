from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from earnings_scanner import earnings_calendar as cal


def _fake_response(rows):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": {"rows": rows}}
    return resp


def test_fetch_earnings_for_date_parses_rows():
    rows = [
        {"symbol": "abc", "name": "ABC Corp", "time": "time-after-hours", "marketCap": "$1,234,567,890"},
        {"symbol": "xyz", "name": "XYZ Inc", "time": "time-pre-market", "marketCap": "N/A"},
        {"symbol": "", "name": "Should be skipped", "time": "time-after-hours", "marketCap": "$1"},
    ]
    cal._cache.clear()
    with patch.object(cal.requests, "get", return_value=_fake_response(rows)) as mock_get:
        events = cal.fetch_earnings_for_date(date(2026, 1, 15), use_cache=False)

    assert mock_get.called
    assert len(events) == 2
    assert events[0].ticker == "ABC"
    assert events[0].session == "after"
    assert events[0].market_cap == 1234567890.0
    assert events[1].ticker == "XYZ"
    assert events[1].session == "before"
    assert events[1].market_cap is None


def test_build_watchlist_combines_today_amc_and_tomorrow_bmo():
    today_rows = [
        {"symbol": "AMC1", "name": "A", "time": "time-after-hours", "marketCap": "$2,000,000,000"},
        {"symbol": "IGNORE_BMO_TODAY", "name": "B", "time": "time-pre-market", "marketCap": "$1,000,000,000"},
    ]
    tomorrow_rows = [
        {"symbol": "BMO1", "name": "C", "time": "time-pre-market", "marketCap": "$3,000,000,000"},
        {"symbol": "IGNORE_AMC_TOMORROW", "name": "D", "time": "time-after-hours", "marketCap": "$500,000,000"},
    ]

    cal._cache.clear()

    def fake_get(url, params=None, headers=None, timeout=None):
        if params["date"] == "2026-01-15":
            return _fake_response(today_rows)
        return _fake_response(tomorrow_rows)

    with patch.object(cal.requests, "get", side_effect=fake_get):
        watchlist = cal.build_watchlist(target_date=date(2026, 1, 15))

    tickers = {e.ticker for e in watchlist}
    assert tickers == {"AMC1", "BMO1"}
    # sorted by market cap desc
    assert watchlist[0].ticker == "BMO1"


def test_build_watchlist_applies_min_market_cap_filter():
    today_rows = [
        {"symbol": "BIG", "name": "Big Co", "time": "time-after-hours", "marketCap": "$10,000,000,000"},
        {"symbol": "SMALL", "name": "Small Co", "time": "time-after-hours", "marketCap": "$1,000,000"},
    ]
    cal._cache.clear()

    def fake_get(url, params=None, headers=None, timeout=None):
        return _fake_response(today_rows if params["date"] == "2026-01-15" else [])

    with patch.object(cal.requests, "get", side_effect=fake_get):
        watchlist = cal.build_watchlist(target_date=date(2026, 1, 15), min_market_cap=500_000_000)

    tickers = {e.ticker for e in watchlist}
    assert tickers == {"BIG"}
