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
from pathlib import Path
import pickle
import random
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import uuid
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "backtest"))

from bps_signal_engine_v2 import BullPutSpreadSignalEngine  # noqa: E402
import dix_fetcher_v2  # noqa: E402
from signal_core import MIN_BARS  # noqa: E402

DEFAULT_IV_CACHE = REPO_ROOT / "state" / "ghost" / "iv_live.pkl"
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "universe.csv"

MIN_Z_MA150_COMBINED = 1.5  # research/proposals/2026-09-17-gex-pin-ma150-extension.md — accepted screening candidate (lowest passing threshold of the three tested: 1.5, 2.0, 2.5)
CANDIDATE_CAP = 15  # hard cap on the per-run IBKR quote-capture loop — observed ~40s/candidate against a 15-minute CLAUDE_TIMEOUT in daily-scan-ghost.sh
FILTER_TAG = "gex_positive_z_ma150_ge_1.5"

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
    args = parser.parse_args()

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
                raw_iv = float(matching.iloc[-1]["iv_current"])
                raw_hv = float(matching.iloc[-1]["hv_current"])
                # Same validity bar as BullPutSpreadSignalEngine._vrp_ratio:
                # finite, non-negative IV, positive HV -- reject silently to
                # null rather than let a bad DoltHub row through to the
                # candidate JSON just because this readout path is separate
                # from the ratio computation that already guards it.
                if math.isfinite(raw_iv) and math.isfinite(raw_hv) and raw_iv >= 0 and raw_hv > 0:
                    iv_current, hv_current = raw_iv, raw_hv

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
            "z_ma150": round(float(entry["z_ma150"]), 4),
            "filter_tag": FILTER_TAG,
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


if __name__ == "__main__":
    main()
