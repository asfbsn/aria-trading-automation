#!/usr/bin/env python3
"""Regression check for bear_entry_checks default gate behavior.

Verifies:
1. Synthetic checks dict with BEAR_DEFAULT_GATE_KEYS=True, but non-default
   keys (near_ma50_rejection, volume_above_avg) set to False:
   - OLD implicit behavior (all non-candle_pattern values True) fails.
   - NEW explicit behavior (BEAR_DEFAULT_GATE_KEYS all True) passes.
2. End-to-end bear_entry_checks() call on synthetic data where non-default
   gates fail (volume below average, near_ma50_rejection false):
   - Confirms entry_confirmed is True under new behavior.
   - Confirms old all(v for k, v in checks.items() if k != "candle_pattern") would fail.
"""
import sys
from pathlib import Path

# Add scripts directory to sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from signal_core import (  # noqa: E402
    BEAR_DEFAULT_GATE_KEYS,
    bear_entry_checks,
)


def test_synthetic_checks_dict():
    # Construct synthetic checks dict matching the real checks dict keys
    checks = {
        "below_ma150": True,
        "rsi_above_50": True,
        "rsi_falling": True,
        "near_ma150_resistance": True,
        "bearish_candle": True,
        # Intentionally non-gating keys set to False (mirroring bull-side asymmetry):
        "near_ma50_rejection": False,
        "volume_above_avg": False,
        "candle_pattern": "shooting_star",
    }

    # Verify all BEAR_DEFAULT_GATE_KEYS are in checks dict
    for k in BEAR_DEFAULT_GATE_KEYS:
        assert k in checks, f"Key {k} missing from checks dict"

    # Old implicit behavior: required every key in checks except candle_pattern
    old_behavior_confirmed = all(v for k, v in checks.items() if k != "candle_pattern")
    assert old_behavior_confirmed is False, (
        "OLD behavior expected to return False when non-default keys are False"
    )

    # New explicit behavior: requires only BEAR_DEFAULT_GATE_KEYS
    new_behavior_confirmed = all(checks[k] for k in BEAR_DEFAULT_GATE_KEYS)
    assert new_behavior_confirmed is True, (
        "NEW behavior expected to return True when all BEAR_DEFAULT_GATE_KEYS are True"
    )

    print("PASS: test_synthetic_checks_dict")
    print(f"  BEAR_DEFAULT_GATE_KEYS: {BEAR_DEFAULT_GATE_KEYS}")
    print(f"  OLD behavior would confirm: {old_behavior_confirmed}")
    print(f"  NEW behavior confirmed:     {new_behavior_confirmed}")


def test_bear_entry_checks_end_to_end():
    # 155 bars to satisfy MIN_BARS requirement
    n_bars = 160
    base_price = 100.0
    closes = [base_price] * (n_bars - 5) + [98.0, 97.0, 96.0, 95.0, 94.0]
    # Volumes: average is 100.0, last bar is 50.0 (volume_above_avg will be False)
    volumes = [100.0] * (n_bars - 1) + [50.0]

    # Preceding bar and signal bar (shooting star on last bar)
    bars = [
        {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0, "volume": 100.0}
        for _ in range(n_bars - 2)
    ]
    # Bar n-2
    bars.append({"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0, "volume": 100.0})
    # Bar n-1 (shooting star: small body at bottom, long upper wick)
    # body: |94.0 - 93.8| = 0.2
    # upper wick: 98.0 - 94.0 = 4.0 >= 2 * 0.2
    # lower wick: 93.8 - 93.7 = 0.1 <= 0.2
    bars.append({"open": 93.8, "high": 98.0, "low": 93.7, "close": 94.0, "volume": 50.0})

    # Injected RSIs: last two are 60.0, 55.0 (> 50 and falling)
    rsis = [50.0] * (n_bars - 2) + [60.0, 55.0]

    checks, entry_confirmed = bear_entry_checks(
        closes=closes,
        volumes=volumes,
        bars=bars,
        rsis=rsis,
        min_structural=None,
        min_confirm=None,
    )

    # Verify that all BEAR_DEFAULT_GATE_KEYS passed
    for k in BEAR_DEFAULT_GATE_KEYS:
        assert checks[k] is True, f"Expected {k} to be True, got {checks[k]}"

    # Verify that volume_above_avg is False
    assert checks["volume_above_avg"] is False, "Expected volume_above_avg to be False"

    # Old implicit behavior would have evaluated to False
    old_confirmed = all(v for k, v in checks.items() if k != "candle_pattern")
    assert old_confirmed is False, "OLD behavior should fail due to volume_above_avg=False"

    # New behavior evaluates to True
    assert entry_confirmed is True, "NEW bear_entry_checks must return True"

    print("PASS: test_bear_entry_checks_end_to_end")
    print(f"  checks: {checks}")
    print(f"  OLD behavior would confirm: {old_confirmed}")
    print(f"  NEW entry_confirmed:        {entry_confirmed}")


def main():
    test_synthetic_checks_dict()
    test_bear_entry_checks_end_to_end()
    print("\nALL REGRESSION CHECKS PASSED.")


if __name__ == "__main__":
    main()
