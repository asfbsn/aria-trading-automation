#!/usr/bin/env python3
"""
ARIA exit-signal computation — evaluates whether an OPEN bull-put-spread
position's underlying thesis is still intact over raw OHLCV.

Input (stdin): JSON {"ticker": str, "short_strike": float, "bars": [{"date","open","high","low","close","volume"}, ...]}
  bars must be ascending by date. Needs >=155 bars for a stable MA150 read.

  Also accepts IBKR get_price_history's native shape directly (no manual
  reshape needed) -- pass its response merged with "ticker" and "short_strike"
  keys, e.g. {"ticker": "GOOGL", "short_strike": 160.0, "time": [...], "open": [...],
  "high": [...], "low": [...], "close": [...], "volume": [...]}. Parallel arrays
  are zipped into the same bar-dict list internally via bars_from_parallel_arrays.

Output (stdout): JSON with computed indicators + per-factor exit checks +
  an aggregate "thesis_invalidated" boolean.

Flags:
  --exclude-last-bar   Drop the most recent bar before computing anything —
    use for the SETTLED pass when the feed's last bar may still be today's
    in-progress session.
  --input <path>   Read the JSON payload from this file instead of stdin.
    REQUIRED in the live daily pipeline: a Bash command whose argument literally
    contains JSON (any `{`/`"` combination — a heredoc body or an echo/pipe) is
    auto-denied by Claude Code's own command-safety heuristic as "expansion
    obfuscation", regardless of the allowedTools prefix grant. There is no
    quoting fix — the payload has to reach the script as a file argument instead,
    written first via a scoped Write permission, never as literal text in the
    command line.
"""
import json
import sys

from signal_core import (
    MIN_BARS,
    bars_from_parallel_arrays,
    exit_checks,
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
    short_strike = payload["short_strike"]
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
            "thesis_invalidated": False,
        }))
        return

    closes = [b["close"] for b in bars]

    rsis = rsi_series(closes, RSI_LENGTH)
    rsi_now = rsis[-1]

    ma150 = sma(closes, 150)

    checks, thesis_invalidated = exit_checks(closes, bars, short_strike, rsis=rsis)

    print(json.dumps({
        "ticker": ticker,
        "insufficient_data": False,
        "settled": exclude_last_bar,
        "close": closes[-1],
        "rsi20": round(rsi_now, 2) if rsi_now is not None else None,
        "ma150": round(ma150, 2) if ma150 is not None else None,
        "short_strike": short_strike,
        "checks": checks,
        "thesis_invalidated": thesis_invalidated,
        "note": "proxy signal — not the actual protected Adi Radmy Pine output",
    }))


if __name__ == "__main__":
    main()
