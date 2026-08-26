#!/usr/bin/env python3
"""ARIA Bull-Put-Spread Prescreen — local technical filter stage.

Downloads daily OHLCV for data/universe.csv tickers via yfinance and filters
out tickers that clearly fail basic MA150 support or MA50/MA150 pullback bands.
The shortlist is deliberately over-inclusive so borderline setups are never
falsely rejected; IBKR price history and scripts/compute_signal.py remain
authoritative downstream.

Python 3, uses yfinance + pandas (both already in scripts/backtest/requirements.txt;
this script is run by the wrapper shell, NOT by the Claude agent, so the "live path
stays stdlib-only" rule in signal_core.py's docstring is not violated).
"""
import argparse
import csv
from datetime import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_core import (
    MIN_BARS,
    RSI_LENGTH,
    entry_checks,
    rsi_series,
    sma,
)


def get_ticker_df(df: pd.DataFrame, ticker: str, total_tickers: int) -> pd.DataFrame:
    """Extract a single ticker's DataFrame from yf.download result."""
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.levels[0]:
            try:
                sub_df = df[ticker]
                if isinstance(sub_df, pd.DataFrame):
                    return sub_df
            except (KeyError, TypeError):
                return pd.DataFrame()
        return pd.DataFrame()
    else:
        if total_tickers == 1:
            return df
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description="ARIA Bull-Put-Spread Prescreen")
    parser.add_argument("--output", required=True, help="Path to write output JSON")
    parser.add_argument("--universe", default=None, help="Path to universe.csv (default: data/universe.csv)")
    args = parser.parse_args()

    universe_path = args.universe
    if not universe_path:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        universe_path = os.path.join(repo_root, "data", "universe.csv")

    universe_tickers = []
    with open(universe_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = row.get("ticker", "").strip()
            if t:
                universe_tickers.append(t)

    universe_count = len(universe_tickers)
    today_ny_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    if universe_count == 0:
        output_data = {
            "date": today_ny_str,
            "universe_count": 0,
            "shortlist": [],
            "prescreen_filtered_count": 0,
            "failures": [],
            "per_ticker": {},
        }
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)
        print("prescreen: universe=0 shortlist=0 filtered=0 failures=0")
        sys.exit(0)

    # Batch-download ~400 calendar days of daily OHLCV
    df = yf.download(
        universe_tickers,
        period="400d",
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
    )

    shortlist = []
    failures = []
    per_ticker = {}

    for ticker in universe_tickers:
        tdf = get_ticker_df(df, ticker, universe_count)
        if tdf.empty:
            failures.append({"ticker": ticker, "reason": "no_data"})
            continue

        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in tdf.columns]
        if len(cols) < 5:
            failures.append({"ticker": ticker, "reason": "no_data"})
            continue

        tdf = tdf.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        if tdf.empty:
            failures.append({"ticker": ticker, "reason": "no_data"})
            continue

        bars = []
        for idx, row in tdf.iterrows():
            bar_date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            bars.append({
                "date": bar_date,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            })

        # SETTLED convention: drop last bar if it equals today (US/Eastern date)
        if bars and bars[-1]["date"] == today_ny_str:
            bars = bars[:-1]

        if len(bars) < MIN_BARS:
            failures.append({"ticker": ticker, "reason": "insufficient_data"})
            continue

        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]

        rsis = rsi_series(closes, RSI_LENGTH)
        rsi_now = rsis[-1]

        ma50 = sma(closes, 50)
        ma150 = sma(closes, 150)
        close = closes[-1]

        checks, entry_confirmed = entry_checks(closes, volumes, bars, rsis=rsis)

        # Shortlist rule (deliberately OVER-INCLUSIVE):
        # (a) close > MA150 * 0.99 (above_ma150 with a 1% grace margin), AND
        # (b) BOTH band checks pass with a ±1.5-percentage-point widened band:
        #     (ma50-close)/ma50 in [-0.015, 0.115] AND (close-ma150)/ma150 in [-0.015, 0.115].
        if ma50 is not None and ma150 is not None and ma50 > 0 and ma150 > 0:
            below_ma50_pct = (ma50 - close) / ma50
            above_ma150_pct = (close - ma150) / ma150
            cond_a = close > (ma150 * 0.99)
            cond_b = (-0.015 <= below_ma50_pct <= 0.115) and (-0.015 <= above_ma150_pct <= 0.115)
            if cond_a and cond_b:
                shortlist.append(ticker)
                per_ticker[ticker] = {
                    "close": close,
                    "ma50": round(ma50, 2),
                    "ma150": round(ma150, 2),
                    "rsi20": round(rsi_now, 2) if rsi_now is not None else None,
                    "entry_confirmed": bool(entry_confirmed),
                    "checks": checks,
                }

    # Pass-through, not silent drop: a ticker yfinance can't evaluate (bad symbol
    # format like BRK-B vs BRKB, delisted row, missing history) is NOT filtered —
    # the over-inclusive contract says only a confirmed band failure may cut a
    # name before the authoritative IBKR pass. Failures join the shortlist and
    # stay listed in "failures" so the report shows why they lack local values.
    shortlist.extend(f["ticker"] for f in failures)
    shortlist.sort()
    failures.sort(key=lambda x: x["ticker"])

    shortlist_count = len(shortlist)
    failures_count = len(failures)
    # failures are inside the shortlist now (pass-through), so filtered is simply
    # universe minus shortlist — no separate failures subtraction.
    prescreen_filtered_count = universe_count - shortlist_count

    output_data = {
        "date": today_ny_str,
        "universe_count": universe_count,
        "shortlist": shortlist,
        "prescreen_filtered_count": prescreen_filtered_count,
        "failures": failures,
        "per_ticker": per_ticker,
    }

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"prescreen: universe={universe_count} shortlist={shortlist_count} filtered={prescreen_filtered_count} failures={failures_count}")

    # Data-outage guard: with pass-through, failures land in the shortlist, so an
    # empty shortlist can no longer signal an outage — a high failure share does.
    if failures_count > 0.20 * universe_count:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
