#!/usr/bin/env python3
"""Regression test for exclude_last_bar date-gating across ARIA signal scripts.

Verifies the fix for the 2026-09-21 live-money bug where pre-market calls with
--exclude-last-bar erroneously dropped the previous completed session's bar.

Covers:
1. scripts/compute_signal.py (CLI subprocess)
2. scripts/compute_exit_signal.py (CLI subprocess)
3. scripts/compute_exit_signal_v2.py (direct function call and CLI)
4. Empirical production reproduction using state/scratch/signal_input_KLAC_exit.json

Run:
    python3 scripts/check_exclude_last_bar_regression.py
"""

from __future__ import annotations

import datetime
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compute_exit_signal  # noqa: E402
import compute_signal  # noqa: E402
from compute_exit_signal_v2 import compute_exit_signal_v2  # noqa: E402
from signal_core import MIN_BARS  # noqa: E402


def build_synthetic_bars(num_bars: int = 160, end_date_str: str = "2026-06-15") -> list[dict]:
    """Generate synthetic daily bars ending on end_date_str.

    Second-to-last close: 100.0
    Last close: 95.0
    """
    end_dt = datetime.date.fromisoformat(end_date_str)
    start_dt = end_dt - datetime.timedelta(days=num_bars)
    bars = []
    for i in range(num_bars):
        d_str = (start_dt + datetime.timedelta(days=i + 1)).strftime("%Y-%m-%d")
        # ISO timestamp format with time suffix matching IBKR bars
        iso_str = f"{d_str}T13:30:00Z"
        c = 100.0 if i < num_bars - 1 else 95.0
        bars.append({
            "date": iso_str,
            "open": c + 0.5,
            "high": c + 1.0,
            "low": c - 1.0,
            "close": c,
            "volume": 100000,
        })
    return bars


