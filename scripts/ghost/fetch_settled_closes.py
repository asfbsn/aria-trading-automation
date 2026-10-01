#!/usr/bin/env python3
"""Fetch real settled closes for ARIA Ghost open positions.

Pulls the last completed trading session's close via yfinance for the CURRENT
set of open-position tickers only (from ghost_exit_logger.open_candidate_ids()),
filters structurally to date <= last_completed_session to exclude any synthetic
or intraday bars, and writes an atomic JSON cache with a _meta.json sidecar.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.ghost.ghost_exit_logger import open_candidate_ids

DEFAULT_OUT = REPO_ROOT / "state" / "ghost" / "settled_closes.json"
DEFAULT_BARS_OUT = REPO_ROOT / "state" / "ghost" / "settled_bars.json"
SETTLED_BARS_N = 220  # >= compute_exit_signal_v2.MIN_BARS (155) plus EMA/RSI warm-up margin


def load_holidays(path: Path) -> Set[str]:
    """Load holiday dates from us-market-holidays.txt (YYYY-MM-DD lines)."""
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip())
    }


def get_last_completed_session(
    as_of_date: datetime.date,
    holidays: Set[str],
    now_ny: Optional[datetime.datetime] = None,
) -> str:
    """Determine the last completed regular trading session prior to or on as_of_date.

    If as_of_date is today in America/New_York and the time is before 16:00 ET
    (regular trading hours are still active or pre-market), today's session has
    not settled yet, so the last completed session is the preceding trading day.
    If as_of_date is in the past, or today after 16:00 ET, that date itself is
    the completed session if it was a trading day.
    """
    if now_ny is None:
        now_ny = datetime.datetime.now(ZoneInfo("America/New_York"))

    # If as_of_date is today and before market close (16:00 ET), today is not completed
    if as_of_date == now_ny.date() and now_ny.time() < datetime.time(16, 0):
        cur = as_of_date - datetime.timedelta(days=1)
    elif as_of_date > now_ny.date():
        # as_of_date in future -> start from today or yesterday
        cur = now_ny.date() if now_ny.time() >= datetime.time(16, 0) else now_ny.date() - datetime.timedelta(days=1)
    else:
        cur = as_of_date

    while True:
        if cur.weekday() < 5 and cur.isoformat() not in holidays:
            return cur.isoformat()
        cur -= datetime.timedelta(days=1)


def fetch_settled_close_for_ticker(
    ticker: str,
    last_completed_session: str,
    retrieval_ts: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch settled close for a single ticker via yfinance.

    Structurally excludes any bar where date > last_completed_session (such as
    synthetic forward-fills, intraday bars, or future dummy rows).
    """
    try:
        t = yf.Ticker(ticker)
        # Fetch 5 days to ensure we have the target session even after weekends/holidays
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
    except Exception as err:
        return None, f"yfinance_exception: {err}"

    if hist is None or hist.empty:
        return None, "empty_history"

    if "Close" not in hist.columns:
        return None, "missing_close_column"

    # Convert timestamps in index to YYYY-MM-DD in America/New_York
    dates: List[str] = []
    for ts in hist.index:
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            dates.append(ts.tz_convert("America/New_York").strftime("%Y-%m-%d"))
        else:
            dates.append(ts.strftime("%Y-%m-%d"))

    # STRUCTURAL EXCLUSION: Filter out any dates > last_completed_session
    # Forward-filled dummy rows, future bars, or uncompleted intraday bars are
    # completely dropped by this mask before extracting evidence.
    valid_mask = [d <= last_completed_session for d in dates]
    filtered_hist = hist[valid_mask]
    filtered_dates = [d for d in dates if d <= last_completed_session]

    if filtered_hist.empty:
        return None, f"no_bars_on_or_before_{last_completed_session}"

    # Target session must be the latest available bar on or before last_completed_session
    latest_date = filtered_dates[-1]
    if latest_date != last_completed_session:
        return None, f"target_session_{last_completed_session}_missing_latest_was_{latest_date}"

    close_val = filtered_hist["Close"].iloc[-1]
    try:
        f_close = float(close_val)
        if not math.isfinite(f_close) or f_close <= 0:
            return None, f"nonpositive_or_nonfinite_close: {close_val}"
    except (TypeError, ValueError):
        return None, f"invalid_close_value: {close_val}"

    return {
        "ticker": ticker,
        "close": round(f_close, 4),
        "session_date": last_completed_session,
        "source": "yfinance",
        "retrieved_ts_utc": retrieval_ts,
    }, None


