#!/usr/bin/env python3
"""Synthetic regression for the 2026-09-14 entry policy.

Run: python3 scripts/check_signal_policy_regression.py
No market data or external dependencies required.
"""

from signal_core import bear_entry_checks, ema, entry_checks, sma


def main():
    failures = []
    for name, evaluate, rsis, bar, momentum_key, candle_key in (
        ("bull", entry_checks, [45.0, 35.0, 40.0],
         {"open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0},
         "rsi_rising", "bullish_candle"),
        ("bear", bear_entry_checks, [55.0, 65.0, 60.0],
         {"open": 102.0, "high": 103.0, "low": 99.0, "close": 100.0},
         "rsi_falling", "bearish_candle"),
    ):
        # Same-color preceding bars exclude engulfing; short wicks exclude
        # hammer/shooting-star patterns. RSI is injected to isolate lookback.
        bars = [dict(bar) for _ in rsis]
        closes = [b["close"] for b in bars]
        volumes = [100.0] * len(bars)
        checks, _ = evaluate(closes, volumes, bars, rsis=rsis)
        old_momentum = (rsis[-1] > rsis[-3] if name == "bull"
                        else rsis[-1] < rsis[-3])
        assert old_momentum is False
        passed = checks[momentum_key] is True
        print(f"{name} RSI: old-behavior-would-fail ({old_momentum}); "
              f"new-behavior-{'passes' if passed else 'FAILS'} "
              f"({momentum_key}={checks[momentum_key]})")
        if not passed:
            failures.append(momentum_key)

        assert checks["candle_pattern"] == "none"
        loose_candle = (bar["close"] > bar["open"] if name == "bull"
                        else bar["close"] < bar["open"])
        assert loose_candle is True
        passed = checks[candle_key] is False
        baseline = ("old-behavior-would-fail" if name == "bull"
                    else "loose-mirror-would-fail (bear already strict)")
        print(f"{name} candle: {baseline} ({loose_candle}); "
              f"new-behavior-{'passes' if passed else 'FAILS'} "
              f"({candle_key}={checks[candle_key]})")
        if not passed:
            failures.append(candle_key)

    # MA150 sign-flip case (2026-09-14 BX investigation): extra older-high
    # history that SMA150's trailing window excludes but EMA150 still partly
    # weights, mirroring the real BX shape (an old high plateau outside the
    # SMA window, a lower recent level inside it).
    extra_high = [160.0] * 100
    recent_low = [124.0] * 145 + [128.0] * 5
    closes = extra_high + recent_low
    volumes = [100.0] * len(closes)
    bars = [{"open": c, "high": c, "low": c, "close": c} for c in closes]
    old_ma150 = sma(closes, 150)
    new_ma150 = ema(closes, 150)
    close = closes[-1]
    old_above = close > old_ma150
    new_above = close > new_ma150
    print(f"ma150 type: old(SMA)={old_ma150:.2f} above={old_above}; "
          f"new(EMA)={new_ma150:.2f} above={new_above}")
    assert old_above is True and new_above is False, (
        "synthetic case must reproduce the old-SMA-says-support/"
        "new-EMA-says-no-support sign flip"
    )
    checks, _ = entry_checks(closes, volumes, bars, rsis=[45.0] * len(closes))
    passed = checks["above_ma150"] is False and checks["near_ma150_support"] is False
    print(f"ma150 gate: new-behavior-{'passes' if passed else 'FAILS'} "
          f"(above_ma150={checks['above_ma150']}, near_ma150_support={checks['near_ma150_support']})")
    if not passed:
        failures.append("ma150_ema")

    assert not failures, f"Policy regressions: {', '.join(failures)}"
    print("PASS: all five policy checks")


if __name__ == "__main__":
    main()
