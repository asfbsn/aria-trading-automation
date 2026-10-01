"""DRAFT for human review: configurable Bull Put Spread entry gates.

Not wired into the live backtest loop. The generate(data_map) -> list[dict]
contract and spread execution mirror bps_signal_engine.py: MA150 rounded
down to $5, $10 width, next-bar open, Friday expiry about 30 calendar days
after entry, and only one open spread per ticker until expiry.

VRP here means cached market-implied IV / historical HV, both in decimal
units. A ratio >= 1.1 asks for implied volatility at least 10% above realized
volatility before selling premium. This is an entry hypothesis, not a claim
that the spread will be profitable. Never substitute synthetic HV for IV.

Mode is an explicit experiment axis: baseline retains the shared five-key
gate, vrp_only retains only its structural above-MA150 requirement plus VRP,
and vrp_plus_baseline adds VRP to the original gate. Spread construction and
position timing stay identical, so future backtests can isolate gate effects.
This draft changes entry selection only; it does not change option pricing
inside options_portfolio or supply a new option-fill valuation model.
"""
from __future__ import annotations

import math
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signal_core import MIN_BARS, ema, entry_checks, sma  # noqa: E402

SPREAD_WIDTH = 10.0
TARGET_DTE = 30
DEFAULT_IV_HV_CACHE_PATH = (
    Path(__file__).resolve().parent / "run_out_dolthub_iv" / "volatility_history.pkl"
)
Mode = Literal["baseline", "vrp_only", "vrp_plus_baseline"]


