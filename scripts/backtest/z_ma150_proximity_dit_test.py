"""Development-only z_ma150 proximity and DIT screen on a fixed VRP entry pool.

Implements research/proposals/2026-09-18-z-ma150-proximity-dit.md (fourth draft).
Evaluates whether entries near EMA150 (0 < z_ma150 <= 0.50) during POSITIVE GEX
reach the 80% profit-capture target within 10 calendar days faster than the
unfiltered-by-proximity comparator (GEX-only), without materially worse terminal
breach, intraperiod breach, or eventual reach rates.

Key characteristics:
- Daily reprice loop: frozen signal-date base IV; smile adjustment recomputed
  per session from that session's own close; nominal-expiry-based tenor for
  pre-settlement Black-Scholes marks; pure intrinsic settlement (no BS mark)
  at the modeled-expiry session specifically.
- Whole-cohort reach_by_10 and reach_by_expiry for all eligible candidates
  after declared exclusions (excluded_oos_cutoff, excluded_missing_terminal_price,
  excluded_missing_midperiod_price).
- Proximity arm masks use '<=' (never the frozen file's '>=' arm_masks).
- Five-gate bootstrap CI structure (Gates A-E) with disjoint branch ordering
  (PASS, else FAIL, else INCONCLUSIVE) and n<30 hard short-circuit.
- Gate E (eventual reach) uses higher-is-better subtraction direction:
  (GEXonly_reach_rate - combined_reach_rate).
- 2x2 contingency table diagnostic over OOS baseline pool.
- Margins (margin_breach, margin_intraperiod, margin_reach) are explicitly
  unset user risk-tolerance decisions (Section 8); compute_verdict structurally
  refuses to compute a verdict without them.
- Self-test suite with named synthetic fixtures (--self-test).

Usage:
    scripts/backtest/.venv/bin/python3 scripts/backtest/z_ma150_proximity_dit_test.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import pickle
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

import bps_signal_engine_v2 as bps  # noqa: E402
from compute_exit_signal_v2 import PROFIT_TARGET_THRESHOLD  # noqa: E402
import dix_fetcher_v2 as dix  # noqa: E402
import exit_aware_full_universe_backtest as full  # noqa: E402
from options_portfolio import bs_price, iv_smile_adjustment  # noqa: E402

# Import shared baseline generation and calendar/scoring helpers from frozen script.
# CRITICAL: arm_masks(), block_tables(), bootstrap(), verdict() from the frozen script
# are deliberately NOT imported — they hardcode '>=', baseline comparator, and breach-only.
from gex_pin_ma150_extension_test import (  # noqa: E402
    BLOCK_DAYS,
    COMMISSION_PER_SPREAD,
    HOLIDAYS_PATH,
    IS_END,
    IS_START,
    MAX_REDRAWS,
    OOS_END_ACTUAL,
    OOS_START,
    OPTIONS_CONFIG,
    attach_gex_regime,
    generate_baseline,
    is_market_wide_closure,
    load_cached_universe,
    load_holidays,
    resolve_expiry,
)

PRIMARY_THRESHOLD = 0.50
EXPLORATORY_THRESHOLDS = (0.15, 0.30)
ALL_THRESHOLDS = (PRIMARY_THRESHOLD, *EXPLORATORY_THRESHOLDS)
BOOTSTRAP_SEED = 20260918  # Proposal Section 7: registration date 2026-09-18
BOOTSTRAP_REPS = 10_000
DIT_HORIZON_CALENDAR_DAYS = 10
ARM_NAMES = ("baseline", "gex_only", "proximity_only", "combined")
EXCLUSIONS = (
    "excluded_oos_cutoff",
    "excluded_missing_terminal_price",
    "excluded_missing_midperiod_price",
)

PASS_CONCLUSION = (
    "PASS identifies a promising fast-capture filter within this fixed historical pool. "
    "It does not establish a dealer mechanism, does not validate production exit "
    "behavior, and does not authorize deployment."
)


def compute_sha256(path: Path) -> str:
    """Compute sha256 checksum of a file in 1MB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_git_metadata() -> Dict[str, Any]:
    """Capture git commit hash and dirty status as supplementary metadata."""
    repo_root = BASE.parent.parent
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(repo_root),
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        )
    except Exception:
        head, dirty = "UNKNOWN", False
    return {"head": head, "dirty": dirty}


def get_provenance_files() -> List[Tuple[str, Path]]:
    """Enumerate the authoritative set of files required for run recovery."""
    return [
        ("SCRIPT", Path(__file__).resolve()),
        ("FROZEN_SCRIPT", BASE / "gex_pin_ma150_extension_test.py"),
        ("SIGNAL_ENGINE", BASE / "bps_signal_engine_v2.py"),
        ("IV_HV_CACHE", bps.DEFAULT_IV_HV_CACHE_PATH),
        ("OHLCV_CACHE", full.CACHE),
        ("GEX_DIX_CACHE", dix.DEFAULT_CACHE_PATH),
        ("UNIVERSE_CSV", full.UNIVERSE_CSV),
        ("HOLIDAYS", HOLIDAYS_PATH),
        ("PROPOSAL", BASE.parent.parent / "research" / "proposals" / "2026-09-18-z-ma150-proximity-dit.md"),
    ]


def print_provenance_hashes() -> Dict[str, Dict[str, str]]:
    """Print and return sha256 hashes of all dependencies before loading data."""
    hashes = {}
    for label, path in get_provenance_files():
        if path.exists():
            h = compute_sha256(path)
            hashes[label] = {"path": str(path), "sha256": h}
            print(f"PROVENANCE SHA256 {label} {path}: {h}", flush=True)
        else:
            hashes[label] = {"path": str(path), "sha256": "MISSING"}
            print(f"PROVENANCE SHA256 {label} {path}: MISSING", flush=True)
    git_meta = get_git_metadata()
    print(f"PROVENANCE GIT: head={git_meta['head']} dirty={git_meta['dirty']}", flush=True)
    return hashes


