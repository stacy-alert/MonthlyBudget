"""
Earnings-volatility screen: term-structure slope, IV30/RV30, and 30-day
liquidity filters used to flag short-volatility (straddle / calendar) setups
around earnings.

This is a hardened rewrite of the original single-file calculator. Fixes
versus the original:

- `todays_data['Close'][0]` used positional indexing on a label-indexed
  Series. Recent pandas raises `KeyError: 0` for that instead of silently
  falling back to position, which is the most likely reason the original
  script simply stopped working. Replaced with `.iloc[0]`.
- Bid/ask "missing" checks used `is not None`, but yfinance represents
  missing quotes as NaN, not None, so illiquid strikes silently produced a
  garbage expected-move number instead of being skipped.
- The original caught every exception and re-raised a message with no detail
  (`f'Error occured processing'`), making failures impossible to debug. This
  version raises `TickerDataError` with the actual cause attached.
- yfinance calls now go through a shared session (curl_cffi w/ Chrome TLS
  impersonation when available) since Yahoo aggressively blocks the plain
  `requests` fingerprint — see market_data.py.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.interpolate import interp1d

from .market_data import get_session, with_retry

logger = logging.getLogger(__name__)

AVG_VOLUME_THRESHOLD = 1_500_000
IV30_RV30_THRESHOLD = 1.25
TS_SLOPE_THRESHOLD = -0.00406


class TickerDataError(Exception):
    """Raised when a recommendation can't be computed for a ticker."""


@dataclass
class Recommendation:
    ticker: str
    verdict: str  # "Recommended" | "Consider" | "Avoid"
    avg_volume_pass: bool
    iv30_rv30_pass: bool
    ts_slope_pass: bool
    avg_volume: float
    iv30_rv30: float
    ts_slope_0_45: float
    underlying_price: float
    expected_move_pct: Optional[float]

    def as_dict(self) -> dict:
        d = {
            "ticker": self.ticker,
            "verdict": self.verdict,
            "avg_volume_pass": self.avg_volume_pass,
            "iv30_rv30_pass": self.iv30_rv30_pass,
            "ts_slope_pass": self.ts_slope_pass,
            "avg_volume": self.avg_volume,
            "iv30_rv30": round(self.iv30_rv30, 3) if self.iv30_rv30 is not None else None,
            "ts_slope_0_45": round(self.ts_slope_0_45, 6) if self.ts_slope_0_45 is not None else None,
            "underlying_price": round(self.underlying_price, 2),
            "expected_move_pct": round(self.expected_move_pct, 2) if self.expected_move_pct is not None else None,
        }
        return d


def _verdict(avg_volume_pass: bool, iv30_rv30_pass: bool, ts_slope_pass: bool) -> str:
    if avg_volume_pass and iv30_rv30_pass and ts_slope_pass:
        return "Recommended"
    if ts_slope_pass and (avg_volume_pass != iv30_rv30_pass):
        return "Consider"
    return "Avoid"


def filter_dates(dates: list[str]) -> list[str]:
    """Keep expirations up through the first one that is >= 45 days out."""
    today = datetime.today().date()
    cutoff_date = today + timedelta(days=45)

    sorted_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)

    chosen: list[date_cls] = []
    for i, d in enumerate(sorted_dates):
        if d >= cutoff_date:
            chosen = sorted_dates[: i + 1]
            break

    if not chosen:
        raise ValueError("No expiration 45 days or more out was found.")

    if chosen[0] == today:
        chosen = chosen[1:]

    if not chosen:
        raise ValueError("No usable expirations after filtering today's date.")

    return [d.strftime("%Y-%m-%d") for d in chosen]


