# Earnings Volatility Scanner

> **Educational / research tool only. Not investment advice.** Options trading
> involves substantial risk. Nothing here is a recommendation to buy or sell
> any security. Always verify prices and Greeks in your own broker before
> placing a trade.

Rebuild of a single-file earnings-volatility calculator into two things:

1. **`earnings_scanner/`** - a fixed, GUI-free version of the original scoring
   logic (term-structure slope, IV30/RV30, 30-day liquidity), usable as a
   library.
2. **A Flask web app (`app.py`)** you self-host, which scans a whole day's
   earnings calendar and gives you a ranked table of tickers instead of
   checking one symbol at a time in a desktop GUI window.

## What the model does

For a given ticker, it pulls the options chain and:

- Builds an implied-volatility term structure from the ATM straddle IV at
  each expiration and measures its slope from the nearest expiration out to
  45 days (`ts_slope_0_45`). More negative = steeper backwardation = the
  market pricing in an outsized near-term move that's expected to fade.
- Compares 30-day implied vol to 30-day realized vol (Yang-Zhang estimator)
  as `iv30_rv30`.
- Checks 30-day average dollar volume as a liquidity filter.
- Estimates the market's expected move from the front-month ATM straddle
  price.

A ticker is flagged:

- **Recommended** - all three filters pass (`ts_slope_0_45 <= -0.00406`,
  `iv30_rv30 >= 1.25`, `avg_volume >= 1,500,000`).
- **Consider** - the slope filter passes plus exactly one of the other two.
- **Avoid** - otherwise.

These are the same thresholds as the original script. Nothing here validates
that they're still well-calibrated for current markets - treat the labels as
a starting filter, not a signal to trade blindly.

## What was actually broken in the original script

- `todays_data['Close'][0]` used positional indexing on a `Series` keyed by
  timestamp. Current pandas raises `KeyError: 0` there instead of silently
  falling back to positional access - this alone is enough to make the
  original tool fail on every single lookup. Fixed with `.iloc[-1]`.
- Bid/ask "missing" checks used `is not None`, but yfinance represents a
  missing quote as `NaN`, not `None` - illiquid strikes were slipping through
  and could produce a bogus expected-move number. Fixed with `pd.notna()`.
- All exceptions were caught and re-raised as the literal string
  `"Error occured processing"` with the real cause discarded, so there was no
  way to tell *why* a ticker failed (blocked request vs. no options vs. bad
  data). This version raises `TickerDataError` with the actual reason.
- Every yfinance call went out over a plain `requests` session. Yahoo
  aggressively blocks that TLS fingerprint, which is the most common reason
  people see silent failures / empty option chains. This version routes
  through `curl_cffi` with Chrome TLS impersonation when it's installed
  (it's in `requirements.txt`), falling back to a browser-like `User-Agent`
  otherwise.
- The desktop GUI (`FreeSimpleGUI`) needs a working Tk/display stack, which
  is a common source of "it just doesn't work" on headless machines, some
  Linux setups, and inside containers. The web app has no GUI toolkit
  dependency at all.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run the web app

```bash
python app.py
```

Open http://127.0.0.1:5000. From there you can:

- Click **Scan Today's Earnings** to pull today's after-hours reporters plus
  tomorrow's before-open reporters (i.e. everything you'd actually be
  entering a position for today, since the strategy opens trades ~15 minutes
  before the close on the day before the announcement) and score every one
  of them concurrently. Results are sorted Recommended → Consider → Avoid.
  A minimum market cap filter (default $500M) keeps the scan from wasting
  time on illiquid names that would fail the volume filter anyway.
- Use the **single ticker lookup** box to check one symbol on demand, same
  as the original desktop tool.

To run it somewhere other than your own machine, put a real WSGI server in
front of it (e.g. `gunicorn app:app`) rather than using Flask's dev server,
and put it behind auth/a reverse proxy if it's reachable from the internet -
there's no login system built in.

## Run the terminal version

```bash
python cli.py AMZN
python cli.py AMZN MSFT NVDA   # check several at once
```

## Data sources & their limits

- **Options/price data**: `yfinance` (unofficial Yahoo Finance wrapper). Can
  be delayed, rate-limited, or occasionally wrong. Yahoo has no public SLA
  here.
- **Earnings calendar**: Nasdaq's undocumented public calendar endpoint. It
  can miss events, misclassify before/after-market timing, or change shape
  without notice - the parser is defensive but not bulletproof. Always
  sanity-check a name's actual earnings date/time before trading it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests mock yfinance/Nasdaq entirely and check the scoring math, expiration
filtering, term-structure interpolation, JSON-serializability of results,
and calendar parsing - they don't hit live data.

## Project layout

```
app.py                          Flask web app (routes + background scan jobs)
cli.py                          No-GUI terminal single/multi-ticker checker
earnings_scanner/
  model.py                      Core recommendation logic (fixed)
  market_data.py                Shared yfinance HTTP session (curl_cffi)
  earnings_calendar.py          Nasdaq earnings-calendar fetch + watchlist build
  scanner.py                    Concurrent multi-ticker scan
templates/, static/             Web UI
tests/                          Unit tests (mocked data)
```