def run_cli_script(script_name: str, payload: dict, extra_flags: list[str] | None = None) -> dict:
    """Run a CLI script via subprocess with JSON over stdin."""
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)]
    if extra_flags:
        cmd.extend(extra_flags)
    proc = subprocess.run(
        cmd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_compute_signal():
    """Verify scripts/compute_signal.py under Case A and Case B."""
    print("--- Testing scripts/compute_signal.py ---")
    today_ny = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    today_ny_str = today_ny.strftime("%Y-%m-%d")
    prior_ny_str = (today_ny - datetime.timedelta(days=3)).strftime("%Y-%m-%d")

    # Case A: pre-market call, last bar dated prior_ny_str != today_ny_str
    bars_a = build_synthetic_bars(MIN_BARS + 5, end_date_str=prior_ny_str)
    payload_a = {"ticker": "TEST", "bars": bars_a}
    out_a = run_cli_script("compute_signal.py", payload_a, ["--exclude-last-bar"])
    assert out_a["close"] == 95.0, (
        f"[FAIL] compute_signal Case A: expected 95.0 (last bar kept), got {out_a['close']}"
    )
    print("PASS: compute_signal Case A (pre-market call keeps last bar: close=95.0)")

    # Case B: regular-hours call, last bar dated today_ny_str == today_ny_str
    bars_b = build_synthetic_bars(MIN_BARS + 5, end_date_str=today_ny_str)
    payload_b = {"ticker": "TEST", "bars": bars_b}
    out_b = run_cli_script("compute_signal.py", payload_b, ["--exclude-last-bar"])
    assert out_b["close"] == 100.0, (
        f"[FAIL] compute_signal Case B: expected 100.0 (in-progress bar dropped), got {out_b['close']}"
    )
    print("PASS: compute_signal Case B (regular-hours call drops last bar: close=100.0)")

    # Case C: post-market call (time >= 16:00 ET), last bar dated today_ny_str == today_ny_str
    # Direct function invocation with mocked time (deterministic, avoids relying on wall-clock time)
    class MockDtPostMarket(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 17, 0, 0, tzinfo=tz)

    bars_c = build_synthetic_bars(MIN_BARS + 5, end_date_str="2026-06-15")
    payload_c = {"ticker": "TEST", "bars": bars_c}
    buf_c = io.StringIO()
    with patch("compute_signal.datetime", MockDtPostMarket), \
         patch("sys.argv", ["compute_signal.py", "--exclude-last-bar"]), \
         patch("sys.stdin", io.StringIO(json.dumps(payload_c))), \
         redirect_stdout(buf_c):
        compute_signal.main()
    out_c = json.loads(buf_c.getvalue())
    assert out_c["close"] == 95.0, (
        f"[FAIL] compute_signal Case C: expected 95.0 (settled bar kept), got {out_c['close']}"
    )
    print("PASS: compute_signal Case C (post-market call keeps last bar: close=95.0)")


def test_compute_exit_signal():
    """Verify scripts/compute_exit_signal.py under Case A, Case B, and real KLAC payload."""
    print("--- Testing scripts/compute_exit_signal.py ---")
    today_ny = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    today_ny_str = today_ny.strftime("%Y-%m-%d")
    prior_ny_str = (today_ny - datetime.timedelta(days=3)).strftime("%Y-%m-%d")

    # Case A: pre-market call, last bar dated prior_ny_str != today_ny_str
    bars_a = build_synthetic_bars(MIN_BARS + 5, end_date_str=prior_ny_str)
    payload_a = {"ticker": "TEST", "short_strike": 98.0, "bars": bars_a}
    out_a = run_cli_script("compute_exit_signal.py", payload_a, ["--exclude-last-bar"])
    assert out_a["close"] == 95.0, (
        f"[FAIL] compute_exit_signal Case A: expected 95.0 (last bar kept), got {out_a['close']}"
    )
    print("PASS: compute_exit_signal Case A (pre-market call keeps last bar: close=95.0)")

    # Case B: regular-hours call, last bar dated today_ny_str == today_ny_str
    bars_b = build_synthetic_bars(MIN_BARS + 5, end_date_str=today_ny_str)
    payload_b = {"ticker": "TEST", "short_strike": 98.0, "bars": bars_b}
    out_b = run_cli_script("compute_exit_signal.py", payload_b, ["--exclude-last-bar"])
    assert out_b["close"] == 100.0, (
        f"[FAIL] compute_exit_signal Case B: expected 100.0 (in-progress bar dropped), got {out_b['close']}"
    )
    print("PASS: compute_exit_signal Case B (regular-hours call drops last bar: close=100.0)")

    # Case C: post-market call (time >= 16:00 ET), last bar dated today_ny_str == today_ny_str
    # Direct function invocation with mocked time (deterministic, avoids relying on wall-clock time)
    class MockDtPostMarket(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 17, 0, 0, tzinfo=tz)

    bars_c = build_synthetic_bars(MIN_BARS + 5, end_date_str="2026-06-15")
    payload_c = {"ticker": "TEST", "short_strike": 98.0, "bars": bars_c}
    buf_c = io.StringIO()
    with patch("compute_exit_signal.datetime", MockDtPostMarket), \
         patch("sys.argv", ["compute_exit_signal.py", "--exclude-last-bar"]), \
         patch("sys.stdin", io.StringIO(json.dumps(payload_c))), \
         redirect_stdout(buf_c):
        compute_exit_signal.main()
    out_c = json.loads(buf_c.getvalue())
    assert out_c["close"] == 95.0, (
        f"[FAIL] compute_exit_signal Case C: expected 95.0 (settled bar kept), got {out_c['close']}"
    )
    print("PASS: compute_exit_signal Case C (post-market call keeps last bar: close=95.0)")

    # Empirical check: real KLAC input from 2026-09-21 morning
    klac_path = REPO_ROOT / "state" / "scratch" / "signal_input_KLAC_exit.json"
    if klac_path.exists():
        with open(klac_path, "r", encoding="utf-8") as f:
            klac_payload = json.load(f)
        out_klac = run_cli_script("compute_exit_signal.py", klac_payload, ["--exclude-last-bar"])
        assert out_klac["close"] == 176.99, (
            f"[FAIL] KLAC empirical reproduction: expected 176.99 (Friday close kept), got {out_klac['close']}"
        )
        assert out_klac["thesis_invalidated"] is False, (
            f"[FAIL] KLAC thesis_invalidated should be False with close 176.99 > strike 170.0, got {out_klac['thesis_invalidated']}"
        )
        print("PASS: compute_exit_signal empirical KLAC payload (close=176.99, thesis_invalidated=False)")


def test_compute_exit_signal_v2():
    """Verify scripts/compute_exit_signal_v2.py under deterministic mocked time."""
    print("--- Testing scripts/compute_exit_signal_v2.py ---")

    class MockDtPreMarket(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 9, 0, 0, tzinfo=tz)

    class MockDtRegularHours(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 14, 0, 0, tzinfo=tz)

    bars_a = build_synthetic_bars(MIN_BARS + 5, end_date_str="2026-06-12")
    payload_a = {
        "ticker": "AAPL",
        "short_strike": 98.0,
        "initial_credit": 1.0,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "bars": bars_a,
    }

    with patch("datetime.datetime", MockDtPreMarket):
        out_a = compute_exit_signal_v2(payload_a, exclude_last_bar=True)
    assert out_a["close"] == 95.0, (
        f"[FAIL] compute_exit_signal_v2 Case A: expected 95.0 (last bar kept), got {out_a['close']}"
    )
    print("PASS: compute_exit_signal_v2 Case A (pre-market call keeps last bar: close=95.0)")

    bars_b = build_synthetic_bars(MIN_BARS + 5, end_date_str="2026-06-15")
    payload_b = {
        "ticker": "AAPL",
        "short_strike": 98.0,
        "initial_credit": 1.0,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "bars": bars_b,
    }

    with patch("datetime.datetime", MockDtRegularHours):
        out_b = compute_exit_signal_v2(payload_b, exclude_last_bar=True)
    assert out_b["close"] == 100.0, (
        f"[FAIL] compute_exit_signal_v2 Case B: expected 100.0 (in-progress bar dropped), got {out_b['close']}"
    )
    print("PASS: compute_exit_signal_v2 Case B (regular-hours call drops last bar: close=100.0)")

    # Case C: post-market call (time >= 16:00 ET), last bar dated today_ny_str == today_ny_str
    class MockDtPostMarket(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 17, 0, 0, tzinfo=tz)

    bars_c = build_synthetic_bars(MIN_BARS + 5, end_date_str="2026-06-15")
    payload_c = {
        "ticker": "AAPL",
        "short_strike": 98.0,
        "initial_credit": 1.0,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "bars": bars_c,
    }

    with patch("datetime.datetime", MockDtPostMarket):
        out_c = compute_exit_signal_v2(payload_c, exclude_last_bar=True)
    assert out_c["close"] == 95.0, (
        f"[FAIL] compute_exit_signal_v2 Case C: expected 95.0 (settled bar kept), got {out_c['close']}"
    )
    print("PASS: compute_exit_signal_v2 Case C (post-market call keeps last bar: close=95.0)")


def main():
    test_compute_signal()
    test_compute_exit_signal()
    test_compute_exit_signal_v2()
    print("\nALL REGRESSION CHECKS PASSED SUCCESSFULLY.")


if __name__ == "__main__":
    main()
