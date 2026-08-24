"""Shared ARIA bull-put-spread entry logic — single source of truth.

Imported by both the live signal path (scripts/compute_signal.py, stdlib only)
and the backtest signal engine (scripts/backtest/bps_signal_engine.py).
Keep this module dependency-free (no pandas/numpy) so the live path stays lean.

Rule (mirrors prompts/bull-put-spread.md):
  1. close > MA150                            (never short support from below)
  2. RSI(20) < 50 and rising vs 2 bars ago
  3. close is BELOW MA50 by 0%-10%, AND ABOVE MA150 by 0%-10%  (squeeze between
     the two averages — pullback zone, real screener filter bands, confirmed
     2026-08-24 from the live TradingView screener panel; replaces the earlier
     "within 2% of nearest MA" placeholder, which was an undisclosed-band guess)
  4. volume > 20-bar volume MA
  5. bullish candle (hammer or bullish engulfing)
"""

RSI_LENGTH = 20
RSI_THRESHOLD = 50.0
MA_LENGTHS = (50, 150)
MA50_BAND = (0.0, 0.10)   # close is 0%-10% BELOW MA50: (ma50-close)/ma50 in this range
MA150_BAND = (0.0, 0.10)  # close is 0%-10% ABOVE MA150: (close-ma150)/ma150 in this range
VOLUME_MA_LENGTH = 20
MIN_BARS = 150 + 5


def sma(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def rsi_series(closes, length=RSI_LENGTH):
    """Wilder-smoothed RSI, one value per bar (None where undefined)."""
    n = len(closes)
    out = [None] * n
    if n < length + 1:
        return out
    gains, losses = [], []
    for i in range(1, length + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    out[length] = rsi_from_avgs(avg_gain, avg_loss)
    for i in range(length + 1, n):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(d, 0.0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-d, 0.0)) / length
        out[i] = rsi_from_avgs(avg_gain, avg_loss)
    return out


def rsi_from_avgs(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def candle_pattern(o, h, l, c, prev_o, prev_c):
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


def entry_checks(closes, volumes, bars, rsis=None):
    """Evaluate the full entry rule on the last element of the given
    ascending series (which share a common length). Returns (checks dict,
    entry_confirmed bool)."""
    if rsis is None:
        rsis = rsi_series(closes)
    rsi_now = rsis[-1]
    rsi_prev2 = rsis[-3] if len(rsis) >= 3 else None

    ma50 = sma(closes, 50)
    ma150 = sma(closes, 150)
    vol_ma20 = sma(volumes, VOLUME_MA_LENGTH)

    close = closes[-1]
    volume = volumes[-1]

    below_ma50_pct = (ma50 - close) / ma50 if ma50 else None
    above_ma150_pct = (close - ma150) / ma150 if ma150 else None

    checks = {
        "above_ma150": ma150 is not None and close > ma150,  # rule 1
        "rsi_below_50": rsi_now is not None and rsi_now < RSI_THRESHOLD,
        "rsi_rising": (rsi_now is not None and rsi_prev2 is not None
                       and rsi_now > rsi_prev2),
        "near_ma50_pullback": (below_ma50_pct is not None
                                and MA50_BAND[0] <= below_ma50_pct <= MA50_BAND[1]),
        "near_ma150_support": (above_ma150_pct is not None
                                and MA150_BAND[0] <= above_ma150_pct <= MA150_BAND[1]),
        "volume_above_avg": vol_ma20 is not None and volume > vol_ma20,
    }
    pattern = candle_pattern(
        bars[-1]["open"], bars[-1]["high"], bars[-1]["low"], bars[-1]["close"],
        bars[-2]["open"] if len(bars) >= 2 else None,
        bars[-2]["close"] if len(bars) >= 2 else None,
    )
    checks["bullish_candle"] = pattern != "none"
    checks["candle_pattern"] = pattern

    confirmed = all(v for k, v in checks.items()
                    if k != "candle_pattern")
    return checks, confirmed