def yang_zhang(price_data: pd.DataFrame, window: int = 30, trading_periods: int = 252,
                return_last_only: bool = True):
    log_ho = (price_data["High"] / price_data["Open"]).apply(np.log)
    log_lo = (price_data["Low"] / price_data["Open"]).apply(np.log)
    log_co = (price_data["Close"] / price_data["Open"]).apply(np.log)

    log_oc = (price_data["Open"] / price_data["Close"].shift(1)).apply(np.log)
    log_oc_sq = log_oc ** 2

    log_cc = (price_data["Close"] / price_data["Close"].shift(1)).apply(np.log)
    log_cc_sq = log_cc ** 2

    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)

    close_vol = log_cc_sq.rolling(window=window, center=False).sum() * (1.0 / (window - 1.0))
    open_vol = log_oc_sq.rolling(window=window, center=False).sum() * (1.0 / (window - 1.0))
    window_rs = rs.rolling(window=window, center=False).sum() * (1.0 / (window - 1.0))

    k = 0.34 / (1.34 + ((window + 1) / (window - 1)))
    result = (open_vol + k * close_vol + (1 - k) * window_rs).apply(np.sqrt) * np.sqrt(trading_periods)

    if return_last_only:
        return result.iloc[-1]
    return result.dropna()


def build_term_structure(days, ivs):
    days = np.array(days)
    ivs = np.array(ivs)

    sort_idx = days.argsort()
    days = days[sort_idx]
    ivs = ivs[sort_idx]

    spline = interp1d(days, ivs, kind="linear", fill_value="extrapolate")

    def term_spline(dte):
        if dte < days[0]:
            return float(ivs[0])
        if dte > days[-1]:
            return float(ivs[-1])
        return float(spline(dte))

    return term_spline


def get_current_price(ticker: yf.Ticker) -> float:
    todays_data = ticker.history(period="1d")
    if todays_data.empty:
        raise TickerDataError("No recent price data returned.")
    return float(todays_data["Close"].iloc[-1])


# Yahoo rate-limits repeated requests hard, and the same ticker often gets
# looked up more than once in a short window (a scan, then a manual re-check
# of one row). Cache successful results briefly so that doesn't cost another
# round trip - this doesn't fix an active IP-level block, but it meaningfully
# cuts how often we hit Yahoo at all once one clears.
_RESULT_CACHE_TTL_SECONDS = 600
_result_cache: dict[str, tuple[float, "Recommendation"]] = {}
_cache_lock = threading.Lock()


def compute_recommendation(symbol: str) -> Recommendation:
    """Fetch options/price data for `symbol` and score it against the model.

    Raises TickerDataError with a human-readable reason on any failure -
    missing options chain, no usable expirations, stale/missing quotes, etc.
    Results are cached for a few minutes per symbol (see module docstring).
    """
    symbol_key = (symbol or "").strip().upper()

    with _cache_lock:
        cached = _result_cache.get(symbol_key)
        if cached and (time.time() - cached[0]) < _RESULT_CACHE_TTL_SECONDS:
            return cached[1]

    rec = _compute_recommendation_uncached(symbol)

    with _cache_lock:
        _result_cache[symbol_key] = (time.time(), rec)

    return rec