def settled_bars_from_history(
    hist: "pd.DataFrame",
    last_completed_session: str,
    n_bars: int = SETTLED_BARS_N,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Pure helper: settled daily OHLCV bars (date <= last_completed_session) from a yfinance history frame.

    Same structural exclusion as the settled-close fetch: any bar dated after the last completed session
    (future/forward-fill dummy rows, today's unfinished bar) is dropped, the latest remaining bar must BE the
    last completed session, and rows with non-finite/non-positive OHLC are dropped (not repaired).
    """
    if hist is None or hist.empty:
        return None, "empty_history"
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in hist.columns:
            return None, f"missing_{col.lower()}_column"
    bars: List[Dict[str, Any]] = []
    for ts, row in hist.iterrows():
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            d = ts.tz_convert("America/New_York").strftime("%Y-%m-%d")
        else:
            d = ts.strftime("%Y-%m-%d")
        if d > last_completed_session:
            continue
        try:
            o, h, l, c = (float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]))
            v = float(row["Volume"]) if math.isfinite(float(row["Volume"])) else 0.0
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            continue
        bars.append({"date": d, "open": round(o, 4), "high": round(h, 4), "low": round(l, 4),
                     "close": round(c, 4), "volume": v})
    if not bars:
        return None, f"no_bars_on_or_before_{last_completed_session}"
    if bars[-1]["date"] != last_completed_session:
        return None, f"target_session_{last_completed_session}_missing_latest_was_{bars[-1]['date']}"
    return bars[-n_bars:], None


def fetch_settled_bars_for_ticker(
    ticker: str,
    last_completed_session: str,
    retrieval_ts: str,
    n_bars: int = SETTLED_BARS_N,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Settled daily bars for one ticker. auto_adjust=False on purpose: IBKR's bars (which the live exit
    guard uses) are unadjusted, so an adjusted series would drift the MA150/RSI checks away from live."""
    try:
        hist = yf.Ticker(ticker).history(period="450d", interval="1d", auto_adjust=False)
    except Exception as err:
        return None, f"yfinance_exception: {err}"
    bars, err = settled_bars_from_history(hist, last_completed_session, n_bars)
    if bars is None:
        return None, err
    return {
        "ticker": ticker,
        "session_date": last_completed_session,
        "bars": bars,
        "source": "yfinance",
        "retrieved_ts_utc": retrieval_ts,
    }, None


def fetch_all_settled_bars(
    tickers: List[str],
    last_completed_session: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    out: Dict[str, Dict[str, Any]] = {}
    failures: Dict[str, str] = {}
    retrieval_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for ticker in tickers:
        entry, err = fetch_settled_bars_for_ticker(ticker, last_completed_session, retrieval_ts)
        if entry is not None:
            out[ticker] = entry
        else:
            failures[ticker] = err or "unknown_error"
    return out, failures


def fetch_all_settled_closes(
    tickers: List[str],
    last_completed_session: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """Fetch settled closes for all tickers sequentially or via batch."""
    settled_closes: Dict[str, Dict[str, Any]] = {}
    failures: Dict[str, str] = {}
    retrieval_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for ticker in tickers:
        entry, err = fetch_settled_close_for_ticker(ticker, last_completed_session, retrieval_ts)
        if entry is not None:
            settled_closes[ticker] = entry
        else:
            failures[ticker] = err or "unknown_error"

    return settled_closes, failures


def run_self_test() -> None:
    """Run verification self-test for fetch_settled_closes.py."""
    print("=== [fetch_settled_closes] Running Self-Tests ===")
    holidays = {"2026-01-01", "2026-07-03", "2026-11-26"}

    # Test session date resolution: Friday before 16:00 ET -> resolves to Thursday
    friday_midday = datetime.datetime(2026, 9, 18, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    sess = get_last_completed_session(friday_midday.date(), holidays, now_ny=friday_midday)
    assert sess == "2026-09-17", f"Expected 2026-09-17, got {sess}"

    # Test session date resolution: Friday after 16:00 ET -> resolves to Friday
    friday_after_close = datetime.datetime(2026, 9, 18, 16, 30, tzinfo=ZoneInfo("America/New_York"))
    sess_close = get_last_completed_session(friday_after_close.date(), holidays, now_ny=friday_after_close)
    assert sess_close == "2026-09-18", f"Expected 2026-09-18, got {sess_close}"

    # Test session date resolution: Monday morning -> resolves to Friday
    monday_morning = datetime.datetime(2026, 9, 21, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    sess_mon = get_last_completed_session(monday_morning.date(), holidays, now_ny=monday_morning)
    assert sess_mon == "2026-09-18", f"Expected 2026-09-18, got {sess_mon}"

    # Test session date resolution across holiday
    day_after_thanksgiving = datetime.date(2026, 11, 27)
    thurs_holiday = datetime.datetime(2026, 11, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    sess_hol = get_last_completed_session(day_after_thanksgiving, holidays, now_ny=thurs_holiday)
    assert sess_hol == "2026-11-25", f"Expected Wednesday 2026-11-25, got {sess_hol}"

    print("PASS: get_last_completed_session calendar and holiday arithmetic")

    # Test mock synthetic bar exclusion
    synthetic_hist = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [102.0, 103.0, 104.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [101.0, 102.0, 103.0],
            "Volume": [1000, 1000, 0],  # 3rd row is dummy
        },
        index=[
            pd.Timestamp("2026-09-16 00:00:00-04:00"),
            pd.Timestamp("2026-09-17 00:00:00-04:00"),
            pd.Timestamp("2026-09-18 00:00:00-04:00"),  # future or forward-fill dummy
        ],
    )
    # When last_completed_session is 2026-09-17, row 2026-09-18 MUST be excluded
    dates = [ts.tz_convert("America/New_York").strftime("%Y-%m-%d") for ts in synthetic_hist.index]
    valid_mask = [d <= "2026-09-17" for d in dates]
    filtered = synthetic_hist[valid_mask]
    assert len(filtered) == 2
    assert filtered.index[-1].strftime("%Y-%m-%d") == "2026-09-17"
    assert filtered["Close"].iloc[-1] == 102.0
    print("PASS: synthetic bar date > last_completed_session structural exclusion")

    # settled_bars_from_history: future row excluded, NaN row dropped, latest must be the session, tail-trimmed
    idx = pd.to_datetime([f"2026-09-{d:02d} 00:00:00-04:00" for d in (14, 15, 16, 17, 18)])
    hist = pd.DataFrame(
        {"Open": [100.0, 101.0, float("nan"), 103.0, 104.0], "High": [101.0] * 5, "Low": [99.0] * 5,
         "Close": [100.5, 101.5, 102.5, 103.5, 104.5], "Volume": [10, 10, 10, 10, 0]},
        index=idx,
    )
    bars, err = settled_bars_from_history(hist, "2026-09-17")
    assert err is None and [b["date"] for b in bars] == ["2026-09-14", "2026-09-15", "2026-09-17"], (bars, err)
    assert all(b["date"] <= "2026-09-17" for b in bars), "bar after the session leaked"
    bars2, err2 = settled_bars_from_history(hist, "2026-09-16")
    assert bars2 is None and err2.startswith("target_session_2026-09-16_missing_latest_was_"), err2  # 09-16 row NaN-dropped
    trimmed, _ = settled_bars_from_history(hist, "2026-09-17", n_bars=2)
    assert [b["date"] for b in trimmed] == ["2026-09-15", "2026-09-17"]
    assert settled_bars_from_history(pd.DataFrame(), "2026-09-17")[1] == "empty_history"
    print("PASS: settled_bars_from_history exclusion / NaN-drop / latest-session / trim")
    print("ALL TESTS PASSED")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch live settled closes for Ghost open positions")
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT),
        help=f"Path to output JSON (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--bars-out",
        type=str,
        default=None,
        help=f"Path to settled daily bars JSON (default: <out dir>/settled_bars.json; repo default {DEFAULT_BARS_OUT})",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=str(REPO_ROOT / "state" / "ghost"),
        help="Path to ghost state dir to discover open candidates",
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="As-of date (YYYY-MM-DD, America/New_York). Defaults to today.",
    )
    parser.add_argument(
        "--session-date",
        type=str,
        default=None,
        help="Explicit settled session date (YYYY-MM-DD). If omitted, determined automatically.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run internal verification suite",
    )
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    now_ny = datetime.datetime.now(ZoneInfo("America/New_York"))
    if args.as_of_date:
        as_of_date = datetime.date.fromisoformat(args.as_of_date)
    else:
        as_of_date = now_ny.date()

    holidays = load_holidays(REPO_ROOT / "us-market-holidays.txt")

    if args.session_date:
        last_completed_session = args.session_date
    else:
        last_completed_session = get_last_completed_session(as_of_date, holidays, now_ny)

    state_dir = Path(args.state_dir)
    open_candidates = open_candidate_ids(state_dir)
    tickers = sorted(list({c["ticker"].strip().upper() for c in open_candidates if c.get("ticker")}))

    print(
        f"fetch_settled_closes: open_positions={len(open_candidates)}, tickers={len(tickers)}, "
        f"last_completed_session={last_completed_session} (as_of={as_of_date})"
    )

    out_path = Path(args.out).resolve()
    meta_path = out_path.parent / (out_path.stem + "_meta.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not tickers:
        print("No open positions found. Writing empty settled closes.")
        settled_closes: Dict[str, Any] = {}
        failures: Dict[str, str] = {}
    else:
        settled_closes, failures = fetch_all_settled_closes(tickers, last_completed_session)

    # Atomic write to output path
    tmp_path = out_path.with_suffix(out_path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settled_closes, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)

    # Dated archive copy: the canonical path is overwritten every run, so
    # without this, tomorrow's fetch destroys today's evidence with nothing
    # left to reconcile a past mark against (Astra, 2026-09-19). Mirrors the
    # ghost_entries_<date>.csv snapshot pattern already used elsewhere in
    # this codebase. Exclusive create -- never overwrite an existing day's
    # archived snapshot.
    dated_path = out_path.parent / f"{out_path.stem}_{last_completed_session}{out_path.suffix}"
    if not dated_path.exists():
        tmp_path = dated_path.with_suffix(dated_path.suffix + f".tmp{os.getpid()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settled_closes, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp_path, dated_path)
            except FileExistsError:
                print(f"Dated archive already exists, preserved: {dated_path}")
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        tmp_path.unlink()
    else:
        print(f"Dated archive already exists, preserved: {dated_path}")

    # Settled daily bars (informational bar-based exit checks). Never allowed to affect the closes file above or
    # the exit code: a bars failure just means the logger falls back to bars=[] (insufficient_data) as before.
    bars_out_path = Path(args.bars_out).resolve() if args.bars_out else out_path.parent / "settled_bars.json"
    settled_bars: Dict[str, Any] = {}
    bars_failures: Dict[str, str] = {}
    try:
        if tickers:
            settled_bars, bars_failures = fetch_all_settled_bars(tickers, last_completed_session)
        tmp_bars = bars_out_path.with_suffix(bars_out_path.suffix + f".tmp{os.getpid()}")
        with open(tmp_bars, "w", encoding="utf-8") as f:
            json.dump(settled_bars, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_bars, bars_out_path)
    except Exception as err:  # noqa: BLE001
        print(f"WARNING: settled bars fetch/write failed ({err}); bar-based checks will be unmeasured")
        settled_bars, bars_failures = {}, {t: f"bars_stage_exception: {err}" for t in tickers}

    fetch_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = {
        "fetch_ts": fetch_ts,
        "as_of_date": as_of_date.isoformat(),
        "last_completed_session": last_completed_session,
        "source": "yfinance",
        "tickers_requested": tickers,
        "tickers_covered": sorted(list(settled_closes.keys())),
        "tickers_failed": failures,
        "bars_covered": sorted(list(settled_bars.keys())),
        "bars_failed": bars_failures,
        "bars_n_target": SETTLED_BARS_N,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(
        f"Wrote {len(settled_closes)}/{len(tickers)} settled closes to {out_path} "
        f"(session {last_completed_session})"
    )
    if failures:
        print(f"Failures: {failures}")
    print(f"Wrote settled bars for {len(settled_bars)}/{len(tickers)} tickers to {bars_out_path}")
    if bars_failures:
        print(f"Bars failures: {bars_failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
