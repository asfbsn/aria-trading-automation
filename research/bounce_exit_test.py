#!/usr/bin/env python3
"""Bounce/Pullback Exit hypothesis test -- research only, isolated from
scripts/signal_core.py and every live prompt. Does NOT touch any live file.

User's observation (2026-09-05, re: Intel/Cadence stopouts): if a breached
short put spread is going to recover to profitability at all, exiting the
instant the strike is breached means exiting at peak IV/panic -- the worst
possible moment to buy back a short put. What if the hard stop instead waits
for the first sign of mean-reversion?

Mechanical rule under test (no AI, no forecast -- pure price-action state
machine, comparable against the same $3k Global Heap Allocator backtest that
produced the frozen Monday baseline):

  1. TRIGGER: short_strike breached (close < short_strike) -> do NOT exit
     that day. Enter PENDING_EXIT state.
  2. BOUNCE EXIT: starting the day AFTER the breach, close on the first day
     with a green candle (close > open). Rationale -- let IV crush happen
     before buying back, instead of paying the panic price.
  3. CIRCUIT BREAKER (hard stop, overrides bounce-waiting): any day in
     PENDING_EXIT whose close is more than 2.5% strictly below the short
     strike closes immediately, regardless of candle color. Caps the
     downside of waiting for a bounce that never comes.
  4. Pre-breach behavior (80%-profit TP) is unchanged from the frozen
     Monday exit rule -- this experiment only changes what happens AFTER a
     breach, nothing before it.

Baseline for comparison: scripts/backtest/loosened_entry_full_pipeline.py's
B_loose_candle entry x ExitA_strike_only (strike-only immediate hard stop),
$3k Global Heap Allocator, 750-ticker mega-cap universe, same 36-month
window -- the exact backtest that produced the $116.21/mo figure quoted in
signal_core.py's exit_checks() docstring and the 05d60b2 commit message.

Single-run caveat (stated up front, not after seeing the result): this is
one 36-month backtest on one universe, not a held-out validation set. A
"win" here is a reason to build the bifurcated/OOS version, not a reason to
touch signal_core.py directly.
"""
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/assaf/Projects/aria-trading/scripts/backtest")
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import (  # noqa: E402
    ENTRY_VARIANTS, WIDTHS, IV_SKEW, IV_CURVATURE, RISK_FREE,
    COMMISSION_PER_SPREAD, generate_entries, data_map, simulate_one,
)
from options_portfolio import bs_price, iv_smile_adjustment  # noqa: E402
from heap_allocator_sim import simulate_heap_allocation  # noqa: E402
from run_heap_allocator_baseline_check import load_sector_map  # noqa: E402

CIRCUIT_BREAKER_PCT = 0.025
MONTHS = 36.0


def simulate_bounce_exit(entry, price_df, k_long, circuit_breaker_pct=CIRCUIT_BREAKER_PCT):
    """Same TP/pricing mechanics as loosened_entry_full_pipeline.simulate_one,
    but the strike-breach hard stop is replaced by the pending-exit state
    machine described in the module docstring. circuit_breaker_pct is a
    parameter (not just the module default) so bounce_exit_oos_test.py can
    sweep it on an in-sample window without duplicating this function.
    """
    entry_date = entry["entry_date"]; expiry = entry["expiry"]
    k_short = entry["short_strike"]
    dates = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry)]
    entry_idx_arr = price_df.index[price_df.index <= entry_date]
    if len(entry_idx_arr) == 0 or len(dates) == 0:
        return None
    entry_hv = price_df.at[entry_idx_arr[-1], "hv30"]
    entry_S = price_df.at[entry_idx_arr[-1], "close"]
    if pd.isna(entry_hv) or entry_hv <= 0:
        return None
    T0 = max((expiry - entry_date).days / 365.0, 0.001)
    iv_s0 = iv_smile_adjustment(entry_S, k_short, entry_hv, IV_SKEW, IV_CURVATURE)
    iv_l0 = iv_smile_adjustment(entry_S, k_long, entry_hv, IV_SKEW, IV_CURVATURE)
    credit = bs_price(entry_S, k_short, T0, RISK_FREE, iv_s0, "put") - bs_price(entry_S, k_long, T0, RISK_FREE, iv_l0, "put")
    if credit <= 0 or pd.isna(credit):
        return None

    pending = False
    for d in dates:
        S = price_df.at[d, "close"]; O = price_df.at[d, "open"]; hv = price_df.at[d, "hv30"]
        if pd.isna(S) or pd.isna(O) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        cost_to_close = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        captured = (credit - cost_to_close) / credit

        if not pending:
            if captured >= 0.80:
                return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit, "reason": "TP"}
            if S < k_short:
                pending = True  # breach day -- do NOT exit, enter Pending Exit
                continue
        else:
            if S < k_short * (1.0 - circuit_breaker_pct):
                return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit, "reason": "CIRCUIT_BREAKER"}
            if S > O:
                return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit, "reason": "BOUNCE"}
            # red/flat day, not yet -2.5% -- keep waiting

    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit, "reason": "EXPIRY"}


