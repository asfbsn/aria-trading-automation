#!/usr/bin/env python3
"""
scripts/ghost/ghost_iv_calib_report.py

Deterministic calibration scorecard and backfill logger for IBKR vs DoltHub IV.
Part of Ghost Phase 2 Step 1 (log-only calibration data collector).

CLI usage:
  scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_iv_calib_report.py
  scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_iv_calib_report.py --backfill
  scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_iv_calib_report.py --test
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import math
from pathlib import Path
import pickle
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import scipy.stats

# PRE-REGISTERED 2026-10-01 -- do not edit after any data has been inspected.
N_MIN_ROWS = 200
N_MIN_SESSIONS = 10
MIN_CLASS_N = 30
SPEARMAN_MIN = 0.9
GATE_AGREEMENT_MIN = 0.90
KAPPA_MIN = 0.7
THRESHOLD = 1.1

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG_CSV = REPO_ROOT / "state" / "ghost" / "iv_ibkr_log.csv"
DEFAULT_PKL = REPO_ROOT / "state" / "ghost" / "iv_live.pkl"
DEFAULT_BACKFILL_CSV = REPO_ROOT / "state" / "ghost" / "iv_dolt_backfill.csv"

BACKFILL_FIELDS = [
    "dolt_date",
    "ticker",
    "iv_current",
    "hv_current",
    "backfilled_ts_utc",
]


def capture_to_ny_date(capture_ts_utc_str: str, fallback_date_str: str = "") -> str:
    """Convert UTC capture timestamp to America/New_York date string (YYYY-MM-DD)."""
    if capture_ts_utc_str:
        try:
            ts_str = capture_ts_utc_str.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ny_dt = dt.astimezone(ZoneInfo("America/New_York"))
            return ny_dt.date().isoformat()
        except Exception:
            pass
    if fallback_date_str:
        return fallback_date_str.strip()
    return ""


def parse_float_safe(val: Any) -> Optional[float]:
    """Parse a float value, returning None if missing, NaN, infinite, or non-numeric."""
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def compute_cohen_kappa(ibkr_gates: List[bool], dolt_gates: List[bool]) -> Tuple[Any, bool]:
    """Compute Cohen's kappa for 2x2 gate table.

    Returns (kappa, is_degenerate).
    If either side has zero variance (all True or all False),
    prints DEGENERATE and counts as FAIL.
    """
    if not ibkr_gates or len(ibkr_gates) != len(dolt_gates):
        return "DEGENERATE", True

    if len(set(ibkr_gates)) < 2 or len(set(dolt_gates)) < 2:
        return "DEGENERATE", True

    n = len(ibkr_gates)
    tp = sum(1 for i, d in zip(ibkr_gates, dolt_gates) if i and d)
    tn = sum(1 for i, d in zip(ibkr_gates, dolt_gates) if not i and not d)
    fp = sum(1 for i, d in zip(ibkr_gates, dolt_gates) if i and not d)
    fn = sum(1 for i, d in zip(ibkr_gates, dolt_gates) if not i and d)

    p_o = (tp + tn) / n
    p_ibkr_pos = (tp + fp) / n
    p_ibkr_neg = (tn + fn) / n
    p_dolt_pos = (tp + fn) / n
    p_dolt_neg = (tn + fp) / n
    p_e = (p_ibkr_pos * p_dolt_pos) + (p_ibkr_neg * p_dolt_neg)

    if math.isclose(p_e, 1.0, rel_tol=1e-9):
        return "DEGENERATE", True

    kappa = (p_o - p_e) / (1.0 - p_e)
    return float(kappa), False


def compute_log_ratio_stats(x: List[float], y: List[float]) -> Tuple[float, float, float, float]:
    """Compute median, IQR, Q25, Q75 of log(x / y)."""
    log_ratios = [math.log(a / b) for a, b in zip(x, y)]
    med = float(np.median(log_ratios))
    q25, q75 = np.percentile(log_ratios, [25, 75])
    iqr = float(q75 - q25)
    return med, iqr, float(q25), float(q75)


def compute_spearman(x: List[float], y: List[float]) -> float:
    """Compute Spearman rank correlation coefficient."""
    if len(x) < 2:
        return float("nan")
    res = scipy.stats.spearmanr(x, y)
    stat = getattr(res, "statistic", None)
    if stat is None:
        stat = res[0]
    return float(stat)


def compute_gate_agreement(ibkr_gates: List[bool], dolt_gates: List[bool]) -> float:
    """Compute proportion of gate agreement."""
    if not ibkr_gates:
        return 0.0
    matches = sum(1 for i, d in zip(ibkr_gates, dolt_gates) if i == d)
    return matches / len(ibkr_gates)


def load_backfill_csv(backfill_path: Path) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Load backfill CSV into (dolt_date, ticker) -> {iv_current, hv_current}."""
    if not backfill_path.exists() or backfill_path.stat().st_size == 0:
        return {}
    res: Dict[Tuple[str, str], Dict[str, float]] = {}
    with backfill_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            d_date = (r.get("dolt_date") or "").strip()
            ticker = (r.get("ticker") or "").strip().upper()
            if not d_date or not ticker:
                continue
            iv = parse_float_safe(r.get("iv_current"))
            hv = parse_float_safe(r.get("hv_current"))
            if iv is not None and hv is not None:
                res[(d_date, ticker)] = {"iv_current": iv, "hv_current": hv}
    return res


