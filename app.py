"""
Earnings Volatility Scanner - web app.

Run with:
    python app.py

Then open http://127.0.0.1:5000
"""
from __future__ import annotations

import threading
import uuid
from datetime import date

from flask import Flask, jsonify, render_template, request

from earnings_scanner.earnings_calendar import build_watchlist
from earnings_scanner.model import TickerDataError, compute_recommendation
from earnings_scanner.scanner import scan_tickers

app = Flask(__name__)

# In-memory job store. Fine for a single-user, self-hosted app; if you need
# multi-worker deployment, swap this for redis/sqlite.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

DEFAULT_MIN_MARKET_CAP = 500_000_000


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def start_scan():
    body = request.get_json(silent=True) or {}

    target_date_str = body.get("date")
    try:
        target_date = date.fromisoformat(target_date_str) if target_date_str else date.today()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    min_market_cap = body.get("min_market_cap", DEFAULT_MIN_MARKET_CAP)

    try:
        events = build_watchlist(target_date, min_market_cap=min_market_cap)
    except Exception as e:
        return jsonify({"error": f"Could not load earnings calendar: {e}"}), 502

    tickers = [e.ticker for e in events]
    watchlist_meta = {e.ticker: {"name": e.name, "market_cap": e.market_cap, "session": e.session} for e in events}

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "date": target_date.isoformat(),
            "progress": {"done": 0, "total": len(tickers)},
            "watchlist_meta": watchlist_meta,
            "results": [],
        }

    thread = threading.Thread(target=_run_scan_job, args=(job_id, tickers), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "watchlist_size": len(tickers), "date": target_date.isoformat()})


def _run_scan_job(job_id: str, tickers: list[str]) -> None:
    job = _jobs[job_id]

    def progress_cb(done: int, total: int) -> None:
        job["progress"] = {"done": done, "total": total}

    try:
        results = scan_tickers(tickers, progress_cb=progress_cb)
        for r in results:
            meta = job["watchlist_meta"].get(r.get("ticker"))
            if meta:
                r["name"] = meta.get("name")
                r["session"] = meta.get("session")
        job["results"] = results
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/scan/<job_id>")
def scan_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job id"}), 404
    return jsonify(job)


@app.route("/api/ticker/<symbol>")
def single_ticker(symbol: str):
    try:
        rec = compute_recommendation(symbol)
        return jsonify(rec.as_dict())
    except TickerDataError as e:
        return jsonify({"ticker": symbol.strip().upper(), "error": str(e)}), 400


if __name__ == "__main__":
    import os

    # Port 5000 is claimed by macOS's AirPlay Receiver on modern Macs, which
    # blocks Flask from binding there with no obvious error in the browser.
    # Default to 5001 instead; override with PORT=xxxx if you need to.
    port = int(os.environ.get("PORT", 5001))
    print(f" * Open http://127.0.0.1:{port} in your browser")
    app.run(debug=True, host="127.0.0.1", port=port)
