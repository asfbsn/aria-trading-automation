#!/usr/bin/env python3
"""Live daily DoltHub IV/HV pull for ARIA Ghost candidate generation.

Pulls recent trading sessions of options volatility history from DoltHub,
builds the exact {code: DataFrame} cache expected by BullPutSpreadSignalEngine,
and writes metadata alongside the cache.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import os
from pathlib import Path
import pickle
import sys
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "backtest"))

from dolthub_iv_pull import (  # noqa: E402
    DOLT_ALIASES,
    FLOAT_COLS,
    fetch_date,
    in_list,
    load_universe,
)

DEFAULT_DAYS_BACK = 10
DEFAULT_OUT = REPO_ROOT / "state" / "ghost" / "iv_live.pkl"


def get_recent_trading_sessions(end_date: datetime.date, n: int) -> List[str]:
    """Walk backward from (end_date - 1 day) skipping Saturday and Sunday to find n sessions."""
    sessions: List[str] = []
    cur = end_date - datetime.timedelta(days=1)
    while len(sessions) < n:
        if cur.weekday() < 5:  # Monday to Friday
            sessions.append(cur.strftime("%Y-%m-%d"))
        cur -= datetime.timedelta(days=1)
    sessions.sort()
    return sessions


def count_valid_symbols(rows: List[Dict[str, Any]], universe_set: set[str]) -> int:
    """Count unique universe symbols with finite, valid IV >= 0 and HV > 0."""
    valid = set()
    rev_aliases = {DOLT_ALIASES.get(c, c): c for c in universe_set}
    for r in rows:
        sym = r.get("act_symbol")
        code = rev_aliases.get(sym)
        if not code or code not in universe_set:
            continue
        iv = r.get("iv_current")
        hv = r.get("hv_current")
        if iv is None or hv is None:
            continue
        try:
            f_iv = float(iv)
            f_hv = float(hv)
            if math.isfinite(f_iv) and math.isfinite(f_hv) and f_iv >= 0 and f_hv > 0:
                valid.add(code)
        except (ValueError, TypeError):
            continue
    return len(valid)


def build_cache_dataframe(
    all_rows: List[Dict[str, Any]], universe: List[str]
) -> Dict[str, pd.DataFrame]:
    """Mirror dolthub_iv_pull.build() to produce {code: DataFrame(date-indexed)}."""
    df = pd.DataFrame(all_rows)
    if df.empty:
        return {}
    rev = {DOLT_ALIASES.get(c, c): c for c in universe}
    df["code"] = df["act_symbol"].map(rev)
    df = df.dropna(subset=["code"])
    df["date"] = pd.to_datetime(df["date"])
    for c in FLOAT_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    cache: Dict[str, pd.DataFrame] = {}
    for code in universe:
        g = (
            df[df["code"] == code]
            .drop(columns=["code"])
            .drop_duplicates("date")
            .set_index("date")
            .sort_index()
        )
        if not g.empty:
            cache[code] = g
    return cache


def run_self_test(pkl_path: Path) -> None:
    """Validate cache contract against BullPutSpreadSignalEngine."""
    print(f"Running --self-test against {pkl_path}...")
    from bps_signal_engine_v2 import BullPutSpreadSignalEngine

    engine = BullPutSpreadSignalEngine(iv_hv_cache_path=pkl_path)
    if not engine._iv_hv_cache:
        raise AssertionError("BullPutSpreadSignalEngine loaded empty cache")
    sample_code = next(iter(engine._iv_hv_cache))
    sample_df = engine._iv_hv_cache[sample_code]
    if "iv_current" not in sample_df.columns or "hv_current" not in sample_df.columns:
        raise AssertionError(f"Cache missing iv_current/hv_current for {sample_code}")
    if not isinstance(sample_df.index, pd.DatetimeIndex):
        raise AssertionError(f"Cache index is not DatetimeIndex for {sample_code}")
    print(
        f"PASS: BullPutSpreadSignalEngine loaded cache cleanly ({len(engine._iv_hv_cache)} tickers)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch live DoltHub IV/HV data for Ghost system")
    parser.add_argument(
        "--days-back",
        type=int,
        default=DEFAULT_DAYS_BACK,
        help=f"Number of trading sessions to fetch (default: {DEFAULT_DAYS_BACK})",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default=None,
        help="Path to universe CSV (default: data/universe.csv via load_universe())",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT),
        help=f"Path to output pickle (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run verification that BullPutSpreadSignalEngine constructs without error",
    )
    args = parser.parse_args()
    if args.days_back < 1:
        parser.error("--days-back must be >= 1")

    out_path = Path(args.out).resolve()
    meta_path = out_path.parent / (out_path.stem + "_meta.json")

    # Load universe
    if args.universe:
        with open(args.universe, mode="r", encoding="utf-8") as f:
            universe = [r["ticker"].strip() for r in csv.DictReader(f) if r.get("ticker", "").strip()]
    else:
        universe = load_universe()
    universe_set = set(universe)
    inl = in_list(universe)

    today_ny = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    sessions = get_recent_trading_sessions(today_ny, args.days_back)
    print(
        f"fetch_iv_live: universe={len(universe)}, fetching {len(sessions)} sessions [{sessions[0]} .. {sessions[-1]}]"
    )

    from concurrent.futures import ThreadPoolExecutor

    results_by_date: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_date = {executor.submit(fetch_date, dt, inl): dt for dt in sessions}
        for fut in future_to_date:
            dt = future_to_date[fut]
            results_by_date[dt] = fut.result()

    # Fail-closed checks on newest requested date
    newest_date = sessions[-1]
    newest_res = results_by_date[newest_date]
    if newest_res.get("rows") is None:
        sys.stderr.write(
            f"ERROR: Most recent requested date {newest_date} failed to fetch after retries: {newest_res.get('error')}\n"
        )
        sys.exit(1)

    newest_valid = count_valid_symbols(newest_res["rows"], universe_set)
    valid_threshold = 0.50 * len(universe)
    if newest_valid < valid_threshold:
        sys.stderr.write(
            f"ERROR: Most recent requested date {newest_date} has only {newest_valid}/{len(universe)} "
            f"valid IV/HV tickers (< 50% threshold of {valid_threshold:.0f})\n"
        )
        sys.exit(1)

    # Collect successful rows and metadata
    all_rows: List[Dict[str, Any]] = []
    dates_covered: List[str] = []
    rows_per_date: Dict[str, int] = {}
    valid_per_date: Dict[str, int] = {}

    for dt in sessions:
        res = results_by_date[dt]
        rows = res.get("rows")
        if rows is not None and len(rows) > 0:
            dates_covered.append(dt)
            rows_per_date[dt] = len(rows)
            valid_per_date[dt] = count_valid_symbols(rows, universe_set)
            all_rows.extend(rows)
        else:
            rows_per_date[dt] = 0
            valid_per_date[dt] = 0

    # Build cache structure
    cache = build_cache_dataframe(all_rows, universe)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: this cache is read daily by ghost_prescreen_v2.py, which
    # may run concurrently with a refresh; a crash or kill mid-write must
    # never leave a truncated/corrupt pickle in place of a working one.
    tmp_path = out_path.with_suffix(out_path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "wb") as f:
        pickle.dump(cache, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)

    fetch_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = {
        "fetch_ts": fetch_ts,
        "dates_covered": dates_covered,
        "rows_per_date": rows_per_date,
        "symbols_with_valid_iv_hv_per_date": valid_per_date,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(
        f"Wrote {len(cache)} symbols to {out_path} ({len(dates_covered)} dates covered, latest {newest_date}: {newest_valid} valid tickers)"
    )
    print(f"Wrote metadata to {meta_path}")

    if args.self_test:
        run_self_test(out_path)


if __name__ == "__main__":
    main()
