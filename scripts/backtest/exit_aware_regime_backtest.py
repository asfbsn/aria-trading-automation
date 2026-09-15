"""Exit-aware Bull Put Spread backtest with VRP entry gate and GEX regime-aware exits.

Research artifact: Combines the two sandboxed v2 draft components reviewed tonight:
1. bps_signal_engine_v2.py (BullPutSpreadSignalEngine):
   VRP Entry Gate (mode='vrp_plus_baseline') requiring both the 5-key technical gate
   AND bar-t implied/historical volatility ratio >= 1.1 (iv_current/hv_current >= 1.1).
   Entries with missing or non-positive IV/HV are rejected.
2. dix_fetcher_v2.py / compute_exit_signal_v2.py:
   GEX Regime-Aware Exit Rule using trailing 252-day 10th percentile rank of SPX dealer gamma:
   - NEGATIVE regime (strict / crash-insurance): technical invalidation triggers on
     underlying close < short_strike (STRIKE_BREACH).
   - POSITIVE regime (peacetime / relaxed): technical invalidation triggers on
     mark-to-market loss > 2.0 * initial_credit (PREMIUM_MULTIPLE_STOP).
     Underlying close < short_strike does NOT trigger exit in POSITIVE regime.
   - PROFIT_TARGET (0.80) and TIME_STOP (DTE <= 7, not underwater, or DTE < 0):
     UNCHANGED across both regimes.

Standard:
- Reuses the 563-ticker covered universe from exit_aware_realiv_backtest.py.
- Real DoltHub IV pricing injection into simulator (options_portfolio) and engine.
- 4 runs compared: iv_hold, iv_exit (loaded from run_out_exit_aware_realiv),
  vrp_hold (new), vrp_regime_exit (new).
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Setup pathing to allow robust imports from scripts/ and scripts/backtest/
BASE = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE.parent
REPO_ROOT = SCRIPTS_DIR.parent
for p in [str(BASE), str(SCRIPTS_DIR), str(REPO_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from signal_core import MIN_BARS, entry_checks, rsi_series  # noqa: E402
import exit_aware_full_universe_backtest as base  # noqa: E402
import exit_aware_realiv_backtest as realiv  # noqa: E402
import options_portfolio  # noqa: E402
from bps_signal_engine_v2 import BullPutSpreadSignalEngine  # noqa: E402
import dix_fetcher_v2  # noqa: E402

# Directories and file locations
REALIV_DIR = BASE / "run_out_exit_aware_realiv"
OUT_DIR = BASE / "run_out_exit_aware_regime"
VRP_THRESHOLD = 1.1
PREMIUM_STOP_LOSS_MULTIPLE = 2.0

RUN_CONFIGS = {
    "vrp_hold": dict(entry_mode="vrp_plus_baseline", exit_aware=False, reentry=False),
    "vrp_regime_exit": dict(entry_mode="vrp_plus_baseline", exit_aware=True, reentry=False),
}


# ==============================================================================
# 1. Regime Precomputation
# ==============================================================================

def precompute_gex_regimes(calendar_dates: List[pd.Timestamp]) -> Dict[str, str]:
    """
    Precompute dealer gamma (GEX) regimes for all unique calendar trading days.

    Calls dix_fetcher_v2.load_series() once at startup, caching to run_out_dix/dix_cache.csv.
    For each unique trading date, calls dix_fetcher_v2.gex_regime(df_dix, dt), which strictly
    uses rows with date < dt (enforcing the strict look-ahead rule).

    Returns a dict mapping string date ('YYYY-MM-DD') -> 'NEGATIVE' | 'POSITIVE'.
    Conservative default: if a date is unknown or missing, defaults to 'NEGATIVE'.
    """
    print("Precomputing GEX regimes across trading calendar...", flush=True)
    df_dix = dix_fetcher_v2.load_series()
    regime_map: Dict[str, str] = {}
    for dt in calendar_dates:
        d_str = str(dt.date()) if hasattr(dt, "date") else str(dt)[:10]
        res = dix_fetcher_v2.gex_regime(df_dix, dt)
        regime_map[d_str] = res["regime"]
    print(f"Precomputed {len(regime_map)} session regimes.", flush=True)
    return regime_map


# ==============================================================================
# 2. Combined V2 Engine
# ==============================================================================

class RegimeAwareBPSEngine(base.ExitAwareBPSEngine):
    """
    Combined Bull Put Spread Signal Engine.

    Subclasses base.ExitAwareBPSEngine:
    1. Swaps the entry selection gate for bps_signal_engine_v2's VRP gate:
       - Evaluates bar t's IV and HV directly from DoltHub volatility history.
       - Requires iv_current / hv_current >= vrp_threshold (1.1).
       - Candidates with missing/invalid IV/HV are skipped.
    2. Implements the GEX regime-aware exit rule natively in the fast walk-forward loop:
       - In NEGATIVE regime: underlying close < short_strike triggers STRIKE_BREACH.
       - In POSITIVE regime: current_loss > 2.0 * initial_credit triggers PREMIUM_MULTIPLE_STOP.
         Underlying close < short_strike is relaxed and does NOT exit.
       - PROFIT_TARGET (0.80) and TIME_STOP (DTE <= 7, captured >= 0, or DTE < 0):
         identical across both regimes.
    """

    def __init__(
        self,
        exit_aware: bool,
        reentry: bool = False,
        width: float = base.SPREAD_WIDTH,
        target_dte: int = base.TARGET_DTE,
        entry_mode: str = "vrp_plus_baseline",
        vrp_threshold: float = VRP_THRESHOLD,
        iv_hv_cache: Optional[Dict[str, pd.DataFrame]] = None,
        regime_by_date: Optional[Dict[str, str]] = None,
    ):
        super().__init__(exit_aware=exit_aware, reentry=reentry, width=width, target_dte=target_dte)
        self.entry_mode = entry_mode
        self.vrp_threshold = vrp_threshold
        self.iv_hv_cache = iv_hv_cache or {}
        self.regime_by_date = regime_by_date or {}
        self.skipped_missing_iv = 0

    def generate(self, data_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        self.entries = []
        self.exits = []
        self.skipped_missing_iv = 0

        for code, df in data_map.items():
            df = df.sort_index()
            closes = df["close"].tolist()
            opens = df["open"].tolist() if "open" in df.columns else closes
            highs = df["high"].tolist() if "high" in df.columns else closes
            lows = df["low"].tolist() if "low" in df.columns else closes
            volumes = df["volume"].tolist() if "volume" in df.columns else [0] * len(closes)
            dates = list(df.index)

            # Volatility series for simulator pricing parity (real IV series injected)
            hv = realiv.patched_historical_volatility(df["close"])
            hv_list = hv.tolist()
            rsis_full = rsi_series(closes)
            open_until = None

            # DoltHub raw volatility history for bar-t VRP gating
            history = self.iv_hv_cache.get(code)
            vol_by_date = {} if history is None else {
                pd.Timestamp(date).date(): (iv, hv_val)
                for date, iv, hv_val in zip(history.index, history["iv_current"], history["hv_current"])
            }

            for i in range(len(df)):
                if i < MIN_BARS or i < 1 or i + 1 >= len(dates):
                    continue

                entry_ts = dates[i + 1]
                date_str = str(entry_ts.date())
                if open_until is not None:
                    if date_str <= open_until:
                        continue
                    open_until = None

                # 1. 5-key technical gate (signal_core.entry_checks)
                bars_slice = [
                    {"open": opens[j], "high": highs[j], "low": lows[j], "close": closes[j]}
                    for j in (i - 1, i)
                ]
                _, entry_confirmed = entry_checks(
                    closes[: i + 1], volumes[: i + 1], bars_slice, rsis=rsis_full[: i + 1]
                )
                if not entry_confirmed:
                    continue

                # 2. VRP gate: bar t volatility, iv_current / hv_current >= vrp_threshold
                signal_date = pd.Timestamp(dates[i]).date()
                vrp_ratio = BullPutSpreadSignalEngine._vrp_ratio(vol_by_date.get(signal_date))
                if self.entry_mode != "baseline":
                    if vrp_ratio is None:
                        self.skipped_missing_iv += 1
                        continue
                    if vrp_ratio < self.vrp_threshold:
                        continue

                # 3. Position construction (unchanged from base)
                ma150 = sum(closes[i - 149: i + 1]) / 150.0
                close = closes[i]
                short_strike = math.floor(ma150 / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((min(ma150, close) - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width
                expiry_ts = entry_ts + pd.Timedelta(days=self.target_dte)
                expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
                expiry_str = str(expiry_ts.date())
                group_id = f"{code}-{date_str}"

                legs = [
                    {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                    {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                ]
                signals.append({
                    "date": date_str,
                    "action": "open",
                    "underlying": code,
                    "price_mode": "open",
                    "legs": legs,
                    "group_id": group_id,
                })

                # Initial credit estimate at T+1 open using real DoltHub IV
                fill_spot = opens[i + 1]
                fill_iv = hv_list[i + 1] if not math.isnan(hv_list[i + 1]) else 0.3
                T0 = max((expiry_ts - entry_ts).days / 365.0, 0.001)
                credit = self._spread_value(fill_spot, fill_iv, T0, short_strike, long_strike)

                entry_rec = {
                    "code": code,
                    "entry_date": date_str,
                    "signal_close": close,
                    "ma150": ma150,
                    "short_strike": short_strike,
                    "long_strike": long_strike,
                    "expiry": expiry_str,
                    "credit_est": credit,
                    "vrp_ratio": vrp_ratio,
                }
                self.entries.append(entry_rec)
                open_until = expiry_str

                if not self.exit_aware or credit <= 0:
                    continue

                # 4. Walk-forward exit evaluation (fast native loop)
                for j in range(i + 1, len(dates) - 1):
                    day = dates[j]
                    if day >= expiry_ts:
                        break
                    fill_day = dates[j + 1]
                    if fill_day >= expiry_ts:
                        break

                    S = closes[j]
                    iv_j = hv_list[j] if not math.isnan(hv_list[j]) else 0.3
                    Tj = max((expiry_ts - day).days / 365.0, 0.001)
                    cost = self._spread_value(S, iv_j, Tj, short_strike, long_strike)
                    captured = (credit - cost) / credit
                    dte = (expiry_ts - day).days
                    day_str = str(day.date())

                    # Lookup precomputed regime for current bar date
                    regime = self.regime_by_date.get(day_str, "NEGATIVE")

                    reason = None
                    if regime == "NEGATIVE":
                        # Strict / crash-insurance regime: structural short strike breach
                        if S < short_strike:
                            reason = "STRIKE_BREACH"
                        elif captured >= base.PROFIT_TARGET:
                            reason = "PROFIT_TARGET"
                        elif dte < 0 or (0 <= dte <= base.TIME_STOP_DTE and captured >= 0):
                            reason = "TIME_STOP"
                    else:
                        # Peacetime / relaxed regime: mark-to-market premium-multiple stop
                        current_loss = max(0.0, cost - credit)
                        if current_loss > PREMIUM_STOP_LOSS_MULTIPLE * credit:
                            reason = "PREMIUM_MULTIPLE_STOP"
                        elif captured >= base.PROFIT_TARGET:
                            reason = "PROFIT_TARGET"
                        elif dte < 0 or (0 <= dte <= base.TIME_STOP_DTE and captured >= 0):
                            reason = "TIME_STOP"

                    if reason is None:
                        continue

                    fill_str = str(fill_day.date())
                    signals.append({
                        "date": fill_str,
                        "action": "close",
                        "underlying": code,
                        "price_mode": "open",
                        "group_id": group_id,
                        "legs": [
                            {"type": "put", "strike": short_strike, "expiry": expiry_str},
                            {"type": "put", "strike": long_strike, "expiry": expiry_str},
                        ],
                    })
                    self.exits.append({
                        "code": code,
                        "entry_date": date_str,
                        "trigger_date": day_str,
                        "exit_fill_date": fill_str,
                        "reason": reason,
                        "captured_at_trigger": captured,
                        "close_at_trigger": S,
                        "dte_at_trigger": dte,
                        "expiry": expiry_str,
                        "regime_at_trigger": regime,
                    })
                    if self.reentry:
                        open_until = fill_str
                    break

        print(
            f"[{self.entry_mode}] entries={len(self.entries)} exits_emitted={len(self.exits)}; "
            f"candidates skipped for missing/invalid IV/HV={self.skipped_missing_iv}",
            flush=True,
        )
        return signals


# ==============================================================================
# 3. Backtest Execution
# ==============================================================================

def run_one(
    run_name: str,
    regime_by_date: Dict[str, str],
    loader: realiv.CoveredCacheLoader,
    iv_cache: Dict[str, pd.DataFrame],
    codes: List[str],
) -> None:
    """Execute one backtest run under real DoltHub IV pricing."""
    cfg = RUN_CONFIGS[run_name]
    print(f"\n{'=' * 80}\nSTARTING RUN: {run_name} {cfg}\n{'=' * 80}", flush=True)

    # Ensure global IV injection points are active
    realiv.ACTIVE_SOURCE = "iv"
    options_portfolio.historical_volatility = realiv.patched_historical_volatility
    base.historical_volatility = realiv.patched_historical_volatility

    run_dir = OUT_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    art_dir = run_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)

    engine = RegimeAwareBPSEngine(
        exit_aware=cfg["exit_aware"],
        reentry=cfg["reentry"],
        entry_mode=cfg["entry_mode"],
        vrp_threshold=VRP_THRESHOLD,
        iv_hv_cache=iv_cache,
        regime_by_date=regime_by_date,
    )

    config = {
        "codes": codes,
        "start_date": base.START,
        "end_date": base.END,
        "initial_cash": base.INITIAL_CASH,
        "commission": 0.0,
        "options_config": dict(base.OPTIONS_CONFIG),
    }

    # Execute simulation
    metrics = base.run_options_backtest(config, loader, engine, run_dir)

    # Write custom artifacts
    pd.DataFrame(engine.entries).to_csv(art_dir / "entries.csv", index=False)
    exits_df = pd.DataFrame(
        engine.exits,
        columns=[
            "code",
            "entry_date",
            "trigger_date",
            "exit_fill_date",
            "reason",
            "captured_at_trigger",
            "close_at_trigger",
            "dte_at_trigger",
            "expiry",
            "regime_at_trigger",
        ],
    )
    exits_df.to_csv(art_dir / "exits.csv", index=False)

    # Record entries_iv.csv for vol audit
    data_map = loader.fetch(codes, base.START, base.END)
    recs = []
    for e in engine.entries:
        code, d = e["code"], pd.Timestamp(e["entry_date"])
        df = data_map[code]
        hv = float(realiv._ORIG_HV(df["close"]).at[d]) if d in df.index else float("nan")
        iv = (
            float(realiv.IV_SERIES[code].at[d])
            if code in realiv.IV_SERIES and d in realiv.IV_SERIES[code].index
            else float("nan")
        )
        src = (
            realiv.IV_SOURCE[code].at[d]
            if code in realiv.IV_SOURCE and d in realiv.IV_SOURCE[code].index
            else "n/a"
        )
        recs.append({
            "code": code,
            "entry_date": e["entry_date"],
            "vol_used": iv,
            "hv30": hv,
            "iv_real": iv,
            "iv_source": src,
            "credit_est": e["credit_est"],
            "vrp_ratio": e.get("vrp_ratio"),
        })
    pd.DataFrame(recs).to_csv(art_dir / "entries_iv.csv", index=False)

    with open(run_dir / "metrics_full.json", "w") as f:
        json.dump(metrics, f, indent=2)

    print(
        f"COMPLETED {run_name}: entries={len(engine.entries)} exits_emitted={len(engine.exits)} "
        f"total_return={metrics.get('total_return')} sharpe={metrics.get('sharpe')}",
        flush=True,
    )


# ==============================================================================
# 4. Reporting & Sanity Assertions
# ==============================================================================

def load_run(out_dir: Path, run_name: str) -> Optional[Dict[str, Any]]:
    """Load run artifacts and compute window-level metrics via base.load_mode."""
    saved = base.OUT_DIR
    base.OUT_DIR = out_dir
    try:
        s = base.load_mode(run_name)
    finally:
        base.OUT_DIR = saved
    if s is None:
        return None
    p = out_dir / run_name / "artifacts" / "entries_iv.csv"
    s["entries_iv"] = pd.read_csv(p) if p.exists() else None
    return s


def build_exit_breakdown(
    s_exit: Dict[str, Any],
    regime_by_date: Dict[str, str],
    calendar_dates: List[pd.Timestamp],
    out_path: Path,
) -> pd.DataFrame:
    """
    Produce exit_breakdown.csv for vrp_regime_exit:
    1. Exit counts for STRIKE_BREACH vs PREMIUM_MULTIPLE_STOP vs PROFIT_TARGET vs TIME_STOP,
       split across IS and OOS windows.
    2. Distribution of trigger-day market regime (NEGATIVE vs POSITIVE).
    3. Baseline calendar trading-day market regime frequencies across the pricing window.
    """
    exits = s_exit["exits"].copy()
    exits["entry_date"] = pd.to_datetime(exits["entry_date"])
    exits["window"] = np.where(exits["entry_date"] <= base.IS_END, "IS", "OOS")

    rows = []
    # 1. Exit reasons
    reasons = ["STRIKE_BREACH", "PREMIUM_MULTIPLE_STOP", "PROFIT_TARGET", "TIME_STOP"]
    tot_exits = len(exits)
    for r in reasons:
        sub = exits[exits["reason"] == r]
        is_cnt = int((sub["window"] == "IS").sum())
        oos_cnt = int((sub["window"] == "OOS").sum())
        tot_cnt = len(sub)
        pct = tot_cnt / tot_exits if tot_exits else 0.0
        rows.append({
            "category": "exit_reason",
            "label": r,
            "IS_count": is_cnt,
            "OOS_count": oos_cnt,
            "total_count": tot_cnt,
            "pct": round(pct, 4),
        })

    # 2. Trigger-day regime distribution
    for reg in ["NEGATIVE", "POSITIVE"]:
        sub = exits[exits["regime_at_trigger"] == reg]
        is_cnt = int((sub["window"] == "IS").sum())
        oos_cnt = int((sub["window"] == "OOS").sum())
        tot_cnt = len(sub)
        pct = tot_cnt / tot_exits if tot_exits else 0.0
        rows.append({
            "category": "trigger_day_regime",
            "label": reg,
            "IS_count": is_cnt,
            "OOS_count": oos_cnt,
            "total_count": tot_cnt,
            "pct": round(pct, 4),
        })

    # 3. Overall calendar trading-day regime frequencies
    a, b = realiv.PRICING_WINDOW
    win_dates = [d for d in calendar_dates if a <= str(d.date()) <= b]
    cal_regimes = [regime_by_date.get(str(d.date()), "NEGATIVE") for d in win_dates]
    tot_cal = len(win_dates)
    for reg in ["NEGATIVE", "POSITIVE"]:
        cnt = cal_regimes.count(reg)
        pct = cnt / tot_cal if tot_cal else 0.0
        rows.append({
            "category": "calendar_day_regime",
            "label": reg,
            "IS_count": "-",
            "OOS_count": "-",
            "total_count": cnt,
            "pct": round(pct, 4),
        })

    df_out = pd.DataFrame(rows)
    df_out.to_csv(out_path, index=False)
    # Also mirror into artifacts dir
    art_path = OUT_DIR / "vrp_regime_exit" / "artifacts" / "exit_breakdown.csv"
    if art_path.parent.exists():
        df_out.to_csv(art_path, index=False)
    return df_out


def run_sanity_assertions(
    runs: Dict[str, Dict[str, Any]],
    regime_by_date: Dict[str, str],
) -> None:
    """
    Mandatory sanity assertions:
    1. Confirm vrp_hold entry count differs from iv_hold entry count (proves VRP gate filtered).
    2. Confirm vrp_regime_exit never shows a STRIKE_BREACH exit on a POSITIVE regime date.
    """
    print("\n" + "=" * 80 + "\nRUNNING SANITY ASSERTIONS\n" + "=" * 80)

    # Check 1: VRP Entry Count vs Baseline
    n_vrp_entries = len(runs["vrp_hold"]["entries"])
    n_iv_entries = len(runs["iv_hold"]["entries"])
    print(f"Sanity Check 1: vrp_hold entries ({n_vrp_entries}) vs iv_hold entries ({n_iv_entries})")
    if n_vrp_entries == n_iv_entries:
        raise RuntimeError(
            f"FATAL: vrp_hold entry count ({n_vrp_entries}) equals iv_hold entry count ({n_iv_entries})! "
            f"The VRP entry gate did not change entry selection."
        )
    print(f"  Passed: VRP gate reduced entries from {n_iv_entries} to {n_vrp_entries} (-{n_iv_entries - n_vrp_entries} trades).")

    # Check 2: No STRIKE_BREACH in POSITIVE regime
    exits_df = runs["vrp_regime_exit"]["exits"]
    breaches = exits_df[exits_df["reason"] == "STRIKE_BREACH"]
    positive_breaches = []
    for _, r in breaches.iterrows():
        trig_d = str(pd.Timestamp(r["trigger_date"]).date())
        reg = regime_by_date.get(trig_d, "NEGATIVE")
        if reg == "POSITIVE":
            positive_breaches.append(r.to_dict())

    print(f"Sanity Check 2: Verifying 0 STRIKE_BREACH exits occurred during POSITIVE regimes...")
    if positive_breaches:
        raise RuntimeError(
            f"FATAL BUG: Found {len(positive_breaches)} STRIKE_BREACH exits on POSITIVE regime dates! "
            f"Sample: {positive_breaches[0]}"
        )
    print(f"  Passed: All {len(breaches)} STRIKE_BREACH exits occurred exclusively during NEGATIVE regimes.")


def generate_summary_table(
    all_runs: Dict[str, Dict[str, Any]],
    out_csv: Path,
) -> pd.DataFrame:
    """Generate and write combined summary.csv matching exact schema of earlier runs."""
    flat = []
    for name, s in all_runs.items():
        rr = {
            "run": name,
            "rejected_margin": s["rejected_margin"],
            "peak_reserve_usd": s["peak_reserve_usd"],
            **{f"credit_{k}": v for k, v in s["credit_check"].items()},
        }
        for w in ("IS", "OOS"):
            for k, v in s[w].items():
                rr[f"{w}_{k}"] = v
        for k in ("total_return", "max_drawdown", "sharpe", "win_rate", "profit_loss_ratio"):
            rr[f"full_{k}"] = s["full"].get(k)
        flat.append(rr)

    summary_df = pd.DataFrame(flat)
    summary_df.to_csv(out_csv, index=False)
    return summary_df


def print_comparison_report(
    all_runs: Dict[str, Dict[str, Any]],
    breakdown_df: pd.DataFrame,
) -> None:
    """Print clean comparison report to stdout with statistical reliability flags."""
    print("\n" + "=" * 130)
    print("BACKTEST COMPARISON: 4 RUNS (REAL DOLTHUB IV PRICING, 563-TICKER COVERED UNIVERSE)")
    print("=" * 130)

    rows = []
    for name, s in all_runs.items():
        i, o = s["IS"], s["OOS"]
        f = s["full"]
        full_n = len(s["grouped"])
        rows.append({
            "Run": name,
            "Total N": full_n,
            "IS N": i["n"],
            "OOS N": o["n"],
            "IS Win%": base.fmt_pct(i["win_rate"]),
            "OOS Win%": base.fmt_pct(o["win_rate"]),
            "Full Win%": base.fmt_pct(f.get("win_rate")),
            "IS $/tr": base.fmt_num(i["per_trade"]),
            "OOS $/tr": base.fmt_num(o["per_trade"]),
            "Full Return": base.fmt_pct(f.get("total_return")),
            "Full MaxDD": base.fmt_pct(f.get("max_drawdown")),
            "Full Sharpe": base.fmt_num(f.get("sharpe"), ".3f"),
            "Low N (<100)": "YES" if (i["n"] < 100 or o["n"] < 100 or full_n < 100) else "NO",
        })

    comp_df = pd.DataFrame(rows)
    print(comp_df.to_string(index=False))

    print("\n" + "=" * 130)
    print("EXIT BREAKDOWN: vrp_regime_exit")
    print("=" * 130)
    print(breakdown_df.to_string(index=False))


# ==============================================================================
# 5. Main Entrypoint
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Exit-Aware Regime Backtest")
    parser.add_argument("--run", choices=["vrp_hold", "vrp_regime_exit"], help="Run specific backtest")
    parser.add_argument("--report", action="store_true", help="Generate report from existing runs")
    parser.add_argument("--all", action="store_true", help="Run both new backtests and report")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load shared inputs
    print("Loading DoltHub IV cache and covered universe...", flush=True)
    iv_cache = realiv.load_iv_cache()
    covered_codes = realiv.covered_codes()
    calendars = realiv.load_ohlcv_index()

    all_dates = set()
    for code in covered_codes:
        cal = calendars.get(code)
        if cal is not None:
            all_dates.update(cal)
    calendar_dates = sorted(list(all_dates))

    regime_by_date = precompute_gex_regimes(calendar_dates)
    loader = realiv.CoveredCacheLoader(iv_cache)

    if args.all or args.run == "vrp_hold" or (not args.run and not args.report):
        run_one("vrp_hold", regime_by_date, loader, iv_cache, covered_codes)

    if args.all or args.run == "vrp_regime_exit" or (not args.run and not args.report):
        run_one("vrp_regime_exit", regime_by_date, loader, iv_cache, covered_codes)

    # Reporting and synthesis
    if args.all or args.report or not (args.run):
        print("\nLoading runs for combined synthesis...", flush=True)
        runs: Dict[str, Dict[str, Any]] = {}
        # Load verified baselines
        for b in ["iv_hold", "iv_exit"]:
            loaded = load_run(REALIV_DIR, b)
            if loaded is None:
                raise RuntimeError(f"Baseline run {b} not found in {REALIV_DIR}")
            runs[b] = loaded

        # Load new runs
        for r in ["vrp_hold", "vrp_regime_exit"]:
            loaded = load_run(OUT_DIR, r)
            if loaded is None:
                raise RuntimeError(f"New run {r} not found in {OUT_DIR}")
            runs[r] = loaded

        # Assertions
        run_sanity_assertions(runs, regime_by_date)

        # Summary and breakdown
        summary_csv = OUT_DIR / "summary.csv"
        generate_summary_table(runs, summary_csv)
        print(f"Wrote summary CSV: {summary_csv}")

        breakdown_csv = OUT_DIR / "exit_breakdown.csv"
        breakdown_df = build_exit_breakdown(
            runs["vrp_regime_exit"], regime_by_date, calendar_dates, breakdown_csv
        )
        print(f"Wrote exit breakdown CSV: {breakdown_csv}")

        # Print report
        print_comparison_report(runs, breakdown_df)


if __name__ == "__main__":
    main()
