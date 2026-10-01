#!/usr/bin/env python3
"""ARIA Ghost Candidate Prescreen v2.

Generates dry-run bull-put-spread candidates based on technical structure
and DoltHub VRP entry gating. No Claude or IBKR interaction involved.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import random
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import uuid
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "backtest"))

from bps_signal_engine_v2 import BullPutSpreadSignalEngine  # noqa: E402
import dix_fetcher_v2  # noqa: E402
from signal_core import MIN_BARS, ema  # noqa: E402

DEFAULT_IV_CACHE = REPO_ROOT / "state" / "ghost" / "iv_live.pkl"
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "universe.csv"

MIN_Z_MA150_COMBINED = 1.5  # research/proposals/2026-09-17-gex-pin-ma150-extension.md — accepted screening candidate (lowest passing threshold of the three tested: 1.5, 2.0, 2.5)
CANDIDATE_CAP = 15  # hard cap on the per-run IBKR quote-capture loop — observed ~40s/candidate against a 15-minute CLAUDE_TIMEOUT in daily-scan-ghost.sh
FILTER_TAG = "gex_positive_z_ma150_ge_1.5"
FILTER_TAG_BASELINE = "gex_positive_z_ma150_ge_1.5_baseline_fallback"
IVPOOL_SAMPLE_N = 40
HV_DEFINITION = "std (ddof=1) of last 30 close-to-close log returns (31 closes ending at signal bar) * sqrt(252)"

# Scope limits on what this collection can and cannot later claim (Astra,
# 2026-09-17 -- keep these narrow, do not let a future readout overreach):
#   1. A Ghost Ledger milestone of N>=30 rows across >=10 distinct
#      signal_date sessions is an OPERATIONAL collection target, not a
#      statistical confirmation threshold. It says nothing about power or
#      re-establishes the research proposal's own frozen bootstrap protocol.
#   2. candidates_list here is combined-filter-only (GEX POSITIVE and
#      z_ma150 >= 1.5) -- this pipeline no longer captures a contemporaneous
#      non-combined population, so Ghost data alone cannot measure
#      "improvement vs. baseline" going forward. That comparison exists only
#      in the frozen historical backtest (gex_pin_ma150_extension_test.py).
#   3. suppress_reentry=False (below) means the SAME ticker can re-qualify
#      on consecutive sessions with no cooldown -- a structurally different
#      population from the suppress_reentry=True pool the research script
#      validated performance on. Valid for this system's actual purpose
#      (displayed-quote friction sampling on the accepted filter); not a
#      like-for-like population for re-deriving the backtest's P&L numbers.


def load_holidays(path: Path) -> set[str]:
    """Load holiday dates from us-market-holidays.txt (YYYY-MM-DD lines)."""
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip())
    }


def get_last_completed_session(
    as_of: Optional[datetime.date] = None,
    holidays: Optional[set[str]] = None,
) -> datetime.date:
    """Derive the most recent completed trading session before as_of date.

    (i.e. yesterday relative to when this script runs, or the most recent session
    if run after a weekend/holiday).
    """
    if as_of is None:
        as_of = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    if holidays is None:
        holidays = set()
    cur = as_of - datetime.timedelta(days=1)
    while cur.weekday() >= 5 or cur.isoformat() in holidays:  # 5=Saturday, 6=Sunday
        cur -= datetime.timedelta(days=1)
    return cur


def get_ticker_df(df: pd.DataFrame, ticker: str, total_tickers: int) -> pd.DataFrame:
    """Extract single ticker DataFrame from yf.download result, mirroring scripts/prescreen.py."""
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.levels[0]:
            try:
                sub_df = df[ticker]
                if isinstance(sub_df, pd.DataFrame):
                    return sub_df
            except (KeyError, TypeError):
                return pd.DataFrame()
        return pd.DataFrame()
    else:
        if total_tickers == 1:
            return df
        return pd.DataFrame()


def build_data_map_from_yfinance(
    universe_tickers: List[str],
    period: str = "400d",
    target_signal_date: Optional[datetime.date] = None,
) -> Tuple[Dict[str, pd.DataFrame], List[Dict[str, str]]]:
    """Download daily OHLCV via yfinance and construct data_map for BullPutSpreadSignalEngine."""
    if not universe_tickers:
        return {}, []

    df = yf.download(
        universe_tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=True,
    )

    data_map: Dict[str, pd.DataFrame] = {}
    failures: List[Dict[str, str]] = []
    total_count = len(universe_tickers)

    for ticker in universe_tickers:
        tdf = get_ticker_df(df, ticker, total_count)
        if tdf.empty:
            failures.append({"ticker": ticker, "reason": "no_data"})
            continue

        req_cols = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in tdf.columns for c in req_cols):
            failures.append({"ticker": ticker, "reason": "missing_columns"})
            continue

        tdf = tdf.dropna(subset=req_cols)
        if len(tdf) < MIN_BARS:
            failures.append({"ticker": ticker, "reason": "insufficient_bars"})
            continue

        tdf = tdf.rename(columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        tdf = tdf[["open", "high", "low", "close", "volume"]].sort_index()

        # Defend against target session being the last bar in tdf:
        # BullPutSpreadSignalEngine skips i if i + 1 >= len(dates) to simulate next-bar entry.
        # If tdf ends on or before target_signal_date, append a forward-filled next business day.
        if target_signal_date is not None:
            last_date = tdf.index[-1].date() if hasattr(tdf.index[-1], "date") else tdf.index[-1]
            if last_date <= target_signal_date:
                next_day = target_signal_date + pd.offsets.BDay(1)
                dummy_row = pd.DataFrame(
                    {
                        "open": [tdf["close"].iloc[-1]],
                        "high": [tdf["close"].iloc[-1]],
                        "low": [tdf["close"].iloc[-1]],
                        "close": [tdf["close"].iloc[-1]],
                        "volume": [0],
                    },
                    index=[next_day],
                )
                tdf = pd.concat([tdf, dummy_row])

        data_map[ticker] = tdf

    return data_map, failures


def filter_entries_to_target_session(
    entries: List[Dict[str, Any]],
    data_map: Dict[str, pd.DataFrame],
    target_signal_date_str: str,
) -> List[Tuple[Dict[str, Any], str]]:
    """Filter engine entries to ONLY those whose signal bar date matches target_signal_date_str.

    Returns list of (entry, signal_bar_date_str).
    """
    surviving: List[Tuple[Dict[str, Any], str]] = []
    for entry in entries:
        code = entry["code"]
        trade_date_str = entry["date"]
        df = data_map.get(code)
        if df is None:
            continue

        # In BullPutSpreadSignalEngine: trade_date_str = dates[i + 1], signal bar is dates[i].
        dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in df.index]
        try:
            fill_idx = dates.index(trade_date_str)
        except ValueError:
            continue

        if fill_idx < 1:
            continue

        signal_bar_date_str = dates[fill_idx - 1]
        if signal_bar_date_str == target_signal_date_str:
            surviving.append((entry, signal_bar_date_str))

    return surviving


def compute_z_ma150_for_entry(
    entry: Dict[str, Any],
    data_map: Dict[str, pd.DataFrame],
) -> float | None:
    """Compute z_ma150 post-hoc for baseline entries, matching BullPutSpreadSignalEngine.generate()."""
    code = entry.get("code")
    trade_date_str = entry.get("date")
    df = data_map.get(code)
    if df is None:
        return None
    df = df.sort_index()

    dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in df.index]
    try:
        fill_idx = dates.index(trade_date_str)
    except ValueError:
        return None

    if fill_idx < 1:
        return None

    i = fill_idx - 1
    closes = df["close"].tolist()
    signal_closes = closes[: i + 1]
    close = closes[i]

    ma150_gate = ema(signal_closes, 150)
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
            return float(z_val) if math.isfinite(z_val) else None
    return None


def generate_candidate_id(
    trade_date: str,
    ticker: str,
    mode: str,
    short_strike: float,
    long_strike: float,
    expiry: str,
) -> str:
    """Stable hash: sha256 truncated of trade_date|ticker|mode|short_strike|long_strike|expiry."""
    key = f"{trade_date}|{ticker}|{mode}|{short_strike}|{long_strike}|{expiry}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def apply_extension_filter_and_cap(
    surviving: List[Tuple[Dict[str, Any], str]],
    gex_regime: str,
    seed: str,
    min_z: float = MIN_Z_MA150_COMBINED,
    cap: int = CANDIDATE_CAP,
) -> Tuple[List[Tuple[Dict[str, Any], str]], List[Tuple[Dict[str, Any], str]], bool]:
    """Filter surviving candidates by GEX POSITIVE and z_ma150 >= min_z, then apply deterministic cap."""
    combined_pass: List[Tuple[Dict[str, Any], str]] = []
    if gex_regime == "POSITIVE":
        for entry, sig_date in surviving:
            z = entry.get("z_ma150")
            if z is not None and z >= min_z:
                combined_pass.append((entry, sig_date))

    if len(combined_pass) > cap:
        subsample_applied = True
        sorted_list = sorted(combined_pass, key=lambda pair: pair[0].get("code", ""))
        final_candidates = random.Random(seed).sample(sorted_list, cap)
    else:
        subsample_applied = False
        final_candidates = list(combined_pass)

    return final_candidates, combined_pass, subsample_applied


def compute_hv30_local(closes: list[float]) -> float | None:
    """HV30: std (ddof=1) of last 30 close-to-close log returns (31 closes ending at signal bar) * sqrt(252)."""
    if closes is None or len(closes) < 31:
        return None
    window = closes[-31:]
    arr = np.asarray(window, dtype=float)
    if not np.all(arr > 0) or not np.all(np.isfinite(arr)):
        return None
    ret = np.log(arr[1:] / arr[:-1])
    if len(ret) != 30:
        return None
    std = float(np.std(ret, ddof=1))
    if not math.isfinite(std) or std <= 0:
        return None
    hv = float(std * np.sqrt(252))
    return hv if math.isfinite(hv) and hv > 0 else None


def validate_hv30_against_dolthub(
    vh_path: Optional[Path] = None,
    ohlcv_path: Optional[Path] = None,
) -> None:
    """Validate compute_hv30_local against DoltHub hv_current from historical pickles."""
    if vh_path is None:
        vh_path = REPO_ROOT / "scripts" / "backtest" / "run_out_dolthub_iv" / "volatility_history.pkl"
    if ohlcv_path is None:
        ohlcv_path = REPO_ROOT / "scripts" / "backtest" / "mega750_ohlcv_cache.pkl"

    if not vh_path.exists():
        sys.stderr.write(f"ERROR: volatility history pickle not found: {vh_path}\n")
        sys.exit(1)
    if not ohlcv_path.exists():
        sys.stderr.write(f"ERROR: OHLCV cache pickle not found: {ohlcv_path}\n")
        sys.exit(1)

    with open(vh_path, "rb") as f:
        vh = pickle.load(f)
    with open(ohlcv_path, "rb") as f:
        mc = pickle.load(f)

    rel_errs = []
    for ticker, vh_df in vh.items():
        if ticker not in mc:
            continue
        mc_df = mc[ticker].sort_index()
        mc_dates = [pd.Timestamp(d).date() for d in mc_df.index]
        mc_date_to_idx = {d: i for i, d in enumerate(mc_dates)}
        closes = mc_df["close"].tolist()

        for d, row in vh_df.iterrows():
            d_date = pd.Timestamp(d).date()
            if d_date not in mc_date_to_idx:
                continue
            idx = mc_date_to_idx[d_date]
            if idx < 30:
                continue
            sub_closes = closes[idx - 30 : idx + 1]
            local_hv = compute_hv30_local(sub_closes)
            dolt_hv = float(row["hv_current"])
            if local_hv is not None and dolt_hv > 0:
                rel_err = abs(local_hv - dolt_hv) / dolt_hv
                rel_errs.append(rel_err)

    n_samples = len(rel_errs)
    if n_samples < 100:
        sys.stderr.write(f"ERROR: insufficient samples for HV validation: {n_samples} < 100\n")
        sys.exit(1)

    med_rel_err = float(np.median(rel_errs))
    p90_rel_err = float(np.percentile(rel_errs, 90))
    p99_rel_err = float(np.percentile(rel_errs, 99))
    max_rel_err = float(np.max(rel_errs))

    print("=== HV30 vs DoltHub hv_current Validation ===")
    print(f"Sample count (ticker-days): {n_samples}")
    print(f"Median abs relative error:  {med_rel_err:.6f}")
    print(f"p90 abs relative error:     {p90_rel_err:.6f}")
    print(f"p99 abs relative error:     {p99_rel_err:.6f}")
    print(f"Max abs relative error:     {max_rel_err:.6f}")
    print(f"Requirement: median <= 0.001 over >= 100 samples")

    if med_rel_err <= 0.001:
        print("RESULT: PASS")
    else:
        print(f"RESULT: FAIL (median {med_rel_err:.6f} > 0.001)")
        sys.exit(1)


def build_ivpool(
    data_map: Dict[str, pd.DataFrame],
    last_session_str: str,
    gex_summary: Dict[str, Any],
    dolt_cache: Optional[Dict[str, Any]] = None,
    context_mode: str = "baseline",
) -> Dict[str, Any]:
    """Build pre-VRP pool: EMA150 gate + GEX POSITIVE + z_ma150 >= 1.5, sampled to IVPOOL_SAMPLE_N."""
    with tempfile.TemporaryDirectory(prefix="ghost-ivpool-") as tmp_dir:
        tmp_cache = Path(tmp_dir) / "empty_iv_cache.pkl"
        with open(tmp_cache, "wb") as f:
            pickle.dump({}, f)
        engine2 = BullPutSpreadSignalEngine(
            mode="vrp_only",
            gate_on_vrp=False,
            suppress_reentry=False,
            iv_hv_cache_path=tmp_cache,
        )
    engine2.generate(data_map)

    # Performance guard: entry['date'] is the FILL bar date (dates[i+1]).
    # Only entries signalled on last completed session (dates[i] == last_session_str)
    # have entry['date'] > last_session_str. Filter raw list from ~115k to ~500.
    raw_surviving = [e for e in engine2.entries if e.get("date", "") > last_session_str]
    surviving = filter_entries_to_target_session(raw_surviving, data_map, last_session_str)

    gex_regime = gex_summary.get("regime", "NEGATIVE")
    cap_val = max(len(surviving), 1)
    _, combined_pass, _ = apply_extension_filter_and_cap(
        surviving, gex_regime=gex_regime, seed=last_session_str, cap=cap_val
    )
    pool = combined_pass
    pool_size_pre_sample = len(pool)

    if pool_size_pre_sample > IVPOOL_SAMPLE_N:
        sampled_flag = True
        sorted_pool = sorted(pool, key=lambda pair: pair[0].get("code", ""))
        sampled_pairs = random.Random(f"ivpool:{last_session_str}").sample(sorted_pool, IVPOOL_SAMPLE_N)
        sampled_pairs = sorted(sampled_pairs, key=lambda pair: pair[0].get("code", ""))
    else:
        sampled_flag = False
        sampled_pairs = sorted(pool, key=lambda pair: pair[0].get("code", ""))

    tickers_list: List[Dict[str, Any]] = []
    for entry, sig_date in sampled_pairs:
        ticker = entry["code"]
        signal_close = round(float(entry["signal_close"]), 4)
        z_ma150 = round(float(entry["z_ma150"]), 4) if entry.get("z_ma150") is not None else None

        local_hv30 = None
        df = data_map.get(ticker)
        if df is not None:
            df_sorted = df.sort_index()
            dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10] for d in df_sorted.index]
            try:
                idx = dates.index(last_session_str)
                if idx >= 30:
                    sub_closes = df_sorted["close"].iloc[idx - 30 : idx + 1].tolist()
                    hv_val = compute_hv30_local(sub_closes)
                    if hv_val is not None:
                        local_hv30 = round(float(hv_val), 6)
            except ValueError:
                pass

        dolt_iv_signal_bar = None
        dolt_hv_signal_bar = None
        if dolt_cache is not None:
            history = dolt_cache.get(ticker)
            if history is not None and not history.empty:
                matching = history[
                    history.index.map(lambda d: pd.Timestamp(d).strftime("%Y-%m-%d")) == last_session_str
                ]
                if not matching.empty:
                    try:
                        val_iv = matching.iloc[-1]["iv_current"] if "iv_current" in matching.columns else None
                        val_hv = matching.iloc[-1]["hv_current"] if "hv_current" in matching.columns else None
                        if val_iv is not None and val_hv is not None:
                            raw_iv = float(val_iv)
                            raw_hv = float(val_hv)
                            if math.isfinite(raw_iv) and math.isfinite(raw_hv) and raw_iv >= 0 and raw_hv > 0:
                                dolt_iv_signal_bar = round(raw_iv, 4)
                                dolt_hv_signal_bar = round(raw_hv, 4)
                    except (KeyError, ValueError, TypeError):
                        dolt_iv_signal_bar, dolt_hv_signal_bar = None, None

        tickers_list.append({
            "ticker": ticker,
            "signal_close": signal_close,
            "z_ma150": z_ma150,
            "local_hv30": local_hv30,
            "dolt_iv_signal_bar": dolt_iv_signal_bar,
            "dolt_hv_signal_bar": dolt_hv_signal_bar,
        })

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    pool_id = f"ivpool_{uuid.uuid4().hex[:8]}_{utc_now.strftime('%Y%m%d_%H%M%S')}"

    return {
        "pool_id": pool_id,
        "generated_ts_utc": utc_now.isoformat(),
        "signal_bar_date": last_session_str,
        "context_mode": context_mode,
        "gex": gex_summary,
        "pool_size_pre_sample": pool_size_pre_sample,
        "sample_n_cap": IVPOOL_SAMPLE_N,
        "sample_seed": f"ivpool:{last_session_str}",
        "sampled": sampled_flag,
        "hv_definition": HV_DEFINITION,
        "tickers": tickers_list,
    }


def run_synthetic_filter_test() -> None:
    """Synthetic unit test proving older qualifying dates appear in engine.entries

    due to suppress_reentry=False, but are dropped by the script's current-session filter.
    """
    print("\n--- Running Ghost Prescreen Current-Session Filter Synthetic Test ---")
    today_target = "2026-09-11"
    older_target = "2026-09-08"

    dates = pd.bdate_range("2026-01-01", periods=MIN_BARS + 10)
    # Map index 150 to older_target, index 153 to today_target
    bar_older_idx = 150
    bar_today_idx = 153

    # Construct synthetic date index with explicit string representation
    dates_list = list(dates)
    # Ensure specific date timestamps
    t_older = pd.Timestamp(older_target)
    t_today = pd.Timestamp(today_target)
    dates_list[bar_older_idx] = t_older
    dates_list[bar_older_idx + 1] = t_older + pd.offsets.BDay(1)
    dates_list[bar_today_idx] = t_today
    dates_list[bar_today_idx + 1] = t_today + pd.offsets.BDay(1)
    idx = pd.DatetimeIndex(dates_list)

    closes = pd.Series([100.0 + k * 0.1 for k in range(len(idx))], index=idx)
    synthetic_df = pd.DataFrame({
        "open": closes - 0.05,
        "high": closes + 0.1,
        "low": closes - 0.1,
        "close": closes,
        "volume": 1_000_000,
    }, index=idx)

    # IV cache with qualifying IV/HV on both older_target and today_target
    cache_df = pd.DataFrame(
        {"iv_current": [0.30, 0.30], "hv_current": [0.20, 0.20]},
        index=[t_older, t_today],
    )
    test_code = "SYNTH_GHOST"
    data_map = {test_code: synthetic_df}
    cache = {test_code: cache_df}

    engine = BullPutSpreadSignalEngine(mode="vrp_only", suppress_reentry=False)
    engine._iv_hv_cache = cache
    engine.generate(data_map)

    # 1. Confirm engine.entries contains both signals (proving suppress_reentry=False worked)
    entry_dates = [e["date"] for e in engine.entries]
    print(f"engine.entries count: {len(engine.entries)}")
    print(f"engine.entries trade dates: {entry_dates}")
    assert len(engine.entries) == 2, (
        f"Expected 2 entries in engine.entries with suppress_reentry=False, got {len(engine.entries)}"
    )

    # 2. Filter using current-session filter for today_target
    surviving = filter_entries_to_target_session(engine.entries, data_map, today_target)
    print(f"Surviving candidates matching {today_target}: {len(surviving)}")

    assert len(surviving) == 1, f"Expected exactly 1 surviving candidate, got {len(surviving)}"
    surviving_entry, surviving_signal_date = surviving[0]
    assert surviving_signal_date == today_target, f"Expected signal date {today_target}, got {surviving_signal_date}"
    assert surviving_entry["code"] == test_code

    # 3. Verify older date is completely absent from output candidate list and output JSON
    candidate_signal_dates = [s_date for _, s_date in surviving]
    assert older_target not in candidate_signal_dates, f"Older date {older_target} leaked into candidate list!"

    candidate = {
        "candidate_id": generate_candidate_id(
            surviving_entry["date"], surviving_entry["code"], "vrp_only",
            surviving_entry["short_strike"], surviving_entry["long_strike"], surviving_entry["expiry"]
        ),
        "ticker": surviving_entry["code"],
        "signal_close": surviving_entry["signal_close"],
        "ma150": surviving_entry["ma150"],
        "vrp_ratio": surviving_entry["vrp_ratio"],
        "iv_current": 0.30,
        "hv_current": 0.20,
        "iv_as_of_date": surviving_signal_date,
        "derived_short_strike": surviving_entry["short_strike"],
        "derived_long_strike": surviving_entry["long_strike"],
        "derived_expiry": surviving_entry["expiry"],
        "z_ma150": round(float(surviving_entry["z_ma150"]), 4) if surviving_entry.get("z_ma150") is not None else None,
        "filter_tag": FILTER_TAG,
    }
    json_payload = {
        "run_id": "test_run",
        "signal_bar_date": today_target,
        "candidates": [candidate],
    }
    output_str = json.dumps(json_payload)
    assert older_target not in output_str, f"Older target date {older_target} found in output JSON!"
    assert today_target in output_str, f"Today target date {today_target} missing from output JSON!"
    assert "z_ma150" in output_str, "z_ma150 missing from output JSON!"
    assert "filter_tag" in output_str, "filter_tag missing from output JSON!"

    # 3b. Verify baseline post-hoc z_ma150 backfill and FILTER_TAG_BASELINE
    baseline_entry = {
        "code": test_code,
        "date": surviving_entry["date"],
        "signal_close": surviving_entry["signal_close"],
        "z_ma150": None,
    }
    backfilled_z = compute_z_ma150_for_entry(baseline_entry, data_map)
    assert backfilled_z is not None, "compute_z_ma150_for_entry returned None for valid synthetic data"
    assert math.isclose(backfilled_z, surviving_entry["z_ma150"], rel_tol=1e-6), (
        f"Parity mismatch: backfilled {backfilled_z} vs vrp_only {surviving_entry['z_ma150']}"
    )
    assert FILTER_TAG_BASELINE != FILTER_TAG
    assert "baseline_fallback" in FILTER_TAG_BASELINE
    print(f"PASS: Baseline backfill z_ma150 ({backfilled_z:.4f}) matches vrp_only exact calculation.")
    print(f"PASS: Distinct FILTER_TAG_BASELINE={FILTER_TAG_BASELINE} verified.")

    print(f"PASS: Older date {older_target} present in engine.entries, strictly absent from output JSON.")
    print(f"PASS: Only target date {today_target} retained in candidate output (1 candidate in final JSON).\n")

    # 4. Extension filter & deterministic cap tests
    print("--- Running Extension Filter & Deterministic Cap Synthetic Tests ---")
    c_qual = ({"code": "QUAL", "z_ma150": 1.75}, today_target)
    c_low = ({"code": "LOWZ", "z_ma150": 1.49}, today_target)
    c_none = ({"code": "NONEZ", "z_ma150": None}, today_target)
    c_missing = ({"code": "MISSZ"}, today_target)

    # Candidate with z_ma150 >= 1.5 and GEX POSITIVE survives
    # Candidate with z_ma150 < 1.5 is excluded
    # Candidate with z_ma150 == None / missing is excluded (fails closed)
    final, passed, subsample = apply_extension_filter_and_cap(
        [c_qual, c_low, c_none, c_missing],
        gex_regime="POSITIVE",
        seed=today_target,
    )
    assert len(final) == 1 and final[0][0]["code"] == "QUAL", f"Expected only QUAL to pass, got {[c[0]['code'] for c in final]}"
    assert subsample is False
    print("PASS: Candidate with z_ma150 >= 1.5 and GEX POSITIVE survives into final list.")
    print("PASS: Candidate with z_ma150 < 1.5 is excluded.")
    print("PASS: Candidate with z_ma150 == None (or missing) is excluded (fails closed).")

    # When run-level GEX regime is NEGATIVE, every candidate is excluded regardless of z_ma150
    final_neg, passed_neg, subsample_neg = apply_extension_filter_and_cap(
        [c_qual, ({"code": "HIGHZ", "z_ma150": 3.5}, today_target)],
        gex_regime="NEGATIVE",
        seed=today_target,
    )
    assert len(final_neg) == 0 and len(passed_neg) == 0, f"Expected 0 passing on NEGATIVE GEX, got {len(final_neg)}"
    assert subsample_neg is False
    print("PASS: Run-level GEX NEGATIVE excludes every candidate regardless of z_ma150.")

    # combined_pass list longer than CANDIDATE_CAP is reduced to exactly CANDIDATE_CAP
    many_candidates = [
        ({"code": f"TICK{i:02d}", "z_ma150": 2.0}, today_target)
        for i in range(CANDIDATE_CAP + 10)
    ]
    final_capped, passed_capped, subsample_capped = apply_extension_filter_and_cap(
        many_candidates,
        gex_regime="POSITIVE",
        seed=today_target,
    )
    assert len(passed_capped) == CANDIDATE_CAP + 10
    assert len(final_capped) == CANDIDATE_CAP
    assert subsample_capped is True
    print(f"PASS: combined_pass list longer than CANDIDATE_CAP is reduced to exactly CANDIDATE_CAP ({CANDIDATE_CAP}).")

    # Two separate calls with same last_session_str seed produce IDENTICAL sampled output (determinism)
    final_capped_2, _, _ = apply_extension_filter_and_cap(
        many_candidates,
        gex_regime="POSITIVE",
        seed=today_target,
    )
    assert [c[0]["code"] for c in final_capped] == [c[0]["code"] for c in final_capped_2], (
        "Subsampling was not deterministic across calls with identical seed!"
    )
    print("PASS: Two separate calls with identical seed produce identical sampled output (determinism).")

    # combined_pass list of length <= CANDIDATE_CAP is returned unchanged (no sampling), subsample_applied is False
    few_candidates = [
        ({"code": f"FEW{i:02d}", "z_ma150": 2.0}, today_target)
        for i in range(CANDIDATE_CAP - 5)
    ]
    final_few, passed_few, subsample_few = apply_extension_filter_and_cap(
        few_candidates,
        gex_regime="POSITIVE",
        seed=today_target,
    )
    assert len(final_few) == len(few_candidates)
    assert final_few == few_candidates
    assert subsample_few is False
    print("PASS: combined_pass list of length <= CANDIDATE_CAP is returned unchanged (no sampling), subsample_applied is False.\n")

    # 5. B4 ivpool synthetic test cases
    print("--- Running B4 Pre-VRP IVPool Synthetic Tests ---")

    # 5a. compute_hv30_local equals hand-computed numpy value
    synth_closes = [
        100.0, 101.5, 99.8, 102.3, 101.0, 103.5, 102.8, 104.0, 103.2, 105.1,
        104.5, 106.0, 105.5, 107.2, 106.8, 108.0, 107.5, 109.1, 108.3, 110.0,
        109.5, 111.2, 110.8, 112.5, 111.9, 113.4, 112.8, 114.2, 113.7, 115.0, 114.5
    ]
    arr_c = np.asarray(synth_closes, dtype=float)
    rets = np.log(arr_c[1:] / arr_c[:-1])
    expected_hv = float(np.std(rets, ddof=1) * np.sqrt(252))
    actual_hv = compute_hv30_local(synth_closes)
    assert actual_hv is not None, "compute_hv30_local returned None for valid 31 closes"
    assert math.isclose(actual_hv, expected_hv, rel_tol=1e-12), (
        f"compute_hv30_local mismatch: actual {actual_hv} vs expected {expected_hv}"
    )
    # Edge cases
    assert compute_hv30_local(synth_closes[:30]) is None, "Expected None for < 31 closes"
    assert compute_hv30_local([-100.0] + synth_closes[1:]) is None, "Expected None for negative close"
    assert compute_hv30_local([float("nan")] + synth_closes[1:]) is None, "Expected None for NaN close"
    print(f"PASS: compute_hv30_local ({actual_hv:.6f}) equals hand-computed numpy value ({expected_hv:.6f}).")

    # 5b. Pool excludes z < 1.5, empty on GEX NEGATIVE, 100-ticker cap/sample determinism & uniform (not first 40)
    pool_test_date = "2026-09-11"
    p_dates = pd.bdate_range("2026-01-01", periods=MIN_BARS + 10)
    p_dates_list = list(p_dates)
    p_dates_list[153] = pd.Timestamp(pool_test_date)
    p_dates_list[154] = pd.Timestamp(pool_test_date) + pd.offsets.BDay(1)
    p_idx = pd.DatetimeIndex(p_dates_list)

    pool_data_map: Dict[str, pd.DataFrame] = {}
    for i in range(100):
        c_series = pd.Series([100.0 + k * 0.1 for k in range(len(p_idx))], index=p_idx)
        pool_data_map[f"TICK{i:03d}"] = pd.DataFrame({
            "open": c_series - 0.05,
            "high": c_series + 0.1,
            "low": c_series - 0.1,
            "close": c_series,
            "volume": 1_000_000,
        }, index=p_idx)

    # Add ticker with z < 1.5 (flat closes, z == 0 < 1.5)
    flat_series = pd.Series([100.0] * len(p_idx), index=p_idx)
    pool_data_map["LOWZ"] = pd.DataFrame({
        "open": flat_series,
        "high": flat_series + 0.01,
        "low": flat_series - 0.01,
        "close": flat_series,
        "volume": 1_000_000,
    }, index=p_idx)

    gex_pos = {"regime": "POSITIVE", "percentile": 80.0, "data_available": True, "as_of": pool_test_date}
    gex_neg = {"regime": "NEGATIVE", "percentile": 20.0, "data_available": True, "as_of": pool_test_date}

    pool_pos_1 = build_ivpool(pool_data_map, pool_test_date, gex_pos)
    pool_pos_2 = build_ivpool(pool_data_map, pool_test_date, gex_pos)
    pool_neg = build_ivpool(pool_data_map, pool_test_date, gex_neg)

    # 1. Pool excludes z < 1.5
    assert pool_pos_1["pool_size_pre_sample"] == 100, (
        f"Expected 100 tickers to qualify (excluding LOWZ), got {pool_pos_1['pool_size_pre_sample']}"
    )
    all_sampled_tickers = [t["ticker"] for t in pool_pos_1["tickers"]]
    assert "LOWZ" not in all_sampled_tickers, "LOWZ (z < 1.5) unexpectedly present in pool sample!"
    print("PASS: Pool excludes tickers failing z >= 1.5 (100 qualified, LOWZ excluded).")

    # 2. Empty pool on GEX NEGATIVE
    assert pool_neg["pool_size_pre_sample"] == 0, (
        f"Expected 0 pre-sample pool size on GEX NEGATIVE, got {pool_neg['pool_size_pre_sample']}"
    )
    assert pool_neg["tickers"] == [], "Expected empty tickers list on GEX NEGATIVE"
    assert pool_neg["sampled"] is False, "Expected sampled=False on GEX NEGATIVE"
    print("PASS: Empty pool on GEX NEGATIVE (pool_size_pre_sample=0, tickers=[]).")

    # 3. Sample size == 40 when pool > 40
    assert len(pool_pos_1["tickers"]) == IVPOOL_SAMPLE_N, (
        f"Expected sample size == {IVPOOL_SAMPLE_N}, got {len(pool_pos_1['tickers'])}"
    )
    assert pool_pos_1["sampled"] is True, "Expected sampled=True when pool > IVPOOL_SAMPLE_N"
    print(f"PASS: Sample size == {IVPOOL_SAMPLE_N} when pool > {IVPOOL_SAMPLE_N}.")

    # 4. Sample deterministic for fixed seed AND not simply alphabetically-first 40
    sample_1 = [t["ticker"] for t in pool_pos_1["tickers"]]
    sample_2 = [t["ticker"] for t in pool_pos_2["tickers"]]
    assert sample_1 == sample_2, "Pool sampling was not deterministic across calls with same seed!"
    alpha_first_40 = [f"TICK{i:03d}" for i in range(40)]
    assert sample_1 != alpha_first_40, (
        "Pool sample with 100 tickers was simply the alphabetically-first 40 (sampling not uniform/random)!"
    )
    print("PASS: Pool sample deterministic for fixed seed AND not simply alphabetically-first 40.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="ARIA Ghost Candidate Prescreen v2")
    parser.add_argument(
        "--mode",
        choices=["vrp_only", "baseline", "vrp_plus_baseline"],
        default="vrp_only",
        help="Gating mode (default: vrp_only)",
    )
    parser.add_argument(
        "--iv-cache",
        type=str,
        default=str(DEFAULT_IV_CACHE),
        help=f"Path to live IV/HV pickle cache (default: {DEFAULT_IV_CACHE})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to output JSON (default: state/scratch/ghost_prescreen_<today>.json)",
    )
    parser.add_argument(
        "--vrp-threshold",
        type=float,
        default=1.1,
        help="Minimum VRP ratio (default: 1.1)",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default=str(DEFAULT_UNIVERSE),
        help=f"Path to universe CSV (default: {DEFAULT_UNIVERSE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap universe size for testing/smoke runs",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run synthetic filter verification test and exit",
    )
    parser.add_argument(
        "--ivpool-output",
        type=str,
        default=None,
        help="Path to pre-VRP pool output JSON (default: None = off)",
    )
    parser.add_argument(
        "--validate-hv",
        action="store_true",
        help="Validate compute_hv30_local against DoltHub hv_current and exit",
    )
    args = parser.parse_args()

    if args.validate_hv:
        validate_hv30_against_dolthub()
        sys.exit(0)

    if args.test:
        run_synthetic_filter_test()
        sys.exit(0)

    today_ny = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    today_ny_str = today_ny.strftime("%Y-%m-%d")

    out_path_str = args.output
    if not out_path_str:
        out_path = REPO_ROOT / "state" / "scratch" / f"ghost_prescreen_{today_ny_str}.json"
    else:
        out_path = Path(out_path_str).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Determine last completed trading session
    holidays = load_holidays(REPO_ROOT / "us-market-holidays.txt")
    last_session = get_last_completed_session(today_ny, holidays=holidays)
    last_session_str = last_session.strftime("%Y-%m-%d")
    print(f"ghost_prescreen_v2: today={today_ny_str}, signal_bar_date={last_session_str}, mode={args.mode}")

    # 2. Load universe tickers
    with open(args.universe, mode="r", encoding="utf-8") as f:
        universe_tickers = [r["ticker"].strip() for r in csv.DictReader(f) if r.get("ticker", "").strip()]
    if args.limit and args.limit > 0:
        universe_tickers = universe_tickers[: args.limit]
    universe_count = len(universe_tickers)
    print(f"Loaded {universe_count} tickers from {args.universe}")

    # 3. Pull market data via yfinance and build data_map
    data_map, failures = build_data_map_from_yfinance(
        universe_tickers, period="400d", target_signal_date=last_session
    )
    print(f"Built data_map for {len(data_map)} tickers ({len(failures)} failures/skipped)")

    # 4. Instantiate BullPutSpreadSignalEngine with suppress_reentry=False
    iv_cache_path = Path(args.iv_cache).resolve()
    if args.mode == "baseline":
        engine = None
        if iv_cache_path.exists():
            try:
                engine = BullPutSpreadSignalEngine(
                    mode=args.mode,
                    vrp_threshold=args.vrp_threshold,
                    iv_hv_cache_path=iv_cache_path,
                    suppress_reentry=False,
                )
            except Exception as exc:
                print(f"INFO: Could not load IV cache ({exc}); using empty cache for baseline mode")
        if engine is None:
            with tempfile.TemporaryDirectory(prefix="ghost-iv-fallback-") as tmp_dir:
                tmp_cache = Path(tmp_dir) / "empty_iv_cache.pkl"
                with open(tmp_cache, "wb") as f:
                    pickle.dump({}, f)
                engine = BullPutSpreadSignalEngine(
                    mode=args.mode,
                    vrp_threshold=args.vrp_threshold,
                    iv_hv_cache_path=tmp_cache,
                    suppress_reentry=False,
                )
    else:
        if not iv_cache_path.exists():
            sys.stderr.write(f"ERROR: IV cache file not found: {iv_cache_path}\n")
            sys.exit(1)

        engine = BullPutSpreadSignalEngine(
            mode=args.mode,
            vrp_threshold=args.vrp_threshold,
            iv_hv_cache_path=iv_cache_path,
            suppress_reentry=False,
        )

    # 5. Generate signals
    engine.generate(data_map)
    print(f"engine.generate finished: {len(engine.entries)} total raw historical entries")

    if args.mode == "baseline":
        for entry in engine.entries:
            if entry.get("z_ma150") is None:
                entry["z_ma150"] = compute_z_ma150_for_entry(entry, data_map)

    # 6. Filter engine.entries to ONLY target session (yesterday / last completed session)
    surviving = filter_entries_to_target_session(engine.entries, data_map, last_session_str)
    print(f"Surviving candidates for session {last_session_str}: {len(surviving)}")

    # 7. Call dix_fetcher_v2.gex_regime() as of signal bar date (last_session_str)
    try:
        series = dix_fetcher_v2.load_series()
        gex_data = dix_fetcher_v2.gex_regime(series, as_of_date=last_session_str)
        gex_regime = gex_data.get("regime", "NEGATIVE")
        gex_percentile = gex_data.get("percentile_rank")
        gex_data_available = gex_data.get("data_available", False)
        gex_as_of = gex_data.get("as_of_date")
    except Exception as exc:
        print(f"WARNING: dix fetch/regime failed: {exc}, using fail-safe NEGATIVE")
        gex_regime = "NEGATIVE"
        gex_percentile = None
        gex_data_available = False
        gex_as_of = today_ny_str

    gex_summary = {
        "regime": gex_regime,
        "percentile": gex_percentile,
        "data_available": gex_data_available,
        "as_of": gex_as_of,
    }

    # 8. Load iv_meta alongside iv_cache
    meta_path = iv_cache_path.parent / (iv_cache_path.stem + "_meta.json")
    iv_meta: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                iv_meta = json.load(f)
        except Exception as exc:
            print(f"WARNING: Could not load iv_meta from {meta_path}: {exc}")

    # 8b. Apply extension filter (GEX POSITIVE + z_ma150 >= 1.5) and deterministic cap
    final_candidates, combined_pass, subsample_applied = apply_extension_filter_and_cap(
        surviving, gex_regime=gex_regime, seed=last_session_str
    )
    print(f"Extension filter: pre={len(surviving)}, pass={len(combined_pass)}, "
          f"emitted={len(final_candidates)} (subsample_applied={subsample_applied})")

    # 9. Format candidates
    filter_tag = FILTER_TAG_BASELINE if args.mode == "baseline" else FILTER_TAG
    candidates_list: List[Dict[str, Any]] = []
    for entry, sig_date in final_candidates:
        ticker = entry["code"]
        trade_date = entry["date"]
        short_strike = float(entry["short_strike"])
        long_strike = float(entry["long_strike"])
        expiry = str(entry["expiry"])

        cand_id = generate_candidate_id(
            trade_date=trade_date,
            ticker=ticker,
            mode=args.mode,
            short_strike=short_strike,
            long_strike=long_strike,
            expiry=expiry,
        )

        # Retrieve exact iv_current and hv_current from cache for this date
        history = engine._iv_hv_cache.get(ticker)
        iv_current = None
        hv_current = None
        if history is not None and not history.empty:
            matching = history[
                history.index.map(lambda d: pd.Timestamp(d).strftime("%Y-%m-%d")) == sig_date
            ]
            if not matching.empty:
                try:
                    val_iv = matching.iloc[-1]["iv_current"] if "iv_current" in matching.columns else None
                    val_hv = matching.iloc[-1]["hv_current"] if "hv_current" in matching.columns else None
                    if val_iv is not None and val_hv is not None:
                        raw_iv = float(val_iv)
                        raw_hv = float(val_hv)
                        # Same validity bar as BullPutSpreadSignalEngine._vrp_ratio:
                        # finite, non-negative IV, positive HV -- reject silently to
                        # null rather than let a bad DoltHub row through to the
                        # candidate JSON just because this readout path is separate
                        # from the ratio computation that already guards it.
                        if math.isfinite(raw_iv) and math.isfinite(raw_hv) and raw_iv >= 0 and raw_hv > 0:
                            iv_current, hv_current = raw_iv, raw_hv
                except (KeyError, ValueError, TypeError):
                    iv_current, hv_current = None, None

        candidates_list.append({
            "candidate_id": cand_id,
            "ticker": ticker,
            "signal_close": round(float(entry["signal_close"]), 4),
            "ma150": round(float(entry["ma150"]), 4),
            "vrp_ratio": round(float(entry["vrp_ratio"]), 4) if entry.get("vrp_ratio") is not None else None,
            "iv_current": round(iv_current, 4) if iv_current is not None else None,
            "hv_current": round(hv_current, 4) if hv_current is not None else None,
            "iv_as_of_date": sig_date,
            "derived_short_strike": short_strike,
            "derived_long_strike": long_strike,
            "derived_expiry": expiry,
            "gex_regime": gex_regime,
            "gex_percentile": gex_percentile,
            "gex_data_available": gex_data_available,
            "gex_as_of": gex_as_of,
            "z_ma150": round(float(entry["z_ma150"]), 4) if entry.get("z_ma150") is not None else None,
            "filter_tag": filter_tag,
        })

    # 10. Write output JSON
    run_ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run_id = f"ghost_{uuid.uuid4().hex[:8]}_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    output_payload = {
        "run_id": run_id,
        "run_ts_utc": run_ts_utc,
        "mode": args.mode,
        "signal_bar_date": last_session_str,
        "gex": gex_summary,
        "iv_meta": iv_meta,
        "candidates": candidates_list,
        "skipped_missing_iv": engine.skipped_missing_iv,
        "universe_count": universe_count,
        "pre_extension_filter_count": len(surviving),
        "combined_filter_pass_count": len(combined_pass),
        "emitted_candidate_count": len(candidates_list),
        "subsample_applied": subsample_applied,
        "subsample_seed": last_session_str,
        "extension_filter_threshold": MIN_Z_MA150_COMBINED,
        "extension_filter_gex_requirement": "POSITIVE",
        "candidate_cap": CANDIDATE_CAP,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)

    print(f"Successfully wrote {len(candidates_list)} candidates to {out_path}")

    if args.ivpool_output:
        try:
            dolt_cache = getattr(engine, "_iv_hv_cache", None)
            if not dolt_cache and iv_cache_path.exists():
                try:
                    with open(iv_cache_path, "rb") as f:
                        dolt_cache = pickle.load(f)
                except Exception:
                    dolt_cache = None

            pool_payload = build_ivpool(
                data_map=data_map,
                last_session_str=last_session_str,
                gex_summary=gex_summary,
                dolt_cache=dolt_cache,
                context_mode=args.mode,
            )
            ivpool_out_path = Path(args.ivpool_output).resolve()
            ivpool_out_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=ivpool_out_path.parent, delete=False, encoding="utf-8") as tmp_file:
                json.dump(pool_payload, tmp_file, indent=2)
                tmp_path = Path(tmp_file.name)
            try:
                os.replace(tmp_path, ivpool_out_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            print(f"Successfully wrote ivpool ({len(pool_payload['tickers'])} tickers) to {ivpool_out_path}")
        except Exception as exc:
            sys.stderr.write(f"WARNING: ivpool build failed: {exc}\n")


if __name__ == "__main__":
    main()