class BullPutSpreadSignalEngine:
    """Draft engine.generate(data_map) -> list[dict] for run_options_backtest."""

    def __init__(
        self,
        width: float = SPREAD_WIDTH,
        target_dte: int = TARGET_DTE,
        mode: Mode = "vrp_plus_baseline",
        vrp_threshold: float = 1.1,
        iv_hv_cache_path: Path | None = None,
        suppress_reentry: bool = True,
        gate_on_vrp: bool = True,
    ):
        if mode not in ("baseline", "vrp_only", "vrp_plus_baseline"):
            raise ValueError(f"Unknown entry mode: {mode!r}")
        if not math.isfinite(vrp_threshold) or vrp_threshold <= 0:
            raise ValueError("vrp_threshold must be finite and positive")
        if not gate_on_vrp and mode != "vrp_only":
            raise ValueError(f"gate_on_vrp=False is only valid for mode='vrp_only', got mode={mode!r}")
        self.width = width
        self.target_dte = target_dte
        self.mode = mode
        self.vrp_threshold = vrp_threshold
        self.suppress_reentry = suppress_reentry
        self.gate_on_vrp = gate_on_vrp
        self.iv_hv_cache_path = (
            DEFAULT_IV_HV_CACHE_PATH if iv_hv_cache_path is None else Path(iv_hv_cache_path)
        )
        # Trusted local artifact only: no downloads, forward fills, or derived
        # volatility. A missing cache file is a setup error; missing rows within
        # this intentionally partial universe are normal and handled below.
        with self.iv_hv_cache_path.open("rb") as handle:
            self._iv_hv_cache = pickle.load(handle)
        self.entries: List[Dict[str, Any]] = []
        self.skipped_missing_iv = 0

    @staticmethod
    def _vrp_ratio(values: tuple[float, float] | None) -> float | None:
        """None means unavailable/unusable, never a synthetic replacement.

        Count NaN, nonfinite values, nonpositive HV (undefined denominator),
        and negative IV as unusable alongside absent rows. Zero IV is valid
        but yields zero VRP and therefore fails a positive threshold.
        """
        if values is None:
            return None
        iv, hv = values
        if pd.isna(iv) or pd.isna(hv):
            return None
        if not math.isfinite(iv) or not math.isfinite(hv) or iv < 0 or hv <= 0:
            return None
        ratio = iv / hv
        return ratio if math.isfinite(ratio) else None

    def generate(self, data_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        self.entries = []
        self.skipped_missing_iv = 0

        for code, df in data_map.items():
            df = df.sort_index()
            closes = df["close"].tolist()
            opens = df["open"].tolist() if "open" in df.columns else closes
            highs = df["high"].tolist() if "high" in df.columns else closes
            lows = df["low"].tolist() if "low" in df.columns else closes
            volumes = df["volume"].tolist() if "volume" in df.columns else [0] * len(closes)
            dates = list(df.index)

            # Match calendar/session dates, tolerating date-only strings or
            # timezone-bearing daily timestamps. No nearest-date/as-of lookup:
            # yesterday's IV cannot silently stand in for today's missing IV.
            history = self._iv_hv_cache.get(code)
            vol_by_date = {} if history is None else {
                pd.Timestamp(date).date(): (iv, hv)
                for date, iv, hv in zip(history.index, history["iv_current"], history["hv_current"])
            }
            open_until = None

            for i in range(len(df)):
                if i < MIN_BARS or i < 1 or i + 1 >= len(dates):
                    continue

                entry_ts = dates[i + 1]
                date_str = str(entry_ts.date())
                if self.suppress_reentry and open_until is not None:
                    if date_str <= open_until:
                        continue
                    open_until = None

                signal_closes = closes[: i + 1]
                # SMA150 remains the strike-derivation anchor for every mode,
                # unchanged -- this is deliberate parity with ProductionEngine
                # (see _self_test's baseline-regression assertion) and a
                # separate design question from which MA type gates entry.
                ma150 = sma(signal_closes, 150)
                close = closes[i]
                ma150_gate: float | None = None
                z_ma150: float | None = None
                if self.mode == "vrp_only":
                    # EMA, not SMA, for the gate itself -- matches
                    # signal_core.py's 2026-09-14 dashboard-fidelity fix (a
                    # full SMA-vs-EMA sign flip was found live on BX the same
                    # night this engine was reviewed). vrp_only never calls
                    # entry_checks(), so without this it would silently keep
                    # gating on the exact divergence that fix corrected.
                    # Strike derivation below still anchors on SMA150 (ma150
                    # above) -- narrower fix, not a blanket switch.
                    ma150_gate = ema(signal_closes, 150)
                    price_gate = ma150_gate is not None and close > ma150_gate

                    tail = np.asarray(signal_closes[-21:], dtype=float)
                    if len(tail) == 21 and np.all(tail > 0) and np.all(np.isfinite(tail)):
                        sigma = float(np.std(np.log(tail[1:] / tail[:-1]), ddof=1))
                        if (
                            math.isfinite(sigma)
                            and sigma > 0
                            and ma150_gate is not None
                            and math.isfinite(ma150_gate)
                            and close > 0
                        ):
                            z_val = (close - ma150_gate) / (close * sigma)
                            z_ma150 = float(z_val) if math.isfinite(z_val) else None
                        else:
                            z_ma150 = None
                    else:
                        z_ma150 = None
                else:
                    bars_slice = [
                        {"open": opens[j], "high": highs[j], "low": lows[j], "close": closes[j]}
                        for j in (i - 1, i)
                    ]
                    # Default arguments preserve all five keys unchanged.
                    # RSI, MA150_BAND, and candle logic belong to signal_core;
                    # entry_checks itself calls candle_pattern. No copied math.
                    _, price_gate = entry_checks(signal_closes, volumes[: i + 1], bars_slice)
                if not price_gate:
                    continue

                # Use bar t's volatility, never the fill bar t+1. Baseline
                # records available VRP for reporting but is never gated on it.
                vrp_ratio = self._vrp_ratio(vol_by_date.get(pd.Timestamp(dates[i]).date()))
                if self.mode != "baseline" and self.gate_on_vrp:
                    if vrp_ratio is None:
                        self.skipped_missing_iv += 1
                        continue
                    if vrp_ratio < self.vrp_threshold:
                        continue

                # Mirror production construction, including its defensive
                # rounding guard. Price can still gap below the strike at the
                # next open; do not inspect that future bar to filter signals.
                short_strike = math.floor((ma150 - 0.01) / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((min(ma150, close) - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width
                expiry_ts = entry_ts + pd.Timedelta(days=self.target_dte)
                expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
                expiry_str = str(expiry_ts.date())

                signals.append({
                    "date": date_str,
                    "action": "open",
                    "underlying": code,
                    "price_mode": "open",
                    "legs": [
                        {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                        {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                    ],
                    "group_id": f"{code}-{date_str}",
                })
                self.entries.append({
                    "code": code, "date": date_str, "signal_close": close,
                    "ma150": ma150, "short_strike": short_strike,
                    "long_strike": long_strike, "expiry": expiry_str,
                    "vrp_ratio": vrp_ratio, "mode": self.mode,
                    "ema150": ma150_gate, "z_ma150": z_ma150,
                })
                if self.suppress_reentry:
                    open_until = expiry_str

        # Candidates mean flat-position, warmed-up bars with a next fill bar
        # and a passing mode-specific price gate. Missing data during an open
        # position, or behind a failed price gate, does not inflate this count.
        print(f"[{self.mode}] entries={len(self.entries)}; "
              f"candidates skipped for missing/invalid IV/HV={self.skipped_missing_iv}")
        return signals


def _self_test() -> None:
    """Local-cache smoke test plus a deterministic gate-separation fixture.

    Synthetic prices are test inputs only. Even the separation fixture uses
    an actual qualifying IV/HV observation from the pickle, never invented IV.
    No test artifacts or cache changes are written to disk.
    """
    engines = {mode: BullPutSpreadSignalEngine(mode=mode) for mode in (
        "baseline", "vrp_only", "vrp_plus_baseline"
    )}
    cache = engines["vrp_only"]._iv_hv_cache
    ohlcv_path = Path(__file__).resolve().parent / "mega750_ohlcv_cache.pkl"
    if ohlcv_path.exists():
        with ohlcv_path.open("rb") as handle:
            prices = pickle.load(handle)
        code = next(code for code in cache if code in prices and len(prices[code]) > MIN_BARS + 1)
        real_code = code
        data_map = {code: prices[code]}
        print(f"Real OHLCV smoke test: {code}")
        outputs = {mode: engine.generate(data_map) for mode, engine in engines.items()}

        # Exact output equality checks production defaults, including timing,
        # strike rounding, expiry, and the per-ticker position lockout.
        from bps_signal_engine import BullPutSpreadSignalEngine as ProductionEngine
        assert outputs["baseline"] == ProductionEngine().generate(data_map), "Baseline regression"
        for mode in ("vrp_only", "vrp_plus_baseline"):
            engine = engines[mode]
            assert all(entry["vrp_ratio"] >= engine.vrp_threshold for entry in engine.entries)
        assert all(entry["ema150"] is None and entry["z_ma150"] is None for entry in engines["baseline"].entries)
        assert all(entry["ema150"] is not None for entry in engines["vrp_only"].entries)

    # Ensure gate separation does not depend on a lucky real price trajectory.
    # Choose a real qualifying observation and place it on the sole eligible
    # signal bar. A steadily rising close has RSI=100, so baseline must reject;
    # it is above MA150, so vrp_only must accept the observed premium.
    qualifying = next(
        (code, pd.Timestamp(date), iv, hv)
        for code, history in cache.items()
        for date, iv, hv in zip(history.index, history["iv_current"], history["hv_current"])
        if (ratio := BullPutSpreadSignalEngine._vrp_ratio((iv, hv))) is not None
        and ratio >= engines["vrp_only"].vrp_threshold
    )
    code, signal_date, iv, hv = qualifying
    dates = pd.bdate_range(end=signal_date, periods=MIN_BARS + 1)
    assert dates[-1].date() == signal_date.date(), "Expected a weekday IV observation"
    dates = dates.append(pd.DatetimeIndex([signal_date + pd.offsets.BDay(1)]))
    closes = pd.Series([100.0 + i * 0.1 for i in range(len(dates))], index=dates)
    synthetic = pd.DataFrame({
        "open": closes - 0.05, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": 1_000_000,
    })
    data_map = {code: synthetic}
    print(f"Controlled gate separation: {code}, signal date {signal_date.date()}")
    outputs = {mode: engine.generate(data_map) for mode, engine in engines.items()}
    assert not outputs["baseline"], "Baseline should reject RSI=100"
    assert len(outputs["vrp_only"]) == 1, "VRP-only should accept above-MA150 with real VRP"
    assert outputs["vrp_only"] != outputs["baseline"], "Mode gates accidentally identical"
    assert not outputs["vrp_plus_baseline"], "Combined mode must retain baseline checks"
    for mode in ("vrp_only", "vrp_plus_baseline"):
        engine = engines[mode]
        assert all(entry["vrp_ratio"] >= engine.vrp_threshold for entry in engine.entries)
        assert all(entry["mode"] == mode for entry in engine.entries)
    signal = outputs["vrp_only"][0]
    assert signal["date"] == str(dates[-1].date()) and signal["price_mode"] == "open"
    assert signal["legs"][0]["strike"] - signal["legs"][1]["strike"] == SPREAD_WIDTH

    # Exercise missing ticker/date, NaNs, and invalid denominators entirely in
    # memory. A future-only observation must not satisfy the signal-date gate.
    engine = engines["vrp_only"]
    for history in (
        None,
        pd.DataFrame({"iv_current": [iv], "hv_current": [hv]}, index=[dates[-1]]),
        pd.DataFrame({"iv_current": [float("nan")], "hv_current": [hv]}, index=[signal_date]),
        pd.DataFrame({"iv_current": [iv], "hv_current": [float("nan")]}, index=[signal_date]),
        pd.DataFrame({"iv_current": [iv], "hv_current": [0.0]}, index=[signal_date]),
    ):
        engine._iv_hv_cache = {} if history is None else {code: history}
        assert not engine.generate(data_map), "Missing/invalid data must fail closed"
        assert engine.skipped_missing_iv == 1, "Missing-data count must reset per generate"

    # Synthetic series proving suppress_reentry: earlier qualifying bar (expiry
    # extends past later bar) plus later qualifying bar.
    # suppress_reentry=True (default): assert only earlier signal appears in engine.entries.
    # suppress_reentry=False: assert BOTH signals appear in engine.entries.
    reentry_dates = pd.bdate_range("2026-01-01", periods=MIN_BARS + 5)
    reentry_closes = pd.Series([100.0 + k * 0.1 for k in range(len(reentry_dates))], index=reentry_dates)
    reentry_df = pd.DataFrame({
        "open": reentry_closes - 0.05,
        "high": reentry_closes + 0.1,
        "low": reentry_closes - 0.1,
        "close": reentry_closes,
        "volume": 1_000_000,
    }, index=reentry_dates)
    bar_a_date = reentry_dates[MIN_BARS]      # index 150
    bar_b_date = reentry_dates[MIN_BARS + 2]  # index 152 (well within ~30 DTE expiry of bar A)
    reentry_history = pd.DataFrame(
        {"iv_current": [0.30, 0.30], "hv_current": [0.20, 0.20]},
        index=[bar_a_date, bar_b_date],
    )
    test_code = "REENTRY_TEST"
    reentry_map = {test_code: reentry_df}
    reentry_cache = {test_code: reentry_history}

    engine_suppressed = BullPutSpreadSignalEngine(mode="vrp_only", suppress_reentry=True)
    engine_suppressed._iv_hv_cache = reentry_cache
    engine_suppressed.generate(reentry_map)
    assert len(engine_suppressed.entries) == 1, (
        f"Expected 1 entry with suppress_reentry=True, got {len(engine_suppressed.entries)}"
    )
    assert engine_suppressed.entries[0]["date"] == str(reentry_dates[MIN_BARS + 1].date())

    engine_unsuppressed = BullPutSpreadSignalEngine(mode="vrp_only", suppress_reentry=False)
    engine_unsuppressed._iv_hv_cache = reentry_cache
    engine_unsuppressed.generate(reentry_map)
    assert len(engine_unsuppressed.entries) == 2, (
        f"Expected 2 entries with suppress_reentry=False, got {len(engine_unsuppressed.entries)}"
    )
    entry_dates = [e["date"] for e in engine_unsuppressed.entries]
    expected_dates = [str(reentry_dates[MIN_BARS + 1].date()), str(reentry_dates[MIN_BARS + 3].date())]
    assert entry_dates == expected_dates, f"Expected entries on {expected_dates}, got {entry_dates}"
    print(f"PASS: suppress_reentry verification (True={len(engine_suppressed.entries)}, False={len(engine_unsuppressed.entries)})")

    # Synthetic verification of z_ma150 math and baseline mode entries:
    synth_entry = engine_suppressed.entries[0]
    expected_tail = np.asarray(reentry_closes[:MIN_BARS + 1].tolist()[-21:], dtype=float)
    expected_sigma = float(np.std(np.log(expected_tail[1:] / expected_tail[:-1]), ddof=1))
    expected_ema150 = ema(reentry_closes[:MIN_BARS + 1].tolist(), 150)
    expected_close = float(reentry_closes.iloc[MIN_BARS])
    expected_z = (expected_close - expected_ema150) / (expected_close * expected_sigma)
    assert synth_entry["z_ma150"] is not None, "Expected z_ma150 to be computed"
    assert math.isclose(synth_entry["z_ma150"], expected_z, rel_tol=1e-9), (
        f"Expected z_ma150 {expected_z}, got {synth_entry['z_ma150']}"
    )
    assert math.isclose(synth_entry["ema150"], expected_ema150, rel_tol=1e-9)

    from unittest.mock import patch
    with patch.object(sys.modules[BullPutSpreadSignalEngine.__module__], "entry_checks", return_value=({}, True)):
        engine_baseline_synth = BullPutSpreadSignalEngine(mode="baseline")
        engine_baseline_synth.generate(reentry_map)
        assert len(engine_baseline_synth.entries) > 0, "Expected baseline synthetic entry"
        assert all(
            e["z_ma150"] is None and e["ema150"] is None
            for e in engine_baseline_synth.entries
        ), "Baseline synthetic entry must have z_ma150 and ema150 as None"
    print("PASS: z_ma150 calculation parity and baseline None-entry verification")

    # Verification of gate_on_vrp:
    for bad_mode in ("baseline", "vrp_plus_baseline"):
        try:
            BullPutSpreadSignalEngine(mode=bad_mode, gate_on_vrp=False)
            raise AssertionError(f"Expected ValueError for gate_on_vrp=False with mode={bad_mode}")
        except ValueError:
            pass

    engine_gated = BullPutSpreadSignalEngine(mode="vrp_only", gate_on_vrp=True, suppress_reentry=False)
    engine_ungated = BullPutSpreadSignalEngine(mode="vrp_only", gate_on_vrp=False, suppress_reentry=False)
    if ohlcv_path.exists():
        real_data_map = {real_code: prices[real_code]}
        engine_gated._iv_hv_cache = cache
        engine_ungated._iv_hv_cache = cache
        engine_gated.generate(real_data_map)
        engine_ungated.generate(real_data_map)
        gated_keys = {(e["code"], e["date"]): e for e in engine_gated.entries}
        ungated_keys = {(e["code"], e["date"]): e for e in engine_ungated.entries}
        assert set(gated_keys.keys()).issubset(set(ungated_keys.keys())), (
            "gate_on_vrp=False vrp_only entries must be a superset of gate_on_vrp=True"
        )
        assert len(ungated_keys) > len(gated_keys)
        for k, g_entry in gated_keys.items():
            u_entry = ungated_keys[k]
            assert (
                g_entry["z_ma150"] == u_entry["z_ma150"]
                or (g_entry["z_ma150"] is not None and u_entry["z_ma150"] is not None and math.isclose(g_entry["z_ma150"], u_entry["z_ma150"], rel_tol=1e-9))
            ), f"z_ma150 mismatch on shared entry {k}"
        print(f"PASS: gate_on_vrp superset verification on real data (gated={len(gated_keys)}, ungated={len(ungated_keys)})")

    synth_vrp_history = pd.DataFrame(
        {"iv_current": [0.30, 0.15], "hv_current": [0.20, 0.20]},
        index=[bar_a_date, bar_b_date],
    )
    synth_vrp_cache = {test_code: synth_vrp_history}
    synth_gated = BullPutSpreadSignalEngine(mode="vrp_only", gate_on_vrp=True, suppress_reentry=False)
    synth_gated._iv_hv_cache = synth_vrp_cache
    synth_gated.generate(reentry_map)
    synth_ungated = BullPutSpreadSignalEngine(mode="vrp_only", gate_on_vrp=False, suppress_reentry=False)
    synth_ungated._iv_hv_cache = synth_vrp_cache
    synth_ungated.generate(reentry_map)

    s_gated_keys = {(e["code"], e["date"]): e for e in synth_gated.entries}
    s_ungated_keys = {(e["code"], e["date"]): e for e in synth_ungated.entries}
    assert set(s_gated_keys.keys()).issubset(set(s_ungated_keys.keys())), (
        "gate_on_vrp=False must be superset of gate_on_vrp=True on synthetic series"
    )
    assert len(s_gated_keys) < len(s_ungated_keys)
    for k, g_entry in s_gated_keys.items():
        u_entry = s_ungated_keys[k]
        assert math.isclose(g_entry["z_ma150"], u_entry["z_ma150"], rel_tol=1e-9)
    b_entry = s_ungated_keys[(test_code, str(reentry_dates[MIN_BARS + 3].date()))]
    assert b_entry["vrp_ratio"] is not None and math.isclose(b_entry["vrp_ratio"], 0.75)
    print("PASS: gate_on_vrp synthetic superset and VRP recording verification")


if __name__ == "__main__":
    # Keep the explicit nonzero exit and a readable FAIL even for cache/setup
    # failures. Run normally (without -O) so Python's assertions remain enabled.
    try:
        _self_test()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    print("PASS: baseline parity, distinct gates, VRP threshold, and missing-data handling")
