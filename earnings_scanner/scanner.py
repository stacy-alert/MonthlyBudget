"""Concurrent scan of a ticker list against the earnings-volatility model."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .model import Recommendation, TickerDataError, compute_recommendation

_VERDICT_ORDER = {"Recommended": 0, "Consider": 1, "Avoid": 2}


def scan_tickers(
    tickers: list[str],
    max_workers: int = 3,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Run compute_recommendation for every ticker concurrently.

    Returns a list of dicts (either a serialized Recommendation or
    {"ticker": ..., "error": ...}), sorted Recommended -> Consider -> Avoid
    -> errors, alphabetically within each group.
    """
    total = len(tickers)
    results: list[dict] = []
    done = 0

    if total == 0:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {executor.submit(compute_recommendation, t): t for t in tickers}

        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                rec: Recommendation = future.result()
                results.append(rec.as_dict())
            except TickerDataError as e:
                results.append({"ticker": ticker, "error": str(e)})
            except Exception as e:  # noqa: BLE001 - never let one bad ticker kill the scan
                results.append({"ticker": ticker, "error": f"Unexpected error: {e}"})

            done += 1
            if progress_cb:
                progress_cb(done, total)

    results.sort(key=lambda r: (_VERDICT_ORDER.get(r.get("verdict"), 3), r.get("ticker", "")))
    return results
