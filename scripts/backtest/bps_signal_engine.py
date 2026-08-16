"""Bull Put Spread signal engine for vibe-trading-ai's
options_portfolio.run_options_backtest.

Entry rule: shared with the live scanner via scripts/signal_core.py — do NOT
reimplement here; change signal_core.py so live and backtest stay in lockstep.

On a confirmed entry (and no open spread on that ticker): open a $10-wide
Bull Put Spread, short strike = MA150 rounded down to nearest $5 (below
support per rule 2), long strike = short - 10, expiry = next Friday ~30
calendar days out. Held to expiration/exercise (engine auto-settles); no
early-close signal.

Execution realism: signals are computed on bar t but emitted for bar t+1
with price_mode "open", so the backtest fills at the next session's open
(the live bot acts intraday with an unsettled bar; filling at bar-t close
would be look-ahead-flavoured optimism).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signal_core import (  # noqa: E402
    MIN_BARS,
    candle_pattern,
    entry_checks,
)

SPREAD_WIDTH = 10.0
TARGET_DTE = 30


class BullPutSpreadSignalEngine:
    """engine.generate(data_map) -> list[dict] for run_options_backtest."""

    def __init__(self, width: float = SPREAD_WIDTH, target_dte: int = TARGET_DTE):
        self.width = width
        self.target_dte = target_dte
        self.entries: List[Dict[str, Any]] = []  # collected for reporting

    def generate(self, data_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        self.entries = []

        for code, df in data_map.items():
            df = df.sort_index()
            closes = df["close"].tolist()
            opens = df["open"].tolist() if "open" in df.columns else closes
            highs = df["high"].tolist() if "high" in df.columns else closes
            lows = df["low"].tolist() if "low" in df.columns else closes
            volumes = df["volume"].tolist() if "volume" in df.columns else [0] * len(closes)
            dates = list(df.index)

            open_until = None  # date index string; None = no open position

            for i in range(len(df)):
                if i < MIN_BARS or i < 1 or i + 1 >= len(dates):
                    continue

                entry_ts = dates[i + 1]  # fill on the bar AFTER the signal bar
                date_str = str(entry_ts.date())

                if open_until is not None:
                    if date_str <= open_until:
                        continue
                    open_until = None

                # entry_checks inspects bars[-2:]; give it signal bar i and
                # its predecessor (never bar i+1 — that would be look-ahead).
                bars_slice = [
                    {"open": opens[j], "high": highs[j], "low": lows[j], "close": closes[j]}
                    for j in (i - 1, i)
                ]
                checks, entry_confirmed = entry_checks(
                    closes[: i + 1], volumes[: i + 1], bars_slice
                )
                if not entry_confirmed:
                    continue

                ma150 = sum(closes[i - 149: i + 1]) / 150.0
                close = closes[i]

                # Rule 2: short strike below MA-150, rounded to nearest $5;
                # width $10 wide.
                short_strike = math.floor(ma150 / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((ma150 - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width

                # Expiry: next Friday on/after signal+TARGET_DTE (real chains
                # list weeklies; a bare calendar offset lands on random days).
                expiry_ts = entry_ts + pd.Timedelta(days=self.target_dte)
                expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
                expiry_str = str(expiry_ts.date())

                signals.append({
                    "date": date_str,
                    "action": "open",
                    "underlying": code,
                    "price_mode": "open",  # execute at bar t+1 open, not close
                    "legs": [
                        {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                        {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                    ],
                    "group_id": f"{code}-{date_str}",  # keep the spread's legs linked
                })
                self.entries.append({
                    "code": code, "date": date_str, "signal_close": close,
                    "ma150": ma150, "short_strike": short_strike,
                    "long_strike": long_strike, "expiry": expiry_str,
                })
                open_until = expiry_str

        return signals