def save_provenance_snapshots(dest_dir: Path) -> Dict[str, Any]:
    """Snapshot copy each hashed dependency into dest_dir for complete recovery."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    for label, path in get_provenance_files():
        if path.exists():
            h = compute_sha256(path)
            target = dest_dir / f"{label}_{path.name}"
            shutil.copy2(path, target)
            records[label] = {"source": str(path), "snapshot": str(target), "sha256": h}
        else:
            records[label] = {"source": str(path), "snapshot": None, "sha256": "MISSING"}
    git_meta = get_git_metadata()
    metadata = {"files": records, "git": git_meta}
    (dest_dir / "provenance_manifest.json").write_text(json.dumps(metadata, indent=2))
    print(f"Provenance snapshot written to {dest_dir}", flush=True)
    return metadata


def score_trade(
    entry: Dict[str, Any],
    frame: pd.DataFrame,
    holidays: set[pd.Timestamp],
    market_sessions: set[pd.Timestamp],
) -> Dict[str, Any]:
    """Score candidate over its full holding period with the daily reprice loop.

    Pricing semantics (Section 5):
    - Entry credit: priced at entry bar's open using frozen signal-date IV and
      nominal Friday expiry for tenor.
    - Pre-settlement marks (t < modeled_expiry): Black-Scholes put pricing using
      nominal Friday expiry for tenor (tenor_t = max((nominal_expiry - t).days / 365.0, 0.001)),
      frozen signal-date base IV, and per-session smile adjustment from that
      session's own close.
    - Terminal settlement mark (t == modeled_expiry): pure intrinsic payoff
      (no Black-Scholes call).
    - reach_by_D: 1 if pct_captured >= PROFIT_TARGET_THRESHOLD (0.80) at any
      session t in [entry_date, min(entry_date + D days, modeled_expiry)].
    - Exclusions:
      - excluded_oos_cutoff: modeled_expiry > OOS_END_ACTUAL
      - excluded_missing_terminal_price: missing/nonfinite close at modeled_expiry
      - excluded_missing_midperiod_price: missing/nonfinite close on any market
        session strictly inside [entry_date, modeled_expiry).
    """
    expiry = resolve_expiry(entry["expiry"], holidays, market_sessions)
    row = {**entry, "modeled_expiry": str(expiry.date()), "exclusion": ""}

    if expiry > pd.Timestamp(OOS_END_ACTUAL):
        row["exclusion"] = "excluded_oos_cutoff"
        return row

    if expiry not in frame.index or not math.isfinite(float(frame.at[expiry, "close"])):
        row["exclusion"] = "excluded_missing_terminal_price"
        return row

    entry_date = pd.Timestamp(entry["date"])

    # Check for missing midperiod prices: any market trading session between
    # entry_date (inclusive) and modeled_expiry (exclusive) must exist and be finite.
    if entry_date not in frame.index or not math.isfinite(float(frame.at[entry_date, "close"])):
        row["exclusion"] = "excluded_missing_midperiod_price"
        return row

    midperiod_sessions = [t for t in market_sessions if entry_date <= t < expiry]
    for t in midperiod_sessions:
        if t not in frame.index or not math.isfinite(float(frame.at[t, "close"])):
            row["exclusion"] = "excluded_missing_midperiod_price"
            return row

    frame_midperiod = frame.loc[(frame.index >= entry_date) & (frame.index < expiry), "close"]
    if not frame_midperiod.empty and not np.all(np.isfinite(frame_midperiod.to_numpy(dtype=float))):
        row["exclusion"] = "excluded_missing_midperiod_price"
        return row

    terminal_close = float(frame.at[expiry, "close"])
    short, long = entry["short_strike"], entry["long_strike"]
    spot, iv = entry["entry_open"], entry["iv_current"]

    # Entry credit using nominal contract maturity (Section 5)
    nominal_expiry = pd.Timestamp(entry["expiry"])
    tenor_entry = max((nominal_expiry - entry_date).days / 365.0, 0.001)
    prices = [
        bs_price(
            spot,
            strike,
            tenor_entry,
            OPTIONS_CONFIG["risk_free_rate"],
            iv_smile_adjustment(
                spot,
                strike,
                iv,
                OPTIONS_CONFIG["iv_skew"],
                OPTIONS_CONFIG["iv_curvature"],
            ),
            "put",
        )
        for strike in (short, long)
    ]
    credit = prices[0] - prices[1]

    holding_frame = frame.loc[(frame.index >= entry_date) & (frame.index <= expiry)]
    holding_closes = holding_frame["close"].to_numpy(dtype=float)
    holding_dates = holding_frame.index

    reached_10 = False
    reached_expiry = False
    dit = math.nan
    ten_days_cutoff = entry_date + pd.Timedelta(days=DIT_HORIZON_CALENDAR_DAYS)

    for t, current_close in zip(holding_dates, holding_closes):
        if t < expiry:
            # Pre-settlement Black-Scholes mark using NOMINAL expiry tenor
            tenor_t = max((nominal_expiry - t).days / 365.0, 0.001)
            short_iv = iv_smile_adjustment(
                current_close, short, iv, OPTIONS_CONFIG["iv_skew"], OPTIONS_CONFIG["iv_curvature"]
            )
            long_iv = iv_smile_adjustment(
                current_close, long, iv, OPTIONS_CONFIG["iv_skew"], OPTIONS_CONFIG["iv_curvature"]
            )
            short_p = bs_price(current_close, short, tenor_t, OPTIONS_CONFIG["risk_free_rate"], short_iv, "put")
            long_p = bs_price(current_close, long, tenor_t, OPTIONS_CONFIG["risk_free_rate"], long_iv, "put")
            cost_to_close = short_p - long_p
        else:
            # Settlement session: pure intrinsic mark, NO Black-Scholes call
            cost_to_close = max(short - current_close, 0.0) - max(long - current_close, 0.0)

        pct_captured = (credit - cost_to_close) / credit if credit > 0 else 0.0

        if pct_captured >= PROFIT_TARGET_THRESHOLD:
            if not reached_expiry:
                reached_expiry = True
                dit = float((t - entry_date).days)
            if t <= ten_days_cutoff and not reached_10:
                reached_10 = True

    breach_at_expiry = bool(terminal_close < short)
    ever_breached_intraperiod = bool((holding_closes < short).any())
    pnl = (
        (credit - max(short - terminal_close, 0.0) + max(long - terminal_close, 0.0))
        * OPTIONS_CONFIG["contract_multiplier"]
        - COMMISSION_PER_SPREAD
    )

    row.update(
        close_at_expiry=terminal_close,
        breach_at_expiry=breach_at_expiry,
        ever_breached_intraperiod=ever_breached_intraperiod,
        credit=credit,
        pnl=pnl,
        reach_by_10=reached_10,
        reach_by_expiry=reached_expiry,
        dit=dit,
    )
    return row


def proximity_masks(rows: pd.DataFrame, threshold: float) -> Dict[str, np.ndarray]:
    """Compute boolean arm membership masks using '<=' proximity upper bound.

    NEW function, parallel to but completely independent of frozen arm_masks().
    Under-extension (z_ma150 <= threshold) defines proximity; NaN fails closed.
    """
    positive = rows["gex_regime"].to_numpy() == "POSITIVE"
    z = rows["z_ma150"].to_numpy(dtype=float)
    proximity = (z <= threshold) & np.isfinite(z)
    return {
        "baseline": np.ones(len(rows), dtype=bool),
        "gex_only": positive,
        "proximity_only": proximity,
        "combined": positive & proximity,
    }


def summarize(rows: pd.DataFrame) -> Dict[str, Any]:
    """Compute arm candidate counts, exclusions, reach rates, and breach rates."""
    scored = rows[rows["exclusion"] == ""]
    n = len(scored)
    reached_trades = scored[scored["reach_by_expiry"] == True]  # noqa: E712
    return {
        "n": n,
        "candidates": len(rows),
        **{reason: int((rows["exclusion"] == reason).sum()) for reason in EXCLUSIONS},
        "reach_by_10_rate": float(scored["reach_by_10"].mean()) if n else math.nan,
        "reach_by_expiry_rate": float(scored["reach_by_expiry"].mean()) if n else math.nan,
        "breach_rate": float(scored["breach_at_expiry"].mean()) if n else math.nan,
        "ever_rate": float(scored["ever_breached_intraperiod"].mean()) if n else math.nan,
        "median_dit": float(reached_trades["dit"].median()) if len(reached_trades) else math.nan,
        "total_pnl": float(scored["pnl"].sum()),
        "mean_pnl": float(scored["pnl"].mean()) if n else math.nan,
        "reached_by_10_count": int(scored["reach_by_10"].sum()) if n else 0,
        "not_reached_by_10_count": int((~scored["reach_by_10"]).sum()) if n else 0,
        "reached_by_expiry_count": int(scored["reach_by_expiry"].sum()) if n else 0,
        "not_reached_by_expiry_count": int((~scored["reach_by_expiry"]).sum()) if n else 0,
    }


def report_arm(label: str, rows: pd.DataFrame, cuts: np.ndarray | None = None) -> Dict[str, Any]:
    """Print descriptive statistics for one arm."""
    stats = summarize(rows)
    print(
        f"{label}: candidates={stats['candidates']} n={stats['n']} "
        f"excluded_oos_cutoff={stats['excluded_oos_cutoff']} "
        f"excluded_missing_terminal_price={stats['excluded_missing_terminal_price']} "
        f"excluded_missing_midperiod_price={stats['excluded_missing_midperiod_price']} "
        f"reach_by_10={stats['reach_by_10_rate']:.2%} "
        f"reach_by_expiry={stats['reach_by_expiry_rate']:.2%} "
        f"median_dit={stats['median_dit']:.1f}d "
        f"breach_at_expiry={stats['breach_rate']:.2%} "
        f"ever_breached_intraperiod={stats['ever_rate']:.2%} "
        f"total_pnl=${stats['total_pnl']:.2f} mean_pnl=${stats['mean_pnl']:.2f}"
    )
    if cuts is not None and stats["n"] > 0:
        scored = rows[rows["exclusion"] == ""]
        bins = np.searchsorted(cuts, scored["cushion_pct"].to_numpy(), side="left")
        for bucket, name in enumerate(("low", "middle", "high")):
            subset = scored.iloc[np.flatnonzero(bins == bucket)]
            rate = float(subset["breach_at_expiry"].mean()) if len(subset) else math.nan
            reach_rate = float(subset["reach_by_10"].mean()) if len(subset) else math.nan
            print(f"  cushion_pct {name}: n={len(subset)} reach_by_10={reach_rate:.2%} breach_at_expiry={rate:.2%}")
    return stats


def compute_overlap_diagnostic(rows: pd.DataFrame) -> Dict[str, Any]:
    """Compute 2x2 contingency table between proximity band and production band.

    Evaluated over the OOS baseline pool (arm 1, before either filter is applied).
    Compares:
      Proximity band: z_ma150 <= 0.50
      Production band: 0.0 <= (signal_close - ema150) / ema150 <= 0.10
    """
    total = len(rows)
    if total == 0:
        return {
            "total": 0,
            "total_prox": 0,
            "total_prod": 0,
            "pct_prox": 0.0,
            "pct_prod": 0.0,
            "cell_yy": 0,
            "cell_yn": 0,
            "cell_ny": 0,
            "cell_nn": 0,
            "pct_cell_yy": 0.0,
            "pct_cell_yn": 0.0,
            "pct_cell_ny": 0.0,
            "pct_cell_nn": 0.0,
            "p_prod_given_prox": math.nan,
            "p_prox_given_prod": math.nan,
        }

    z = rows["z_ma150"].to_numpy(dtype=float)
    in_prox = (z <= PRIMARY_THRESHOLD) & np.isfinite(z)

    signal_close = rows["signal_close"].to_numpy(dtype=float)
    ema150 = rows["ema150"].to_numpy(dtype=float)
    prod_rel = (signal_close - ema150) / ema150
    in_prod = (prod_rel >= 0.0) & (prod_rel <= 0.10) & np.isfinite(prod_rel)

    cell_yy = int((in_prox & in_prod).sum())
    cell_yn = int((in_prox & ~in_prod).sum())
    cell_ny = int((~in_prox & in_prod).sum())
    cell_nn = int((~in_prox & ~in_prod).sum())

    total_prox = cell_yy + cell_yn
    total_prod = cell_yy + cell_ny

    return {
        "total": total,
        "total_prox": total_prox,
        "total_prod": total_prod,
        "pct_prox": total_prox / total,
        "pct_prod": total_prod / total,
        "cell_yy": cell_yy,
        "cell_yn": cell_yn,
        "cell_ny": cell_ny,
        "cell_nn": cell_nn,
        "pct_cell_yy": cell_yy / total,
        "pct_cell_yn": cell_yn / total,
        "pct_cell_ny": cell_ny / total,
        "pct_cell_nn": cell_nn / total,
        "p_prod_given_prox": (cell_yy / total_prox) if total_prox > 0 else math.nan,
        "p_prox_given_prod": (cell_yy / total_prod) if total_prod > 0 else math.nan,
    }


def print_overlap_diagnostic(diag: Dict[str, Any]) -> None:
    """Print the formatted 2x2 contingency table and containment fractions."""
    print("\n" + "=" * 65)
    print("=== 2x2 Overlap Diagnostic (OOS Baseline Pool, Arm 1) ===")
    print(f"Total OOS baseline candidates: {diag['total']}")
    print(f"Proximity band (z_ma150 <= 0.50): {diag['total_prox']} ({diag['pct_prox']:.2%})")
    print(f"Production band (0 <= (close-ema150)/ema150 <= 0.10): {diag['total_prod']} ({diag['pct_prod']:.2%})")
    print("\nContingency Table:")
    print(f"  [In Proximity & In Production]:     {diag['cell_yy']:>5} ({diag['pct_cell_yy']:.2%})")
    print(f"  [In Proximity & NOT In Production]: {diag['cell_yn']:>5} ({diag['pct_cell_yn']:.2%})")
    print(f"  [NOT In Proximity & In Production]: {diag['cell_ny']:>5} ({diag['pct_cell_ny']:.2%})")
    print(f"  [NOT In Proximity & NOT Production]:{diag['cell_nn']:>5} ({diag['pct_cell_nn']:.2%})")
    print("\nOne-way Containment Fractions:")
    p_prod_prox = f"{diag['p_prod_given_prox']:.2%}" if math.isfinite(diag['p_prod_given_prox']) else "N/A"
    p_prox_prod = f"{diag['p_prox_given_prod']:.2%}" if math.isfinite(diag['p_prox_given_prod']) else "N/A"
    print(f"  P(In Production | In Proximity) = {p_prod_prox} ({diag['cell_yy']}/{diag['total_prox']})")
    print(f"  P(In Proximity | In Production) = {p_prox_prod} ({diag['cell_yy']}/{diag['total_prod']})")
    print("=" * 65 + "\n")


def block_tables(
    rows: pd.DataFrame,
    threshold: float,
    outcome_col: str | None = None,
):
    """Build overlapping 45-day block count and event matrices.

    If outcome_col is provided, returns events for that specific column.
    Otherwise, returns events_dict for all four outcome columns:
    reach_by_10, reach_by_expiry, breach_at_expiry, ever_breached_intraperiod.
    """
    start, end = pd.Timestamp(OOS_START), pd.Timestamp(OOS_END_ACTUAL)
    starts = pd.date_range(start, end - pd.Timedelta(days=BLOCK_DAYS), freq="D")
    dates = pd.to_datetime(rows["date"]).to_numpy()
    membership = (
        (dates[None, :] >= starts.to_numpy()[:, None])
        & (dates[None, :] < (starts + pd.Timedelta(days=BLOCK_DAYS)).to_numpy()[:, None])
    )
    masks_dict = proximity_masks(rows, threshold)
    masks = np.column_stack([masks_dict[arm] for arm in ARM_NAMES]).astype(np.int64)
    counts = membership.astype(np.int64) @ masks
    n_blocks = math.ceil(((end - start).days + 1) / BLOCK_DAYS)

    if outcome_col is not None:
        col_vals = rows[outcome_col].to_numpy(dtype=np.int64)[:, None]
        events = membership.astype(np.int64) @ (masks * col_vals)
        return starts, counts, events, n_blocks
    else:
        events_dict = {}
        for col in ("reach_by_10", "reach_by_expiry", "breach_at_expiry", "ever_breached_intraperiod"):
            col_vals = rows[col].to_numpy(dtype=np.int64)[:, None]
            events_dict[col] = membership.astype(np.int64) @ (masks * col_vals)
        return starts, counts, events_dict, n_blocks


def bootstrap_iteration(
    rng,
    starts,
    counts,
    events,
    n_blocks,
    threshold,
    iteration,
    draw=None,
):
    """Execute one bootstrap draw shared across arms and outcomes with redraw cap."""
    if draw is None:
        draw = lambda: rng.integers(0, len(starts), size=n_blocks)
    for attempt in range(MAX_REDRAWS + 1):
        chosen = draw()
        totals = counts[chosen].sum(axis=0)
        # gex_only (1), proximity_only (2), and combined (3) arms all divide
        # into rates below -- a redraw with zero blocks in ANY of them, not
        # just combined, produces a division-by-zero for that arm's rate
        # without tripping this guard (CodeRabbit finding, 2026-09-21).
        if totals[1] > 0 and totals[2] > 0 and totals[3] > 0:
            if isinstance(events, dict):
                rates = {col: ev[chosen].sum(axis=0) / totals for col, ev in events.items()}
            else:
                rates = events[chosen].sum(axis=0) / totals
            return rates, starts[chosen], attempt
    raise RuntimeError(
        f"z_threshold={threshold}: bootstrap iteration {iteration} "
        f"has zero combined-arm trades after {MAX_REDRAWS} redraw attempts"
    )


def bootstrap(
    rows: pd.DataFrame,
    threshold: float,
    outcome_col: str,
    arm_a: str,
    arm_b: str,
    seed: int = BOOTSTRAP_SEED,
    reps: int = BOOTSTRAP_REPS,
) -> tuple[float, float]:
    """Compute 95% bootstrap CI for (arm_a - arm_b) on outcome_col."""
    arm_map = {name: i for i, name in enumerate(ARM_NAMES)}
    idx_a, idx_b = arm_map[arm_a], arm_map[arm_b]
    starts, counts, events, n_blocks = block_tables(rows, threshold, outcome_col=outcome_col)
    rng = np.random.default_rng(seed)
    diffs = np.empty(reps)
    for iteration in range(reps):
        rates, _, _ = bootstrap_iteration(rng, starts, counts, events, n_blocks, threshold, iteration)
        diffs[iteration] = rates[idx_a] - rates[idx_b]
    low, high = np.percentile(diffs, [2.5, 97.5])
    return float(low), float(high)


def bootstrap_all_gates(
    rows: pd.DataFrame,
    threshold: float,
    seed: int = BOOTSTRAP_SEED,
    reps: int = BOOTSTRAP_REPS,
) -> Dict[str, tuple[float, float]]:
    """Compute 95% bootstrap CIs for all five gates sharing draws within each iteration.

    Gates and subtraction directions:
      Gate A: reach_by_10: combined - gex_only
      Gate B: reach_by_10: combined - proximity_only
      Gate C: breach_at_expiry: combined - gex_only
      Gate D: ever_breached_intraperiod: combined - gex_only
      Gate E: reach_by_expiry: gex_only - combined (HIGHER-IS-BETTER! OPPOSITE DIRECTION!)
    """
    starts, counts, events_dict, n_blocks = block_tables(rows, threshold)
    rng = np.random.default_rng(seed)
    diffs = {gate: np.empty(reps) for gate in ("gate_a", "gate_b", "gate_c", "gate_d", "gate_e")}
    for iteration in range(reps):
        rates, _, _ = bootstrap_iteration(rng, starts, counts, events_dict, n_blocks, threshold, iteration)
        diffs["gate_a"][iteration] = rates["reach_by_10"][3] - rates["reach_by_10"][1]
        diffs["gate_b"][iteration] = rates["reach_by_10"][3] - rates["reach_by_10"][2]
        diffs["gate_c"][iteration] = rates["breach_at_expiry"][3] - rates["breach_at_expiry"][1]
        diffs["gate_d"][iteration] = rates["ever_breached_intraperiod"][3] - rates["ever_breached_intraperiod"][1]
        # Gate E: higher-is-better, deterioration := GEXonly - combined
        diffs["gate_e"][iteration] = rates["reach_by_expiry"][1] - rates["reach_by_expiry"][3]

    cis = {}
    for gate in ("gate_a", "gate_b", "gate_c", "gate_d", "gate_e"):
        low, high = np.percentile(diffs[gate], [2.5, 97.5])
        cis[gate] = (float(low), float(high))
    return cis


def compute_gate_verdict_superiority(ci: tuple[float, float]) -> str:
    """Evaluate superiority gate with disjoint branch ordering.

    Ordering:
      1. PASS if low > 0
      2. Else FAIL if high < 0
      3. Else INCONCLUSIVE
    """
    low, high = ci
    if low > 0:
        return "PASS"
    elif high < 0:
        return "FAIL"
    else:
        return "INCONCLUSIVE"


def compute_gate_verdict_non_inferiority(ci: tuple[float, float], margin: float) -> str:
    """Evaluate non-inferiority gate with disjoint branch ordering.

    Ordering:
      1. PASS if high <= margin
      2. Else FAIL if low > margin
      3. Else INCONCLUSIVE
    Note: high == margin cleanly resolves to PASS by strict evaluation order.
    """
    low, high = ci
    if high <= margin:
        return "PASS"
    elif low > margin:
        return "FAIL"
    else:
        return "INCONCLUSIVE"


def compute_verdict(
    stats: Dict[str, Dict[str, Any]],
    gate_cis: Dict[str, tuple[float, float]],
    *,
    margin_breach: float,
    margin_intraperiod: float,
    margin_reach: float,
) -> Dict[str, Any]:
    """Compute the overall five-gate verdict.

    Requires explicit margin parameters with NO defaults.
    Enforces n<30 hard short-circuit and disjoint branch ordering.
    """
    margins = {
        "margin_breach": margin_breach,
        "margin_intraperiod": margin_intraperiod,
        "margin_reach": margin_reach,
    }
    for name, val in margins.items():
        if val is None:
            raise ValueError(f"{name} must be explicitly provided (no default allowed)")
        if not isinstance(val, (int, float)) or not math.isfinite(val):
            raise ValueError(f"{name} must be a finite float, got {val!r}")

    combined = stats["combined"]
    n_combined = combined["n"]

    if n_combined < 30:
        return {
            "overall": "INCONCLUSIVE",
            "primary_overall": "INCONCLUSIVE",
            "non_inferiority_overall": "INCONCLUSIVE",
            "reason": f"Combined arm trades n={n_combined} < 30 (hard short-circuit)",
            "gates": {
                "gate_a": {"verdict": "INCONCLUSIVE", "ci": gate_cis.get("gate_a")},
                "gate_b": {"verdict": "INCONCLUSIVE", "ci": gate_cis.get("gate_b")},
                "gate_c": {"verdict": "INCONCLUSIVE", "ci": gate_cis.get("gate_c"), "margin": margin_breach},
                "gate_d": {"verdict": "INCONCLUSIVE", "ci": gate_cis.get("gate_d"), "margin": margin_intraperiod},
                "gate_e": {"verdict": "INCONCLUSIVE", "ci": gate_cis.get("gate_e"), "margin": margin_reach},
            },
            "n_combined": n_combined,
        }

    v_a = compute_gate_verdict_superiority(gate_cis["gate_a"])
    v_b = compute_gate_verdict_superiority(gate_cis["gate_b"])
    v_c = compute_gate_verdict_non_inferiority(gate_cis["gate_c"], margin_breach)
    v_d = compute_gate_verdict_non_inferiority(gate_cis["gate_d"], margin_intraperiod)
    v_e = compute_gate_verdict_non_inferiority(gate_cis["gate_e"], margin_reach)

    # Primary metric combination (Section 7)
    if v_a == "PASS" and v_b == "PASS":
        primary_overall = "PASS"
    elif v_a == "FAIL" or v_b == "FAIL":
        primary_overall = "FAIL"
    else:
        primary_overall = "INCONCLUSIVE"

    # Non-inferiority combination (Section 7)
    if v_c == "PASS" and v_d == "PASS" and v_e == "PASS":
        ni_overall = "PASS"
    elif v_c == "FAIL" or v_d == "FAIL" or v_e == "FAIL":
        ni_overall = "FAIL"
    else:
        ni_overall = "INCONCLUSIVE"

    # Overall verdict (Section 7)
    if primary_overall == "PASS" and ni_overall == "PASS":
        overall = "PASS"
    elif primary_overall == "FAIL" or ni_overall == "FAIL":
        overall = "FAIL"
    else:
        overall = "INCONCLUSIVE"

    return {
        "overall": overall,
        "primary_overall": primary_overall,
        "non_inferiority_overall": ni_overall,
        "gates": {
            "gate_a": {"verdict": v_a, "ci": gate_cis["gate_a"]},
            "gate_b": {"verdict": v_b, "ci": gate_cis["gate_b"]},
            "gate_c": {"verdict": v_c, "ci": gate_cis["gate_c"], "margin": margin_breach},
            "gate_d": {"verdict": v_d, "ci": gate_cis["gate_d"], "margin": margin_intraperiod},
            "gate_e": {"verdict": v_e, "ci": gate_cis["gate_e"], "margin": margin_reach},
        },
        "n_combined": n_combined,
    }


def print_verdict(threshold: float, verdict_info: Dict[str, Any], is_exploratory: bool = False) -> None:
    """Print detailed gate and overall verdict."""
    tag = " (Exploratory)" if is_exploratory else " (Gating)"
    print(f"\n--- Threshold z<={threshold:.2f}{tag} ---")
    print(f"Combined arm trades: n={verdict_info['n_combined']}")
    gates = verdict_info["gates"]

    print(f"Gate A (reach_by_10 vs GEX-only): {gates['gate_a']['verdict']} (CI=[{gates['gate_a']['ci'][0]:.2%}, {gates['gate_a']['ci'][1]:.2%}])")
    print(f"Gate B (reach_by_10 vs Proximity-only): {gates['gate_b']['verdict']} (CI=[{gates['gate_b']['ci'][0]:.2%}, {gates['gate_b']['ci'][1]:.2%}])")
    print(f"  -> Primary Metric Overall: {verdict_info['primary_overall']}")

    print(f"Gate C (terminal breach non-inferiority): {gates['gate_c']['verdict']} (CI=[{gates['gate_c']['ci'][0]:.2%}, {gates['gate_c']['ci'][1]:.2%}], margin={gates['gate_c']['margin']:.2%})")
    print(f"Gate D (intraperiod breach non-inferiority): {gates['gate_d']['verdict']} (CI=[{gates['gate_d']['ci'][0]:.2%}, {gates['gate_d']['ci'][1]:.2%}], margin={gates['gate_d']['margin']:.2%})")
    print(f"Gate E (eventual reach non-inferiority): {gates['gate_e']['verdict']} (CI=[{gates['gate_e']['ci'][0]:.2%}, {gates['gate_e']['ci'][1]:.2%}], margin={gates['gate_e']['margin']:.2%})")
    print(f"  -> Non-Inferiority Overall: {verdict_info['non_inferiority_overall']}")

    print(f"OVERALL VERDICT: {verdict_info['overall']}")
    if verdict_info["overall"] == "PASS":
        print(PASS_CONCLUSION)


def run(
    margin_breach: float | None = None,
    margin_intraperiod: float | None = None,
    margin_reach: float | None = None,
    provenance_dir: Path | None = None,
) -> None:
    """Execute the full backtest pipeline, overlap diagnostic, and stopping rule."""
    print_provenance_hashes()
    if provenance_dir is not None:
        save_provenance_snapshots(provenance_dir)

    print("Development screen: z_ma150 proximity and DIT on fixed VRP entry pool.")
    print("Implements research/proposals/2026-09-18-z-ma150-proximity-dit.md (fourth draft).")
    print(
        f"IS={IS_START}..{IS_END}; OOS={OOS_START}..{OOS_END_ACTUAL}; "
        f"bootstrap seed={BOOTSTRAP_SEED}, reps={BOOTSTRAP_REPS}, block_days={BLOCK_DAYS}"
    )
    print(
        f"Primary gating threshold: z_ma150_proximity_upper_bound = {PRIMARY_THRESHOLD}; "
        f"exploratory: {EXPLORATORY_THRESHOLDS}"
    )

    data_map = load_cached_universe()
    holidays = load_holidays(HOLIDAYS_PATH)
    market_sessions = {session for prices in data_map.values() for session in prices.index}
    print(
        f"Calendar: holiday file authoritative for {sorted({day.year for day in holidays})}; "
        "other years use weekday sessions pooled across the loaded cached universe."
    )

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
    print(
        f"Undefined z_ma150 (zero/nonfinite stdev): {int(frame['z_ma150'].isna().sum())}; "
        "retained in baseline, fails proximity filters closed."
    )
    for column in ("breach_at_expiry", "ever_breached_intraperiod", "pnl", "reach_by_10", "reach_by_expiry", "dit"):
        if column not in frame:
            frame[column] = np.nan

    pooled = frame[frame["exclusion"] == ""]
    if pooled.empty:
        raise RuntimeError("No scored baseline trades after exclusions")
    cuts = np.quantile(pooled["cushion_pct"], [1 / 3, 2 / 3])
    print(f"Pooled scored baseline cushion_pct terciles: {cuts.tolist()}; ties stay together.")

    # 2x2 overlap diagnostic over OOS baseline pool (arm 1, before either filter)
    oos_baseline = frame[(frame["date"] >= OOS_START) & (frame["date"] <= OOS_END_ACTUAL)]
    diag = compute_overlap_diagnostic(oos_baseline)
    print_overlap_diagnostic(diag)

    oos_stats = {}
    for window, start, end in (("IS", IS_START, IS_END), ("OOS", OOS_START, OOS_END_ACTUAL)):
        subset = frame[(frame["date"] >= start) & (frame["date"] <= end)]
        common = {
            name: report_arm(
                f"{window} {name}",
                subset.loc[proximity_masks(subset, PRIMARY_THRESHOLD)[name]],
                cuts,
            )
            for name in ("baseline", "gex_only")
        }
        for threshold in ALL_THRESHOLDS:
            masks = proximity_masks(subset, threshold)
            stats = {
                **common,
                **{
                    name: report_arm(
                        f"{window} z<={threshold:.2f} {name}",
                        subset.loc[masks[name]],
                        cuts,
                    )
                    for name in ("proximity_only", "combined")
                },
            }
            if window == "OOS":
                oos_stats[threshold] = stats

    oos_scored = pooled[(pooled["date"] >= OOS_START) & (pooled["date"] <= OOS_END_ACTUAL)]

    if margin_breach is None or margin_intraperiod is None or margin_reach is None:
        print("\n" + "=" * 65)
        print("STOP: Cannot compute real verdict.")
        print("margin_breach, margin_intraperiod, and margin_reach are unset user risk-tolerance decisions (Section 8).")
        print("Supply them via CLI flags to evaluate the stopping rule.")
        print("=" * 65 + "\n")
        return

    for threshold in ALL_THRESHOLDS:
        arm_ns = [arm["n"] for arm in oos_stats[threshold].values() if isinstance(arm, dict) and "n" in arm]
        if oos_stats[threshold]["combined"]["n"] < 30 or any(n == 0 for n in arm_ns):
            # bootstrap_iteration cannot resample an empty arm; compute_verdict short-circuits to INCONCLUSIVE
            # on combined n < 30 and the NaN CIs keep every other gate non-passing.
            gate_cis = {gate: (math.nan, math.nan) for gate in ("gate_a", "gate_b", "gate_c", "gate_d", "gate_e")}
        else:
            gate_cis = bootstrap_all_gates(oos_scored, threshold)
        verdict_res = compute_verdict(
            oos_stats[threshold],
            gate_cis,
            margin_breach=margin_breach,
            margin_intraperiod=margin_intraperiod,
            margin_reach=margin_reach,
        )
        print_verdict(threshold, verdict_res, is_exploratory=(threshold != PRIMARY_THRESHOLD))


def _self_test() -> None:
    """Run all 9 required synthetic fixtures and supplementary checks."""

    def fixture_entry(
        expiry="2026-04-03",
        date="2026-03-02",
        signal_date="2026-02-27",
        z_ma150=0.30,
        gex_regime="POSITIVE",
    ):
        return dict(
            code="TEST",
            date=date,
            signal_date=signal_date,
            expiry=expiry,
            entry_open=100.0,
            signal_close=99.0,
            ema150=95.0,
            daily_log_stdev20=0.02,
            vrp_ratio=1.5,
            iv_current=0.30,
            short_strike=95.0,
            long_strike=85.0,
            z_ma150=z_ma150,
            cushion_pct=4 / 99,
            gex_regime=gex_regime,
        )

    # Fixture 1: Baseline/GEX-only arm membership and inherited fields
    def fixture_1_baseline_and_gex_arm_parity():
        from gex_pin_ma150_extension_test import arm_masks as old_arm_masks
        from gex_pin_ma150_extension_test import score_trade as old_score_trade

        entries = [
            fixture_entry(date="2025-06-02", expiry="2025-06-20", z_ma150=0.20, gex_regime="POSITIVE"),
            fixture_entry(date="2025-06-02", expiry="2025-06-20", z_ma150=0.45, gex_regime="NEGATIVE"),
            fixture_entry(date="2025-06-02", expiry="2025-06-20", z_ma150=1.80, gex_regime="POSITIVE"),
            fixture_entry(date="2025-06-02", expiry="2025-06-20", z_ma150=2.50, gex_regime="NEGATIVE"),
        ]
        dates = pd.bdate_range("2025-06-02", "2025-06-20")
        frame = pd.DataFrame({"close": 100.0, "open": 100.0}, index=dates)
        holidays = set()
        market_sessions = set(dates)

        old_scored = [old_score_trade(e, frame, holidays, market_sessions) for e in entries]
        new_scored = [score_trade(e, frame, holidays, market_sessions) for e in entries]

        df_old = pd.DataFrame(old_scored)
        df_new = pd.DataFrame(new_scored)

        old_masks = old_arm_masks(df_old, 2.0)
        new_masks = proximity_masks(df_new, 0.50)

        np.testing.assert_array_equal(old_masks["baseline"], new_masks["baseline"])
        np.testing.assert_array_equal(old_masks["gex_only"], new_masks["gex_only"])

        inherited_fields = [
            "code", "date", "short_strike", "long_strike", "expiry",
            "vrp_ratio", "z_ma150", "gex_regime", "modeled_expiry",
            "close_at_expiry", "breach_at_expiry", "ever_breached_intraperiod",
            "credit", "pnl", "exclusion"
        ]
        for field in inherited_fields:
            for r_old, r_new in zip(old_scored, new_scored):
                assert math.isclose(float(r_old[field]), float(r_new[field]), rel_tol=1e-9) if isinstance(r_old[field], (int, float)) else r_old[field] == r_new[field], (
                    f"Field mismatch on {field}: {r_old[field]} vs {r_new[field]}"
                )

        # New script carries DIT fields; old script does not
        for r_old, r_new in zip(old_scored, new_scored):
            assert "reach_by_10" in r_new and "reach_by_10" not in r_old
            assert "reach_by_expiry" in r_new and "reach_by_expiry" not in r_old
            assert "dit" in r_new and "dit" not in r_old

    # Fixture 2: Hand-computed DIT and reach_by_D verification
    def fixture_2_hand_computed_dit():
        entry = fixture_entry(date="2026-03-02", expiry="2026-03-20")
        dates = pd.bdate_range("2026-03-02", "2026-03-20")
        # Days:
        # 2026-03-02 (Mon, day 0): close=100.0
        # 2026-03-03 (Tue, day 1): close=101.0
        # 2026-03-04 (Wed, day 2): close=102.0
        # 2026-03-05 (Thu, day 3): close=160.0 (huge jump OTM)
        prices = [100.0, 101.0, 102.0, 160.0] + [160.0] * (len(dates) - 4)
        frame = pd.DataFrame({"close": prices, "open": prices}, index=dates)

        res = score_trade(entry, frame, set(), set(dates))
        assert res["exclusion"] == ""
        # 2026-03-05 - 2026-03-02 = 3 calendar days
        assert res["dit"] == 3.0, f"Expected DIT 3, got {res['dit']}"
        assert res["reach_by_10"] is True
        assert res["reach_by_expiry"] is True

        # Now test crossing after day 10 (e.g. on 2026-03-16, 14 calendar days)
        prices_late = [100.0] * 10 + [160.0] * (len(dates) - 10)
        frame_late = pd.DataFrame({"close": prices_late, "open": prices_late}, index=dates)
        res_late = score_trade(entry, frame_late, set(), set(dates))
        assert res_late["dit"] == 14.0, f"Expected DIT 14, got {res_late['dit']}"
        assert res_late["reach_by_10"] is False
        assert res_late["reach_by_expiry"] is True

        # Never crosses
        prices_never = [80.0] * len(dates)
        frame_never = pd.DataFrame({"close": prices_never, "open": prices_never}, index=dates)
        res_never = score_trade(entry, frame_never, set(), set(dates))
        assert math.isnan(res_never["dit"])
        assert res_never["reach_by_10"] is False
        assert res_never["reach_by_expiry"] is False

    # Fixture 3: Nominal-vs-modeled-expiry tenor distinction
    def fixture_3_holiday_shifted_tenor_distinction():
        nominal_expiry = "2026-04-03"  # Good Friday
        modeled_settlement = "2026-04-02"  # Thursday
        holidays = {pd.Timestamp(nominal_expiry)}
        entry_date = "2026-03-16"
        entry = fixture_entry(expiry=nominal_expiry, date=entry_date)

        dates = pd.date_range("2026-03-16", "2026-04-02", freq="D")
        frame = pd.DataFrame({"close": 98.0, "open": 98.0}, index=dates)
        frame.loc[pd.Timestamp("2026-04-01"), "close"] = 97.0
        frame.loc[pd.Timestamp("2026-04-02"), "close"] = 96.0

        res = score_trade(entry, frame, holidays, set(dates))
        assert res["modeled_expiry"] == modeled_settlement

        # 1. Pre-settlement Black-Scholes marks on 2026-04-01:
        # Nominal tenor uses 2026-04-03: (2026-04-03 - 2026-04-01) = 2 days
        # Modeled tenor would use 2026-04-02: (2026-04-02 - 2026-04-01) = 1 day
        t_pre = pd.Timestamp("2026-04-01")
        tenor_nom = max((pd.Timestamp(nominal_expiry) - t_pre).days / 365.0, 0.001)
        tenor_mod = max((pd.Timestamp(modeled_settlement) - t_pre).days / 365.0, 0.001)
        spot_pre = 97.0
        short, long, iv = entry["short_strike"], entry["long_strike"], entry["iv_current"]

        cost_nom = bs_price(spot_pre, short, tenor_nom, OPTIONS_CONFIG["risk_free_rate"], iv, "put") - \
            bs_price(spot_pre, long, tenor_nom, OPTIONS_CONFIG["risk_free_rate"], iv, "put")
        cost_mod = bs_price(spot_pre, short, tenor_mod, OPTIONS_CONFIG["risk_free_rate"], iv, "put") - \
            bs_price(spot_pre, long, tenor_mod, OPTIONS_CONFIG["risk_free_rate"], iv, "put")
        assert abs(cost_nom - cost_mod) > 1e-4, "Pre-settlement mark must differ between nominal and modeled tenors"

        # 2. Terminal intrinsic settlement mark on 2026-04-02:
        # Pure intrinsic formula: max(short - close, 0) - max(long - close, 0)
        # Indifferent to tenor by construction (never calls bs_price).
        term_close = 96.0
        intrinsic_terminal = max(short - term_close, 0.0) - max(long - term_close, 0.0)
        assert math.isclose(intrinsic_terminal, 0.0) if short <= term_close else intrinsic_terminal > 0
        pnl_expected = (res["credit"] - intrinsic_terminal) * OPTIONS_CONFIG["contract_multiplier"] - COMMISSION_PER_SPREAD
        assert math.isclose(res["pnl"], pnl_expected, rel_tol=1e-9)

    # Fixture 4: Censoring and reach accounting reconciliation
    def fixture_4_censoring_and_reach_accounting():
        records = []
        # Construct synthetic records with controlled counts
        # 10 reached by 10
        for _ in range(10):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=True, reach_by_expiry=True, dit=4.0,
                breach_at_expiry=False, ever_breached_intraperiod=False,
                pnl=100.0, exclusion="", cushion_pct=0.1
            ))
        # 15 reached between day 10 and expiry
        for _ in range(15):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=False, reach_by_expiry=True, dit=14.0,
                breach_at_expiry=False, ever_breached_intraperiod=False,
                pnl=100.0, exclusion="", cushion_pct=0.1
            ))
        # 20 never reached
        for _ in range(20):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=False, reach_by_expiry=False, dit=math.nan,
                breach_at_expiry=True, ever_breached_intraperiod=True,
                pnl=-500.0, exclusion="", cushion_pct=0.1
            ))
        # 5 oos cutoff
        for _ in range(5):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=False, reach_by_expiry=False, dit=math.nan,
                breach_at_expiry=False, ever_breached_intraperiod=False,
                pnl=0.0, exclusion="excluded_oos_cutoff", cushion_pct=0.1
            ))
        # 4 missing terminal
        for _ in range(4):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=False, reach_by_expiry=False, dit=math.nan,
                breach_at_expiry=False, ever_breached_intraperiod=False,
                pnl=0.0, exclusion="excluded_missing_terminal_price", cushion_pct=0.1
            ))
        # 3 missing midperiod
        for _ in range(3):
            records.append(dict(
                date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE",
                reach_by_10=False, reach_by_expiry=False, dit=math.nan,
                breach_at_expiry=False, ever_breached_intraperiod=False,
                pnl=0.0, exclusion="excluded_missing_midperiod_price", cushion_pct=0.1
            ))

        df = pd.DataFrame(records)
        stats = summarize(df)

        assert stats["candidates"] == 57
        assert stats["n"] == 45
        assert stats["excluded_oos_cutoff"] == 5
        assert stats["excluded_missing_terminal_price"] == 4
        assert stats["excluded_missing_midperiod_price"] == 3
        assert stats["candidates"] == (
            stats["n"] + stats["excluded_oos_cutoff"] +
            stats["excluded_missing_terminal_price"] + stats["excluded_missing_midperiod_price"]
        )

        assert stats["reached_by_10_count"] == 10
        assert stats["not_reached_by_10_count"] == 35
        assert stats["reached_by_10_count"] + stats["not_reached_by_10_count"] == stats["n"]

        assert stats["reached_by_expiry_count"] == 25
        assert stats["not_reached_by_expiry_count"] == 20
        assert stats["reached_by_expiry_count"] + stats["not_reached_by_expiry_count"] == stats["n"]

    # Fixture 5: Catches wrong mask direction ('>=' vs '<=')
    def fixture_5_wrong_mask_direction():
        from gex_pin_ma150_extension_test import arm_masks as old_arm_masks

        records = [
            # Candidate in proximity (<= 0.50), NOT extended (>= 0.50)
            dict(date="2025-06-02", z_ma150=0.30, gex_regime="POSITIVE", reach_by_10=True),
            # Candidate NOT in proximity (<= 0.50), IN extension (>= 0.50)
            dict(date="2025-06-02", z_ma150=2.00, gex_regime="POSITIVE", reach_by_10=False),
        ]
        df = pd.DataFrame(records)
        correct_mask = proximity_masks(df, 0.50)
        wrong_mask = old_arm_masks(df, 0.50)

        assert bool(correct_mask["combined"][0]) is True and bool(correct_mask["combined"][1]) is False
        assert bool(wrong_mask["combined"][0]) is False and bool(wrong_mask["combined"][1]) is True

        correct_rate = df.loc[correct_mask["combined"], "reach_by_10"].mean()
        wrong_rate = df.loc[wrong_mask["combined"], "reach_by_10"].mean()
        assert math.isclose(correct_rate, 1.0) and math.isclose(wrong_rate, 0.0)

    # Fixture 6: Catches substituting baseline for GEX-only comparator
    def fixture_6_wrong_comparator_baseline_vs_gex_only():
        stats = {
            "baseline": {"reach_by_10_rate": 0.20, "n": 100},
            "gex_only": {"reach_by_10_rate": 0.60, "n": 50},
            "proximity_only": {"reach_by_10_rate": 0.30, "n": 40},
            "combined": {"reach_by_10_rate": 0.40, "n": 35},
        }
        # Comparison vs GEX-only (CORRECT): combined - gex_only = 0.40 - 0.60 = -0.20 (NEGATIVE -> FAIL)
        diff_gex = stats["combined"]["reach_by_10_rate"] - stats["gex_only"]["reach_by_10_rate"]
        # Comparison vs Baseline (WRONG): combined - baseline = 0.40 - 0.20 = +0.20 (POSITIVE -> PASS)
        diff_base = stats["combined"]["reach_by_10_rate"] - stats["baseline"]["reach_by_10_rate"]

        assert math.isclose(diff_gex, -0.20) and diff_gex < 0
        assert math.isclose(diff_base, 0.20) and diff_base > 0
        ci_gex = (-0.25, -0.15)
        ci_base = (0.15, 0.25)
        assert compute_gate_verdict_superiority(ci_gex) == "FAIL"
        assert compute_gate_verdict_superiority(ci_base) == "PASS"

    # Fixture 7: Catches reversed subtraction on Terminal Breach (Gate C)
    def fixture_7_terminal_breach_gate_c_subtraction_direction():
        # Lower-is-better metric: deterioration := combined_breach - gex_only_breach
        # Synthetic test margin: 0.02
        margin_breach_test = 0.02

        # Combined breach rate (25%) is worse than GEX-only (10%)
        combined_breach = 0.25
        gex_breach = 0.10

        # Correct direction: combined - gex_only = +0.15 > 0.02 -> FAIL
        correct_diff = combined_breach - gex_breach
        correct_ci = (0.13, 0.17)
        assert math.isclose(correct_diff, 0.15)
        assert compute_gate_verdict_non_inferiority(correct_ci, margin_breach_test) == "FAIL"

        # Reversed direction: gex_only - combined = -0.15 <= 0.02 -> FALSE PASS
        reversed_diff = gex_breach - combined_breach
        reversed_ci = (-0.17, -0.13)
        assert math.isclose(reversed_diff, -0.15)
        assert compute_gate_verdict_non_inferiority(reversed_ci, margin_breach_test) == "PASS"

    # Fixture 8: Catches reversed subtraction on Reach-by-Expiry (Gate E) specifically
    def fixture_8_reach_by_expiry_gate_e_subtraction_direction():
        # Higher-is-better metric: deterioration := gex_reach - combined_reach
        # Synthetic test margin: 0.03
        margin_reach_test = 0.03

        # Combined eventual reach (60%) is worse than GEX-only (80%)
        combined_reach = 0.60
        gex_reach = 0.80

        # Correct Gate E direction: gex_only - combined = +0.20 > 0.03 -> FAIL
        correct_gate_e_diff = gex_reach - combined_reach
        correct_gate_e_ci = (0.18, 0.22)
        assert math.isclose(correct_gate_e_diff, 0.20)
        assert compute_gate_verdict_non_inferiority(correct_gate_e_ci, margin_reach_test) == "FAIL"

        # Copy-pasted breach direction: combined - gex_only = -0.20 <= 0.03 -> FALSE PASS
        copy_pasted_breach_diff = combined_reach - gex_reach
        copy_pasted_breach_ci = (-0.22, -0.18)
        assert math.isclose(copy_pasted_breach_diff, -0.20)
        assert compute_gate_verdict_non_inferiority(copy_pasted_breach_ci, margin_reach_test) == "PASS"

    # Fixture 9: Margin enforcement, synthetic verdict evaluation, disjoint branch ordering
    def fixture_9_verdict_enforcement_and_disjoint_ordering():
        stats = {
            "combined": {"n": 50, "reach_by_10_rate": 0.50, "reach_by_expiry_rate": 0.85, "breach_rate": 0.10, "ever_rate": 0.20},
            "gex_only": {"n": 80, "reach_by_10_rate": 0.30, "reach_by_expiry_rate": 0.82, "breach_rate": 0.11, "ever_rate": 0.22},
            "proximity_only": {"n": 70, "reach_by_10_rate": 0.35, "reach_by_expiry_rate": 0.80, "breach_rate": 0.12, "ever_rate": 0.23},
            "baseline": {"n": 150, "reach_by_10_rate": 0.25, "reach_by_expiry_rate": 0.75, "breach_rate": 0.15, "ever_rate": 0.28},
        }
        gate_cis_pass = {
            "gate_a": (0.05, 0.25),
            "gate_b": (0.02, 0.20),
            "gate_c": (-0.05, 0.01),
            "gate_d": (-0.06, 0.01),
            "gate_e": (-0.08, 0.01),
        }

        # 9a: Missing or invalid margins MUST raise ValueError/TypeError
        try:
            compute_verdict(stats, gate_cis_pass, margin_breach=None, margin_intraperiod=0.03, margin_reach=0.02)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError when margin_breach is None")

        try:
            compute_verdict(stats, gate_cis_pass, margin_breach=0.02, margin_intraperiod=None, margin_reach=0.02)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError when margin_intraperiod is None")

        try:
            compute_verdict(stats, gate_cis_pass, margin_breach=0.02, margin_intraperiod=0.03, margin_reach=None)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError when margin_reach is None")

        try:
            compute_verdict(stats, gate_cis_pass, margin_breach=math.nan, margin_intraperiod=0.03, margin_reach=0.02)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError on NaN margin")

        # 9b: Synthetic placeholder margins (clearly labeled for testing)
        synthetic_margins = {
            "margin_breach": 0.02,
            "margin_intraperiod": 0.03,
            "margin_reach": 0.02,
        }
        res_pass = compute_verdict(stats, gate_cis_pass, **synthetic_margins)
        assert res_pass["overall"] == "PASS"

        # Gate A fails -> overall FAIL
        cis_fail_a = {**gate_cis_pass, "gate_a": (-0.15, -0.01)}
        assert compute_verdict(stats, cis_fail_a, **synthetic_margins)["overall"] == "FAIL"

        # Gate C fails -> overall FAIL
        cis_fail_c = {**gate_cis_pass, "gate_c": (0.025, 0.06)}
        assert compute_verdict(stats, cis_fail_c, **synthetic_margins)["overall"] == "FAIL"

        # Gate E fails -> overall FAIL
        cis_fail_e = {**gate_cis_pass, "gate_e": (0.03, 0.08)}
        assert compute_verdict(stats, cis_fail_e, **synthetic_margins)["overall"] == "FAIL"

        # Straddling zero on Gate A -> INCONCLUSIVE
        cis_inconclusive = {**gate_cis_pass, "gate_a": (-0.05, 0.15)}
        assert compute_verdict(stats, cis_inconclusive, **synthetic_margins)["overall"] == "INCONCLUSIVE"

        # Hard short-circuit: n < 30 -> INCONCLUSIVE regardless of CIs
        stats_small_n = {**stats, "combined": {**stats["combined"], "n": 22}}
        res_small = compute_verdict(stats_small_n, gate_cis_pass, **synthetic_margins)
        assert res_small["overall"] == "INCONCLUSIVE"
        assert "hard short-circuit" in res_small["reason"]

        # 9c: Disjoint-branch-ordering fix (round 4): upper == margin must resolve to PASS
        test_margin = 0.02
        # Upper bound exactly equals margin: (-0.01, 0.02) -> must be PASS, not INCONCLUSIVE or FAIL
        assert compute_gate_verdict_non_inferiority((-0.01, 0.02), test_margin) == "PASS"
        # Lower bound equals margin: (0.02, 0.05) -> must be INCONCLUSIVE, not FAIL
        assert compute_gate_verdict_non_inferiority((0.02, 0.05), test_margin) == "INCONCLUSIVE"
        # Lower bound strictly greater than margin: (0.0201, 0.05) -> FAIL
        assert compute_gate_verdict_non_inferiority((0.0201, 0.05), test_margin) == "FAIL"

    # Fixture 10: Missing midperiod price exclusion
    def fixture_10_missing_midperiod_data_gap():
        entry = fixture_entry(date="2026-03-02", expiry="2026-03-20")
        dates = pd.bdate_range("2026-03-02", "2026-03-20")
        # Wednesday 2026-03-04 is in market_sessions, but NaN in ticker frame
        prices = [100.0] * len(dates)
        frame = pd.DataFrame({"close": prices, "open": prices}, index=dates)
        frame.loc[pd.Timestamp("2026-03-04"), "close"] = math.nan

        res = score_trade(entry, frame, set(), set(dates))
        assert res["exclusion"] == "excluded_missing_midperiod_price"

    # Fixture 11: Shared resampling determinism with seed 20260918
    def fixture_11_shared_resampling_determinism():
        records = []
        for i in range(40):
            date = str((pd.Timestamp("2025-06-02") + pd.Timedelta(days=i % 10)).date())
            for prox, pos in ((True, True), (True, False), (False, True), (False, False)):
                records.append(dict(
                    date=date,
                    z_ma150=0.30 if prox else 1.50,
                    gex_regime="POSITIVE" if pos else "NEGATIVE",
                    reach_by_10=(prox and pos),
                    reach_by_expiry=True,
                    breach_at_expiry=not (prox and pos),
                    ever_breached_intraperiod=True,
                    pnl=10.0,
                    exclusion="",
                    cushion_pct=0.1,
                ))
        df = pd.DataFrame(records)
        cis_1 = bootstrap_all_gates(df, 0.50, seed=BOOTSTRAP_SEED, reps=1000)
        cis_2 = bootstrap_all_gates(df, 0.50, seed=BOOTSTRAP_SEED, reps=1000)

        for gate in ("gate_a", "gate_b", "gate_c", "gate_d", "gate_e"):
            assert math.isclose(cis_1[gate][0], cis_2[gate][0], abs_tol=1e-12)
            assert math.isclose(cis_1[gate][1], cis_2[gate][1], abs_tol=1e-12)

        # Single-gate bootstrap with identical seed produces identical CI
        single_ci_a = bootstrap(df, 0.50, "reach_by_10", "combined", "gex_only", seed=BOOTSTRAP_SEED, reps=1000)
        assert math.isclose(single_ci_a[0], cis_1["gate_a"][0], abs_tol=1e-12)
        assert math.isclose(single_ci_a[1], cis_1["gate_a"][1], abs_tol=1e-12)

    # Fixture 12: Zero combined-arm redraw and cap
    def fixture_12_zero_redraw_cap():
        starts = pd.to_datetime(["2025-01-27", "2025-06-02"])
        counts = np.array([[1, 1, 1, 0], [4, 2, 2, 1]])
        events = np.array([[1, 1, 1, 0], [3, 1, 1, 0]])
        choices = iter([np.array([0]), np.array([1])])
        rates, drawn, retries = bootstrap_iteration(
            None, starts, counts, events, 1, 0.50, 1, draw=lambda: next(choices)
        )
        assert retries == 1 and drawn[0] == starts[1] and rates[3] == 0

        calls = 0

        def always_empty():
            nonlocal calls
            calls += 1
            return np.array([0])

        try:
            bootstrap_iteration(None, starts, counts, events, 1, 0.50, 1, draw=always_empty)
        except RuntimeError as exc:
            assert "0.5" in str(exc) and "200" in str(exc)
            assert calls == 201, "Initial draw plus exactly 200 redraws"
        else:
            raise AssertionError("Expected RuntimeError after exhausted redraws")

    # Fixture 13: 2x2 contingency table diagnostic logic
    def fixture_13_contingency_table_logic():
        records = [
            # In prox (z<=0.50) & In prod (close-ema)/ema in [0, 0.10]
            dict(z_ma150=0.30, signal_close=105.0, ema150=100.0),
            # In prox & NOT in prod (close-ema)/ema = 0.15 > 0.10
            dict(z_ma150=0.40, signal_close=115.0, ema150=100.0),
            # NOT in prox (z=1.20) & In prod (close-ema)/ema = 0.05
            dict(z_ma150=1.20, signal_close=105.0, ema150=100.0),
            # NOT in prox & NOT in prod
            dict(z_ma150=1.50, signal_close=120.0, ema150=100.0),
        ]
        df = pd.DataFrame(records)
        diag = compute_overlap_diagnostic(df)
        assert diag["total"] == 4
        assert diag["cell_yy"] == 1
        assert diag["cell_yn"] == 1
        assert diag["cell_ny"] == 1
        assert diag["cell_nn"] == 1
        assert math.isclose(diag["p_prod_given_prox"], 0.5)
        assert math.isclose(diag["p_prox_given_prod"], 0.5)

    cases = [
        ("1. Baseline/GEX-only arm membership and inherited fields", fixture_1_baseline_and_gex_arm_parity),
        ("2. Hand-computed DIT and reach_by_D verification", fixture_2_hand_computed_dit),
        ("3. Nominal-vs-modeled-expiry tenor distinction", fixture_3_holiday_shifted_tenor_distinction),
        ("4. Censoring and reach accounting reconciliation", fixture_4_censoring_and_reach_accounting),
        ("5. Proximity mask direction (<= vs >=)", fixture_5_wrong_mask_direction),
        ("6. GEX-only comparator vs baseline", fixture_6_wrong_comparator_baseline_vs_gex_only),
        ("7. Terminal breach (Gate C) subtraction direction", fixture_7_terminal_breach_gate_c_subtraction_direction),
        ("8. Eventual reach (Gate E) subtraction direction", fixture_8_reach_by_expiry_gate_e_subtraction_direction),
        ("9. Verdict margin enforcement & disjoint ordering", fixture_9_verdict_enforcement_and_disjoint_ordering),
        ("10. Missing midperiod price exclusion", fixture_10_missing_midperiod_data_gap),
        ("11. Shared resampling determinism", fixture_11_shared_resampling_determinism),
        ("12. Redraw cap on empty combined arm", fixture_12_zero_redraw_cap),
        ("13. 2x2 contingency table diagnostic", fixture_13_contingency_table_logic),
    ]

    failures = 0
    print("\nRunning self-tests for scripts/backtest/z_ma150_proximity_dit_test.py...")
    for name, case in cases:
        try:
            case()
            print(f"PASS: {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL: {name}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    if failures:
        print(f"\nTotal failures: {failures}/{len(cases)}")
        raise SystemExit(1)
    else:
        print(f"\nAll {len(cases)} fixtures passed successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run synthetic fixtures only")
    parser.add_argument("--margin-breach", type=float, default=None, help="Terminal breach margin (user risk decision)")
    parser.add_argument("--margin-intraperiod", type=float, default=None, help="Intraperiod breach margin (user risk decision)")
    parser.add_argument("--margin-reach", type=float, default=None, help="Eventual reach margin (user risk decision)")
    parser.add_argument("--provenance-dir", type=Path, default=None, help="Directory to store provenance snapshot copies")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
    else:
        run(
            margin_breach=args.margin_breach,
            margin_intraperiod=args.margin_intraperiod,
            margin_reach=args.margin_reach,
            provenance_dir=args.provenance_dir,
        )


if __name__ == "__main__":
    main()
