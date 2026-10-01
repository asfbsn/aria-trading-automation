#!/usr/bin/env python3
"""
scripts/compute_exit_signal_v2.py
=================================

DRAFT MODULE FOR HUMAN REVIEW — DO NOT WIRE INTO PRODUCTION DIRECTLY.

ARIA exit-signal computation v2 — evaluates whether an OPEN bull-put-spread
position's underlying thesis is still intact over raw OHLCV, augmented by
market-wide dealer gamma (GEX) regime awareness.

Key Changes from v1 (scripts/compute_exit_signal.py):
-----------------------------------------------------
1. Two-Regime Stop Logic:
   - GEX Regime is determined via scripts/backtest/dix_fetcher_v2.py:
     * 'NEGATIVE' (Strict / Crash-Insurance Regime):
       Dealer gamma percentile rank < 10th percentile over trailing 252 sessions
       (or missing/stale data fail-safe). Volatility amplification is elevated.
       Hard technical stop is structural: `close < short_strike`.
     * 'POSITIVE' (Relaxed / Peacetime Regime):
       Dealer gamma percentile rank >= 10th percentile. Normal dealer shock absorption.
       Equity dips below short strike frequently mean-revert before expiry.
       Hard technical stop switches to a mark-to-market premium-multiple stop:
       `current_loss > 2.0 * initial_credit`, where:
         current_loss = max(0, spread_current_mark_cost_to_close - initial_credit)
       (or derived directly from position unrealized P&L: max(0, -unrealized_pnl)).

2. Unchanged Deterministic Hard-CLOSE Triggers:
   The profit-target and time-stop hard triggers remain identical across both regimes:
   - Profit Target: pct_max_profit_captured >= 0.80 (buy back at <= 20% of credit)
   - Time Stop: DTE < 0 (expired), OR (0 <= DTE <= 7 AND pct_max_profit_captured >= 0)

3. Empirical Rationale:
   The Feb-Mar 2025 crash analysis showed raw `gex < 0` failed to flag 92.5% of crash days
   due to long-term index drift. The 10th percentile rank threshold flagged 52.5% of crash
   days (~5x baseline enrichment). This script uses the verified percentile regime.

Input (JSON via stdin or `--input <path>`):
-------------------------------------------
{
  "ticker": str,
  "short_strike": float,
  "bars": [...],                      # ascending OHLCV dicts, or IBKR get_price_history parallel arrays
  "initial_credit": float,            # REQUIRED: $ credit received at entry, PER SHARE
  "entry_date": "YYYY-MM-DD",         # REQUIRED: entry date for as_of_date framing
  "contracts": int | null,            # position size; defaults to 1. Used ONLY to normalize
                                       # unrealized_pnl (see below) -- irrelevant otherwise.
  "unrealized_pnl": float | null,     # live path: TOTAL position dollar P&L straight from IBKR's
                                       # get_account_positions (negative = losing) -- already scaled
                                       # by the 100x option multiplier AND contracts; normalized
                                       # internally to per-share before comparing against
                                       # initial_credit. Do not pre-normalize this yourself.
  "current_mark": float | null,       # backtest path: PER-SHARE mark-to-market cost-to-close
                                       # (used if unrealized_pnl is null) -- already per-share,
                                       # not normalized.
  "gex_regime": dict | null,          # optional pre-computed gex_regime dict; if null, fetches via dix_fetcher_v2.latest()
  "pct_max_profit_captured": float | null, # optional pre-computed profit capture fraction (e.g. 0.85 = 85%)
  "dte": int | null                   # optional days to expiration for time-stop evaluation
}

Output (JSON to stdout):
------------------------
When insufficient_data is true (fewer than MIN_BARS price bars), the
response is a distinct, deliberately minimal shape: ticker, insufficient_data,
settled, bars_provided, bars_required, regime, regime_detail, stop_mode,
premium_stop, premium_stop_unknown, profit_target, time_stop, hard_close,
thesis_invalidated. profit_target/time_stop/premium_stop ARE evaluated even
here -- none need price bars, only credit/pricing/DTE -- so hard_close can
still be true for an already-expired or 80%-captured position, and in
POSITIVE regime thesis_invalidated reflects premium_stop's own triggered
state (that regime's hard gate needs no bars either). In NEGATIVE regime
thesis_invalidated stays false (short-strike breach genuinely needs a
settled close this branch doesn't have -- unmeasured, not verified intact).
No checks/close/rsi20/ma150/short_strike fields -- callers must not read
them in this case.

Extends v1 output:
{
  "ticker": str,
  "insufficient_data": bool,
  "settled": bool,
  "close": float,
  "rsi20": float | null,
  "ma150": float | null,
  "short_strike": float,
  "checks": {
    "short_strike_breached": bool,
    "short_strike_unknown": bool,
    "broke_ma150_support": bool,
    "volume_confirmed_breakdown": bool,
    "rsi_overbought": bool,
    "bearish_candle": bool,
    "candle_pattern": str,
    "premium_stop_unknown": bool,        # POSITIVE regime only: true when premium_stop
                                          # couldn't be evaluated (missing MTM data or invalid
                                          # initial_credit) -- thesis_invalidated defaulted to
                                          # False as an UNMEASURED state, not a verified read.
    "premium_stop_triggered": bool | null,
    "profit_target_triggered": bool | null,
    "time_stop_triggered": bool | null
  },
  "regime": "NEGATIVE" | "POSITIVE",
  "regime_detail": {...},
  "stop_mode": "HARD_STRUCTURAL" | "PREMIUM_MULTIPLE",
  "premium_stop": {
    "evaluated": bool,
    "current_loss": float | null,
    "threshold": float | null,
    "triggered": bool | null,
    "reason": str (optional)
  },
  "profit_target": {
    "evaluated": bool,
    "pct_captured": float | null,
    "threshold": 0.80,
    "triggered": bool | null
  },
  "time_stop": {
    "evaluated": bool,
    "dte": int | null,
    "triggered": bool | null
  },
  "hard_close": bool,
  "thesis_invalidated": bool,
  "note": str
}

Flags:
  --exclude-last-bar   Drop most recent bar before computing indicators (settled read).
  --input <path>       Read payload from file instead of stdin.
  --self-test          Run internal test suite with synthetic inputs and exit.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure current scripts directory and scripts/backtest are importable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_BACKTEST = _HERE / "backtest"
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from signal_core import (  # noqa: E402
    MIN_BARS,
    VOLUME_MA_LENGTH,
    bars_from_parallel_arrays,
    bearish_candle_pattern,
    ema,
    rsi_series,
    sma,
)

# Optional import of dix_fetcher_v2 for live/cron regime resolution
try:
    from dix_fetcher_v2 import latest as dix_latest
except ImportError:
    try:
        from backtest.dix_fetcher_v2 import latest as dix_latest  # type: ignore
    except ImportError:
        dix_latest = None  # type: ignore

RSI_LENGTH = 20
PROFIT_TARGET_THRESHOLD = 0.80
TIME_STOP_MAX_DTE = 7
PREMIUM_STOP_LOSS_MULTIPLE = 2.0
CONTRACT_MULTIPLIER = 100.0


def resolve_gex_regime(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Resolve GEX regime dict from payload or live dix_fetcher_v2.

    If payload provides "gex_regime", it is used directly (ideal for backtests
    avoiding redundant network fetches).
    Otherwise, attempts to call dix_fetcher_v2.latest().
    If dix_fetcher_v2 fails or is unavailable, defaults to conservative fail-safe:
    regime='NEGATIVE', data_available=False.
    """
    precomputed = payload.get("gex_regime")
    if precomputed is not None and isinstance(precomputed, dict):
        # Validate that regime key is present
        if "regime" not in precomputed:
            precomputed["regime"] = "NEGATIVE"
        return precomputed

    # Live / cron path: attempt live fetch
    if dix_latest is not None:
        try:
            res = dix_latest()
            if isinstance(res, dict) and "regime" in res:
                return res
        except Exception as err:
            return {
                "as_of_date": datetime.date.today().strftime("%Y-%m-%d"),
                "gex_date": None,
                "gex": None,
                "percentile_rank": None,
                "regime": "NEGATIVE",
                "lookback_used": 0,
                "data_available": False,
                "staleness_days": None,
                "reason": f"dix_fetcher_error: {err}",
            }

    # Module missing or unavailable: fail-safe strict regime
    return {
        "as_of_date": datetime.date.today().strftime("%Y-%m-%d"),
        "gex_date": None,
        "gex": None,
        "percentile_rank": None,
        "regime": "NEGATIVE",
        "lookback_used": 0,
        "data_available": False,
        "staleness_days": None,
        "reason": "dix_fetcher_v2_unavailable",
    }


