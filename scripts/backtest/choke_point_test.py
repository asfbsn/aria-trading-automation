"""Choke-point isolation: on the 750-ticker mega-cap-only universe, how much
does raw entry-signal count jump if the strict candle-pattern check
(hammer/bullish_engulfing) is loosened to "any green candle" (close > open),
and/or the RSI momentum confirm ("rising vs 2 bars ago") is dropped?

Reuses signal_core's sma/rsi_series/candle_pattern for the parts NOT being
varied (above_ma150, rsi_below_50, near_ma50_pullback, near_ma150_support,
volume_above_avg -- untouched). Same no-overlapping-open-position gating as
the live engine (bps_signal_engine.py) so variant A should reproduce the
known baseline (~597 signals / 36mo = 16.6/mo on width=10).
"""
import csv
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from signal_core import sma, rsi_series, candle_pattern, MA50_BAND, MA150_BAND, MIN_BARS, VOLUME_MA_LENGTH, RSI_THRESHOLD

UNIVERSE_CSV = BASE.parent.parent / "data" / "universe.csv"
START = "2023-07-26"
END = "2026-07-26"
TARGET_DTE = 30

with open(UNIVERSE_CSV) as f:
    TICKERS = [row["ticker"].strip() for row in csv.DictReader(f) if row.get("ticker", "").strip()]
print(f"Universe: {len(TICKERS)} mega-cap tickers")

print("Fetching OHLCV (750 tickers, ~3yr)...", flush=True)
raw = yf.download(TICKERS, start=START, end=END, progress=False, auto_adjust=True,
                   group_by="ticker", threads=True)
data_map = {}
for code in TICKERS:
    try:
        df = raw[code].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
    except KeyError:
        continue
    if df is None or df.empty or df["Close"].dropna().empty:
        continue
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df.index = pd.to_datetime(df.index)
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    data_map[code] = df
print(f"Loaded {len(data_map)}/{len(TICKERS)} tickers.", flush=True)

VARIANTS = {
    "A. Baseline (strict candle + rsi_rising, current live)": dict(loosen_candle=False, drop_momentum=False),
    "B. Loosened candle only (any green close, keep rsi_rising)": dict(loosen_candle=True, drop_momentum=False),
    "C. Loosened momentum only (drop rsi_rising, keep strict candle)": dict(loosen_candle=False, drop_momentum=True),
    "D. Both loosened (any green close + no rsi_rising requirement)": dict(loosen_candle=True, drop_momentum=True),
}

months = 36.0
results = {}
for label, cfg in VARIANTS.items():
    total_signals = 0
    for code, df in data_map.items():
        closes = df["close"].tolist()
        opens = df["open"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        volumes = df["volume"].tolist()
        dates = list(df.index)
        rsis = rsi_series(closes)
        open_until = None
        for i in range(len(df)):
            if i < MIN_BARS or i < 1 or i + 1 >= len(dates):
                continue
            entry_ts = dates[i + 1]
            date_str = str(entry_ts.date())
            if open_until is not None:
                if date_str <= open_until:
                    continue
                open_until = None

            close = closes[i]
            ma50 = sma(closes[: i + 1], 50)
            ma150 = sma(closes[: i + 1], 150)
            vol_ma20 = sma(volumes[: i + 1], VOLUME_MA_LENGTH)
            rsi_now = rsis[i]
            rsi_prev2 = rsis[i - 2] if i >= 2 else None

            below_ma50_pct = (ma50 - close) / ma50 if ma50 else None
            above_ma150_pct = (close - ma150) / ma150 if ma150 else None

            structural_ok = (
                ma150 is not None and close > ma150
                and rsi_now is not None and rsi_now < RSI_THRESHOLD
                and below_ma50_pct is not None and MA50_BAND[0] <= below_ma50_pct <= MA50_BAND[1]
                and above_ma150_pct is not None and MA150_BAND[0] <= above_ma150_pct <= MA150_BAND[1]
            )
            if not structural_ok:
                continue

            volume_ok = vol_ma20 is not None and volumes[i] > vol_ma20
            if not volume_ok:
                continue

            if cfg["drop_momentum"]:
                momentum_ok = True
            else:
                momentum_ok = rsi_now is not None and rsi_prev2 is not None and rsi_now > rsi_prev2
            if not momentum_ok:
                continue

            if cfg["loosen_candle"]:
                candle_ok = closes[i] > opens[i]
            else:
                pattern = candle_pattern(opens[i], highs[i], lows[i], closes[i],
                                          opens[i - 1] if i >= 1 else None,
                                          closes[i - 1] if i >= 1 else None)
                candle_ok = pattern != "none"
            if not candle_ok:
                continue

            total_signals += 1
            expiry_ts = entry_ts + pd.Timedelta(days=TARGET_DTE)
            expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
            open_until = str(expiry_ts.date())

    results[label] = total_signals
    print(f"{label}: {total_signals} signals ({total_signals/months:.1f}/month)")

baseline = results["A. Baseline (strict candle + rsi_rising, current live)"]
print("\n=== Multiplier vs baseline ===")
for label, count in results.items():
    mult = count / baseline if baseline else float("nan")
    print(f"{label}: {mult:.2f}x")
