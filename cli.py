"""
Terminal single-ticker checker - no GUI toolkit required.

    python cli.py AMZN
    python cli.py AMZN MSFT NVDA
"""
from __future__ import annotations

import sys

from earnings_scanner.model import TickerDataError, compute_recommendation

RESET = "\033[0m"
COLORS = {"Recommended": "\033[92m", "Consider": "\033[93m", "Avoid": "\033[91m"}


def _pass_fail(label: str, passed: bool) -> str:
    color = "\033[92m" if passed else "\033[91m"
    return f"  {label}: {color}{'PASS' if passed else 'FAIL'}{RESET}"


def check(symbol: str) -> None:
    try:
        rec = compute_recommendation(symbol)
    except TickerDataError as e:
        print(f"{symbol.strip().upper()}: {e}\n")
        return

    color = COLORS.get(rec.verdict, "")
    print(f"{rec.ticker}: {color}{rec.verdict}{RESET}")
    print(_pass_fail("avg_volume", rec.avg_volume_pass) + f" ({rec.avg_volume:,.0f})")
    print(_pass_fail("iv30_rv30", rec.iv30_rv30_pass) + f" ({rec.iv30_rv30:.3f})")
    print(_pass_fail("ts_slope_0_45", rec.ts_slope_pass) + f" ({rec.ts_slope_0_45:.6f})")
    if rec.expected_move_pct is not None:
        print(f"  expected move: {rec.expected_move_pct:.2f}%")
    print(f"  underlying price: ${rec.underlying_price:.2f}\n")


def main() -> None:
    symbols = sys.argv[1:]
    if not symbols:
        symbols = [input("Enter stock symbol: ")]

    for symbol in symbols:
        check(symbol)


if __name__ == "__main__":
    main()
