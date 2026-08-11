#!/usr/bin/env python3
"""
ARIA entry-signal proxy — replaces the "Premium Trading Dashboard - Adi Radmy
Edition" Pine indicator read (which requires rendering each ticker on a
TradingView chart) with a deterministic local computation over raw OHLCV.

The Pine script is a protected/invite-only publish (confirmed: "Source code"
menu item is present but unclickable), so its exact internal formulas are
not available. This reproduces the dashboard's declared *input parameters*
(RSI length 20 / threshold 50, MA50, MA150, Volume MA length 20 — read off
the indicator's own Inputs dialog) with best-effort comparison logic for the
parts the script does not expose (support band width, momentum lookback,
candle-pattern rules). This is an approximation of Adi's signal, not a
replica.

The entry rule itself lives in scripts/signal_core.py and is shared with the
backtest engine (scripts/backtest/bps_signal_engine.py) — do NOT duplicate
logic here; change signal_core.py so live and backtest stay in lockstep.

Input (stdin): JSON {"ticker": str, "bars": [{"date","open","high","low","close","volume"}, ...]}
  bars must be ascending by date. Needs >=155 bars for a stable MA150 read.

Output (stdout): JSON with computed indicators + per-factor checks +
  an aggregate "entry_confirmed" boolean (proxy for "יש אישור כניסה").
"""
import json
import sys

from signal_core import (
    MA_LENGTHS,
    MIN_BARS,
    entry_checks,
    rsi_series,
    sma,
)

RSI_LENGTH = 20


def main():
    payload = json.load(sys.stdin)
    ticker = payload["ticker"]
    bars = payload["bars"]

    if len(bars) < MIN_BARS:
        print(json.dumps({
            "ticker": ticker,
            "insufficient_data": True,
            "bars_provided": len(bars),
            "bars_required": MIN_BARS,
            "entry_confirmed": False,
        }))
        return

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    rsis = rsi_series(closes, RSI_LENGTH)
    rsi_now = rsis[-1]
    rsi_prev2 = rsis[-3] if len(rsis) >= 3 else None

    ma50 = sma(closes, 50)
    ma150 = sma(closes, 150)
    vol_ma20 = sma(volumes, 20)

    checks, entry_confirmed = entry_checks(closes, volumes, bars, rsis=rsis)

    print(json.dumps({
        "ticker": ticker,
        "insufficient_data": False,
        "close": closes[-1],
        "rsi20": round(rsi_now, 2) if rsi_now is not None else None,
        "rsi20_2bars_ago": round(rsi_prev2, 2) if rsi_prev2 is not None else None,
        "ma50": round(ma50, 2) if ma50 else None,
        "ma150": round(ma150, 2) if ma150 else None,
        "volume": volumes[-1],
        "volume_ma20": round(vol_ma20, 2) if vol_ma20 else None,
        "checks": checks,
        "entry_confirmed": entry_confirmed,
        "note": "proxy signal — not the actual protected Adi Radmy Pine output",
    }))


if __name__ == "__main__":
    main()
