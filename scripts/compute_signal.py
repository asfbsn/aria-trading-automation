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
parts the script does not expose (momentum lookback, candle-pattern rules).
The support band width is NOT a guess — it's `signal_core.py`'s MA50_BAND /
MA150_BAND, confirmed 2026-08-24 from the live screener's own filter panel
(close 0%-10% below MA50 AND 0%-10% above MA150). This is an approximation of
Adi's signal, not a replica.

The entry rule itself lives in scripts/signal_core.py and is shared with the
backtest engine (scripts/backtest/bps_signal_engine.py) — do NOT duplicate
logic here; change signal_core.py so live and backtest stay in lockstep.

Input (stdin): JSON {"ticker": str, "bars": [{"date","open","high","low","close","volume"}, ...]}
  bars must be ascending by date. Needs >=155 bars for a stable MA150 read.

  Also accepts IBKR get_price_history's native shape directly (no manual
  reshape needed) -- pass its response merged with a "ticker" key, e.g.
  {"ticker": "GOOGL", "time": [...], "open": [...], "high": [...],
  "low": [...], "close": [...], "volume": [...]}. Parallel arrays are
  zipped into the same bar-dict list internally.

Output (stdout): JSON with computed indicators + per-factor checks +
  an aggregate "entry_confirmed" boolean (proxy for "יש אישור כניסה").

Flags:
  --exclude-last-bar   Drop the most recent bar before computing anything —
    use for the SETTLED pass when the feed's last bar may still be today's
    in-progress session (see prompts/bull-put-spread.md's settled-vs-
    provisional split). Omit for the PROVISIONAL pass (full payload as-is).
    This can't be done by hand-editing the JSON: the prompt is instructed to
    pass IBKR's response unmodified (index drift risk across 150+ values), and
    the allowed Bash scope has no separate transform command — so the drop
    has to happen inside this script.
  --input <path>   Read the JSON payload from this file instead of stdin.
    REQUIRED in the live daily-scan pipeline (confirmed 2026-08-25): a Bash
    command whose argument literally contains JSON (any `{`/`"` combination —
    a heredoc body or an echo/pipe) is auto-denied by Claude Code's own
    command-safety heuristic as "expansion obfuscation", regardless of the
    allowedTools prefix grant. There is no quoting fix — the payload has to
    reach the script as a file argument instead, written first via a scoped
    Write permission, never as literal text in the command line.
"""
import json
import sys

from signal_core import (
    MA_LENGTHS,
    MIN_BARS,
    bars_from_parallel_arrays,
    entry_checks,
    rsi_series,
    sma,
)

RSI_LENGTH = 20


def main():
    args = sys.argv[1:]
    exclude_last_bar = "--exclude-last-bar" in args

    if "--input" in args:
        input_path = args[args.index("--input") + 1]
        with open(input_path) as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)
    ticker = payload["ticker"]
    bars = payload["bars"] if "bars" in payload else bars_from_parallel_arrays(payload)

    if exclude_last_bar and bars:
        bars = bars[:-1]

    if len(bars) < MIN_BARS:
        print(json.dumps({
            "ticker": ticker,
            "insufficient_data": True,
            "settled": exclude_last_bar,
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
        "settled": exclude_last_bar,
        "close": closes[-1],
        "rsi20": round(rsi_now, 2) if rsi_now is not None else None,
        "rsi20_2bars_ago": round(rsi_prev2, 2) if rsi_prev2 is not None else None,
        "ma50": round(ma50, 2) if ma50 is not None else None,
        "ma150": round(ma150, 2) if ma150 is not None else None,
        "volume": volumes[-1],
        "volume_ma20": round(vol_ma20, 2) if vol_ma20 is not None else None,
        "checks": checks,
        "entry_confirmed": entry_confirmed,
        "note": "proxy signal — not the actual protected Adi Radmy Pine output",
    }))


if __name__ == "__main__":
    main()
