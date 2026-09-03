"""
Unit tests for earnings_scanner.model using synthetic data.

Note: this sandbox's network egress policy blocks Yahoo Finance / Nasdaq
domains outright, so these tests mock yfinance entirely rather than hitting
live data. Exercise the app against real tickers once it's running somewhere
with normal internet access.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from earnings_scanner import model


def make_price_history(n_days: int = 65, start_price: float = 100.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=datetime.today().date() - timedelta(days=1), periods=n_days)

    closes = [start_price]
    for _ in range(n_days - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.015)))
    closes = np.array(closes)

    opens = closes * (1 + rng.normal(0, 0.003, n_days))
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.004, n_days)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.004, n_days)))
    volume = rng.integers(1_000_000, 3_000_000, n_days).astype(float)

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volume},
        index=dates,
    )


def make_chain(strikes, atm_strike, iv):
    calls = pd.DataFrame(
        {
            "strike": strikes,
            "impliedVolatility": [iv] * len(strikes),
            "bid": [1.5 if s == atm_strike else 0.5 for s in strikes],
            "ask": [1.7 if s == atm_strike else 0.6 for s in strikes],
        }
    )
    puts = calls.copy()
    return SimpleNamespace(calls=calls, puts=puts)


class FakeTicker:
    def __init__(self, expirations, underlying_price, atm_strike, ivs, price_history):
        self._expirations = expirations
        self._underlying_price = underlying_price
        self._atm_strike = atm_strike
        self._ivs = ivs
        self._price_history = price_history

    @property
    def options(self):
        return self._expirations

    def option_chain(self, exp_date):
        strikes = [self._atm_strike - 5, self._atm_strike, self._atm_strike + 5]
        return make_chain(strikes, self._atm_strike, self._ivs[exp_date])

    def history(self, period=None):
        if period == "1d":
            return pd.DataFrame({"Close": [self._underlying_price]})
        return self._price_history


def _expirations_45_plus():
    today = date.today()
    exps = [today + timedelta(days=d) for d in (7, 14, 30, 45, 60)]
    return [d.strftime("%Y-%m-%d") for d in exps]


def test_filter_dates_keeps_through_first_45_day_expiry():
    today = date.today()
    dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in (7, 14, 30, 45, 60)]
    filtered = model.filter_dates(dates)
    assert filtered[-1] == dates[3]  # the 45-day one
    assert len(filtered) == 4


def test_filter_dates_raises_when_nothing_45_days_out():
    today = date.today()
    dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in (7, 14, 20)]
    with pytest.raises(ValueError):
        model.filter_dates(dates)


def test_build_term_structure_interpolates_and_extrapolates():
    spline = model.build_term_structure([10, 30, 60], [0.5, 0.3, 0.25])
    assert spline(30) == pytest.approx(0.3)
    assert spline(5) == pytest.approx(0.5)   # below range -> clamp to first
    assert spline(90) == pytest.approx(0.25)  # above range -> clamp to last
    mid = spline(20)
    assert 0.3 < mid < 0.5  # linear interpolation between 10d and 30d points


def test_yang_zhang_returns_finite_positive_annualized_vol():
    history = make_price_history()
    vol = model.yang_zhang(history)
    assert np.isfinite(vol)
    assert vol > 0


def test_compute_recommendation_backwardation_flags_recommended():
    expirations = _expirations_45_plus()
    history = make_price_history()
    # Steep backwardation (short-dated IV >> longer-dated) + rich vs realized vol
    ivs = {expirations[0]: 0.90, expirations[1]: 0.70, expirations[2]: 0.55, expirations[3]: 0.45, expirations[4]: 0.40}
    ticker = FakeTicker(expirations, underlying_price=100.0, atm_strike=100, ivs=ivs, price_history=history)

    with patch.object(model.yf, "Ticker", return_value=ticker):
        rec = model.compute_recommendation("TEST")

    assert rec.ticker == "TEST"
    assert rec.ts_slope_pass is True
    assert rec.expected_move_pct is not None
    assert rec.verdict in ("Recommended", "Consider")


def test_compute_recommendation_flat_term_structure_is_avoid():
    expirations = _expirations_45_plus()
    history = make_price_history()
    flat_iv = 0.30
    ivs = {e: flat_iv for e in expirations}
    ticker = FakeTicker(expirations, underlying_price=100.0, atm_strike=100, ivs=ivs, price_history=history)

    with patch.object(model.yf, "Ticker", return_value=ticker):
        rec = model.compute_recommendation("FLAT")

    assert rec.ts_slope_pass is False
    assert rec.verdict == "Avoid"


def test_compute_recommendation_no_options_raises():
    ticker = FakeTicker([], underlying_price=100.0, atm_strike=100, ivs={}, price_history=make_price_history())
    with patch.object(model.yf, "Ticker", return_value=ticker):
        with pytest.raises(model.TickerDataError):
            model.compute_recommendation("NOPE")


def test_compute_recommendation_blank_symbol_raises():
    with pytest.raises(model.TickerDataError):
        model.compute_recommendation("   ")


def test_get_current_price_uses_iloc_not_positional_label():
    """Regression test for the original script's `todays_data['Close'][0]` bug:
    that raises KeyError on a normal DatetimeIndex under current pandas."""
    idx = pd.date_range("2024-01-01", periods=3)
    df = pd.DataFrame({"Close": [10.0, 11.0, 12.0]}, index=idx)
    ticker = SimpleNamespace(history=lambda period=None: df)
    price = model.get_current_price(ticker)
    assert price == 12.0
