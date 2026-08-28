"""Shared ARIA bull-put-spread entry and exit rules — single source of truth.

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

Exit rule:
  - Thesis invalidation = close below short strike OR close below MA150 (hard)
  - RSI > 70 / bearish candle (shooting star, bearish engulfing) = discretionary watch signals only
"""

RSI_LENGTH = 20
RSI_THRESHOLD = 50.0
MA_LENGTHS = (50, 150)
MA50_BAND = (0.0, 0.10)   # close is 0%-10% BELOW MA50: (ma50-close)/ma50 in this range
MA150_BAND = (0.0, 0.10)  # close is 0%-10% ABOVE MA150: (close-ma150)/ma150 in this range
VOLUME_MA_LENGTH = 20
MIN_BARS = 150 + 5

# Gate check categorization for backtest A/B comparison (not yet wired into live scan)
STRUCTURAL_KEYS = (
    "above_ma150",
    "rsi_below_50",
    "near_ma50_pullback",
    "near_ma150_support",
)
CONFIRM_KEYS = (
    "rsi_rising",
    "volume_above_avg",
    "bullish_candle",
)



def bars_from_parallel_arrays(payload):
    """Zip IBKR get_price_history's parallel-array response into the
    bar-dict list the rest of this script expects."""
    times = payload["time"]
    opens = payload["open"]
    highs = payload["high"]
    lows = payload["low"]
    closes = payload["close"]
    volumes = payload["volume"]
    n = len(times)
    if not (len(opens) == len(highs) == len(lows) == len(closes) == len(volumes) == n):
        raise ValueError(
            f"IBKR parallel arrays have mismatched lengths: "
            f"time={n} open={len(opens)} high={len(highs)} "
            f"low={len(lows)} close={len(closes)} volume={len(volumes)}"
        )
    return [
        {
            "date": times[i],
            "open": opens[i],
            "high": highs[i],
            "low": lows[i],
            "close": closes[i],
            "volume": volumes[i],
        }
        for i in range(n)
    ]


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


def entry_checks(
    closes,
    volumes,
    bars,
    rsis=None,
    min_structural=None,
    min_confirm=None,
):
    """Evaluate the full entry rule on the last element of the given
    ascending series (which share a common length). Returns (checks dict,
    entry_confirmed bool).

    Optional min_structural and min_confirm parameters are for backtest A/B
    comparison and are not yet wired into the live scan.
    """
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

    # Gate confirmation: default (None, None) keeps exact legacy all-7 behavior.
    # When parameterized for backtest A/B comparison:
    # entry_confirmed = (structural checks >= min_structural) and (confirm checks >= min_confirm).
    if min_structural is None and min_confirm is None:
        confirmed = all(v for k, v in checks.items()
                        if k != "candle_pattern")
    else:
        req_struct = len(STRUCTURAL_KEYS) if min_structural is None else min_structural
        req_conf = len(CONFIRM_KEYS) if min_confirm is None else min_confirm
        struct_passed = sum(1 for k in STRUCTURAL_KEYS if checks.get(k, False)) >= req_struct
        conf_passed = sum(1 for k in CONFIRM_KEYS if checks.get(k, False)) >= req_conf
        confirmed = struct_passed and conf_passed
    return checks, confirmed


def bearish_candle_pattern(o, h, l, c, prev_o, prev_c):
    """Shooting star / bearish engulfing — mirrors candle_pattern()'s hammer/
    bullish_engulfing logic but for the bearish case."""
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return "none"
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if upper_wick >= 2 * body and lower_wick <= body and body > 0:
        return "shooting_star"
    if prev_o is not None and prev_c is not None:
        if prev_c > prev_o and c < o and o >= prev_c and c <= prev_o:
            return "bearish_engulfing"
    return "none"


def exit_checks(closes, bars, short_strike, rsis=None):
    """Evaluate whether an OPEN short-put-spread position's underlying thesis
    is still intact. Purely technical (stock-level) — does NOT know about
    credit received or current option pricing; that's computed separately by
    the caller from live option quotes.

    Returns (checks dict, thesis_invalidated bool).
    """
    if rsis is None:
        rsis = rsi_series(closes)
    rsi_now = rsis[-1]
    ma150 = sma(closes, 150)
    close = closes[-1]
    volumes = [b["volume"] for b in bars]
    vol_ma20 = sma(volumes, VOLUME_MA_LENGTH)

    short_breached = short_strike is not None and close < short_strike
    ma150_breached = ma150 is not None and close < ma150
    volume_confirmed = (
        (short_breached or ma150_breached)
        and vol_ma20 is not None
        and volumes[-1] > vol_ma20
    )

    checks = {
        "short_strike_breached": short_breached,
        "broke_ma150_support": ma150_breached,
        "volume_confirmed_breakdown": volume_confirmed,
        "rsi_overbought": rsi_now is not None and rsi_now > 70.0,
    }
    pattern = bearish_candle_pattern(
        bars[-1]["open"], bars[-1]["high"], bars[-1]["low"], bars[-1]["close"],
        bars[-2]["open"] if len(bars) >= 2 else None,
        bars[-2]["close"] if len(bars) >= 2 else None,
    )
    checks["bearish_candle"] = pattern != "none"
    checks["candle_pattern"] = pattern

    # Thesis invalidated = either hard technical break. RSI/candle are
    # discretionary WATCH signals only, never force a close on their own.
    thesis_invalidated = checks["short_strike_breached"] or checks["broke_ma150_support"]
    return checks, thesis_invalidated


