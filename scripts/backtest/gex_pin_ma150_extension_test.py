"""Development-only GEX / EMA150 extension screen on a fixed VRP entry pool.

Implements research/proposals/2026-09-17-gex-pin-ma150-extension.md (third
revision), with the implementation request's frozen seed, block boundaries,
redraw cap, and verdict precedence: below 30 is INCONCLUSIVE, otherwise a
failed PASS condition is FAIL (including a CI containing zero).

Why: terminal short-strike breach avoids the regime-dependent production
exit label. It does not test safe decay, dealer causality, or production
exits. Cushion terciles diagnose, but do not remove, the cushion confound.
The 2026-09-14 BX SMA/EMA divergence is why we capture the engine's actual
EMA gate result, rather than reuse its emitted SMA strike anchor:
bps_signal_engine_v2.py:133-150, 159-198. One generate() call retains its
original reentry lockout across all eight filtered series.

Inputs/config reuse exit_aware_full_universe_backtest.py:98-120, 130-177.
Only its cached universe members are passed to CacheLoader: its live
yfinance fallback at lines 154-173 must never run here. GEX is parsed from
the existing local CSV, without load_series()'s network refresh. Pricing
uses options_portfolio.py:36-67, 142-162 and the $1.30 open-only convention
in run_exit_rule_ab_test.py:47. American is the inherited contract style;
the frozen holding policy overrides early exercise as well as early exits.
The portfolio simulator is therefore deliberately not called.

P&L gate compares mean dollars per signal (proposal Section 4: "average
credit collected"), not total portfolio dollars -- total dollars mechanically
falls for any selective filter regardless of per-trade quality and would
wrongly fail a config that improves every surviving trade. Total dollars are
also shown, informationally only, never gating. Daily log-return stdev uses
20 observations and
sample ddof=1, matching options_portfolio.py:134-135, without annualizing.
Tercile cuts are computed once on all scored baseline entries pooled over
IS and OOS; verdicts and bootstrap use OOS only. No threshold is tuned on IS.

Usage:
    python3 scripts/backtest/gex_pin_ma150_extension_test.py --self-test
    python3 scripts/backtest/gex_pin_ma150_extension_test.py
Real execution is for the user's registered run; self-tests use only
synthetic memory/tempdir inputs. No registration or deployment capability.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import math
import pickle
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

import bps_signal_engine_v2 as bps  # noqa: E402
import dix_fetcher_v2 as dix  # noqa: E402
import exit_aware_full_universe_backtest as full  # noqa: E402
from options_portfolio import bs_price, iv_smile_adjustment  # noqa: E402

OPTIONS_CONFIG = full.OPTIONS_CONFIG
IS_START, IS_END = full.IS_START, full.IS_END
OOS_START = full.OOS_START
OOS_END_ACTUAL = "2026-07-24"
HOLIDAYS_PATH = BASE.parent.parent / "us-market-holidays.txt"
Z_THRESHOLDS = (1.5, 2.0, 2.5)
COMMISSION_PER_SPREAD = 1.30
BLOCK_DAYS = 45
BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20260917  # Frozen before any run; never changed after seeing results.
MAX_REDRAWS = 200
ARM_NAMES = ("baseline", "gex_only", "extension_only", "combined")
PASS_CONCLUSION = (
    "PASS identifies a promising filter within this fixed historical candidate pool. "
    "It does not establish a dealer mechanism, does not validate production exit "
    "behavior, and does not authorize deployment."
)
EXCLUSIONS = ("excluded_oos_cutoff", "excluded_missing_terminal_price")


def print_input_hashes() -> None:
    """First real-run output, before loading or analyzing any observations."""
    for label, path in (("IV/HV", bps.DEFAULT_IV_HV_CACHE_PATH), ("OHLCV", full.CACHE)):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        print(f"INPUT SHA256 {label} {path}: {digest.hexdigest()}", flush=True)


def load_cached_universe() -> Dict[str, pd.DataFrame]:
    """Reuse the sibling's universe and exact OHLCV cleaning, cache-only."""
    codes = full.load_universe()
    with full.CACHE.open("rb") as handle:
        raw = pickle.load(handle)
    cached_codes = [code for code in codes if raw.get(code) is not None]
    missing = [code for code in codes if code not in cached_codes]
    del raw
    print(f"Universe cache omissions ({len(missing)}): {missing}", flush=True)
    return full.CacheLoader().fetch(cached_codes, full.START, full.END)


