#!/usr/bin/env python3
"""Slippage/bid-ask stress test for the frozen 05d60b2 baseline. Research
only -- isolated, does not touch signal_core.py or any live prompt.

Every backtest run tonight (loosened_entry_full_pipeline.py, bounce_exit_
test.py, the OOS split) prices fills at the theoretical Black-Scholes MID
-- bs_price() has no bid/ask spread concept anywhere in options_portfolio.py.
Real fills happen at the bid (selling) or ask (buying), never at mid. This
script quantifies how much of the $116.21/mo edge that assumption is
worth, by sweeping a per-leg slippage haircut across a plausible range and
re-running the exact same entries/allocator.

Model: a 2-leg vertical opened and closed pays slippage on 4 leg-crossings
total -- 2 at entry (sell short leg at bid, buy long leg at ask: credit
reduced by 2 x slippage_per_leg) and 2 at exit (buy back short at ask,
sell long at bid: cost-to-close increased by 2 x slippage_per_leg).

No live NBBO data was pulled for this -- get_option_data snapshots aren't
available outside a live IBKR session, so this is a SWEEP across assumed
per-leg slippage, not a single "the real number is $X" claim. Treat the
sweep's shape (how fast the edge dies) as the finding; verify the true
per-leg spread on next week's actual candidate strikes via get_option_data
before trusting any single point on this curve.
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
BASELINE_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
SLIPPAGE_GRID = [0.00, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]


def simulate_with_slippage(entry, price_df, k_long, exit_variant, slippage_per_leg):
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
    mid_credit = bs_price(entry_S, k_short, T0, RISK_FREE, iv_s0, "put") - bs_price(entry_S, k_long, T0, RISK_FREE, iv_l0, "put")
    # Entry: sell short leg at bid (mid - slippage), buy long leg at ask (mid + slippage).
    credit = mid_credit - 2.0 * slippage_per_leg
    if credit <= 0 or pd.isna(credit):
        return None

    for d in dates:
        S = price_df.at[d, "close"]; ma150 = price_df.at[d, "ma150"]; hv = price_df.at[d, "hv30"]
        if pd.isna(S) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        mid_cost = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        # Exit: buy back short leg at ask (mid + slippage), sell long leg at bid (mid - slippage).
        cost_to_close = mid_cost + 2.0 * slippage_per_leg
        captured = (credit - cost_to_close) / credit
        if exit_variant["tp"] and captured >= 0.80:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
        ma150_breach = exit_variant["ma150_stop"] and not pd.isna(ma150) and S < ma150
        strike_breach = exit_variant["strike_stop"] and S < k_short
        if ma150_breach or strike_breach:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    # Expiry close-out still crosses the spread once more if ITM and must be
    # closed rather than let assign/exercise -- approximated here as the same
    # 2x slippage on the terminal mark, conservative (assign/exercise at actual
    # expiry has its own separate cost structure not modeled either way).
    mid_payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    payoff = mid_payoff - 2.0 * slippage_per_leg if (k_short - S_T) > 0 or (k_long - S_T) > 0 else mid_payoff
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit}


def build_merged(entries, slippage_per_leg):
    rows_by_key = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_with_slippage(entry, pdf, k_long, BASELINE_EXIT, slippage_per_leg)
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


def simulate_stop_only_slippage(entry, price_df, k_long, slippage_per_leg):
    """Realistic/calibrated variant (per advisor review of the first sweep):
    the first pass above charges slippage on every exit, including the ~70%
    that hit the 80%-of-credit take-profit. Live, TP is a resting GTC LIMIT
    order -- it doesn't cross the spread, it waits for the market to come to
    it; its cost is fill delay, not price concession. Entry is likewise a
    posted limit (mid, floored at width/3.5) -- its real cost is fill RATE,
    not price concession either, and isn't modeled here (a separate, harder
    problem). Only a strike-breach stop is realistically an urgent market-
    style close that actually crosses the spread. This isolates that.
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

    for d in dates:
        S = price_df.at[d, "close"]; hv = price_df.at[d, "hv30"]
        if pd.isna(S) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        mid_cost = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        if (credit - mid_cost) / credit >= 0.80:
            return {"exit_date": d, "pnl": (credit - mid_cost) * 100.0, "credit": credit}
        if S < k_short:
            cost_to_close = mid_cost + 2.0 * slippage_per_leg
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit}


def build_merged_stop_only(entries, slippage_per_leg):
    rows_by_key = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_stop_only_slippage(entry, pdf, k_long, slippage_per_leg)
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
    print("=== PESSIMISTIC BOUND: every exit (incl. TP) crosses the spread ===")
    print("Model: 4 total leg-crossings per round trip (2 at entry, 2 at exit); "
          "each column is the assumed distance from mid to bid/ask, per leg, per side.\n")

    mid_monthly = None
    for slip in SLIPPAGE_GRID:
        merged = build_merged(entries, slip)
        result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                           commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
        wr = result.wins / result.allocated_count if result.allocated_count else 0
        monthly = result.total_pnl / MONTHS
        if slip == 0.00:
            mid_monthly = monthly
        degradation = ((monthly - mid_monthly) / mid_monthly * 100) if mid_monthly else float("nan")
        flag = "  <-- BASELINE (no slippage, theoretical mid)" if slip == 0.00 else ""
        print(f"slippage_per_leg=${slip:.2f}: allocated={result.allocated_count:4d} "
              f"win_rate={wr:.1%} monthly=${monthly:,.2f}  ({degradation:+.1f}% vs mid){flag}")

    print("\n=== CALIBRATED ESTIMATE: TP fills at its resting-limit mid, only the "
          "urgent strike-breach stop realistically crosses the spread ===\n")
    for slip in [0.00, 0.05, 0.10, 0.15, 0.20]:
        merged = build_merged_stop_only(entries, slip)
        result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                           commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
        wr = result.wins / result.allocated_count if result.allocated_count else 0
        monthly = result.total_pnl / MONTHS
        print(f"stop-only slippage_per_leg=${slip:.2f}: allocated={result.allocated_count:4d} "
              f"win_rate={wr:.1%} monthly=${monthly:,.2f}")

    print("\nCaveat: no live NBBO pulled for either table -- verify the real per-leg "
          "spread on next week's actual candidate strikes via get_option_data before "
          "trusting any single point on either curve as truth rather than a sensitivity check. "
          "Entry-side fill-RATE risk (posted limits going unfilled) is not modeled in either table.")


if __name__ == "__main__":
    main()