def evaluate_exit_indicators(
    closes: List[float],
    bars: List[Dict[str, Any]],
    short_strike: Optional[float],
    rsis: Optional[List[Optional[float]]] = None,
) -> Tuple[Dict[str, Any], Optional[float], Optional[float]]:
    """
    Compute core technical exit indicators and candlestick patterns.
    Reuses signal_core primitives without invoking the v1 exit_checks() hard gate.
    """
    if rsis is None:
        rsis = rsi_series(closes, RSI_LENGTH)
    rsi_now = rsis[-1] if rsis else None
    # EMA, not SMA -- matches signal_core.py's 2026-09-14 dashboard-fidelity
    # fix (all four ma150 = sma(...) call sites there switched to ema()
    # after a real false-positive incident where SMA150 and the live
    # TradingView dashboard's EMA150 disagreed on support by a full sign
    # flip). This file's broke_ma150_support check was never updated to
    # match, so it could show the same false reading on a live open
    # position (CodeRabbit finding, 2026-09-15).
    ma150 = ema(closes, 150)
    close = closes[-1]

    volumes = [b.get("volume", 0) for b in bars]
    vol_ma20 = sma(volumes, VOLUME_MA_LENGTH)

    short_breached = short_strike is not None and close < short_strike
    ma150_breached = ma150 is not None and close < ma150
    volume_confirmed = bool(
        (short_breached or ma150_breached)
        and vol_ma20 is not None
        and volumes[-1] > vol_ma20
    )

    pattern = bearish_candle_pattern(
        bars[-1]["open"],
        bars[-1]["high"],
        bars[-1]["low"],
        bars[-1]["close"],
        bars[-2]["open"] if len(bars) >= 2 else None,
        bars[-2]["close"] if len(bars) >= 2 else None,
    )

    checks: Dict[str, Any] = {
        "short_strike_breached": short_breached,
        "short_strike_unknown": short_strike is None,
        "broke_ma150_support": ma150_breached,
        "volume_confirmed_breakdown": volume_confirmed,
        "rsi_overbought": rsi_now is not None and rsi_now > 70.0,
        "bearish_candle": pattern != "none",
        "candle_pattern": pattern,
    }

    return checks, rsi_now, ma150


def evaluate_premium_stop(
    initial_credit: Optional[float],
    unrealized_pnl: Optional[float],
    current_mark: Optional[float],
) -> Dict[str, Any]:
    """
    Evaluate mark-to-market premium-multiple stop for relaxed (POSITIVE) regime.

    Rule: current_loss > 2.0 * initial_credit
    Where:
      - If unrealized_pnl is supplied (live path, straight from IBKR):
        loss = max(0.0, -unrealized_pnl)
      - If current_mark is supplied (backtest mark-to-market cost-to-close):
        loss = max(0.0, current_mark - initial_credit)
      - If both are null: cannot evaluate; return evaluated=False, reason="no_mtm_data"
    """
    if unrealized_pnl is None and current_mark is None:
        return {
            "evaluated": False,
            "reason": "no_mtm_data",
            "current_loss": None,
            "threshold": (
                round(PREMIUM_STOP_LOSS_MULTIPLE * float(initial_credit), 4)
                if initial_credit is not None
                else None
            ),
            "triggered": None,
        }

    if initial_credit is None or float(initial_credit) <= 0:
        return {
            "evaluated": False,
            "reason": "invalid_initial_credit",
            "current_loss": None,
            "threshold": None,
            "triggered": None,
        }

    credit_val = float(initial_credit)
    threshold = PREMIUM_STOP_LOSS_MULTIPLE * credit_val

    if unrealized_pnl is not None:
        # Live path: unrealized_pnl is dollar P&L (negative = losing)
        pnl_val = float(unrealized_pnl)
        current_loss = max(0.0, -pnl_val)
    else:
        # Backtest path: current_mark is cost-to-close
        mark_val = float(current_mark)  # type: ignore
        current_loss = max(0.0, mark_val - credit_val)

    triggered = bool(current_loss > threshold)

    return {
        "evaluated": True,
        "current_loss": round(current_loss, 4),
        "threshold": round(threshold, 4),
        "triggered": triggered,
    }


