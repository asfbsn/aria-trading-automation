#!/usr/bin/env python3
"""TP=50% vs TP=80% take-profit threshold, under the same stop-only
slippage model as slippage_stress_test.py. Research only -- isolated,
does not touch signal_core.py or any live prompt.

Motivation (per advisor review of a "how do we increase revenue" request):
tastytrade's published research across 4,000+ SPY put credit spreads found
managing winners at 50% of max profit beats holding to a higher target --
faster capital recycling, less exposure to the final-week gamma zone where
theta decay flattens and price risk dominates. That result was on a
70-85%-win-rate regime; this system runs at 42% win rate with 1.5-2.5 R/R,
a materially different risk profile, so the external finding is a
hypothesis to test locally, not a conclusion to import.

Also directly relevant to tonight's slippage audit: the 80% TP threshold
is why tiny slippage crushed the strategy so fast (calibrated sweep) --
trades that fall short of 80% captured keep running and become exposed to
the stop side, which IS where slippage bites. A lower TP threshold should,
mechanically, convert more trades into IV-favorable TP exits (no spread-
crossing) before they can drift into stop territory.

PRE-REGISTERED CRITERION: TP=50% is worth a real IS/OOS study (like Bounce
Exit got) only if it beats TP=80% on BOTH (a) zero-slippage monthly P&L and
(b) the breakeven slippage point from slippage_stress_test.py's calibrated
model ($0.05-$0.10/leg for TP=80%). Beating one but not the other is a
wash, not a win -- report both, don't cherry-pick.
"""
import sys
from pathlib import Path

import pandas as pd

BASE = Path("/home/assaf/Projects/aria-trading/scripts/backtest")
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import (  # noqa: E402
    ENTRY_VARIANTS, WIDTHS, IV_SKEW, IV_CURVATURE, RISK_FREE,
    COMMISSION_PER_SPREAD, generate_entries, data_map,
)
from options_portfolio import bs_price, iv_smile_adjustment  # noqa: E402
from heap_allocator_sim import simulate_heap_allocation  # noqa: E402
from run_heap_allocator_baseline_check import load_sector_map  # noqa: E402

MONTHS = 36.0
SLIPPAGE_GRID = [0.00, 0.05, 0.10, 0.15]
TP_THRESHOLDS = {"TP=80% (locked live)": 0.80, "TP=50% (tastytrade-style)": 0.50}


def simulate_stop_only_slippage(entry, price_df, k_long, slippage_per_leg, tp_threshold):
    """Identical to slippage_stress_test.py's calibrated model, tp_threshold
    parameterized instead of hardcoded 0.80."""
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

    for d in dates:
        S = price_df.at[d, "close"]; hv = price_df.at[d, "hv30"]
        if pd.isna(S) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        mid_cost = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        if (credit - mid_cost) / credit >= tp_threshold:
            return {"exit_date": d, "pnl": (credit - mid_cost) * 100.0, "credit": credit}
        if S < k_short:
            cost_to_close = mid_cost + 2.0 * slippage_per_leg
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit}


def build_merged(entries, slippage_per_leg, tp_threshold):
    rows_by_key = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_stop_only_slippage(entry, pdf, k_long, slippage_per_leg, tp_threshold)
            if r is None:
                continue
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
    return pd.DataFrame(merged_rows)


def main():
    sector_map = load_sector_map()
    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    print(f"B_loose_candle entries: {len(entries)} ({len(entries)/MONTHS:.1f}/mo)\n")

    for tp_label, tp in TP_THRESHOLDS.items():
        print(f"=== {tp_label} ===")
        for slip in SLIPPAGE_GRID:
            merged = build_merged(entries, slip, tp)
            result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                               commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
            wr = result.wins / result.allocated_count if result.allocated_count else 0
            monthly = result.total_pnl / MONTHS
            print(f"  slippage_per_leg=${slip:.2f}: allocated={result.allocated_count:4d} "
                  f"win_rate={wr:.1%} monthly=${monthly:,.2f}")
        print()

    print("Caveat: single 36-month run, one universe, no IS/OOS split -- this is a "
          "screening pass to decide whether TP=50% deserves the full Bounce-Exit-style "
          "rigor, not a verdict on its own.")


if __name__ == "__main__":
    main()