def _compute_recommendation_uncached(symbol: str) -> Recommendation:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise TickerDataError("No stock symbol provided.")

    session = get_session()
    stock = yf.Ticker(symbol, session=session)

    try:
        raw_options = with_retry(lambda: stock.options, f"{symbol} options list")
    except Exception as e:
        raise TickerDataError(f"Could not load options for '{symbol}': {e}") from e

    if not raw_options:
        raise TickerDataError(f"No options found for stock symbol '{symbol}'.")

    try:
        exp_dates = filter_dates(list(raw_options))
    except ValueError as e:
        raise TickerDataError(f"Not enough option expiration data for '{symbol}': {e}") from e

    options_chains = {}
    for exp_date in exp_dates:
        try:
            options_chains[exp_date] = with_retry(
                lambda d=exp_date: stock.option_chain(d), f"{symbol} {exp_date} chain"
            )
        except Exception as e:
            logger.warning("Skipping expiration %s for %s: %s", exp_date, symbol, e)

    if not options_chains:
        raise TickerDataError(f"Could not load any option chains for '{symbol}'.")

    try:
        underlying_price = with_retry(lambda: get_current_price(stock), f"{symbol} current price")
    except Exception as e:
        raise TickerDataError(f"Unable to retrieve underlying price for '{symbol}': {e}") from e

    atm_iv: dict[str, float] = {}
    straddle = None
    first_expiry_seen = False

    for exp_date, chain in options_chains.items():
        calls = chain.calls
        puts = chain.puts

        if calls.empty or puts.empty:
            continue

        call_idx = (calls["strike"] - underlying_price).abs().idxmin()
        call_iv = calls.loc[call_idx, "impliedVolatility"]

        put_idx = (puts["strike"] - underlying_price).abs().idxmin()
        put_iv = puts.loc[put_idx, "impliedVolatility"]

        if pd.isna(call_iv) or pd.isna(put_iv):
            continue

        atm_iv[exp_date] = (float(call_iv) + float(put_iv)) / 2.0

        if not first_expiry_seen:
            call_bid = calls.loc[call_idx, "bid"]
            call_ask = calls.loc[call_idx, "ask"]
            put_bid = puts.loc[put_idx, "bid"]
            put_ask = puts.loc[put_idx, "ask"]

            call_mid = (call_bid + call_ask) / 2.0 if pd.notna(call_bid) and pd.notna(call_ask) else None
            put_mid = (put_bid + put_ask) / 2.0 if pd.notna(put_bid) and pd.notna(put_ask) else None

            if call_mid is not None and put_mid is not None:
                straddle = call_mid + put_mid

            first_expiry_seen = True

    if not atm_iv:
        raise TickerDataError(f"Could not determine ATM IV for any expiration of '{symbol}'.")

    today = datetime.today().date()
    dtes, ivs = [], []
    for exp_date, iv in atm_iv.items():
        exp_date_obj = datetime.strptime(exp_date, "%Y-%m-%d").date()
        dtes.append((exp_date_obj - today).days)
        ivs.append(iv)

    term_spline = build_term_structure(dtes, ivs)
    ts_slope_0_45 = (term_spline(45) - term_spline(min(dtes))) / (45 - min(dtes))

    try:
        price_history = with_retry(lambda: stock.history(period="3mo"), f"{symbol} price history")
    except Exception as e:
        raise TickerDataError(f"Unable to retrieve price history for '{symbol}': {e}") from e

    if len(price_history) < 30:
        raise TickerDataError(f"Not enough price history for '{symbol}' to compute realized volatility.")

    rv30 = yang_zhang(price_history)
    if not rv30 or np.isnan(rv30) or rv30 == 0:
        raise TickerDataError(f"Could not compute realized volatility for '{symbol}'.")

    iv30_rv30 = term_spline(30) / rv30

    avg_volume_series = price_history["Volume"].rolling(30).mean().dropna()
    if avg_volume_series.empty:
        raise TickerDataError(f"Not enough volume history for '{symbol}'.")
    avg_volume = float(avg_volume_series.iloc[-1])

    expected_move_pct = (straddle / underlying_price * 100.0) if straddle else None

    # bool(...) matters here: comparisons against numpy scalars yield numpy.bool_,
    # which the stdlib json encoder (and therefore Flask's jsonify) rejects.
    avg_volume_pass = bool(avg_volume >= AVG_VOLUME_THRESHOLD)
    iv30_rv30_pass = bool(iv30_rv30 >= IV30_RV30_THRESHOLD)
    ts_slope_pass = bool(ts_slope_0_45 <= TS_SLOPE_THRESHOLD)

    return Recommendation(
        ticker=symbol,
        verdict=_verdict(avg_volume_pass, iv30_rv30_pass, ts_slope_pass),
        avg_volume_pass=avg_volume_pass,
        iv30_rv30_pass=iv30_rv30_pass,
        ts_slope_pass=ts_slope_pass,
        avg_volume=avg_volume,
        iv30_rv30=float(iv30_rv30),
        ts_slope_0_45=float(ts_slope_0_45),
        underlying_price=underlying_price,
        expected_move_pct=expected_move_pct,
    )
