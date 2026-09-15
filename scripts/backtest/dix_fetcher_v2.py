"""
scripts/backtest/dix_fetcher_v2.py
==================================

DRAFT MODULE FOR HUMAN REVIEW — DO NOT WIRE INTO PRODUCTION DIRECTLY.

Purpose
-------
Fetches SqueezeMetrics' free public DIX / GEX daily dataset and exposes a dealer gamma
regime signal for both live trading decision engines (exit guards) and historical backtests.

Data Source:
    URL: https://squeezemetrics.com/monitor/static/DIX.csv
    Format: CSV with header `date,price,dix,gex`
    Coverage: 2011-05-02 through present (daily trading sessions, unauthenticated)
    Columns:
        - `date`: Trading day (YYYY-MM-DD)
        - `price`: S&P 500 (SPX) index closing level
        - `dix`: Dark Index (0.0 to 1.0), dark pool buying sentiment indicator
        - `gex`: Net SPX dealer gamma exposure in dollar gamma per 1% move ($)

Empirical Rationale: Why Percentile Rank Over Raw Sign (gex < 0)?
---------------------------------------------------------------
The original naive heuristic for gamma regime detection triggered whenever raw
`gex < 0`. However, backtesting against historical market regimes demonstrated that raw
dollar GEX has a strong positive drift over decades due to SPX index growth, open
interest expansion, and option market volume growth.

When evaluated against the Feb-Mar 2025 market crash window (40 trading days):
    1. Raw `gex < 0` flagged ONLY 3 of 40 days (7.5%) as negative regime.
       Under raw sign filtering, defensive exit-guards and crash-insurance logic
       would have remained disengaged for 92.5% of the crash window.
    2. A trailing 252-session percentile-rank threshold at the 10th percentile
       (rank < 0.10) flagged 21 of 40 days (52.5%) as negative regime.
       Compared to the baseline rate of ~10.7% across the entire historical series,
       this represents an empirical ~5x enrichment during severe market stress.

Regime Semantics:
    - 'NEGATIVE': STRICT / Crash-insurance regime. Dealer gamma is historically depressed
                  (in the lowest 10% of the trailing year). Market fragility and volatility
                  risk are elevated because dealer hedging amplifies rather than dampens moves.
    - 'POSITIVE': RELAXED / Peacetime regime. Dealer gamma is normal or high. Dealer hedging
                  acts as a shock absorber, dampening volatility.

Look-Ahead Bias Hard Rule:
    For any decision evaluated as of `as_of_date` (date t), ONLY rows with `date < as_of_date`
    may be utilized. End-of-day options open interest and dealer gamma for session t are
    computed after market close and are not available during trading hours of session t.
    Intraday evaluations on date t MUST use positioning data from t-1 or earlier.

Fail-Safe Default:
    If market data is missing, stale (> max_staleness_days), or has insufficient lookback
    (< 20 sessions), the engine always defaults to `regime = 'NEGATIVE'` and
    `data_available = False`. The system NEVER fails open into the relaxed regime.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

# SqueezeMetrics public DIX URL
DIX_URL: str = "https://squeezemetrics.com/monitor/static/DIX.csv"

# Default cache path relative to this script: scripts/backtest/run_out_dix/dix_cache.csv
DEFAULT_CACHE_DIR: Path = Path(__file__).resolve().parent / "run_out_dix"
DEFAULT_CACHE_PATH: Path = DEFAULT_CACHE_DIR / "dix_cache.csv"

# In-process memoization storage: maps resolved cache path string -> pd.DataFrame
_MEMO_CACHE: dict[str, pd.DataFrame] = {}


def _parse_dix_dataframe(source: io.BytesIO | Path | str) -> pd.DataFrame:
    """
    Parse CSV content from bytes buffer or file path into a validated DataFrame.

    Output format:
        - Index: pd.DatetimeIndex ascending, named 'date', timezone-naive pd.Timestamp elements
        - Columns: ['price', 'dix', 'gex'] as float64
    """
    df = pd.read_csv(source)
    required_cols = {"date", "price", "dix", "gex"}
    if not required_cols.issubset(df.columns):
        missing = required_cols - set(df.columns)
        raise ValueError(f"DIX CSV missing required columns: {sorted(missing)}")

    # Ensure date column is parsed to datetime and sorted ascending
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if df["date"].isna().any():
        df = df.dropna(subset=["date"])

    df = df.sort_values("date")
    df = df.set_index("date")
    df.index.name = "date"

    # Normalize index to timezone-naive midnight timestamps
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()

    # Enforce float types for price, dix, gex
    for col in ["price", "dix", "gex"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[["price", "dix", "gex"]]
    if df.empty:
        raise ValueError("Parsed DIX dataset is empty.")

    return df


def load_series(
    cache_path: Path | None = None,
    force_refresh: bool = False,
    timeout: int = 30,
) -> pd.DataFrame:
    """
    Fetch https://squeezemetrics.com/monitor/static/DIX.csv, parse to a
    pandas DataFrame indexed by date (ascending, pd.Timestamp index),
    columns ['price', 'dix', 'gex'] (floats).
    On network failure: fall back to a local CSV cache at
    scripts/backtest/run_out_dix/dix_cache.csv (create the dir if missing).
    On network SUCCESS: overwrite that same cache file with the fresh data
    before returning (so the cache is always the last-known-good fetch).
    If both the live fetch AND the cache are unavailable, raise a clear
    RuntimeError (do not return empty/fake data silently).
    force_refresh=True bypasses any in-process memoization (no need for
    disk-level TTL logic — the caller controls frequency).
    """
    target_cache = Path(cache_path) if cache_path is not None else DEFAULT_CACHE_PATH
    target_cache_key = str(target_cache.resolve())

    # Return in-process memoized copy if available and force_refresh is False
    if not force_refresh and target_cache_key in _MEMO_CACHE:
        return _MEMO_CACHE[target_cache_key].copy()

    # Attempt live network fetch
    raw_csv_bytes: bytes | None = None
    network_error: Exception | None = None

    try:
        req = urllib.request.Request(
            DIX_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (AriaTrading-DIX-Fetcher/2.0; Linux x86_64)",
                "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise urllib.error.HTTPError(
                    DIX_URL, response.status, f"HTTP Error {response.status}", response.headers, None
                )
            raw_csv_bytes = response.read()

        # Parse and validate the downloaded bytes
        df = _parse_dix_dataframe(io.BytesIO(raw_csv_bytes))

        # Overwrite the cache file atomically on network success
        try:
            target_cache.parent.mkdir(parents=True, exist_ok=True)
            temp_cache = target_cache.with_name(f".{target_cache.name}.tmp.{os.getpid()}")
            temp_cache.write_bytes(raw_csv_bytes)
            temp_cache.replace(target_cache)
        except Exception as cache_write_err:
            print(
                f"[dix_fetcher_v2] Warning: Could not write cache file {target_cache}: {cache_write_err}",
                file=sys.stderr,
            )

        # Update in-process memoization and return
        _MEMO_CACHE[target_cache_key] = df.copy()
        return df.copy()

    except Exception as exc:
        network_error = exc
        print(
            f"[dix_fetcher_v2] Network fetch from {DIX_URL} failed ({type(exc).__name__}: {exc}). "
            f"Falling back to local cache at: {target_cache}",
            file=sys.stderr,
        )

    # Fallback to local cache
    if target_cache.exists() and target_cache.is_file():
        try:
            df = _parse_dix_dataframe(target_cache)
            _MEMO_CACHE[target_cache_key] = df.copy()
            return df.copy()
        except Exception as cache_parse_err:
            raise RuntimeError(
                f"DIX data unavailable: live network fetch failed ({network_error}) and local "
                f"cache file at {target_cache} is corrupted or unparseable ({cache_parse_err})."
            ) from network_error
    else:
        raise RuntimeError(
            f"DIX data unavailable: live network fetch failed ({network_error}) and local "
            f"cache file does not exist at {target_cache}."
        ) from network_error


def gex_regime(
    series: pd.DataFrame,
    as_of_date: Any,
    lookback: int = 252,
    percentile_threshold: float = 0.10,
    max_staleness_days: int = 5,
) -> dict[str, Any]:
    """
    Compute the regime as of `as_of_date` (a date or pd.Timestamp), using ONLY
    rows with date < as_of_date (never same-day — OI/GEX for date t reflects
    end-of-day-t positioning, so a decision made ON day t may only use gex
    from t-1 or earlier; this is a hard look-ahead rule, not a suggestion).

    Find the most recent row with date < as_of_date. If none exists, or its
    date is more than `max_staleness_days` calendar days before as_of_date,
    return {'data_available': False, 'regime': 'NEGATIVE', 'reason': 'stale_or_missing', ...}
    -- NEGATIVE (i.e. the STRICT/conservative regime) is the fail-safe default
    when data is missing or stale. Never fail open into the relaxed regime.

    Otherwise: take that row's gex value. Compute its percentile rank against
    the trailing `lookback` sessions strictly before it (rank = fraction of
    those trailing sessions with a LOWER gex value; 0.0 = lowest in the
    window, 1.0 = highest). If fewer than lookback available, use what exists
    (min 20 sessions, else data_available False).

    regime = 'NEGATIVE' if percentile_rank < percentile_threshold else 'POSITIVE'.
    ('NEGATIVE' names the STRICT/crash-insurance regime, 'POSITIVE' names the
    RELAXED/peacetime regime -- names describe regime character, chosen
    because the design originally used raw gex<0 as the trigger and was
    empirically replaced with this percentile version; see the module
    docstring you write for why, using the numbers below.)

    Return dict: {'as_of_date': str, 'gex_date': str, 'gex': float,
                   'percentile_rank': float, 'regime': 'NEGATIVE'|'POSITIVE',
                   'lookback_used': int, 'data_available': True,
                   'staleness_days': int}
    """
    # Standardize as_of_date into a timezone-naive midnight Timestamp
    if isinstance(as_of_date, str):
        as_of_ts = pd.Timestamp(as_of_date)
    elif isinstance(as_of_date, (datetime.datetime, pd.Timestamp)):
        as_of_ts = pd.Timestamp(as_of_date)
    elif isinstance(as_of_date, datetime.date):
        as_of_ts = pd.Timestamp(as_of_date)
    else:
        as_of_ts = pd.to_datetime(as_of_date)

    if as_of_ts.tzinfo is not None:
        as_of_ts = as_of_ts.tz_localize(None)
    as_of_day = as_of_ts.normalize()
    as_of_str = as_of_day.strftime("%Y-%m-%d")

    # Hard look-ahead filter: strictly date < as_of_day (never same-day)
    history = series[series.index < as_of_day]

    if history.empty:
        return {
            "as_of_date": as_of_str,
            "gex_date": None,
            "gex": None,
            "percentile_rank": None,
            "regime": "NEGATIVE",
            "lookback_used": 0,
            "data_available": False,
            "staleness_days": None,
            "reason": "stale_or_missing",
        }

    # The evaluated row is the most recent trading session before as_of_date
    current_row = history.iloc[-1]
    gex_date_ts = history.index[-1]
    gex_date_str = gex_date_ts.strftime("%Y-%m-%d")
    gex_val = float(current_row["gex"])

    # Calendar staleness: number of days between as_of_day and evaluated gex date
    staleness_days = (as_of_day - gex_date_ts).days

    if staleness_days > max_staleness_days:
        return {
            "as_of_date": as_of_str,
            "gex_date": gex_date_str,
            "gex": gex_val,
            "percentile_rank": None,
            "regime": "NEGATIVE",
            "lookback_used": 0,
            "data_available": False,
            "staleness_days": staleness_days,
            "reason": "stale_or_missing",
        }

    # Trailing lookback window strictly before the evaluated row
    prior_sessions = history.iloc[:-1]

    # Select trailing window (up to lookback sessions), THEN drop NaN rows
    # before counting/computing. A NaN-contaminated row previously still
    # counted toward the "20 sessions required" minimum (checked on the raw,
    # pre-dropna row count) while never being able to count as "strictly
    # lower" in the percentile comparison below -- it silently diluted the
    # comparison's effective sample size below 20 real observations without
    # ever failing the minimum-sessions gate meant to guarantee that
    # (CodeRabbit finding, 2026-09-15; the file's own self-test at line ~408
    # already asserts lookback_used >= 20 whenever data_available is True --
    # this fix makes that assertion mean what it says).
    trailing_window = prior_sessions.iloc[-lookback:]
    window_gex = trailing_window["gex"].dropna()
    lookback_used = len(window_gex)

    # Minimum 20 VALID sessions required for valid percentile calculation
    if lookback_used < 20:
        return {
            "as_of_date": as_of_str,
            "gex_date": gex_date_str,
            "gex": gex_val,
            "percentile_rank": None,
            "regime": "NEGATIVE",
            "lookback_used": lookback_used,
            "data_available": False,
            "staleness_days": staleness_days,
            "reason": "stale_or_missing",
        }

    # Compute percentile rank: fraction of trailing sessions with a strictly lower GEX
    # 0.0 = lowest in window, 1.0 = highest
    percentile_rank = float((window_gex < gex_val).mean())

    # Regime assignment: NEGATIVE (strict crash insurance) if percentile < threshold
    regime = "NEGATIVE" if percentile_rank < percentile_threshold else "POSITIVE"

    return {
        "as_of_date": as_of_str,
        "gex_date": gex_date_str,
        "gex": gex_val,
        "percentile_rank": percentile_rank,
        "regime": regime,
        "lookback_used": lookback_used,
        "data_available": True,
        "staleness_days": staleness_days,
    }


def latest(
    percentile_threshold: float = 0.10,
    lookback: int = 252,
    max_staleness_days: int = 3,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Convenience: gex_regime() as of today (real wall-clock date), using
    load_series() to get fresh data. This is what the live exit-guard calls.
    """
    series = load_series(force_refresh=force_refresh)
    today = datetime.date.today()
    return gex_regime(
        series=series,
        as_of_date=today,
        lookback=lookback,
        percentile_threshold=percentile_threshold,
        max_staleness_days=max_staleness_days,
    )