def load_iv_live_pkl(pkl_path: Path) -> Dict[str, pd.DataFrame]:
    """Load iv_live.pkl cache."""
    if not pkl_path.exists():
        return {}
    try:
        with pkl_path.open("rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def lookup_pkl(
    pkl_data: Dict[str, pd.DataFrame], date_str: str, ticker: str
) -> Tuple[Optional[float], Optional[float]]:
    """Look up iv_current and hv_current in iv_live.pkl for (date_str, ticker)."""
    if not date_str or not ticker or ticker not in pkl_data:
        return None, None
    df = pkl_data[ticker]
    if df is None or df.empty:
        return None, None
    try:
        ts = pd.Timestamp(date_str)
        if ts in df.index:
            row = df.loc[ts]
        elif date_str in df.index:
            row = df.loc[date_str]
        else:
            return None, None

        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]

        iv = parse_float_safe(row.get("iv_current"))
        hv = parse_float_safe(row.get("hv_current"))
        return iv, hv
    except Exception:
        return None, None


def run_backfill(
    log_path: Path,
    pkl_path: Path,
    backfill_path: Path,
) -> int:
    """Run backfill mode: append missing (dolt_date, ticker) rows to backfill CSV.

    Uses fcntl.flock lock file and fail-loud header check.
    Returns number of new rows appended.
    """
    if not log_path.exists():
        print(f"Backfill skipped: log file does not exist: {log_path}")
        return 0
    if not pkl_path.exists():
        print(f"Backfill skipped: pkl file does not exist: {pkl_path}")
        return 0

    pkl_data = load_iv_live_pkl(pkl_path)
    if not pkl_data:
        print(f"Backfill skipped: pkl file empty or unparseable: {pkl_path}")
        return 0

    with log_path.open("r", newline="", encoding="utf-8") as f:
        log_rows = list(csv.DictReader(f))

    candidates: List[Dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for r in log_rows:
        ticker = (r.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        sb_date = (r.get("signal_bar_date") or "").strip()
        cap_ny_date = capture_to_ny_date(
            (r.get("capture_ts_utc") or ""), (r.get("capture_date") or "").strip()
        )

        dates_to_check = set()
        if sb_date:
            dates_to_check.add(sb_date)
        if cap_ny_date:
            dates_to_check.add(cap_ny_date)

        for d_date in dates_to_check:
            iv, hv = lookup_pkl(pkl_data, d_date, ticker)
            if iv is not None and hv is not None:
                candidates.append({
                    "dolt_date": d_date,
                    "ticker": ticker,
                    "iv_current": f"{iv:.6f}",
                    "hv_current": f"{hv:.6f}",
                    "backfilled_ts_utc": now_utc,
                })

    backfill_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = backfill_path.parent / f".{backfill_path.name}.lock"

    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing_pairs: Set[Tuple[str, str]] = set()
            header_needed = not backfill_path.exists() or backfill_path.stat().st_size == 0
            if not header_needed:
                with backfill_path.open("r", newline="", encoding="utf-8") as existing:
                    existing_header = next(csv.reader(existing), [])
                if existing_header != BACKFILL_FIELDS:
                    raise ValueError(
                        f"{backfill_path} header does not match current fields -- would silently "
                        f"misalign every column from here on. Existing: {existing_header}. "
                        f"Current: {BACKFILL_FIELDS}. Migrate or archive the old file before writing."
                    )
                with backfill_path.open("r", newline="", encoding="utf-8") as existing:
                    reader = csv.DictReader(existing)
                    for row in reader:
                        d = (row.get("dolt_date") or "").strip()
                        t = (row.get("ticker") or "").strip().upper()
                        if d and t:
                            existing_pairs.add((d, t))

            to_append: List[Dict[str, Any]] = []
            for c in candidates:
                pair = (c["dolt_date"], c["ticker"])
                if pair not in existing_pairs:
                    to_append.append(c)
                    existing_pairs.add(pair)

            if to_append:
                with backfill_path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=BACKFILL_FIELDS, extrasaction="ignore")
                    if header_needed:
                        writer.writeheader()
                    for item in to_append:
                        writer.writerow(item)

            print(
                f"Backfill complete: inspected {len(candidates)} candidates, "
                f"appended {len(to_append)} new rows to {backfill_path}"
            )
            return len(to_append)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def evaluate_comparison(
    name: str,
    x_ibkr: List[float],
    y_dolt: List[float],
    local_hvs: List[float],
    dolt_hvs: List[Optional[float]],
    session_dates: List[str],
    is_comp1: bool,
) -> Dict[str, Any]:
    """Calculate scorecard metrics and criterion outcomes for one comparison."""
    n = len(x_ibkr)
    unique_sessions = len(set(session_dates))

    if n == 0:
        return {
            "name": name,
            "n": 0,
            "sessions": 0,
            "status": "PENDING" if not is_comp1 else "FAIL",
            "is_pending": not is_comp1,
            "pass": False,
        }

    med, iqr, q25, q75 = compute_log_ratio_stats(x_ibkr, y_dolt)
    rho = compute_spearman(x_ibkr, y_dolt)

    ibkr_vrp = [x / h for x, h in zip(x_ibkr, local_hvs)]
    dolt_vrp = [y / h for y, h in zip(y_dolt, local_hvs)]

    ibkr_gates = [v >= THRESHOLD for v in ibkr_vrp]
    dolt_gates = [v >= THRESHOLD for v in dolt_vrp]

    class_ge = sum(1 for g in dolt_gates if g)
    class_lt = sum(1 for g in dolt_gates if not g)
    min_class = min(class_ge, class_lt)

    gate_agree = compute_gate_agreement(ibkr_gates, dolt_gates)
    kappa, is_degenerate = compute_cohen_kappa(ibkr_gates, dolt_gates)

    # Informational 1: True DoltHub gate agreement (dolt_iv / dolt_hv >= 1.1)
    true_pairs = [
        (ig, dy / dh >= THRESHOLD)
        for ig, dy, dh in zip(ibkr_gates, y_dolt, dolt_hvs)
        if dh is not None and dh > 0
    ]
    if true_pairs:
        true_dolt_agree = sum(1 for ig, dg in true_pairs if ig == dg) / len(true_pairs)
        true_dolt_n = len(true_pairs)
    else:
        true_dolt_agree = float("nan")
        true_dolt_n = 0

    # Informational 2: Count of rows with |log(ibkr_iv / dolt_iv)| > 0.3
    log_diffs = [abs(math.log(a / b)) for a, b in zip(x_ibkr, y_dolt)]
    large_log_count = sum(1 for diff in log_diffs if diff > 0.3)
    large_log_pct = (large_log_count / n) * 100.0 if n > 0 else 0.0

    # Criteria checks
    c_n = n >= N_MIN_ROWS
    c_sessions = unique_sessions >= N_MIN_SESSIONS
    c_class = min_class >= MIN_CLASS_N
    c_rho = (not math.isnan(rho)) and (rho >= SPEARMAN_MIN)
    c_agree = gate_agree >= GATE_AGREEMENT_MIN
    c_kappa = (not is_degenerate) and (isinstance(kappa, (int, float))) and (kappa >= KAPPA_MIN)

    all_pass = c_n and c_sessions and c_class and c_rho and c_agree and c_kappa

    return {
        "name": name,
        "n": n,
        "sessions": unique_sessions,
        "class_ge": class_ge,
        "class_lt": class_lt,
        "min_class": min_class,
        "median": med,
        "iqr": iqr,
        "q25": q25,
        "q75": q75,
        "rho": rho,
        "gate_agree": gate_agree,
        "kappa": kappa,
        "is_degenerate": is_degenerate,
        "true_dolt_agree": true_dolt_agree,
        "true_dolt_n": true_dolt_n,
        "large_log_count": large_log_count,
        "large_log_pct": large_log_pct,
        "c_n": c_n,
        "c_sessions": c_sessions,
        "c_class": c_class,
        "c_rho": c_rho,
        "c_agree": c_agree,
        "c_kappa": c_kappa,
        "pass": all_pass,
        "is_pending": False,
    }


def generate_scorecard_report(
    log_path: Path,
    backfill_path: Path,
    pkl_path: Path,
) -> Tuple[str, bool]:
    """Generate calibration scorecard text report.

    Returns (report_text, overall_pass).
    """
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("           IBKR vs DoltHub IV Calibration Scorecard (Pre-registered)")
    lines.append("=" * 80)
    lines.append(f"Log CSV:      {log_path}")
    lines.append(f"Backfill CSV: {backfill_path}")
    lines.append(f"IV Live PKL:  {pkl_path}")

    if not log_path.exists():
        lines.append("-" * 80)
        lines.append(f"ERROR: Log file not found: {log_path}")
        lines.append("Counts by status: none")
        lines.append("Usable rows: 0 (min required: 200), Sessions: 0 (min required: 10)")
        lines.append("INSUFFICIENT DATA")
        lines.append("-" * 80)
        lines.append("OVERALL: FAIL (INSUFFICIENT DATA)")
        lines.append("=" * 80)
        return "\n".join(lines), False

    with log_path.open("r", newline="", encoding="utf-8") as f:
        log_rows = list(csv.DictReader(f))

    status_counts: Dict[str, int] = {}
    for r in log_rows:
        st = (r.get("status") or "").strip() or "empty"
        status_counts[st] = status_counts.get(st, 0) + 1

    lines.append(f"Total rows in log: {len(log_rows)}")
    lines.append("Counts by status:")
    for st, count in sorted(status_counts.items()):
        lines.append(f"  {st:20s}: {count}")

    backfill_map = load_backfill_csv(backfill_path)
    pkl_data = load_iv_live_pkl(pkl_path)

    # Filter usable rows: status == 'ok' and local_hv30, ibkr_iv, dolt_iv_signal_bar all present/finite
    usable_rows: List[Dict[str, Any]] = []
    comp1_x: List[float] = []
    comp1_y: List[float] = []
    comp1_local_hv: List[float] = []
    comp1_dolt_hv: List[Optional[float]] = []
    comp1_sessions: List[str] = []

    seen_ticker_days = set()
    for r in log_rows:
        if (r.get("status") or "").strip() != "ok":
            continue

        local_hv = parse_float_safe(r.get("local_hv30"))
        ibkr_iv = parse_float_safe(r.get("ibkr_iv"))
        if local_hv is None or local_hv <= 0 or ibkr_iv is None or ibkr_iv <= 0:
            continue

        sb_date = (r.get("signal_bar_date") or "").strip()
        ticker = (r.get("ticker") or "").strip().upper()
        if (sb_date, ticker) in seen_ticker_days:
            continue
        seen_ticker_days.add((sb_date, ticker))

        # Frozen dolt_iv_signal_bar in log row wins over backfill
        dolt_iv_signal = parse_float_safe(r.get("dolt_iv_signal_bar"))
        dolt_hv_signal = parse_float_safe(r.get("dolt_hv_signal_bar"))

        if dolt_iv_signal is None or dolt_iv_signal <= 0:
            if (sb_date, ticker) in backfill_map:
                bf_entry = backfill_map[(sb_date, ticker)]
                dolt_iv_signal = bf_entry.get("iv_current")
                if dolt_hv_signal is None or dolt_hv_signal <= 0:
                    dolt_hv_signal = bf_entry.get("hv_current")

        if dolt_iv_signal is None or dolt_iv_signal <= 0:
            continue

        usable_rows.append(r)
        comp1_x.append(ibkr_iv)
        comp1_y.append(dolt_iv_signal)
        comp1_local_hv.append(local_hv)
        comp1_dolt_hv.append(dolt_hv_signal)
        comp1_sessions.append(sb_date)

    n_usable = len(usable_rows)
    distinct_sessions = len(set(comp1_sessions))

    lines.append(f"Usable rows:       {n_usable} (min required: {N_MIN_ROWS})")
    lines.append(f"Distinct sessions: {distinct_sessions} (min required: {N_MIN_SESSIONS})")
    lines.append("-" * 80)

    if n_usable < N_MIN_ROWS or distinct_sessions < N_MIN_SESSIONS:
        lines.append("INSUFFICIENT DATA")
        lines.append(
            f"Requirements not met: Usable rows ({n_usable} / {N_MIN_ROWS}), "
            f"Sessions ({distinct_sessions} / {N_MIN_SESSIONS})"
        )
        lines.append("-" * 80)
        lines.append("OVERALL: FAIL (INSUFFICIENT DATA)")
        lines.append("=" * 80)
        return "\n".join(lines), False

    # Comparison 1 Evaluation
    res1 = evaluate_comparison(
        name="Comparison 1 (Gate Experience: ibkr_iv vs dolt_iv_signal_bar)",
        x_ibkr=comp1_x,
        y_dolt=comp1_y,
        local_hvs=comp1_local_hv,
        dolt_hvs=comp1_dolt_hv,
        session_dates=comp1_sessions,
        is_comp1=True,
    )

    lines.append(res1["name"])
    lines.append(f"  Usable ticker-days (n):       {res1['n']:4d}  [{'PASS' if res1['c_n'] else 'FAIL'}: >= {N_MIN_ROWS}]")
    lines.append(f"  Distinct sessions:             {res1['sessions']:4d}  [{'PASS' if res1['c_sessions'] else 'FAIL'}: >= {N_MIN_SESSIONS}]")
    lines.append(
        f"  Dolt class counts:            >=1.1: {res1['class_ge']}, <1.1: {res1['class_lt']}  "
        f"[{'PASS' if res1['c_class'] else 'FAIL'}: min({res1['class_ge']}, {res1['class_lt']}) = {res1['min_class']} >= {MIN_CLASS_N}]"
    )
    lines.append(f"  log(ibkr_iv/dolt_iv) median:  {res1['median']:+.4f}")
    lines.append(f"  log(ibkr_iv/dolt_iv) IQR:     {res1['iqr']:.4f} (Q25: {res1['q25']:+.4f}, Q75: {res1['q75']:+.4f})")
    lines.append(f"  Spearman rho:                 {res1['rho']:.4f}  [{'PASS' if res1['c_rho'] else 'FAIL'}: >= {SPEARMAN_MIN:.2f}]")
    lines.append(
        f"  Gate agreement (local HV30):  {res1['gate_agree'] * 100:.2f}%  "
        f"[{'PASS' if res1['c_agree'] else 'FAIL'}: >= {GATE_AGREEMENT_MIN * 100:.2f}%]"
    )
    kappa_str = "DEGENERATE" if res1["is_degenerate"] else f"{res1['kappa']:.4f}"
    lines.append(f"  Cohen's kappa:                {kappa_str}  [{'PASS' if res1['c_kappa'] else 'FAIL'}: >= {KAPPA_MIN:.2f}]")
    lines.append("  -- Informational --")
    if not math.isnan(res1["true_dolt_agree"]):
        lines.append(
            f"  Gate agreement (True Dolt HV): {res1['true_dolt_agree'] * 100:.2f}% (n={res1['true_dolt_n']} with dolt_hv)"
        )
    else:
        lines.append("  Gate agreement (True Dolt HV): N/A (no dolt_hv available)")
    lines.append(
        f"  |log(ibkr_iv/dolt_iv)| > 0.3:  {res1['large_log_count']} ({res1['large_log_pct']:.2f}%) (likely earnings jumps)"
    )
    lines.append(f"  Comparison 1 Outcome:         {'PASS' if res1['pass'] else 'FAIL'}")
    lines.append("-" * 80)

    # Comparison 2 Data Collection
    comp2_x: List[float] = []
    comp2_y: List[float] = []
    comp2_local_hv: List[float] = []
    comp2_dolt_hv: List[Optional[float]] = []
    comp2_sessions: List[str] = []

    for r in usable_rows:
        ticker = (r.get("ticker") or "").strip().upper()
        cap_ny_date = capture_to_ny_date(
            (r.get("capture_ts_utc") or ""), (r.get("capture_date") or "").strip()
        )
        if not cap_ny_date:
            continue

        ibkr_iv = parse_float_safe(r.get("ibkr_iv"))
        local_hv = parse_float_safe(r.get("local_hv30"))
        if ibkr_iv is None or local_hv is None:
            continue

        dolt_iv_cap = None
        dolt_hv_cap = None
        if (cap_ny_date, ticker) in backfill_map:
            bf_entry = backfill_map[(cap_ny_date, ticker)]
            dolt_iv_cap = bf_entry.get("iv_current")
            dolt_hv_cap = bf_entry.get("hv_current")
        elif pkl_data:
            dolt_iv_cap, dolt_hv_cap = lookup_pkl(pkl_data, cap_ny_date, ticker)

        if dolt_iv_cap is not None and dolt_iv_cap > 0:
            comp2_x.append(ibkr_iv)
            comp2_y.append(dolt_iv_cap)
            comp2_local_hv.append(local_hv)
            comp2_dolt_hv.append(dolt_hv_cap)
            comp2_sessions.append(cap_ny_date)

    lines.append("Comparison 2 (Contemporaneous: ibkr_iv vs dolt_iv_capture)")
    if len(comp2_x) == 0:
        lines.append("  Status: PENDING (0 available capture sessions published by DoltHub)")
        lines.append("  (Informational: comparison 2 becomes active once DoltHub publishes capture dates)")
    else:
        res2 = evaluate_comparison(
            name="Comparison 2",
            x_ibkr=comp2_x,
            y_dolt=comp2_y,
            local_hvs=comp2_local_hv,
            dolt_hvs=comp2_dolt_hv,
            session_dates=comp2_sessions,
            is_comp1=False,
        )
        is_info = res2["n"] < N_MIN_ROWS
        status_suffix = " (INFORMATIONAL: n < 200)" if is_info else ""
        lines.append(f"  Usable ticker-days (n):       {res2['n']:4d}  [{'PASS' if res2['c_n'] else 'FAIL'}: >= {N_MIN_ROWS}]")
        lines.append(f"  Distinct sessions:             {res2['sessions']:4d}  [{'PASS' if res2['c_sessions'] else 'FAIL'}: >= {N_MIN_SESSIONS}]")
        lines.append(
            f"  Dolt class counts:            >=1.1: {res2['class_ge']}, <1.1: {res2['class_lt']}  "
            f"[{'PASS' if res2['c_class'] else 'FAIL'}: min({res2['class_ge']}, {res2['class_lt']}) = {res2['min_class']} >= {MIN_CLASS_N}]"
        )
        lines.append(f"  log(ibkr_iv/dolt_iv) median:  {res2['median']:+.4f}")
        lines.append(f"  log(ibkr_iv/dolt_iv) IQR:     {res2['iqr']:.4f} (Q25: {res2['q25']:+.4f}, Q75: {res2['q75']:+.4f})")
        lines.append(f"  Spearman rho:                 {res2['rho']:.4f}  [{'PASS' if res2['c_rho'] else 'FAIL'}: >= {SPEARMAN_MIN:.2f}]")
        lines.append(
            f"  Gate agreement (local HV30):  {res2['gate_agree'] * 100:.2f}%  "
            f"[{'PASS' if res2['c_agree'] else 'FAIL'}: >= {GATE_AGREEMENT_MIN * 100:.2f}%]"
        )
        kappa_str2 = "DEGENERATE" if res2["is_degenerate"] else f"{res2['kappa']:.4f}"
        lines.append(f"  Cohen's kappa:                {kappa_str2}  [{'PASS' if res2['c_kappa'] else 'FAIL'}: >= {KAPPA_MIN:.2f}]")
        lines.append("  -- Informational --")
        if not math.isnan(res2["true_dolt_agree"]):
            lines.append(
                f"  Gate agreement (True Dolt HV): {res2['true_dolt_agree'] * 100:.2f}% (n={res2['true_dolt_n']} with dolt_hv)"
            )
        else:
            lines.append("  Gate agreement (True Dolt HV): N/A (no dolt_hv available)")
        lines.append(
            f"  |log(ibkr_iv/dolt_iv)| > 0.3:  {res2['large_log_count']} ({res2['large_log_pct']:.2f}%) (likely earnings jumps)"
        )
        lines.append(f"  Comparison 2 Outcome:         {'PASS' if res2['pass'] else 'FAIL'}{status_suffix}")

    lines.append("-" * 80)
    overall_pass = res1["pass"]
    lines.append(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
    lines.append("=" * 80)

    return "\n".join(lines), overall_pass


def run_self_tests() -> None:
    """Execute test suite verifying metrics, backfill idempotency, degenerate cases."""
    print("Running self-tests for ghost_iv_calib_report.py...")

    # 1. Known Cohen's kappa test
    # 2x2 table: TP=45, FP=5, FN=5, TN=45 -> n=100
    # p_o = 0.90, p_e = 0.50 -> kappa = 0.40 / 0.50 = 0.80
    ibkr_k = [True] * 45 + [True] * 5 + [False] * 5 + [False] * 45
    dolt_k = [True] * 45 + [False] * 5 + [True] * 5 + [False] * 45
    k_val, is_deg = compute_cohen_kappa(ibkr_k, dolt_k)
    assert not is_deg, "Known kappa should not be degenerate"
    assert math.isclose(k_val, 0.80, abs_tol=1e-5), f"Expected kappa 0.80, got {k_val}"
    print("  [PASS] Known Cohen's kappa (0.80)")

    # 2. DEGENERATE Cohen's kappa test (zero variance on one side)
    ibkr_deg = [True] * 100
    dolt_deg = [True] * 80 + [False] * 20
    k_deg, is_deg2 = compute_cohen_kappa(ibkr_deg, dolt_deg)
    assert is_deg2 and k_deg == "DEGENERATE", "Zero variance should be DEGENERATE"
    print("  [PASS] DEGENERATE Cohen's kappa (zero variance)")

    # 3. Known Spearman rho test
    x_mono = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    y_mono = [0.15, 0.25, 0.35, 0.45, 0.55, 0.65]
    rho = compute_spearman(x_mono, y_mono)
    assert math.isclose(rho, 1.0, abs_tol=1e-5), f"Expected rho 1.0, got {rho}"

    y_inv = list(reversed(y_mono))
    rho_inv = compute_spearman(x_mono, y_inv)
    assert math.isclose(rho_inv, -1.0, abs_tol=1e-5), f"Expected rho -1.0, got {rho_inv}"
    print("  [PASS] Known Spearman rho (+1.0 and -1.0)")

    # 4. Log ratio median and IQR test
    x_r = [0.20, 0.20, 0.20, 0.20]
    y_r = [0.20, 0.20, 0.20, 0.20]
    med, iqr, q25, q75 = compute_log_ratio_stats(x_r, y_r)
    assert math.isclose(med, 0.0, abs_tol=1e-9)
    assert math.isclose(iqr, 0.0, abs_tol=1e-9)
    print("  [PASS] Log ratio median & IQR")

    # 5. Gate agreement test
    g_ibkr = [True, True, False, False]
    g_dolt = [True, False, False, True]
    agree = compute_gate_agreement(g_ibkr, g_dolt)
    assert math.isclose(agree, 0.50, abs_tol=1e-5)
    print("  [PASS] Gate agreement")

    # 6. Backfill idempotency, fail-loud header check, lock file
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        log_csv = tmp_path / "iv_ibkr_log.csv"
        pkl_file = tmp_path / "iv_live.pkl"
        backfill_csv = tmp_path / "iv_dolt_backfill.csv"

        # Create synthetic pkl
        dates = [pd.Timestamp(f"2026-09-{d:02d}") for d in range(15, 26)]
        pkl_dict = {
            "AAPL": pd.DataFrame({
                "iv_current": [0.25 + 0.001 * i for i in range(len(dates))],
                "hv_current": [0.20 + 0.001 * i for i in range(len(dates))],
            }, index=dates),
            "MSFT": pd.DataFrame({
                "iv_current": [0.30 + 0.001 * i for i in range(len(dates))],
                "hv_current": [0.22 + 0.001 * i for i in range(len(dates))],
            }, index=dates),
        }
        with pkl_file.open("wb") as f:
            pickle.dump(pkl_dict, f)

        # Create synthetic log CSV with 2 rows
        log_rows = [
            {
                "capture_date": "2026-09-16",
                "signal_bar_date": "2026-09-15",
                "pool_id": "test_pool",
                "ticker": "AAPL",
                "conid": "11111",
                "status": "ok",
                "ibkr_iv": "0.26",
                "ibkr_hv_info": "0.21",
                "top_status": "REALTIME",
                "capture_ts_utc": "2026-09-16T14:30:00Z",
                "capture_ts_source": "last_ts",
                "local_hv30": "0.20",
                "vrp_ibkr": "1.3",
                "dolt_iv_signal_bar": "",
                "dolt_hv_signal_bar": "",
                "z_ma150": "1.6",
                "pool_size_pre_sample": "10",
                "sampled": "True",
                "code_version_hash": "testhash",
            },
            {
                "capture_date": "2026-09-17",
                "signal_bar_date": "2026-09-16",
                "pool_id": "test_pool",
                "ticker": "MSFT",
                "conid": "22222",
                "status": "ok",
                "ibkr_iv": "0.31",
                "ibkr_hv_info": "0.23",
                "top_status": "REALTIME",
                "capture_ts_utc": "2026-09-17T14:30:00Z",
                "capture_ts_source": "last_ts",
                "local_hv30": "0.22",
                "vrp_ibkr": "1.4",
                "dolt_iv_signal_bar": "",
                "dolt_hv_signal_bar": "",
                "z_ma150": "1.7",
                "pool_size_pre_sample": "10",
                "sampled": "True",
                "code_version_hash": "testhash",
            },
        ]
        from ghost_iv_extract import CSV_FIELDS
        with log_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(log_rows)

        # Run backfill pass 1
        n_written1 = run_backfill(log_csv, pkl_file, backfill_csv)
        assert n_written1 == 4, f"Expected 4 backfill rows (2 dates x 2 tickers), got {n_written1}"
        assert backfill_csv.exists(), "Backfill CSV should exist after pass 1"

        # Run backfill pass 2 (idempotency check)
        n_written2 = run_backfill(log_csv, pkl_file, backfill_csv)
        assert n_written2 == 0, f"Expected 0 new rows on idempotent pass 2, got {n_written2}"
        print("  [PASS] Backfill write & idempotency")

        # Test fail-loud header check on corrupt backfill CSV
        corrupt_csv = tmp_path / "corrupt_backfill.csv"
        with corrupt_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["wrong_col1", "wrong_col2"])
            w.writeheader()
            w.writerow({"wrong_col1": "a", "wrong_col2": "b"})

        try:
            run_backfill(log_csv, pkl_file, corrupt_csv)
            raise AssertionError("Corrupt header should have raised ValueError")
        except ValueError as e:
            assert "header does not match current fields" in str(e)
            print("  [PASS] Backfill fail-loud header mismatch check")

    # 7. End-to-end Scorecard: INSUFFICIENT DATA branch
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        sparse_log = tmp_path / "sparse_log.csv"
        empty_bf = tmp_path / "empty_bf.csv"
        empty_pkl = tmp_path / "empty.pkl"

        with sparse_log.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerow({
                "capture_date": "2026-09-16",
                "signal_bar_date": "2026-09-15",
                "ticker": "AAPL",
                "status": "ok",
                "ibkr_iv": "0.25",
                "local_hv30": "0.20",
                "dolt_iv_signal_bar": "0.25",
            })

        report_txt, passed = generate_scorecard_report(sparse_log, empty_bf, empty_pkl)
        assert not passed, "Sparse data must return overall False"
        assert "INSUFFICIENT DATA" in report_txt
        print("  [PASS] Scorecard INSUFFICIENT DATA branch")

    # 8. End-to-end Scorecard: 200+ rows, 10+ sessions PASSING case
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        full_log = tmp_path / "full_log.csv"
        bf_csv = tmp_path / "bf.csv"
        pkl_file = tmp_path / "full.pkl"

        rows = []
        # Generate 220 rows across 11 sessions, 20 tickers per session
        # Ensure class counts >= 30: half with vrp >= 1.1, half with vrp < 1.1
        # High Spearman correlation and gate agreement
        for s_idx in range(11):
            s_date = f"2026-09-{15 + s_idx:02d}"
            c_date = f"2026-09-{16 + s_idx:02d}"
            for t_idx in range(20):
                ticker = f"TICK{t_idx:02d}"
                # Alternate between VRP >= 1.1 and VRP < 1.1
                local_hv = 0.20
                if t_idx < 10:
                    # vrp = 0.26 / 0.20 = 1.30 (>= 1.1)
                    dolt_iv = 0.26 + 0.001 * t_idx
                    ibkr_iv = dolt_iv + 0.001  # closely matched
                else:
                    # vrp = 0.18 / 0.20 = 0.90 (< 1.1)
                    dolt_iv = 0.18 + 0.001 * t_idx
                    ibkr_iv = dolt_iv - 0.001  # closely matched

                rows.append({
                    "capture_date": c_date,
                    "signal_bar_date": s_date,
                    "pool_id": f"pool_{s_date}",
                    "ticker": ticker,
                    "conid": str(1000 + t_idx),
                    "status": "ok",
                    "ibkr_iv": f"{ibkr_iv:.4f}",
                    "ibkr_hv_info": "0.20",
                    "top_status": "REALTIME",
                    "capture_ts_utc": f"{c_date}T14:30:00Z",
                    "capture_ts_source": "last_ts",
                    "local_hv30": f"{local_hv:.4f}",
                    "vrp_ibkr": f"{ibkr_iv / local_hv:.4f}",
                    "dolt_iv_signal_bar": f"{dolt_iv:.4f}",
                    "dolt_hv_signal_bar": "0.20",
                    "z_ma150": "1.8",
                    "pool_size_pre_sample": "20",
                    "sampled": "True",
                    "code_version_hash": "testversion",
                })

        with full_log.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)

        report_txt, passed = generate_scorecard_report(full_log, bf_csv, pkl_file)
        assert passed, f"Expected full scorecard PASS, got FAIL:\n{report_txt}"
        assert "OVERALL: PASS" in report_txt
        assert "Comparison 1 Outcome:         PASS" in report_txt
        print("  [PASS] Full 220-row / 11-session Scorecard PASS case")

    # 9. Scorecard FAILING case (gate agreement failure)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        fail_log = tmp_path / "fail_log.csv"
        bf_csv = tmp_path / "bf.csv"
        pkl_file = tmp_path / "full.pkl"

        rows = []
        for s_idx in range(11):
            s_date = f"2026-09-{15 + s_idx:02d}"
            c_date = f"2026-09-{16 + s_idx:02d}"
            for t_idx in range(20):
                ticker = f"TICK{t_idx:02d}"
                local_hv = 0.20
                # Invert gate decision completely: dolt says >= 1.1, ibkr says < 1.1
                dolt_iv = 0.26
                ibkr_iv = 0.16
                rows.append({
                    "capture_date": c_date,
                    "signal_bar_date": s_date,
                    "ticker": ticker,
                    "status": "ok",
                    "ibkr_iv": f"{ibkr_iv:.4f}",
                    "local_hv30": f"{local_hv:.4f}",
                    "dolt_iv_signal_bar": f"{dolt_iv:.4f}",
                    "dolt_hv_signal_bar": "0.20",
                })

        with fail_log.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

        report_txt, passed = generate_scorecard_report(fail_log, bf_csv, pkl_file)
        assert not passed, "Inverted gate must produce FAIL"
        assert "OVERALL: FAIL" in report_txt
        print("  [PASS] Scorecard gate disagreement FAIL case")

    print("ALL TESTS PASSED CLEANLY.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ghost Phase 2 Step 1: Pre-registered IV calibration scorecard & backfill logger."
    )
    parser.add_argument(
        "--log-csv",
        type=Path,
        default=DEFAULT_LOG_CSV,
        help=f"Path to iv_ibkr_log.csv (default: {DEFAULT_LOG_CSV})",
    )
    parser.add_argument(
        "--pkl",
        type=Path,
        default=DEFAULT_PKL,
        help=f"Path to iv_live.pkl (default: {DEFAULT_PKL})",
    )
    parser.add_argument(
        "--backfill-csv",
        type=Path,
        default=DEFAULT_BACKFILL_CSV,
        help=f"Path to iv_dolt_backfill.csv (default: {DEFAULT_BACKFILL_CSV})",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Run backfill mode to append historical DoltHub IV/HV from pkl to backfill CSV",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run deterministic unit and scorecard test suite",
    )

    args = parser.parse_args()

    if args.test:
        run_self_tests()
        sys.exit(0)

    if args.backfill:
        run_backfill(
            log_path=args.log_csv,
            pkl_path=args.pkl,
            backfill_path=args.backfill_csv,
        )
        sys.exit(0)

    report_text, _ = generate_scorecard_report(
        log_path=args.log_csv,
        backfill_path=args.backfill_csv,
        pkl_path=args.pkl,
    )
    print(report_text)


if __name__ == "__main__":
    main()
