#!/usr/bin/env python3
"""$6k-capital baseline + two screened hypotheses (IV Rank entry filter,
21-DTE management exit) from the 2026-09-06 revenue-research report.
Research only -- isolated, does not touch signal_core.py, any live prompt,
or data/universe.csv. All three variants run the locked B_loose_candle
entry + strike-only-stop/80%-TP exit logic (05d60b2) as their common base;
only the allocator's net_liq and the two named additions differ.

Variant A (IV Rank > 50): proxy is 252-trading-day trailing HV percentile,
computed as-of the same bar the entry signal itself uses (the bar before
entry_date, matching generate_entries' own signal timing). Entries with
fewer than 252 valid trailing hv30 readings are dropped (can't compute a
clean rank), not defaulted to pass -- consistent with how a real IV-rank
screen would treat a name without a year of history.

Variant B (21-DTE exit): closes at that day's theoretical mid close-out
value the first day DTE<=21, if TP/strike-stop haven't already fired.
Caveat stated up front, not after seeing results: the classic "45 DTE in,
21 DTE out" rule assumes ~24 days of runway before the cutoff. This system
enters at ~30 DTE, so a literal DTE<=21 cutoff leaves only ~9 days of
runway -- a much more aggressive rule than the source heuristic. Applying
the threshold literally (as asked) rather than proportionally, but the
result should be read as "DTE<=21 on a 30-DTE entry," not a clean test of
the external 45/21 rule on its own terms.

Pre-registered framing (per the prior report): a variant only "moves the
needle enough to warrant OOS testing" if it beats the $6k baseline by a
margin comparable to what Bounce Exit showed in-sample (+6.1%) before
that turned out to be noise on OOS. A smaller edge than that, on a single
36-month run with no split, isn't worth the OOS-testing cost by the same
standard already applied to Bounce Exit tonight.
"""
import sys
from pathlib import Path

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

MONTHS = 36.0
NET_LIQ = 6000.0
BASELINE_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
IV_RANK_WINDOW = 252
IV_RANK_MIN_PCTILE = 50.0
DTE_CUTOFF = 21


def compute_hv_percentile(pdf: pd.DataFrame, entry_date, window: int = IV_RANK_WINDOW):
    as_of = pdf.index[pdf.index < entry_date]
    if len(as_of) == 0:
        return None
    as_of_date = as_of[-1]
    hv_hist = pdf["hv30"].loc[:as_of_date].dropna()
    if len(hv_hist) < window:
        return None
    hv_window = hv_hist.tail(window)
    hv_now = hv_window.iloc[-1]
    return float((hv_window <= hv_now).mean() * 100.0)


def filter_by_iv_rank(entries, min_pctile: float = IV_RANK_MIN_PCTILE):
    kept, dropped_no_history, dropped_low_iv = [], 0, 0
    for e in entries:
        pdf = data_map.get(e["code"])
        if pdf is None:
            continue
        pct = compute_hv_percentile(pdf, e["entry_date"])
        if pct is None:
            dropped_no_history += 1
            continue
        if pct > min_pctile:
            kept.append(e)
        else:
            dropped_low_iv += 1
    print(f"  IV-rank filter: {len(entries)} candidates -> {len(kept)} kept "
          f"({dropped_no_history} dropped: <{IV_RANK_WINDOW}d history, "
          f"{dropped_low_iv} dropped: HV percentile <= {min_pctile:.0f})")
    return kept


def simulate_dte21(entry, price_df, k_long):
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
        cost_to_close = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        captured = (credit - cost_to_close) / credit
        if captured >= 0.80:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
        if S < k_short:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
        if (expiry - d).days <= DTE_CUTOFF:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit}


def build_merged(entries, simulate_fn):
    rows_by_key = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_fn(entry, pdf, k_long)
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


def run_variant(label, entries, simulate_fn, sector_map):
    merged = build_merged(entries, simulate_fn)
    result = simulate_heap_allocation(merged, net_liq=NET_LIQ, sector_map=sector_map,
                                       commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    monthly = result.total_pnl / MONTHS
    return {
        "label": label, "candidates": len(entries), "rr_merged": len(merged),
        "allocated": result.allocated_count, "blocked": len(result.blocked),
        "win_rate": wr, "monthly_pnl": monthly, "total_pnl": result.total_pnl,
    }


def main():
    sector_map = load_sector_map()
    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    print(f"B_loose_candle raw candidates: {len(entries)} ({len(entries)/MONTHS:.1f}/mo)\n")

    baseline_sim = lambda e, pdf, kl: simulate_one(e, pdf, kl, BASELINE_EXIT)  # noqa: E731

    print("1. New Baseline ($6k, locked logic)...")
    r1 = run_variant("1. New Baseline ($6k)", entries, baseline_sim, sector_map)

    print("2. Variant A (IV Rank > 50 entry filter)...")
    iv_entries = filter_by_iv_rank(entries)
    r2 = run_variant("2. Variant A (IV Rank>50)", iv_entries, baseline_sim, sector_map)

    print("3. Variant B (21-DTE exit)...")
    r3 = run_variant("3. Variant B (21-DTE)", entries, simulate_dte21, sector_map)

    print("\n" + "=" * 100)
    header = f"{'Variant':<28}{'Candidates':>11}{'Allocated':>10}{'Blocked':>9}{'WinRate':>9}{'Monthly P&L':>14}{'Total P&L':>13}"
    print(header)
    print("-" * len(header))
    for r in (r1, r2, r3):
        print(f"{r['label']:<28}{r['candidates']:>11}{r['allocated']:>10}{r['blocked']:>9}"
              f"{r['win_rate']:>8.1%} {r['monthly_pnl']:>13,.2f} {r['total_pnl']:>12,.2f}")
    print("=" * 100)

    base_monthly = r1["monthly_pnl"]
    for r in (r2, r3):
        delta = r["monthly_pnl"] - base_monthly
        pct = (delta / abs(base_monthly)) * 100 if base_monthly else float("nan")
        bounce_exit_is_edge = 0.061  # +6.1% in-sample, later failed OOS -- the bar for "worth an OOS test"
        note = ("clears the Bounce-Exit in-sample bar -- would warrant OOS testing"
                 if pct / 100 > bounce_exit_is_edge
                 else "below the Bounce-Exit in-sample bar (+6.1%) that itself failed OOS -- not worth an OOS run on this evidence alone")
        print(f"{r['label']}: {delta:+,.2f}/mo ({pct:+.1f}% vs new baseline) -- {note}")


if __name__ == "__main__":
    main()
