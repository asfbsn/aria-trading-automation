"""Bull Put Spread signal engine wired to aria-trading's actual entry rule
(scripts/compute_signal.py + prompts/bull-put-spread.md), for use with
vibe-trading-ai's options_portfolio.run_options_backtest.

Rule (mirrors compute_signal.py's entry_confirmed + the MA-150-above-price
rule from prompts/bull-put-spread.md rule 1):
  - close > MA150 (rule 1: never short support from below)
  - RSI(20) < 50 and rising vs 2 bars ago
  - close within 2% of nearest MA (50 or 150) -- "near support"
  - volume > 20-bar volume MA
  - bullish candle (hammer or bullish engulfing, same shape rules as compute_signal.py)

On a confirmed entry (and no open spread on that ticker): open a $10-wide
Bull Put Spread, short strike = MA150 rounded down to nearest $5 (below
support per rule 2), long strike = short - 10, expiry = ~30 calendar days out.
Held to expiration/exercise (engine auto-settles); no early-close signal.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

RSI_LENGTH = 20
RSI_THRESHOLD = 50.0
MA_LENGTHS = (50, 150)
MA_SUPPORT_BAND = 0.02
VOLUME_MA_LENGTH = 20
MIN_BARS = MA_LENGTHS[-1] + 5
SPREAD_WIDTH = 10.0
TARGET_DTE = 30


def _rsi_series(closes: List[float], length: int) -> List[float | None]:
    n = len(closes)
    out: List[float | None] = [None] * n
    if n < length + 1:
        return out
    gains, losses = [], []
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    out[length] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(length + 1, n):
        d = closes[i] - closes[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sma(values: List[float], length: int) -> float | None:
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def _candle_pattern(o, h, l, c, prev_o, prev_c) -> str:
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return "none"
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if lower_wick >= 2 * body and upper_wick <= body and body > 0:
        return "hammer"
    if prev_o is not None and prev_c is not None:
        if prev_c < prev_o and c > o and o <= prev_c and c >= prev_o:
            return "bullish_engulfing"
    return "none"


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

            rsis = _rsi_series(closes, RSI_LENGTH)

            open_until = None  # date index string; None = no open position

            for i in range(len(df)):
                if i < MIN_BARS:
                    continue

                date_str = str(dates[i].date())

                if open_until is not None:
                    if date_str <= open_until:
                        continue
                    open_until = None

                closes_so_far = closes[: i + 1]
                volumes_so_far = volumes[: i + 1]
                rsi_now = rsis[i]
                rsi_prev2 = rsis[i - 2] if i >= 2 else None

                ma50 = _sma(closes_so_far, 50)
                ma150 = _sma(closes_so_far, 150)
                vol_ma20 = _sma(volumes_so_far, VOLUME_MA_LENGTH)

                close = closes[i]
                volume = volumes[i]

                if ma150 is None or ma50 is None or vol_ma20 is None or rsi_now is None:
                    continue

                ma_candidates = [("MA50", ma50), ("MA150", ma150)]
                nearest_val, nearest_dist = None, None
                for _, val in ma_candidates:
                    dist = abs(close - val) / val
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_val, nearest_dist = val, dist

                rsi_below_50 = rsi_now < RSI_THRESHOLD
                rsi_rising = rsi_prev2 is not None and rsi_now > rsi_prev2
                near_ma_support = nearest_dist is not None and nearest_dist <= MA_SUPPORT_BAND
                volume_above_avg = volume > vol_ma20
                pattern = _candle_pattern(
                    opens[i], highs[i], lows[i], closes[i],
                    opens[i - 1] if i >= 1 else None,
                    closes[i - 1] if i >= 1 else None,
                )
                bullish_candle = pattern != "none"
                above_ma150 = close > ma150  # rule 1

                entry_confirmed = all([
                    above_ma150, rsi_below_50, rsi_rising, near_ma_support,
                    volume_above_avg, bullish_candle,
                ])

                if not entry_confirmed:
                    continue

                # Rule 2: short strike below MA-150 (or swing low), rounded to
                # nearest $5 for a clean strike; width $10 wide.
                short_strike = math.floor(ma150 / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((ma150 - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width

                entry_ts = dates[i]
                expiry_ts = entry_ts + __import__("pandas").Timedelta(days=self.target_dte)
                expiry_str = str(expiry_ts.date())

                signals.append({
                    "date": date_str,
                    "action": "open",
                    "underlying": code,
                    "legs": [
                        {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                        {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                    ],
                })
                self.entries.append({
                    "code": code, "date": date_str, "close": close, "ma150": ma150,
                    "short_strike": short_strike, "long_strike": long_strike,
                    "expiry": expiry_str,
                })
                open_until = expiry_str

        return signals