def run_self_test() -> None:
    """
    Self-test verifying the core contracts of load_series() and gex_regime().
    Asserts:
        1. Series is non-empty.
        2. Series is sorted ascending by date.
        3. gex_regime() on the most recent date returns expected keys and valid regime.
        4. gex_regime() on a date far outside range (year 2000) returns data_available=False, regime='NEGATIVE'.
    """
    print("=== [dix_fetcher_v2] Running Self-Tests ===")
    try:
        # 1. Load series and verify non-empty
        print("[TEST 1] Testing load_series()...")
        series = load_series()
        assert not series.empty, "Assertion failed: series DataFrame is empty."
        print(f"  -> PASS: Loaded series with {len(series)} rows (columns: {list(series.columns)})")

        # 2. Verify sorting order
        print("[TEST 2] Verifying ascending date order...")
        assert series.index.is_monotonic_increasing, (
            "Assertion failed: series index is not monotonically increasing."
        )
        print(f"  -> PASS: Dates strictly sorted ({series.index[0].date()} to {series.index[-1].date()})")

        # 3. Test gex_regime() on most recent date in series
        print("[TEST 3] Evaluating gex_regime() on most recent date in series...")
        most_recent_date = series.index[-1]
        res = gex_regime(series, as_of_date=most_recent_date)

        expected_keys = {
            "as_of_date",
            "gex_date",
            "gex",
            "percentile_rank",
            "regime",
            "lookback_used",
            "data_available",
            "staleness_days",
        }
        for k in expected_keys:
            assert k in res, f"Assertion failed: missing expected key '{k}' in result dict: {res}"

        assert res["regime"] in {"NEGATIVE", "POSITIVE"}, (
            f"Assertion failed: invalid regime value '{res['regime']}'"
        )
        assert res["data_available"] is True, "Assertion failed: expected data_available=True"
        assert res["lookback_used"] >= 20, f"Assertion failed: lookback_used {res['lookback_used']} < 20"
        print(
            f"  -> PASS: Result valid. as_of={res['as_of_date']}, gex_date={res['gex_date']}, "
            f"gex={res['gex']:.2e}, rank={res['percentile_rank']:.4f}, regime={res['regime']}"
        )

        # 4. Test gex_regime() far outside series range (year 2000)
        print("[TEST 4] Evaluating gex_regime() for date in year 2000 (pre-series)...")
        res_ancient = gex_regime(series, as_of_date="2000-01-01")
        assert res_ancient["data_available"] is False, (
            f"Assertion failed: expected data_available=False for year 2000, got: {res_ancient}"
        )
        assert res_ancient["regime"] == "NEGATIVE", (
            f"Assertion failed: expected regime='NEGATIVE' for fail-safe, got '{res_ancient['regime']}'"
        )
        assert res_ancient["reason"] == "stale_or_missing", (
            f"Assertion failed: expected reason='stale_or_missing', got '{res_ancient.get('reason')}'"
        )
        print(f"  -> PASS: Fail-safe triggered correctly: {res_ancient}")

        # 5. Additional sanity check: latest() convenience function
        print("[TEST 5] Testing latest() convenience call...")
        res_latest = latest()
        assert "regime" in res_latest, "Assertion failed: missing 'regime' in latest() output."
        assert res_latest["regime"] in {"NEGATIVE", "POSITIVE"}, "Assertion failed: invalid regime in latest()."
        print(f"  -> PASS: latest() returned regime={res_latest['regime']}, data_available={res_latest['data_available']}")

        print("\n=== ALL SELF-TESTS PASSED ===")

    except AssertionError as aerr:
        print(f"\n[FAIL] Assertion Error: {aerr}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n[FAIL] Unexpected Exception: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """
    CLI Entrypoint.
    Supports:
        python3 dix_fetcher_v2.py --json [--as-of YYYY-MM-DD] [--percentile-threshold 0.10] [--lookback 252] [--force-refresh]
        python3 dix_fetcher_v2.py --test
    Neither flag: prints usage (2026-09-15 -- previously silently ran
    self-tests even without --test, making that flag decorative; CodeRabbit
    finding).
    """
    parser = argparse.ArgumentParser(
        description="SqueezeMetrics DIX/GEX fetcher and regime signal calculator."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print signal dictionary as JSON to stdout (for consumption by jq).",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        type=str,
        default=None,
        help="As-of evaluation date (YYYY-MM-DD). If omitted, evaluates latest() as of wall-clock today.",
    )
    parser.add_argument(
        "--percentile-threshold",
        dest="percentile_threshold",
        type=float,
        default=0.10,
        help="Percentile rank threshold below which regime is NEGATIVE (default: 0.10).",
    )
    parser.add_argument(
        "--lookback",
        dest="lookback",
        type=int,
        default=252,
        help="Lookback window in trading sessions for percentile ranking (default: 252).",
    )
    parser.add_argument(
        "--max-staleness-days",
        dest="max_staleness_days",
        type=int,
        default=None,
        help="Maximum allowed staleness in calendar days (defaults: 3 for latest(), 5 for gex_regime()).",
    )
    parser.add_argument(
        "--force-refresh",
        dest="force_refresh",
        action="store_true",
        help="Bypass in-process memoization and force network re-fetch.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run module self-tests.",
    )

    args = parser.parse_args()

    if args.json:
        try:
            if args.as_of is not None:
                series_data = load_series(force_refresh=args.force_refresh)
                staleness = args.max_staleness_days if args.max_staleness_days is not None else 5
                output = gex_regime(
                    series=series_data,
                    as_of_date=args.as_of,
                    lookback=args.lookback,
                    percentile_threshold=args.percentile_threshold,
                    max_staleness_days=staleness,
                )
            else:
                staleness = args.max_staleness_days if args.max_staleness_days is not None else 3
                output = latest(
                    percentile_threshold=args.percentile_threshold,
                    lookback=args.lookback,
                    max_staleness_days=staleness,
                    force_refresh=args.force_refresh,
                )
            # Output pure JSON to stdout
            print(json.dumps(output, indent=2))
        except Exception as cli_err:
            print(f"[dix_fetcher_v2] Error: {cli_err}", file=sys.stderr)
            sys.exit(1)
    elif args.test:
        run_self_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