def evaluate_profit_target_and_time_stop(
    payload: Dict[str, Any],
    initial_credit: Optional[float],
    unrealized_pnl: Optional[float],
    current_mark: Optional[float],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Evaluate deterministic Profit Target and Time Stop triggers.
    These triggers are identical across both NEGATIVE and POSITIVE regimes.
    """
    # 1. Profit capture calculation
    pct_captured: Optional[float] = None
    if payload.get("pct_max_profit_captured") is not None:
        try:
            pct_captured = float(payload["pct_max_profit_captured"])
        except (ValueError, TypeError):
            pct_captured = None
    elif initial_credit is not None and float(initial_credit) > 0:
        if unrealized_pnl is not None:
            pct_captured = float(unrealized_pnl) / float(initial_credit)
        elif current_mark is not None:
            pct_captured = (float(initial_credit) - float(current_mark)) / float(initial_credit)

    if pct_captured is not None:
        pt_triggered = bool(pct_captured >= PROFIT_TARGET_THRESHOLD)
        profit_target = {
            "evaluated": True,
            "pct_captured": round(pct_captured, 4),
            "threshold": PROFIT_TARGET_THRESHOLD,
            "triggered": pt_triggered,
        }
    else:
        profit_target = {
            "evaluated": False,
            "reason": "no_pricing_data",
            "pct_captured": None,
            "threshold": PROFIT_TARGET_THRESHOLD,
            "triggered": False,
        }

    # 2. Time stop calculation
    raw_dte = payload.get("dte")
    if raw_dte is None and "days_to_expiration" in payload:
        raw_dte = payload["days_to_expiration"]

    if raw_dte is not None:
        try:
            dte = int(raw_dte)
            is_expired = dte < 0
            is_underwater = pct_captured is not None and pct_captured < 0.0
            # not_underwater requires a KNOWN pct_captured >= 0, not merely
            # "not known to be underwater" -- `not is_underwater` is True
            # whenever pct_captured is None (missing pricing data), which
            # would time-stop-close a position we have no evidence is safe
            # to close, possibly one that's actually deeply underwater.
            not_underwater = pct_captured is not None and pct_captured >= 0.0
            # Time stop trigger: DTE < 0 (always), OR (0 <= DTE <= 7 AND confirmed not underwater)
            ts_triggered = is_expired or (0 <= dte <= TIME_STOP_MAX_DTE and not_underwater)
            time_stop = {
                "evaluated": True,
                "dte": dte,
                "triggered": bool(ts_triggered),
                "is_expired": is_expired,
                "underwater": is_underwater,
            }
        except (ValueError, TypeError):
            time_stop = {
                "evaluated": False,
                "reason": "invalid_dte_format",
                "dte": None,
                "triggered": False,
            }
    else:
        time_stop = {
            "evaluated": False,
            "reason": "no_dte_data",
            "dte": None,
            "triggered": False,
        }

    return profit_target, time_stop


def get_expected_last_completed_session(
    ref_date_str: Optional[str] = None,
    holidays_file: Optional[Path] = None,
    now_ny: Optional[datetime.datetime] = None,
) -> str:
    """Calculate the expected last completed trading session prior to ref_date.

    Time-aware like scripts/ghost/fetch_settled_closes.get_last_completed_session: when ref_date is TODAY in
    America/New_York and it is already 16:00 ET or later, today's session has settled and IS the last completed
    session. (Previously this always returned ref_date - 1, so any forced mark run after the close made
    validate_settled_close reject a perfectly good same-day settled close as `synthetic_or_future_date_rejected`.)
    For any other ref_date (all historical replays, and every normal 15:30-15:59 ET mark run) behavior is unchanged."""
    if holidays_file is None:
        holidays_file = _HERE.parent / "us-market-holidays.txt"
    holidays = set()
    if holidays_file.exists():
        import re
        holidays = {
            line.strip()
            for line in holidays_file.read_text(encoding="utf-8").splitlines()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip())
        }

    if ref_date_str:
        try:
            ref_date = datetime.date.fromisoformat(ref_date_str)
        except (ValueError, TypeError):
            from zoneinfo import ZoneInfo
            ref_date = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    else:
        from zoneinfo import ZoneInfo
        ref_date = datetime.datetime.now(ZoneInfo("America/New_York")).date()

    from zoneinfo import ZoneInfo
    if now_ny is None:
        now_ny = datetime.datetime.now(ZoneInfo("America/New_York"))
    if ref_date == now_ny.date() and now_ny.time() >= datetime.time(16, 0):
        cur = ref_date
    else:
        cur = ref_date - datetime.timedelta(days=1)
    while True:
        if cur.weekday() < 5 and cur.isoformat() not in holidays:
            return cur.isoformat()
        cur -= datetime.timedelta(days=1)


def validate_settled_close(
    settled_close: Any,
    expected_ticker: str,
    expected_session_date: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[float]]:
    """Validate settled_close input dictionary.

    Returns: (is_valid, reason, close_price)
    """
    if settled_close is None:
        return False, "missing_settled_close", None
    if not isinstance(settled_close, dict):
        return False, "invalid_settled_close_format", None

    # Synthetic / dummy row exclusion (e.g. forward-filled bars from ghost_prescreen_v2)
    if settled_close.get("synthetic") is True or settled_close.get("is_dummy") is True:
        return False, "synthetic_or_dummy_close_rejected", None

    # Validate close price
    raw_close = settled_close.get("close")
    if isinstance(raw_close, bool) or not isinstance(raw_close, (int, float)):
        return False, "missing_or_nonnumeric_close", None
    import math
    if not math.isfinite(raw_close) or raw_close <= 0:
        return False, "nonpositive_or_nonfinite_close", None

    # Validate ticker identity -- required, not merely checked when present:
    # a settled_close missing provenance (ticker/source/retrieved_ts_utc) is
    # not distinguishable from one that was never actually resolved against
    # this position (Astra, 2026-09-19).
    close_ticker = settled_close.get("ticker")
    if not close_ticker or not isinstance(close_ticker, str):
        return False, "missing_ticker", None
    if str(close_ticker).strip().upper() != str(expected_ticker).strip().upper():
        return False, f"ticker_mismatch: expected {expected_ticker}, got {close_ticker}", None

    source = settled_close.get("source")
    if not source or not isinstance(source, str):
        return False, "missing_source", None
    retrieved_ts_utc = settled_close.get("retrieved_ts_utc")
    if not retrieved_ts_utc or not isinstance(retrieved_ts_utc, str):
        return False, "missing_retrieved_ts_utc", None

    # Validate session_date
    session_date = settled_close.get("session_date")
    if not session_date or not isinstance(session_date, str):
        return False, "missing_or_invalid_session_date", None
    try:
        dt = datetime.date.fromisoformat(session_date)
        if dt.isoformat() != session_date:
            return False, "invalid_session_date_iso_format", None
    except (ValueError, TypeError):
        return False, "invalid_session_date_format", None

    if expected_session_date is not None:
        # A malformed expected_session_date must fail closed, not silently
        # skip the freshness check -- the prior `except: pass` let a
        # 2020-01-02 close validate as fine whenever the caller's own
        # expected-date computation was itself broken (Astra, 2026-09-19,
        # reproduced: validate_settled_close(..., expected_session_date=
        # "malformed") returned valid=True).
        try:
            exp_dt = datetime.date.fromisoformat(expected_session_date)
        except (ValueError, TypeError):
            return False, "malformed_expected_session_date", None
        if dt > exp_dt:
            return False, f"synthetic_or_future_date_rejected: {session_date} > {expected_session_date}", None
        if dt != exp_dt:
            return False, f"stale_settled_close: expected {expected_session_date}, got {session_date}", None

    return True, None, float(raw_close)


def compute_exit_signal_v2(
    payload: Dict[str, Any],
    exclude_last_bar: bool = False,
) -> Dict[str, Any]:
    """
    Core functional engine for exit signal v2 computation.
    Takes a validated payload dict, returns the full output dict.
    """
    ticker = payload.get("ticker", "UNKNOWN")
    short_strike = payload.get("short_strike")
    if short_strike is not None:
        short_strike = float(short_strike)

    initial_credit = payload.get("initial_credit")
    if initial_credit is not None:
        initial_credit = float(initial_credit)

    unrealized_pnl = payload.get("unrealized_pnl")
    if unrealized_pnl is not None:
        unrealized_pnl = float(unrealized_pnl)
        # unrealized_pnl straight from IBKR's get_account_positions is TOTAL
        # position dollars -- already scaled by the 100x option multiplier
        # AND by contract count. initial_credit/current_mark are per-share,
        # per-spread figures the rest of this module compares it against.
        # Left unnormalized, current_loss (evaluate_premium_stop) and
        # pct_captured (evaluate_profit_target_and_time_stop) would compare
        # a total-dollar loss against a per-share*2 threshold -- off by
        # ~100x*contracts, which for any real position with contracts>=1
        # makes the POSITIVE-regime premium stop misfire on essentially
        # every open position regardless of actual health (CodeRabbit
        # finding, 2026-09-15). Normalize down to the same per-share scale.
        # A MISSING contracts key is treated the same as an invalid one
        # (unmeasured, not silently assumed to be 1) -- the prompt is now
        # required to always supply it alongside unrealized_pnl; a caller
        # that omits it has a real bug upstream, and defaulting to 1 would
        # hide that bug by silently misnormalizing any position sized above
        # 1 contract (CodeRabbit finding, 2026-09-16).
        contracts = payload.get("contracts")
        try:
            contracts = float(contracts) if contracts is not None else None
        except (TypeError, ValueError):
            contracts = None
        if contracts is not None and contracts > 0:
            unrealized_pnl = unrealized_pnl / (CONTRACT_MULTIPLIER * contracts)
        else:
            # Malformed contracts -- can't safely normalize a total-dollar
            # figure to per-share scale. Treat as unmeasured (matches this
            # file's existing fail-safe style: evaluated=False with a
            # reason, never a hard crash) rather than comparing mismatched
            # units silently.
            unrealized_pnl = None

    current_mark = payload.get("current_mark")
    if current_mark is not None:
        current_mark = float(current_mark)

    # 1. Resolve GEX regime
    gex_detail = resolve_gex_regime(payload)
    regime = gex_detail.get("regime", "NEGATIVE")
    if regime not in ("NEGATIVE", "POSITIVE"):
        regime = "NEGATIVE"

    stop_mode = "HARD_STRUCTURAL" if regime == "NEGATIVE" else "PREMIUM_MULTIPLE"

    # 1b. Settled close validation for bar-independent structural evaluation
    has_settled_close = "settled_close" in payload
    settled_close_raw = payload.get("settled_close") if has_settled_close else None
    sc_valid = False
    sc_reason = "missing_settled_close"
    sc_close: Optional[float] = None
    sc_breached: Optional[bool] = None
    sc_unknown = True

    if has_settled_close:
        expected_session = payload.get("expected_session_date")
        if expected_session is None:
            ref_date_str = payload.get("as_of_date") or payload.get("mark_date")
            expected_session = get_expected_last_completed_session(ref_date_str)
        sc_valid, sc_reason, sc_close = validate_settled_close(
            settled_close_raw, ticker, expected_session
        )
        if sc_valid:
            if short_strike is not None:
                sc_breached = bool(sc_close < short_strike)
                sc_unknown = False
            else:
                sc_valid = False
                sc_breached = None
                sc_unknown = True
                sc_reason = "short_strike_missing"
        else:
            sc_breached = None
            sc_unknown = True

    # Profit-target, time-stop, AND (in POSITIVE regime) premium_stop all
    # need only credit/pricing/DTE inputs, never price bars -- evaluate them
    # before the bars check so an insufficient-data position (can't compute
    # MA150/RSI/candles) still gets a real hard_close/thesis_invalidated read
    # when it's already expired, hit the 80% profit target, or (POSITIVE
    # regime only -- premium_stop IS that regime's hard structural gate, no
    # bars involved) breached the 2.0x-credit mark-to-market loss threshold.
    # Otherwise those CLOSE triggers were silently dropped just because the
    # technical side is unmeasured (CodeRabbit finding, 2026-09-16).
    profit_target, time_stop = evaluate_profit_target_and_time_stop(
        payload=payload,
        initial_credit=initial_credit,
        unrealized_pnl=unrealized_pnl,
        current_mark=current_mark,
    )
    premium_stop = evaluate_premium_stop(
        initial_credit=initial_credit,
        unrealized_pnl=unrealized_pnl,
        current_mark=current_mark,
    )
    premium_stop_unknown_insufficient = regime == "POSITIVE" and not premium_stop["evaluated"]

    # In POSITIVE regime, premium_stop is the hard gate (bar-independent).
    # In NEGATIVE regime, structural breach against settled_close is evaluable
    # without bars if settled_close was supplied.
    thesis_invalidated_by_insufficient: Optional[str] = None
    if regime == "POSITIVE":
        thesis_invalidated_insufficient = (
            premium_stop["evaluated"] and bool(premium_stop["triggered"])
        )
        if thesis_invalidated_insufficient:
            thesis_invalidated_by_insufficient = "premium_stop"
    else:  # NEGATIVE regime
        if has_settled_close and sc_valid:
            thesis_invalidated_insufficient = bool(sc_breached)
            if thesis_invalidated_insufficient:
                thesis_invalidated_by_insufficient = "structural_breach"
        else:
            # Missing/stale/invalid settled close: structural check is UNKNOWN
            thesis_invalidated_insufficient = False

    # 2. Extract and sanitize OHLCV bars
    bars = payload["bars"] if "bars" in payload else bars_from_parallel_arrays(payload)
    if exclude_last_bar and bars:
        from zoneinfo import ZoneInfo
        now_ny = datetime.datetime.now(ZoneInfo("America/New_York"))
        today_ny_str = now_ny.strftime("%Y-%m-%d")
        if bars[-1]["date"][:10] == today_ny_str and now_ny.time() < datetime.time(16, 0):
            bars = bars[:-1]

    if len(bars) < MIN_BARS:
        hard_close_insufficient = (
            thesis_invalidated_insufficient
            or bool(profit_target.get("triggered"))
            or bool(time_stop.get("triggered"))
        )
        res_insufficient: Dict[str, Any] = {
            "ticker": ticker,
            "insufficient_data": True,
            "settled": exclude_last_bar,
            "bars_provided": len(bars),
            "bars_required": MIN_BARS,
            "regime": regime,
            "regime_detail": gex_detail,
            "stop_mode": stop_mode,
            "premium_stop": premium_stop,
            "premium_stop_unknown": premium_stop_unknown_insufficient,
            "profit_target": profit_target,
            "time_stop": time_stop,
            "hard_close": hard_close_insufficient,
            "thesis_invalidated": thesis_invalidated_insufficient,
        }
        # Additive only: new keys appear only when caller opted into settled_close
        if has_settled_close:
            res_insufficient["thesis_invalidated_by"] = thesis_invalidated_by_insufficient
            res_insufficient["structural_evaluation"] = {
                "evaluated": sc_valid,
                "session_date": settled_close_raw.get("session_date") if isinstance(settled_close_raw, dict) else None,
                "close": sc_close if sc_valid else None,
                "short_strike": short_strike,
                "breached": sc_breached,
                "unknown": sc_unknown,
                "reason": sc_reason,
            }
        return res_insufficient

    closes = [float(b["close"]) for b in bars]

    # 3. Technical indicators & candlestick checks
    rsis = rsi_series(closes, RSI_LENGTH)
    checks, rsi_now, ma150 = evaluate_exit_indicators(
        closes=closes,
        bars=bars,
        short_strike=short_strike,
        rsis=rsis,
    )

    # 4. Premium stop already evaluated above (bar-independent).

    # 5. Regime-gated thesis_invalidated decision
    premium_stop_unknown = False
    thesis_invalidated_by: Optional[str] = None
    if regime == "NEGATIVE":
        # Strict regime: short strike breach is the hard structural stop
        if has_settled_close and sc_valid:
            thesis_invalidated = bool(sc_breached)
        else:
            # Legacy bars path or fallback when settled_close invalid/missing
            thesis_invalidated = bool(checks["short_strike_breached"])

        # PRECEDENCE DECISION:
        # In NEGATIVE regime (stop_mode="HARD_STRUCTURAL"), if both a structural breach
        # (close < short strike) and a premium-multiple stop (current_loss > 2x credit)
        # are true simultaneously, structural breach takes precedence ('structural_breach').
        # Rationale: Structural breach is the defining hard stop of the NEGATIVE regime,
        # designed to halt positions experiencing gamma amplification during crash/stress
        # conditions. Premium-multiple stop is POSITIVE regime's peacetime mechanism.
        if thesis_invalidated:
            thesis_invalidated_by = "structural_breach"
    else:
        # Relaxed regime: short strike breach alone does NOT invalidate thesis.
        # Must breach the 2.0x credit mark-to-market loss threshold.
        if premium_stop["evaluated"]:
            thesis_invalidated = bool(premium_stop["triggered"])
        else:
            thesis_invalidated = False
            premium_stop_unknown = True

        if thesis_invalidated:
            thesis_invalidated_by = "premium_stop"

    # 6. Profit target & Time stop already evaluated above (bar-independent).

    # Augment checks dict with sub-trigger results
    checks["premium_stop_unknown"] = premium_stop_unknown
    checks["premium_stop_triggered"] = premium_stop.get("triggered")
    checks["profit_target_triggered"] = profit_target.get("triggered")
    checks["time_stop_triggered"] = time_stop.get("triggered")

    hard_close_triggered = (
        thesis_invalidated
        or bool(profit_target.get("triggered"))
        or bool(time_stop.get("triggered"))
    )

    res_full: Dict[str, Any] = {
        "ticker": ticker,
        "insufficient_data": False,
        "settled": exclude_last_bar,
        "close": closes[-1],
        "rsi20": round(rsi_now, 2) if rsi_now is not None else None,
        "ma150": round(ma150, 2) if ma150 is not None else None,
        "short_strike": short_strike,
        "checks": checks,
        "regime": regime,
        "regime_detail": gex_detail,
        "stop_mode": stop_mode,
        "premium_stop": premium_stop,
        "profit_target": profit_target,
        "time_stop": time_stop,
        "hard_close": hard_close_triggered,
        "thesis_invalidated": thesis_invalidated,
        "note": "proxy signal v2 (regime-aware) — draft for human review",
    }
    # Additive only: new keys appear only when caller opted into settled_close
    if has_settled_close:
        res_full["thesis_invalidated_by"] = thesis_invalidated_by
        res_full["structural_evaluation"] = {
            "evaluated": sc_valid,
            "session_date": settled_close_raw.get("session_date") if isinstance(settled_close_raw, dict) else None,
            "close": sc_close if sc_valid else None,
            "short_strike": short_strike,
            "breached": sc_breached,
            "unknown": sc_unknown,
            "reason": sc_reason,
        }
    return res_full


def run_self_tests() -> None:
    """
    Run self-contained verification suite on compute_exit_signal_v2 with synthetic inputs.

    Asserts:
      (a) with regime NEGATIVE and close < short_strike, thesis_invalidated is True via stop_mode HARD_STRUCTURAL.
      (b) with regime POSITIVE and close < short_strike but current_loss under 2x credit, thesis_invalidated is False.
      (c) with regime POSITIVE and current_loss over 2x credit, thesis_invalidated is True via stop_mode PREMIUM_MULTIPLE.
      (d) profit-target and time-stop triggers still fire identically regardless of regime.
      (e) unrealized_pnl (total position dollars) normalizes by both the 100x
          option multiplier and contracts count before comparing against
          per-share initial_credit -- same result at 1 and 5 contracts.
      (f) unrealized_pnl supplied but contracts missing (not just invalid) is
          treated as unmeasured, never silently defaulted to contracts=1.
      (g) insufficient_data still evaluates profit_target/time_stop (bar-
          independent) and can set hard_close=True.
      (h) insufficient_data in POSITIVE regime still evaluates premium_stop
          (also bar-independent, that regime's hard gate) and can set
          thesis_invalidated=True, not just hard_close.
      (n) settled_close with missing short_strike resets sc_valid to False,
          leaving structural_evaluation unmeasured (evaluated=False,
          reason="short_strike_missing") and falling back to checks["short_strike_breached"].
      (o) exclude_last_bar=True with last bar date != today keeps last bar (pre-market call).
      (p) exclude_last_bar=True with last bar date == today drops in-progress bar (regular-hours call).
      (q) exclude_last_bar=True with last bar date == today and time >= 16:00 ET keeps settled bar (post-market call).
    """
    print("=== [compute_exit_signal_v2] Running Self-Tests ===")
    failures = 0

    # Build synthetic 160 daily bars with close at 95.0, volume at 1000
    # Last bar: close=95.0, open=96.0, high=97.0, low=94.0
    synthetic_bars: List[Dict[str, Any]] = []
    base_date = datetime.date(2026, 1, 1)
    for i in range(160):
        d_str = (base_date + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        # Steady baseline around 100.0, tapering to 95.0 on final bar
        c = 100.0 if i < 159 else 95.0
        synthetic_bars.append({
            "date": d_str,
            "open": c + 0.5,
            "high": c + 1.0,
            "low": c - 1.0,
            "close": c,
            "volume": 1000,
        })

    short_strike = 100.0  # Final close is 95.0 -> close < short_strike is TRUE
    initial_credit = 1.00  # $1.00 credit received per spread

    neg_regime_dict = {
        "as_of_date": "2026-06-10",
        "gex_date": "2026-06-09",
        "gex": 1.2e8,
        "percentile_rank": 0.05,
        "regime": "NEGATIVE",
        "lookback_used": 252,
        "data_available": True,
        "staleness_days": 1,
    }

    pos_regime_dict = {
        "as_of_date": "2026-06-10",
        "gex_date": "2026-06-09",
        "gex": 5.5e9,
        "percentile_rank": 0.45,
        "regime": "POSITIVE",
        "lookback_used": 252,
        "data_available": True,
        "staleness_days": 1,
    }

    # -------------------------------------------------------------------------
    # Test (a): NEGATIVE regime and close < short_strike -> HARD_STRUCTURAL trigger
    # -------------------------------------------------------------------------
    payload_a = {
        "ticker": "TEST_A",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        # unrealized_pnl is TOTAL position dollars (IBKR convention, 100x
        # multiplier * contracts) -- -50.0 normalizes to -0.50/share, a
        # 0.50x-credit loss (< 2x threshold, though NEGATIVE regime doesn't
        # gate on this anyway).
        "unrealized_pnl": -50.0,
        "current_mark": None,
        "gex_regime": neg_regime_dict,
        "bars": synthetic_bars,
    }
    res_a = compute_exit_signal_v2(payload_a)
    cond_a = (
        res_a["regime"] == "NEGATIVE"
        and res_a["stop_mode"] == "HARD_STRUCTURAL"
        and res_a["checks"]["short_strike_breached"] is True
        and res_a["thesis_invalidated"] is True
    )
    if cond_a:
        print("[PASS] (a) NEGATIVE regime: close < short_strike triggers thesis_invalidated via HARD_STRUCTURAL")
    else:
        print(f"[FAIL] (a) Expected HARD_STRUCTURAL invalidation, got: {res_a}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (b): POSITIVE regime, close < short_strike, current_loss < 2x credit -> False
    # -------------------------------------------------------------------------
    payload_b = {
        "ticker": "TEST_B",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        # -150.0 total dollars normalizes to -1.50/share (<= 2.0x credit).
        "unrealized_pnl": -150.0,
        "current_mark": None,
        "gex_regime": pos_regime_dict,
        "bars": synthetic_bars,
    }
    res_b = compute_exit_signal_v2(payload_b)
    cond_b = (
        res_b["regime"] == "POSITIVE"
        and res_b["stop_mode"] == "PREMIUM_MULTIPLE"
        and res_b["checks"]["short_strike_breached"] is True
        and res_b["premium_stop"]["current_loss"] == 1.50
        and res_b["premium_stop"]["triggered"] is False
        and res_b["thesis_invalidated"] is False
    )
    if cond_b:
        print("[PASS] (b) POSITIVE regime: close < short_strike but loss <= 2x credit -> thesis intact (False)")
    else:
        print(f"[FAIL] (b) Expected intact thesis under relaxed regime, got: {res_b}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (c): POSITIVE regime, current_loss > 2x credit -> True via PREMIUM_MULTIPLE
    # -------------------------------------------------------------------------
    payload_c = {
        "ticker": "TEST_C",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        # -250.0 total dollars normalizes to -2.50/share (> 2.0 * 1.00 credit).
        "unrealized_pnl": -250.0,
        "current_mark": None,
        "gex_regime": pos_regime_dict,
        "bars": synthetic_bars,
    }
    res_c = compute_exit_signal_v2(payload_c)
    cond_c = (
        res_c["regime"] == "POSITIVE"
        and res_c["stop_mode"] == "PREMIUM_MULTIPLE"
        and res_c["premium_stop"]["current_loss"] == 2.50
        and res_c["premium_stop"]["threshold"] == 2.00
        and res_c["premium_stop"]["triggered"] is True
        and res_c["thesis_invalidated"] is True
    )
    if cond_c:
        print("[PASS] (c) POSITIVE regime: loss > 2x credit triggers thesis_invalidated via PREMIUM_MULTIPLE")
    else:
        print(f"[FAIL] (c) Expected PREMIUM_MULTIPLE trigger, got: {res_c}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (d): Profit-target and time-stop triggers fire identically in both regimes
    # -------------------------------------------------------------------------
    sub_scenarios = [
        # (name, extra_fields, expected_pt, expected_ts)
        ("Profit Target Captured (85%)", {"pct_max_profit_captured": 0.85, "dte": 20}, True, False),
        ("Profit Target Not Captured (50%)", {"pct_max_profit_captured": 0.50, "dte": 20}, False, False),
        ("Time Stop Expired (DTE -1)", {"pct_max_profit_captured": -0.10, "dte": -1}, False, True),
        ("Time Stop Final Week Not Underwater (DTE 4, +20%)", {"pct_max_profit_captured": 0.20, "dte": 4}, False, True),
        ("Time Stop Final Week Underwater (DTE 4, -30%)", {"pct_max_profit_captured": -0.30, "dte": 4}, False, False),
        ("Holding Clean (DTE 18, +30%)", {"pct_max_profit_captured": 0.30, "dte": 18}, False, False),
    ]

    all_d_pass = True
    for s_name, extra_fields, exp_pt, exp_ts in sub_scenarios:
        # Evaluate under NEGATIVE regime (using short_strike=90 so strike is NOT breached)
        p_neg = {
            "ticker": "TEST_D",
            "short_strike": 90.0,
            "initial_credit": 1.00,
            "entry_date": "2026-05-01",
            "gex_regime": neg_regime_dict,
            "bars": synthetic_bars,
            **extra_fields,
        }
        res_neg = compute_exit_signal_v2(p_neg)

        # Evaluate under POSITIVE regime (using current_mark=0.50 so premium stop does NOT fire)
        p_pos = {
            "ticker": "TEST_D",
            "short_strike": 90.0,
            "initial_credit": 1.00,
            "entry_date": "2026-05-01",
            "current_mark": 0.50,
            "gex_regime": pos_regime_dict,
            "bars": synthetic_bars,
            **extra_fields,
        }
        res_pos = compute_exit_signal_v2(p_pos)

        pt_neg = res_neg["profit_target"]["triggered"]
        pt_pos = res_pos["profit_target"]["triggered"]
        ts_neg = res_neg["time_stop"]["triggered"]
        ts_pos = res_pos["time_stop"]["triggered"]

        if not (pt_neg == pt_pos == exp_pt and ts_neg == ts_pos == exp_ts):
            print(f"  [SUB-FAIL] {s_name}: NEG(pt={pt_neg}, ts={ts_neg}) vs POS(pt={pt_pos}, ts={ts_pos}) != EXP(pt={exp_pt}, ts={exp_ts})")
            all_d_pass = False

    if all_d_pass:
        print("[PASS] (d) Profit-target and time-stop triggers fire identically regardless of regime")
    else:
        print("[FAIL] (d) Mismatch in regime-invariance for profit target or time stop triggers")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (e): unrealized_pnl normalization across contracts -- same per-share
    # economics as test (c) (loss 2.50/share > 2.0x credit) but at 5 contracts,
    # so the raw total-dollar figure is 5x larger. Must normalize to the same
    # current_loss/threshold/triggered result as the 1-contract case, proving
    # the contracts divisor (not just the 100x multiplier) is applied
    # correctly (CodeRabbit finding, 2026-09-15).
    # -------------------------------------------------------------------------
    payload_e = {
        "ticker": "TEST_E",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 5,
        "unrealized_pnl": -1250.0,  # -250.0 * 5 contracts
        "current_mark": None,
        "gex_regime": pos_regime_dict,
        "bars": synthetic_bars,
    }
    res_e = compute_exit_signal_v2(payload_e)
    cond_e = (
        res_e["premium_stop"]["current_loss"] == 2.50
        and res_e["premium_stop"]["threshold"] == 2.00
        and res_e["premium_stop"]["triggered"] is True
        and res_e["thesis_invalidated"] is True
    )
    if cond_e:
        print("[PASS] (e) unrealized_pnl normalizes by contracts, not just the 100x multiplier")
    else:
        print(f"[FAIL] (e) Expected contracts-normalized current_loss=2.50, got: {res_e}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (f): unrealized_pnl supplied but contracts MISSING (not just invalid)
    # -- must be treated as unmeasured (premium_stop unevaluated,
    # premium_stop_unknown=True, thesis_invalidated stays False), never
    # silently defaulted to contracts=1 (CodeRabbit finding, 2026-09-16).
    # -------------------------------------------------------------------------
    payload_f = {
        "ticker": "TEST_F",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "unrealized_pnl": -250.0,  # would trigger PREMIUM_MULTIPLE if wrongly defaulted to contracts=1
        "current_mark": None,
        "gex_regime": pos_regime_dict,
        "bars": synthetic_bars,
    }
    res_f = compute_exit_signal_v2(payload_f)
    cond_f = (
        res_f["premium_stop"]["evaluated"] is False
        and res_f["checks"]["premium_stop_unknown"] is True
        and res_f["thesis_invalidated"] is False
    )
    if cond_f:
        print("[PASS] (f) Missing contracts treated as unmeasured, not defaulted to 1")
    else:
        print(f"[FAIL] (f) Expected unmeasured premium_stop on missing contracts, got: {res_f}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (g): insufficient_data (too few bars) must still evaluate
    # profit_target/time_stop (bar-independent) and set hard_close=True for
    # an already-expired position, instead of silently dropping a real CLOSE
    # trigger just because the technical side is unmeasured (CodeRabbit
    # finding, 2026-09-16).
    # -------------------------------------------------------------------------
    payload_g = {
        "ticker": "TEST_G",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "dte": -1,  # already expired -- unconditional time-stop CLOSE
        "pct_max_profit_captured": -0.10,  # underwater; irrelevant to DTE<0
        "gex_regime": neg_regime_dict,
        "bars": synthetic_bars[:50],  # far below MIN_BARS=155
    }
    res_g = compute_exit_signal_v2(payload_g)
    cond_g = (
        res_g["insufficient_data"] is True
        and "checks" not in res_g
        and res_g["time_stop"]["triggered"] is True
        and res_g["hard_close"] is True
        and res_g["thesis_invalidated"] is False
    )
    if cond_g:
        print("[PASS] (g) insufficient_data still evaluates bar-independent hard_close triggers")
    else:
        print(f"[FAIL] (g) Expected hard_close=True via time_stop despite insufficient_data, got: {res_g}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (h): insufficient_data + POSITIVE regime + premium_stop triggered
    # (loss > 2x credit) -- premium_stop IS that regime's hard structural
    # gate and needs no bars either, so thesis_invalidated must be True here
    # too, not just hard_close (CodeRabbit finding, 2026-09-16).
    # -------------------------------------------------------------------------
    payload_h = {
        "ticker": "TEST_H",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "unrealized_pnl": -250.0,  # -2.50/share > 2.0x credit
        "current_mark": None,
        "gex_regime": pos_regime_dict,
        "bars": synthetic_bars[:50],  # far below MIN_BARS=155
    }
    res_h = compute_exit_signal_v2(payload_h)
    cond_h = (
        res_h["insufficient_data"] is True
        and res_h["premium_stop"]["triggered"] is True
        and res_h["hard_close"] is True
        and res_h["thesis_invalidated"] is True
    )
    if cond_h:
        print("[PASS] (h) insufficient_data + POSITIVE regime still evaluates premium_stop as the hard gate")
    else:
        print(f"[FAIL] (h) Expected thesis_invalidated=True via premium_stop despite insufficient_data, got: {res_h}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (i): settled_close in NEGATIVE regime evaluates structural breach
    # independent of MIN_BARS (bars=[]) -- thesis_invalidated=True,
    # thesis_invalidated_by="structural_breach", hard_close=True.
    # -------------------------------------------------------------------------
    payload_i = {
        "ticker": "TEST_I",
        "short_strike": 100.0,
        "initial_credit": 1.00,
        "entry_date": "2026-05-01",
        "as_of_date": "2026-09-18",
        "expected_session_date": "2026-09-17",
        "current_mark": 1.00,
        "gex_regime": neg_regime_dict,
        "bars": [],
        "settled_close": {
            "ticker": "TEST_I",
            "close": 95.0,  # 95 < 100 -> breached!
            "session_date": "2026-09-17",
            "source": "yfinance",
            "retrieved_ts_utc": "2026-09-18T12:00:00Z",
        },
    }
    res_i = compute_exit_signal_v2(payload_i)
    cond_i = (
        res_i["insufficient_data"] is True
        and res_i["thesis_invalidated"] is True
        and res_i["thesis_invalidated_by"] == "structural_breach"
        and res_i["hard_close"] is True
        and res_i["structural_evaluation"]["evaluated"] is True
        and res_i["structural_evaluation"]["breached"] is True
        and res_i["structural_evaluation"]["unknown"] is False
    )
    if cond_i:
        print("[PASS] (i) settled_close in NEGATIVE regime evaluates structural breach independent of MIN_BARS")
    else:
        print(f"[FAIL] (i) Expected structural breach via settled_close, got: {res_i}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (j): Stale or dummy settled_close -> structural result UNKNOWN, not False, not True
    # -------------------------------------------------------------------------
    payload_j_stale = {
        "ticker": "TEST_J",
        "short_strike": 100.0,
        "initial_credit": 1.00,
        "entry_date": "2026-05-01",
        "expected_session_date": "2026-09-17",
        "gex_regime": neg_regime_dict,
        "bars": [],
        "settled_close": {
            "ticker": "TEST_J",
            "close": 95.0,
            "session_date": "2026-09-10",  # STALE
            "source": "yfinance",
            "retrieved_ts_utc": "2026-09-17T12:00:00Z",
        },
    }
    res_j = compute_exit_signal_v2(payload_j_stale)
    cond_j = (
        res_j["structural_evaluation"]["evaluated"] is False
        and res_j["structural_evaluation"]["unknown"] is True
        and res_j["structural_evaluation"]["breached"] is None
        and res_j["thesis_invalidated"] is False
        and "stale_settled_close" in res_j["structural_evaluation"]["reason"]
    )
    if cond_j:
        print("[PASS] (j) Stale settled_close results in structural evaluation explicitly UNKNOWN")
    else:
        print(f"[FAIL] (j) Expected UNKNOWN on stale settled_close, got: {res_j}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (k): Precedence rule in NEGATIVE regime -- if both structural breach
    # AND premium multiple stop trigger, structural breach takes precedence.
    # -------------------------------------------------------------------------
    payload_k = {
        "ticker": "TEST_K",
        "short_strike": 100.0,
        "initial_credit": 1.00,
        "entry_date": "2026-05-01",
        "expected_session_date": "2026-09-17",
        "current_mark": 4.00,  # loss = 3.00 > 2.0 * 1.00 -> premium_stop triggered
        "gex_regime": neg_regime_dict,
        "bars": [],
        "settled_close": {
            "ticker": "TEST_K",
            "close": 95.0,  # close < short_strike -> structural breach triggered
            "session_date": "2026-09-17",
            "source": "yfinance",
            "retrieved_ts_utc": "2026-09-17T12:00:00Z",
        },
    }
    res_k = compute_exit_signal_v2(payload_k)
    cond_k = (
        res_k["premium_stop"]["triggered"] is True
        and res_k["structural_evaluation"]["breached"] is True
        and res_k["thesis_invalidated"] is True
        and res_k["thesis_invalidated_by"] == "structural_breach"
    )
    if cond_k:
        print("[PASS] (k) NEGATIVE regime precedence: structural breach takes precedence over premium stop")
    else:
        print(f"[FAIL] (k) Expected structural_breach precedence in NEGATIVE regime, got: {res_k}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (l): a malformed expected_session_date must fail validation closed,
    # never silently skip the freshness check (Astra, 2026-09-19 -- reproduced
    # a 2020-01-02 close validating as fine when the caller's own expected-date
    # computation was itself broken).
    # -------------------------------------------------------------------------
    l_valid, l_reason, l_close = validate_settled_close(
        {"ticker": "TEST_L", "close": 95.0, "session_date": "2020-01-02",
         "source": "yfinance", "retrieved_ts_utc": "2026-09-17T12:00:00Z"},
        "TEST_L",
        "malformed",
    )
    cond_l = l_valid is False and l_reason == "malformed_expected_session_date" and l_close is None
    if cond_l:
        print("[PASS] (l) Malformed expected_session_date fails validation closed")
    else:
        print(f"[FAIL] (l) Expected fail-closed on malformed expected_session_date, got: "
              f"valid={l_valid} reason={l_reason} close={l_close}")
        failures += 1

    # -------------------------------------------------------------------------
    # Test (m): settled_close missing provenance (ticker/source/retrieved_ts_utc)
    # is rejected, not silently accepted (Astra, 2026-09-19).
    # -------------------------------------------------------------------------
    m_cases = [
        ({"close": 100.0, "session_date": "2026-09-18", "source": "yfinance",
          "retrieved_ts_utc": "2026-09-17T12:00:00Z"}, "missing_ticker"),
        ({"ticker": "TEST_M", "close": 100.0, "session_date": "2026-09-18",
          "retrieved_ts_utc": "2026-09-17T12:00:00Z"}, "missing_source"),
        ({"ticker": "TEST_M", "close": 100.0, "session_date": "2026-09-18",
          "source": "yfinance"}, "missing_retrieved_ts_utc"),
    ]
    cond_m = True
    for sc, expected_reason in m_cases:
        valid, reason, close = validate_settled_close(sc, "TEST_M", "2026-09-18")
        if not (valid is False and reason == expected_reason and close is None):
            cond_m = False
            print(f"[FAIL] (m) case missing field for reason={expected_reason}: "
                  f"got valid={valid} reason={reason} close={close}")
    if cond_m:
        print("[PASS] (m) settled_close missing provenance (ticker/source/retrieved_ts_utc) rejected")
    else:
        failures += 1

    # -------------------------------------------------------------------------
    # Test (n): Valid settled_close with missing short_strike resets sc_valid
    # to False, leaving structural_evaluation unmeasured (evaluated=False,
    # unknown=True, reason="short_strike_missing") and falling back to the
    # bars-based checks["short_strike_breached"] instead of silently
    # defaulting thesis_invalidated to False via bool(None).
    # -------------------------------------------------------------------------
    payload_n = {
        "ticker": "TEST_N",
        "short_strike": None,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "expected_session_date": "2026-09-17",
        "gex_regime": neg_regime_dict,
        "bars": synthetic_bars,
        "settled_close": {
            "ticker": "TEST_N",
            "close": 95.0,
            "session_date": "2026-09-17",
            "source": "yfinance",
            "retrieved_ts_utc": "2026-09-18T12:00:00Z",
        },
    }
    res_n = compute_exit_signal_v2(payload_n)

    # Prove fallback path is reached: if bars-based check had breached, thesis_invalidated
    # becomes True under the fix (taking the else branch), whereas the old bug would have
    # evaluated bool(sc_breached) == bool(None) == False.
    def mock_eval_indicators(*args, **kwargs):
        c, r, m = orig_eval(*args, **kwargs)
        c["short_strike_breached"] = True
        return c, r, m

    orig_eval = globals()["evaluate_exit_indicators"]
    try:
        globals()["evaluate_exit_indicators"] = mock_eval_indicators
        res_n_fallback = compute_exit_signal_v2(payload_n)
    finally:
        globals()["evaluate_exit_indicators"] = orig_eval

    cond_n = (
        res_n["structural_evaluation"]["evaluated"] is False
        and res_n["structural_evaluation"]["close"] is None
        and res_n["structural_evaluation"]["unknown"] is True
        and res_n["structural_evaluation"]["breached"] is None
        and res_n["structural_evaluation"]["reason"] == "short_strike_missing"
        and res_n["thesis_invalidated"] is False
        and res_n["thesis_invalidated"] == bool(res_n["checks"]["short_strike_breached"])
        and res_n_fallback["checks"]["short_strike_breached"] is True
        and res_n_fallback["thesis_invalidated"] is True
        and res_n_fallback["thesis_invalidated_by"] == "structural_breach"
    )
    if cond_n:
        print("[PASS] (n) settled_close with missing short_strike sets evaluated=False and falls back to checks")
    else:
        print(f"[FAIL] (n) Expected evaluated=False and fallback routing for missing short_strike, got: {res_n}")
        failures += 1

    # (o) exclude_last_bar=True with last bar date != today (Case A: pre-market)
    # The last bar must NOT be dropped; returned close must match the last bar.
    from unittest.mock import patch

    class MockDtCaseA(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 9, 0, 0, tzinfo=tz)

    payload_o = {
        "ticker": "AAPL",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "gex_regime": neg_regime_dict,
        "bars": [b.copy() for b in synthetic_bars],
    }
    payload_o["bars"][-1]["date"] = "2026-06-12T13:30:00Z"
    payload_o["bars"][-1]["close"] = 95.0
    payload_o["bars"][-2]["close"] = 100.0

    with patch("datetime.datetime", MockDtCaseA):
        res_o = compute_exit_signal_v2(payload_o, exclude_last_bar=True)

    if res_o.get("close") == 95.0:
        print("[PASS] (o) exclude_last_bar keeps last bar when bar date != today (pre-market call)")
    else:
        print(f"[FAIL] (o) Expected close 95.0 (kept), got {res_o.get('close')}")
        failures += 1

    # (p) exclude_last_bar=True with last bar date == today (Case B: regular hours)
    # The in-progress last bar MUST be dropped; returned close must match the second-to-last bar.
    class MockDtCaseB(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 14, 0, 0, tzinfo=tz)

    payload_p = {
        "ticker": "AAPL",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "gex_regime": neg_regime_dict,
        "bars": [b.copy() for b in synthetic_bars],
    }
    payload_p["bars"][-1]["date"] = "2026-06-15T18:00:00Z"
    payload_p["bars"][-1]["close"] = 95.0
    payload_p["bars"][-2]["close"] = 100.0

    with patch("datetime.datetime", MockDtCaseB):
        res_p = compute_exit_signal_v2(payload_p, exclude_last_bar=True)

    if res_p.get("close") == 100.0:
        print("[PASS] (p) exclude_last_bar drops last bar when bar date == today (regular-hours call)")
    else:
        print(f"[FAIL] (p) Expected close 100.0 (dropped), got {res_p.get('close')}")
        failures += 1

    # (q) exclude_last_bar=True with last bar date == today and time >= 16:00 ET (Case C: post-market)
    # The settled last bar must NOT be dropped; returned close must match the last bar.
    class MockDtCaseC(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 6, 15, 17, 0, 0, tzinfo=tz)

    payload_q = {
        "ticker": "AAPL",
        "short_strike": short_strike,
        "initial_credit": initial_credit,
        "entry_date": "2026-05-01",
        "contracts": 1,
        "gex_regime": neg_regime_dict,
        "bars": [b.copy() for b in synthetic_bars],
    }
    payload_q["bars"][-1]["date"] = "2026-06-15T21:00:00Z"
    payload_q["bars"][-1]["close"] = 95.0
    payload_q["bars"][-2]["close"] = 100.0

    with patch("datetime.datetime", MockDtCaseC):
        res_q = compute_exit_signal_v2(payload_q, exclude_last_bar=True)

    if res_q.get("close") == 95.0:
        print("[PASS] (q) exclude_last_bar keeps last bar when bar date == today and time >= 16:00 ET (post-market call)")
    else:
        print(f"[FAIL] (q) Expected close 95.0 (kept), got {res_q.get('close')}")
        failures += 1

    print("--------------------------------------------------")
    if failures == 0:
        print("ALL ASSERTIONS PASSED (17/17)")
    else:
        print(f"FAILED: {failures} assertion(s) failed.")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]

    # Self-test invocation: explicit flag or TTY with no args
    if "--self-test" in args or (len(args) == 0 and sys.stdin.isatty()):
        run_self_tests()
        sys.exit(0)

    exclude_last_bar = "--exclude-last-bar" in args

    if "--input" in args:
        idx = args.index("--input")
        if idx + 1 >= len(args):
            sys.stderr.write("Error: --input requires a path argument\n")
            sys.exit(2)
        input_path = args[idx + 1]
        with open(input_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    result = compute_exit_signal_v2(payload, exclude_last_bar=exclude_last_bar)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