def generate_baseline(engine: bps.BullPutSpreadSignalEngine,
                      data_map: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
    """Thread actual gate EMA and signal-date IV/HV through one generate call.

    The read-only engine has no feature hook. Scoped wrappers observe its
    ema() return and successful _vrp_ratio() calls, without changing either
    return value. In vrp_only each successful ratio produces exactly one
    entry (bps_signal_engine_v2.py:159-198). Restore both hooks even on error.
    """
    if engine.mode != "vrp_only" or not engine.suppress_reentry:
        raise ValueError("The fixed pool requires vrp_only and suppress_reentry=True")
    original_ema, original_ratio = bps.ema, engine._vrp_ratio
    gate: Dict[str, Any] = {}
    accepted = []

    def capture_ema(closes, period):
        value = original_ema(closes, period)
        gate.clear()
        gate.update(ema150=value, signal_close=closes[-1], tail=closes[-21:])
        return value

    def capture_ratio(values):
        ratio = original_ratio(values)
        if ratio is not None and ratio >= engine.vrp_threshold:
            tail = np.asarray(gate["tail"], dtype=float)
            sigma = float(np.std(np.log(tail[1:] / tail[:-1]), ddof=1))
            close, ema150 = gate["signal_close"], gate["ema150"]
            # Undefined extension fails the extension filter closed, while
            # preserving the original candidate in baseline and GEX-only.
            z = (close - ema150) / (close * sigma) if sigma > 0 else math.nan
            accepted.append({"ema150": ema150, "signal_close": close,
                             "daily_log_stdev20": sigma, "z_ma150": z,
                             "iv_current": values[0], "hv_current": values[1]})
        return ratio

    with patch.object(bps, "ema", capture_ema), patch.object(engine, "_vrp_ratio", capture_ratio):
        signals = engine.generate(data_map)
    if len(accepted) != len(engine.entries) or len(signals) != len(accepted):
        raise RuntimeError("Entry engine changed: captured features no longer align with entries")
    entries = []
    for entry, features, signal in zip(engine.entries, accepted, signals):
        if (entry["signal_close"] != features["signal_close"]
                or (entry["code"], entry["date"]) != (signal["underlying"], signal["date"])
                or signal["price_mode"] != "open"):
            raise RuntimeError("Entry engine changed: signal/feature alignment failed")
        row = {**entry, **features}
        frame = data_map[row["code"]].sort_index()
        entry_idx = frame.index.get_loc(pd.Timestamp(row["date"]))
        row["signal_date"] = str(frame.index[entry_idx - 1].date())
        row["entry_open"] = float(frame.iloc[entry_idx]["open"])
        row["strike_cushion"] = row["signal_close"] - row["short_strike"]
        row["cushion_pct"] = row["strike_cushion"] / row["signal_close"]
        entries.append(row)
    return entries


def load_holidays(path: Path) -> set[pd.Timestamp]:
    return {pd.Timestamp(line.strip()) for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def is_market_wide_closure(date: pd.Timestamp, holidays: set[pd.Timestamp],
                          market_sessions: set[pd.Timestamp]) -> bool:
    """Holiday-file years are authoritative; other years use pooled sessions.

    A single cached ticker trading on a weekday establishes a market session.
    Its absence from another ticker is a data gap, never a market closure.
    """
    if date.weekday() >= 5:
        return False
    if date.year in {holiday.year for holiday in holidays}:
        return date in holidays
    return date not in market_sessions


def resolve_expiry(nominal: str, holidays: set[pd.Timestamp],
                   market_sessions: set[pd.Timestamp]) -> pd.Timestamp:
    session = pd.Timestamp(nominal)
    while session.weekday() >= 5 or is_market_wide_closure(session, holidays, market_sessions):
        if session.year not in {holiday.year for holiday in holidays}:
            if not market_sessions or session < min(market_sessions):
                raise RuntimeError(f"No pooled calendar coverage to resolve expiry {nominal}")
        session -= pd.Timedelta(days=1)
    return session


def score_trade(entry: Dict[str, Any], frame: pd.DataFrame,
                holidays: set[pd.Timestamp],
                market_sessions: set[pd.Timestamp]) -> Dict[str, Any]:
    """Exact terminal session only; no early exit, assignment, or stale mark."""
    expiry = resolve_expiry(entry["expiry"], holidays, market_sessions)
    row = {**entry, "modeled_expiry": str(expiry.date()), "exclusion": ""}
    if expiry > pd.Timestamp(OOS_END_ACTUAL):
        row["exclusion"] = "excluded_oos_cutoff"
        return row
    if expiry not in frame.index or not math.isfinite(float(frame.at[expiry, "close"])):
        row["exclusion"] = "excluded_missing_terminal_price"
        return row
    terminal = float(frame.at[expiry, "close"])
    short, long = entry["short_strike"], entry["long_strike"]
    spot, iv = entry["entry_open"], entry["iv_current"]
    # Preserve the engine's nominal contract maturity for entry valuation;
    # only settlement moves back when the market is closed on that Friday.
    tenor = max((pd.Timestamp(entry["expiry"]) - pd.Timestamp(entry["date"])).days / 365.0, 0.001)
    prices = [bs_price(spot, strike, tenor, OPTIONS_CONFIG["risk_free_rate"],
                       iv_smile_adjustment(spot, strike, iv, OPTIONS_CONFIG["iv_skew"],
                                           OPTIONS_CONFIG["iv_curvature"]), "put")
              for strike in (short, long)]
    credit = prices[0] - prices[1]
    holding = frame.loc[(frame.index >= pd.Timestamp(entry["date"])) & (frame.index <= expiry), "close"]
    row.update(close_at_expiry=terminal, breach_at_expiry=terminal < short,
               ever_breached_intraperiod=bool((holding < short).any()), credit=credit,
               pnl=(credit - max(short - terminal, 0.0) + max(long - terminal, 0.0))
               * OPTIONS_CONFIG["contract_multiplier"] - COMMISSION_PER_SPREAD)
    return row


def attach_gex_regime(entry: Dict[str, Any], gex_series: pd.DataFrame,
                       regimes: Dict[str, Dict[str, Any]]) -> None:
    """Mutate entry with its GEX regime, read as of the SIGNAL date.

    Every other input in this experiment (VRP/IV, the EMA gate itself) is
    evaluated at the signal bar; the fill happens the next bar. Reading GEX
    at entry["date"] (the fill date) instead of entry["signal_date"] would
    admit one extra session of GEX data the signal itself never saw -- a
    different design than the one this proposal specified and reviewed, even
    though it is not a look-ahead violation relative to real order placement
    (Astra's correction, 2026-09-17). `regimes` memoizes by signal_date
    across entries sharing one.
    """
    signal_date = entry["signal_date"]
    if signal_date not in regimes:
        regimes[signal_date] = dix.gex_regime(gex_series, signal_date)
    entry["gex_regime"] = regimes[signal_date]["regime"]


def arm_masks(rows: pd.DataFrame, threshold: float) -> Dict[str, np.ndarray]:
    positive = rows["gex_regime"].to_numpy() == "POSITIVE"
    extended = rows["z_ma150"].to_numpy() >= threshold
    return dict(baseline=np.ones(len(rows), dtype=bool), gex_only=positive,
                extension_only=extended, combined=positive & extended)


def summarize(rows: pd.DataFrame) -> Dict[str, Any]:
    scored = rows[rows["exclusion"] == ""]
    n = len(scored)
    return {"n": n, "candidates": len(rows),
            **{reason: int((rows["exclusion"] == reason).sum()) for reason in EXCLUSIONS},
            "breach_rate": float(scored["breach_at_expiry"].mean()) if n else math.nan,
            "ever_rate": float(scored["ever_breached_intraperiod"].mean()) if n else math.nan,
            "total_pnl": float(scored["pnl"].sum()),
            "mean_pnl": float(scored["pnl"].mean()) if n else math.nan}


def block_tables(rows: pd.DataFrame, threshold: float):
    """Count each trade in every overlapping [start, start+45d) block.

    Starts include both frozen endpoints. Repeated drawn blocks repeat their
    trades; there is no deduplication or truncation of the final drawn block.
    """
    start, end = pd.Timestamp(OOS_START), pd.Timestamp(OOS_END_ACTUAL)
    starts = pd.date_range(start, end - pd.Timedelta(days=BLOCK_DAYS), freq="D")
    dates = pd.to_datetime(rows["date"]).to_numpy()
    membership = ((dates[None, :] >= starts.to_numpy()[:, None])
                  & (dates[None, :] < (starts + pd.Timedelta(days=BLOCK_DAYS)).to_numpy()[:, None]))
    masks = np.column_stack(list(arm_masks(rows, threshold).values())).astype(np.int64)
    counts = membership.astype(np.int64) @ masks
    breaches = membership.astype(np.int64) @ (masks * rows["breach_at_expiry"].to_numpy(dtype=np.int64)[:, None])
    n_blocks = math.ceil(((end - start).days + 1) / BLOCK_DAYS)
    return starts, counts, breaches, n_blocks


def bootstrap_iteration(rng, starts, counts, breaches, n_blocks, threshold, iteration,
                        draw=None):
    """One shared draw for all four arms, then at most 200 replacement draws."""
    if draw is None:
        draw = lambda: rng.integers(0, len(starts), size=n_blocks)
    for attempt in range(MAX_REDRAWS + 1):
        chosen = draw()
        totals = counts[chosen].sum(axis=0)
        if totals[3] > 0:
            rates = breaches[chosen].sum(axis=0) / totals
            return rates, starts[chosen], attempt
    raise RuntimeError(f"z_threshold={threshold}: bootstrap iteration {iteration} "
                       f"has zero combined-arm trades after {MAX_REDRAWS} redraw attempts")


def bootstrap(rows: pd.DataFrame, threshold: float) -> tuple[float, float]:
    starts, counts, breaches, n_blocks = block_tables(rows, threshold)
    # Restart the identical literal seed for each threshold, never one RNG
    # shared sequentially across thresholds. Pairing is within each draw.
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    improvements = np.empty(BOOTSTRAP_REPS)
    for iteration in range(BOOTSTRAP_REPS):
        rates, _, _ = bootstrap_iteration(rng, starts, counts, breaches, n_blocks,
                                          threshold, iteration)
        improvements[iteration] = rates[0] - rates[3]
    low, high = np.percentile(improvements, [2.5, 97.5])
    return float(low), float(high)


def verdict(stats: Dict[str, Dict[str, Any]], ci: tuple[float, float]) -> str:
    baseline, combined = stats["baseline"], stats["combined"]
    if combined["n"] < 30:
        return "INCONCLUSIVE"
    # Proposal Section 7, verbatim: "INCONCLUSIVE: n_combined < 30, or the
    # bootstrap CI includes zero." An earlier implementation pass collapsed
    # this into a bare n<30 check and treated a zero-including CI as FAIL --
    # that was an error in the delegation prompt's restatement, not a
    # considered deviation from the reviewed proposal; reverted (Astra's
    # correction, 2026-09-17). A CI including zero means insufficient
    # evidence either way, not a rejected hypothesis.
    if ci[0] <= 0 <= ci[1]:
        return "INCONCLUSIVE"
    improvement = baseline["breach_rate"] - combined["breach_rate"]
    # ci[0] > 0 is explicit and load-bearing, not implied by "not INCONCLUSIVE"
    # above: that check only rules out a CI straddling zero, and an entirely
    # negative CI also skips it. improvement here is a point estimate computed
    # directly from full-pool breach rates, independent of the bootstrap
    # distribution -- it can disagree with a negative bootstrap CI (block
    # resampling weights differently than naive pooling), so without this
    # explicit guard a case with improvement >= 5pp but a confidently negative
    # CI would wrongly PASS (reproduced with ci=(-0.08, -0.01); Astra's
    # correction, 2026-09-17).
    if (ci[0] > 0 and improvement >= 0.05
            and combined["breach_rate"] < stats["extension_only"]["breach_rate"]
            and combined["breach_rate"] < stats["gex_only"]["breach_rate"]
            and combined["mean_pnl"] >= baseline["mean_pnl"]):
        return "PASS"
    return "FAIL"


def print_verdict(threshold: float, stats: Dict[str, Dict[str, Any]],
                  ci: tuple[float, float]) -> str:
    label = verdict(stats, ci)
    baseline, combined = stats["baseline"], stats["combined"]
    improvement = baseline["breach_rate"] - combined["breach_rate"]
    print(f"z={threshold:.1f}: {label}; n_combined={combined['n']}; "
          f"improvement={improvement:.2%}; 95% CI=[{ci[0]:.2%}, {ci[1]:.2%}]")
    if label == "PASS":
        print(PASS_CONCLUSION)
        # Flag any worsening, so no unregistered 'much worse' cutoff hides it.
        if combined["ever_rate"] > baseline["ever_rate"]:
            print("FLAG: terminal outcomes improve but intraperiod breach worsens "
                  f"({baseline['ever_rate']:.2%} -> {combined['ever_rate']:.2%}); "
                  "this does not support the original safe-decay claim.")
    return label


def report_arm(label: str, rows: pd.DataFrame, cuts: np.ndarray) -> Dict[str, Any]:
    stats = summarize(rows)
    print(f"{label}: candidates={stats['candidates']} n={stats['n']} "
          f"excluded_oos_cutoff={stats['excluded_oos_cutoff']} "
          f"excluded_missing_terminal_price={stats['excluded_missing_terminal_price']} "
          f"breach_at_expiry={stats['breach_rate']:.2%} "
          f"ever_breached_intraperiod={stats['ever_rate']:.2%} "
          f"total_pnl=${stats['total_pnl']:.2f} mean_pnl=${stats['mean_pnl']:.2f}")
    scored = rows[rows["exclusion"] == ""]
    # Ties stay together: low <= q1, q1 < middle <= q2, high > q2.
    bins = np.searchsorted(cuts, scored["cushion_pct"].to_numpy(), side="left")
    for bucket, name in enumerate(("low", "middle", "high")):
        subset = scored.iloc[np.flatnonzero(bins == bucket)]
        rate = float(subset["breach_at_expiry"].mean()) if len(subset) else math.nan
        print(f"  cushion_pct {name}: n={len(subset)} breach_at_expiry={rate:.2%}")
    return stats


def run() -> None:
    print_input_hashes()
    print("Development: fixed explored historical pool; exploratory three-threshold "
          "sweep; no Bonferroni correction. Eight series; no interaction claim.")
    print(f"IS={IS_START}..{IS_END}; OOS={OOS_START}..{OOS_END_ACTUAL}; "
          f"bootstrap seed={BOOTSTRAP_SEED}, reps={BOOTSTRAP_REPS}, block_days={BLOCK_DAYS}")
    print(f"OPTIONS_CONFIG={OPTIONS_CONFIG}; one contract/signal; open-only commission=$1.30; "
          "hold to expiry; theoretical mid fills; P&L gate uses per-trade mean dollars "
          "(total dollars shown informationally only, never gating).")
    data_map = load_cached_universe()
    holidays = load_holidays(HOLIDAYS_PATH)
    market_sessions = {session for prices in data_map.values() for session in prices.index}
    print(f"Calendar: holiday file authoritative for {sorted({day.year for day in holidays})}; "
          "other years use weekday sessions pooled across the loaded cached universe.")
    gex_series = dix._parse_dix_dataframe(dix.DEFAULT_CACHE_PATH)
    engine = bps.BullPutSpreadSignalEngine(mode="vrp_only", suppress_reentry=True)
    entries = generate_baseline(engine, data_map)
    regimes: Dict[str, Dict[str, Any]] = {}
    rows = []
    for entry in entries:
        if not IS_START <= entry["date"] <= OOS_END_ACTUAL:
            continue
        attach_gex_regime(entry, gex_series, regimes)
        rows.append(score_trade(entry, data_map[entry["code"]], holidays, market_sessions))
    if not rows:
        raise RuntimeError("No baseline candidates in the frozen historical windows")
    frame = pd.DataFrame(rows)
    print(f"Undefined z_ma150 (zero/nonfinite stdev): {int(frame['z_ma150'].isna().sum())}; "
          "retained in baseline, fails extension filters closed.")
    # Keep metric columns even if every candidate was censored.
    for column in ("breach_at_expiry", "ever_breached_intraperiod", "pnl"):
        if column not in frame:
            frame[column] = np.nan
    pooled = frame[frame["exclusion"] == ""]
    if pooled.empty:
        raise RuntimeError("No scored baseline trades after terminal-price exclusions")
    cuts = np.quantile(pooled["cushion_pct"], [1 / 3, 2 / 3])
    print(f"Pooled scored baseline cushion_pct terciles: {cuts.tolist()}; ties stay together.")
    print("Cushion stratification is a diagnostic, not control or removal of the confound.")
    print("45-day dependence approximation is fixed; borderline CIs remain borderline, "
          "with no block-length retuning.")
    oos_stats = {}
    for window, start, end in (("IS", IS_START, IS_END), ("OOS", OOS_START, OOS_END_ACTUAL)):
        subset = frame[(frame["date"] >= start) & (frame["date"] <= end)]
        masks = arm_masks(subset, Z_THRESHOLDS[0])
        common = {name: report_arm(f"{window} {name}", subset.loc[masks[name]], cuts)
                  for name in ("baseline", "gex_only")}
        for threshold in Z_THRESHOLDS:
            masks = arm_masks(subset, threshold)
            stats = {**common, **{
                name: report_arm(f"{window} z={threshold:.1f} {name}", subset.loc[masks[name]], cuts)
                for name in ("extension_only", "combined")}}
            if window == "OOS":
                oos_stats[threshold] = stats
    oos = pooled[(pooled["date"] >= OOS_START) & (pooled["date"] <= OOS_END_ACTUAL)]
    labels = {}
    for threshold in Z_THRESHOLDS:
        ci = bootstrap(oos, threshold)
        labels[threshold] = print_verdict(threshold, oos_stats[threshold], ci)
    passing = [threshold for threshold, label in labels.items() if label == "PASS"]
    if passing:
        print(f"Accepted screening candidate: lowest passing z={min(passing):.1f}")
    elif all(label == "INCONCLUSIVE" for label in labels.values()):
        print("All thresholds INCONCLUSIVE: final outcome; no further sweep.")
    else:
        print("No passing threshold: hypothesis rejected under this design; no further sweep.")


def _self_test() -> None:
    """Named synthetic fixtures only; never open a real research data file."""
    def fixture_entry(expiry="2026-04-03"):
        return dict(code="TEST", date="2026-03-02", expiry=expiry,
                    entry_open=110.0, signal_close=109.0, iv_current=0.30,
                    short_strike=100.0, long_strike=90.0, z_ma150=3.0,
                    cushion_pct=9 / 109, gex_regime="POSITIVE")

    def holiday_expiry():
        # Good Friday 2026 is present in the repo table; copy only this date
        # into a temp fixture so --self-test never reads the real table.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "us-market-holidays.txt"
            path.write_text("# --- 2026 ---\n2026-04-03\n")
            holidays = load_holidays(path)
            frame = pd.DataFrame({"close": [105.0, 80.0]},
                                 index=pd.to_datetime(["2026-04-02", "2026-04-06"]))
            result = score_trade(fixture_entry(), frame, holidays, set(frame.index))
            assert result["modeled_expiry"] == "2026-04-02"
            assert result["close_at_expiry"] == 105.0 and not result["breach_at_expiry"]

    def missing_terminal():
        frame = pd.DataFrame({"close": [105.0, 80.0]},
                             index=pd.to_datetime(["2026-04-01", "2026-04-06"]))
        result = score_trade(fixture_entry(), frame, {pd.Timestamp("2026-04-03")}, set(frame.index))
        assert result["modeled_expiry"] == "2026-04-02"
        assert result["exclusion"] == "excluded_missing_terminal_price"
        assert "close_at_expiry" not in result and "pnl" not in result

    def cutoff():
        frame = pd.DataFrame({"close": [105.0, 80.0]},
                             index=pd.to_datetime([OOS_END_ACTUAL, "2026-07-31"]))
        result = score_trade(fixture_entry("2026-07-31"), frame, set(), set(frame.index))
        assert result["exclusion"] == "excluded_oos_cutoff"
        assert "breach_at_expiry" not in result and "pnl" not in result

    def synthetic_arms(kind="PASS"):
        # All four disjoint membership cells share dates. Overlapping arm
        # rates are thus deterministic for any nonempty bootstrap draw.
        records = []
        n = 20 if kind == "INCONCLUSIVE" else 40
        for i in range(n):
            date = str((pd.Timestamp("2025-06-02") + pd.Timedelta(days=i % 10)).date())
            for extended, positive in ((True, True), (True, False), (False, True), (False, False)):
                # FAIL: invert the PASS pattern so combined breaches MORE than
                # baseline (confidently negative improvement) rather than
                # uniform breach=True everywhere, which collapses every arm to
                # an identical rate and produces a degenerate CI=[0, 0] --
                # indistinguishable from "CI includes zero" (INCONCLUSIVE),
                # not the clear-negative-effect FAIL this fixture means to test.
                breach = (extended and positive) if kind == "FAIL" else not (extended and positive)
                records.append(dict(date=date, z_ma150=3.0 if extended else 1.0,
                                    gex_regime="POSITIVE" if positive else "NEGATIVE",
                                    breach_at_expiry=breach, ever_breached_intraperiod=True,
                                    pnl=-10.0 if breach else 10.0, exclusion="", cushion_pct=0.1))
        return pd.DataFrame(records)

    def shared_resampling():
        rows = synthetic_arms()
        # Vary breach outcomes by both date and arm, so independent arm
        # draws cannot pass merely because all blocks have identical rates.
        rows["breach_at_expiry"] = (np.arange(len(rows)) % 13) < 5
        starts, counts, breaches, n_blocks = block_tables(rows, 2.0)
        assert starts[0] == pd.Timestamp(OOS_START)
        assert starts[-1] == pd.Timestamp(OOS_END_ACTUAL) - pd.Timedelta(days=45)
        assert n_blocks == math.ceil(((pd.Timestamp(OOS_END_ACTUAL) - pd.Timestamp(OOS_START)).days + 1) / 45)
        args = (starts, counts, breaches, n_blocks, 2.0, 0)
        rng = np.random.default_rng(BOOTSTRAP_SEED)
        rates, drawn, _ = bootstrap_iteration(rng, *args)
        rates_again, drawn_again, _ = bootstrap_iteration(np.random.default_rng(BOOTSTRAP_SEED), *args)
        assert drawn.equals(drawn_again)
        np.testing.assert_array_equal(rates, rates_again)
        # Independently reconstruct multiplicities using those exact starts
        # for each arm, including repeated starts and overlapping blocks.
        dates = pd.to_datetime(rows["date"])
        weights = sum(((dates >= start) & (dates < start + pd.Timedelta(days=45))).astype(int)
                      for start in drawn)
        for index, mask in enumerate(arm_masks(rows, 2.0).values()):
            expected = np.dot(weights[mask], rows.loc[mask, "breach_at_expiry"]) / weights[mask].sum()
            assert rates[index] == expected

    def check_verdict(kind):
        rows = synthetic_arms(kind)
        stats = {name: summarize(rows.loc[mask]) for name, mask in arm_masks(rows, 2.0).items()}
        ci = bootstrap(rows, 2.0)
        assert verdict(stats, ci) == kind
        output = io.StringIO()
        with redirect_stdout(output):
            assert print_verdict(2.0, stats, ci) == kind
        assert (PASS_CONCLUSION in output.getvalue()) == (kind == "PASS")
        if kind == "PASS":
            assert verdict(stats, (-0.01, 0.8)) == "INCONCLUSIVE", (
                "Proposal Section 7: a CI including zero is INCONCLUSIVE, not FAIL"
            )
            assert verdict(stats, (-0.08, -0.01)) == "FAIL", (
                "An entirely negative CI must FAIL even when the point-estimate "
                "improvement alone clears 5pp -- ci[0] > 0 is required explicitly "
                "in the PASS branch, not implied by 'not INCONCLUSIVE' (Astra's "
                "reproduction, 2026-09-17)"
            )
            for single in ("extension_only", "gex_only"):
                changed = {name: dict(values) for name, values in stats.items()}
                changed[single]["breach_rate"] = changed["combined"]["breach_rate"]
                assert verdict(changed, ci) == "FAIL", "Both singles must be strictly beaten"
            changed = {name: dict(values) for name, values in stats.items()}
            changed["combined"]["mean_pnl"] = changed["baseline"]["mean_pnl"] - 1
            assert verdict(changed, ci) == "FAIL", "Per-trade mean P&L gates, not total dollars"

    def zero_redraw():
        starts = pd.to_datetime(["2025-01-27", "2025-06-02"])
        counts = np.array([[1, 1, 1, 0], [4, 2, 2, 1]])
        breaches = np.array([[1, 1, 1, 0], [3, 1, 1, 0]])
        choices = iter([np.array([0]), np.array([1])])
        rates, drawn, retries = bootstrap_iteration(None, starts, counts, breaches, 1,
                                                     2.5, 7, draw=lambda: next(choices))
        assert retries == 1 and drawn[0] == starts[1] and rates[3] == 0
        calls = 0

        def always_empty():
            nonlocal calls
            calls += 1
            return np.array([0])

        try:
            bootstrap_iteration(None, starts, counts, breaches, 1, 2.5, 7, draw=always_empty)
        except RuntimeError as exc:
            assert "2.5" in str(exc) and "iteration 7" in str(exc) and "200" in str(exc)
            assert calls == 201, "Initial draw plus exactly 200 redraws"
        else:
            raise AssertionError("Expected RuntimeError after exhausted redraws")

    def ema_threading():
        with tempfile.TemporaryDirectory() as tmp:
            dates = pd.bdate_range("2024-01-02", periods=bps.MIN_BARS + 45)
            close = 100 + np.arange(len(dates)) * 0.1 + np.sin(np.arange(len(dates))) * 0.02
            frame = pd.DataFrame(dict(open=close + 0.2, high=close + 0.3, low=close - 0.1,
                                      close=close, volume=1000), index=dates)
            history = pd.DataFrame(dict(iv_current=0.30, hv_current=0.20), index=dates)
            cache = Path(tmp) / "vol.pkl"
            cache.write_bytes(pickle.dumps({"TEST": history}))
            engine = bps.BullPutSpreadSignalEngine(mode="vrp_only", suppress_reentry=True,
                                                  iv_hv_cache_path=cache)
            original = bps.ema
            captured = []

            def observe(closes, period):
                value = original(closes, period)
                captured.append(value)
                return value

            with redirect_stdout(io.StringIO()), patch.object(bps, "ema", observe), \
                    patch.object(engine, "generate", wraps=engine.generate) as generated:
                entries = generate_baseline(engine, {"TEST": frame})
                assert generated.call_count == 1
            assert len(entries) >= 2 and len(captured) == len(entries)
            assert [entry["ema150"] for entry in entries] == captured
            assert bps.ema is original
            for entry in entries:
                idx = dates.get_loc(pd.Timestamp(entry["signal_date"]))
                sigma = np.std(np.diff(np.log(close[idx - 20:idx + 1])), ddof=1)
                assert math.isclose(entry["daily_log_stdev20"], sigma, rel_tol=1e-9)
                assert entry["signal_close"] == close[idx]
                assert entry["entry_open"] == frame.iloc[idx + 1]["open"]
                assert entry["iv_current"] == 0.30
                assert entry["ema150"] != entry["ma150"]
                assert math.isclose(entry["z_ma150"], (close[idx] - entry["ema150"]) / (close[idx] * sigma), rel_tol=1e-9)
            # A fill-date-only observation must not rescue missing signal IV.
            engine._iv_hv_cache = {"TEST": history.iloc[[-1]]}
            with redirect_stdout(io.StringIO()):
                assert generate_baseline(engine, {"TEST": frame}) == []

    def hold_pnl():
        entry = fixture_entry("2026-04-02")
        frame = pd.DataFrame({"close": [80.0, 105.0]},
                             index=pd.to_datetime([entry["date"], entry["expiry"]]))
        result = score_trade(entry, frame, set(), set(frame.index))
        assert result["ever_breached_intraperiod"] and not result["breach_at_expiry"]
        assert math.isclose(result["pnl"], result["credit"] * 100 - 1.30)
        frame.at[pd.Timestamp(entry["expiry"]), "close"] = 80
        loss = score_trade(entry, frame, set(), set(frame.index))
        assert math.isclose(loss["pnl"], result["pnl"] - 1000)
        frame.at[pd.Timestamp(entry["expiry"]), "close"] = 100
        assert not score_trade(entry, frame, set(), set(frame.index))["breach_at_expiry"]

    def hybrid_calendar():
        holidays = {pd.Timestamp("2026-04-03"), pd.Timestamp("2027-03-26")}
        friday, thursday = pd.Timestamp("2024-03-29"), pd.Timestamp("2024-03-28")
        own = pd.DataFrame({"close": [105.0, 80.0]},
                           index=pd.to_datetime([thursday, "2024-04-01"]))
        universe = {"A": own.copy(), "B": own.copy(), "GAP": own.copy()}
        pooled = {day for frame in universe.values() for day in frame.index}
        assert is_market_wide_closure(friday, holidays, pooled)
        assert resolve_expiry(str(friday.date()), holidays, pooled) == thursday
        entry = {**fixture_entry(str(friday.date())), "date": "2024-02-26"}
        result = score_trade(entry, own, holidays, pooled)
        assert result["modeled_expiry"] == "2024-03-28" and result["exclusion"] == ""
        for ticker in ("A", "B"):
            universe[ticker].loc[friday, "close"] = 106.0
        pooled = {day for frame in universe.values() for day in frame.index}
        assert not is_market_wide_closure(friday, holidays, pooled)
        result = score_trade(entry, own, holidays, pooled)
        assert result["modeled_expiry"] == "2024-03-29"
        assert result["exclusion"] == "excluded_missing_terminal_price"
        assert "close_at_expiry" not in result
        # Even one ticker suffices, and covered years ignore pooled data.
        assert not is_market_wide_closure(friday, holidays, {friday})
        assert is_market_wide_closure(pd.Timestamp("2026-04-03"), holidays,
                                     {pd.Timestamp("2026-04-03")})
        assert not is_market_wide_closure(pd.Timestamp("2026-04-02"), holidays, set())

    def cache_provenance_and_terciles():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache, iv_path, universe = root / "ohlcv.pkl", root / "iv.pkl", root / "universe.csv"
            prices = pd.DataFrame(dict(open=[100.0], high=[101.0], low=[99.0],
                                       close=[100.0], volume=[1000], ma150=[math.nan]),
                                  index=pd.to_datetime(["2025-06-02"]))
            cache.write_bytes(pickle.dumps({"CACHED": prices}))
            iv_path.write_bytes(pickle.dumps({}))
            universe.write_text("ticker\nCACHED\nMISSING\n")
            output = io.StringIO()
            with patch.object(full, "CACHE", cache), patch.object(full, "UNIVERSE_CSV", universe), \
                    patch.object(bps, "DEFAULT_IV_HV_CACHE_PATH", iv_path), redirect_stdout(output):
                print_input_hashes()
                loaded = load_cached_universe()
            lines = output.getvalue().splitlines()
            assert "INPUT SHA256 IV/HV" in lines[0] and "INPUT SHA256 OHLCV" in lines[1]
            assert hashlib.sha256(iv_path.read_bytes()).hexdigest() in lines[0]
            assert hashlib.sha256(cache.read_bytes()).hexdigest() in lines[1]
            assert list(loaded) == ["CACHED"] and len(loaded["CACHED"]) == 1
            assert list(loaded["CACHED"].columns) == ["open", "high", "low", "close", "volume"]
            assert "MISSING" in output.getvalue()
        rows = synthetic_arms()
        rows["cushion_pct"] = np.arange(len(rows)) / len(rows)
        cuts = np.quantile(rows["cushion_pct"], [1 / 3, 2 / 3])
        output = io.StringIO()
        with redirect_stdout(output):
            for name, mask in arm_masks(rows, 2.0).items():
                report_arm(name, rows.loc[mask], cuts)
            report_arm("empty", rows.iloc[:0], cuts)
        for label in ("low", "middle", "high"):
            assert output.getvalue().count(f"cushion_pct {label}:") == 5

    def gex_signal_timing():
        # dates[0..22]: low background (1.0). dates[23]: a high spike (1000.0)
        # -- the session evaluated when as-of is SIGNAL_date, ranking above
        # its all-low trailing window -> POSITIVE. dates[24] (signal_date's
        # own row): a low outlier (0.5) -- the session evaluated when as-of
        # is ENTRY_date one day later, ranking below everything in its
        # trailing window (which now includes the 1000.0 spike too) ->
        # NEGATIVE. Uniform values were tried first and rejected: a
        # percentile rank of "fraction strictly lower" is 0.0 for every row
        # in a uniform series regardless of which row is evaluated, so it
        # cannot distinguish signal-date from entry-date evaluation at all.
        dates = pd.bdate_range("2025-03-03", periods=26)
        signal_date, entry_date = dates[24], dates[25]
        values = [1.0] * 23 + [1000.0, 0.5]
        series = pd.DataFrame({"gex": values}, index=dates[:25])
        entry = {"signal_date": str(signal_date.date()), "date": str(entry_date.date())}
        # Prove the fixture actually flips the regime under an entry-date
        # read (sanity check on the fixture itself, not the fix under test).
        assert dix.gex_regime(series, entry_date)["regime"] == "NEGATIVE"
        assert dix.gex_regime(series, signal_date)["regime"] == "POSITIVE"
        attach_gex_regime(entry, series, {})
        assert entry["gex_regime"] == "POSITIVE", (
            "attach_gex_regime must read GEX at signal_date, not date (entry/fill date)"
        )

    cases = [("Holiday expiry", holiday_expiry),
             ("Missing terminal price (data gap)", missing_terminal),
             ("OOS cutoff exclusion", cutoff),
             ("Shared resampling determinism", shared_resampling),
             ("PASS verdict and printed narrow conclusion", lambda: check_verdict("PASS")),
             ("FAIL verdict", lambda: check_verdict("FAIL")),
             ("INCONCLUSIVE verdict", lambda: check_verdict("INCONCLUSIVE")),
             ("Zero-combined-arm successful redraw and exhausted cap", zero_redraw),
             ("Hybrid calendar: 2024 closure versus single-ticker gap", hybrid_calendar),
             ("GEX read at signal date, not entry/fill date", gex_signal_timing),
             ("Single entry generation, EMA threading, signal-date IV", ema_threading),
             ("Hold-to-expiry P&L and intraperiod breach", hold_pnl),
             ("Cache-only loader, input hashes, unconditional terciles", cache_provenance_and_terciles)]
    failures = 0
    for name, case in cases:
        try:
            case()
            print(f"PASS: {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL: {name}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run synthetic fixtures only")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
    else:
        run()


if __name__ == "__main__":
    main()