def build_merged(entries, simulate_fn):
    rows_by_key = {}
    reason_counts = Counter()
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_fn(entry, pdf, k_long)
            if r is None:
                continue
            if r.get("reason") is not None and w == WIDTHS[0]:
                reason_counts[r["reason"]] += 1  # count once per entry, not per width
            key = (entry["code"], entry["entry_date"])
            suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
            rows_by_key.setdefault(key, {})[w] = {
                "max_loss": (w - r["credit"]) * 100.0, "credit": r["credit"],
                "pnl": r["pnl"], "exit_date": r["exit_date"],
            }
    merged_rows = []
    for (code, entry_date), widths in rows_by_key.items():
        row = {"code": code, "entry_date": entry_date}
        for w in WIDTHS:
            suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
            d = widths.get(w)
            row[f"max_loss_{suffix}"] = d["max_loss"] if d else None
            row[f"credit_{suffix}"] = d["credit"] if d else None
            row[f"spread_pnl_{suffix}"] = d["pnl"] if d else None
            row[f"exit_date_{suffix}"] = d["exit_date"] if d else None
        merged_rows.append(row)
    return pd.DataFrame(merged_rows), reason_counts


def run_and_report(label, merged, sector_map):
    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                       commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    monthly = result.total_pnl / MONTHS
    print(f"{label}: R/R_merged={len(merged)} allocated={result.allocated_count} "
          f"blocked={len(result.blocked)} win_rate={wr:.1%} "
          f"total_pnl=${result.total_pnl:,.2f} monthly=${monthly:,.2f}")
    return result, monthly


BASELINE_EXIT_VARIANT = dict(tp=True, ma150_stop=False, strike_stop=True)


def baseline_sim(entry, pdf, k_long):
    return simulate_one(entry, pdf, k_long, BASELINE_EXIT_VARIANT)


def main():
    sector_map = load_sector_map()
    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    print(f"B_loose_candle entries: {len(entries)} ({len(entries)/MONTHS:.1f}/mo)\n")

    baseline_merged, _ = build_merged(entries, baseline_sim)
    baseline_result, baseline_monthly = run_and_report("BASELINE (ExitA strike-only, immediate stop)", baseline_merged, sector_map)

    bounce_merged, reason_counts = build_merged(entries, simulate_bounce_exit)
    bounce_result, bounce_monthly = run_and_report("BOUNCE (pending-exit, wait for green close / -2.5% circuit breaker)", bounce_merged, sector_map)

    print(f"\nExit-reason breakdown (bounce variant, one count per entry at widest width): {dict(reason_counts)}")

    delta = bounce_monthly - baseline_monthly
    pct = (delta / abs(baseline_monthly)) * 100 if baseline_monthly else float("nan")
    print(f"\nBASELINE monthly: ${baseline_monthly:,.2f}")
    print(f"BOUNCE   monthly: ${bounce_monthly:,.2f}")
    print(f"Delta: ${delta:,.2f} ({pct:+.1f}%)")
    print("\nCaveat: single 36-month run, one universe, no out-of-sample split -- "
          "a win here justifies a bifurcated/OOS follow-up, not a live change.")
    if bounce_monthly > baseline_monthly:
        print("VERDICT: Bounce Exit outperforms the blind immediate-stop baseline on this run.")
    else:
        print("VERDICT: Bounce Exit does NOT outperform the blind immediate-stop baseline on this run.")


if __name__ == "__main__":
    main()
