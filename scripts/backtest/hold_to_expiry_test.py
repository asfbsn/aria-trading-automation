"""User post-mortem question (INTC, CDNS -- manually panic-exited ~1wk before
expiry at a $300+ loss, then the stock mean-reverted and would have expired
profitable/breakeven): is the strike-breach hard stop tricking us into
realizing final-week gamma-driven losses that Mega-Caps would otherwise
mean-revert out of? Test: on the LOCKED live config's entry (750-ticker
mega-cap-only, loosened "any green close" candle), remove the strike-breach
hard stop entirely -- only exits are 80% TP or hold-to-expiration (absorbing
max loss if ITM). Compare vs the locked exit (strike-breach hard stop) under
the same $3k Global Heap Allocator. Research only -- no live files touched.
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import (  # noqa: E402
    ENTRY_VARIANTS, generate_entries, simulate_one, data_map, WIDTHS,
    COMMISSION_PER_SPREAD,
)
from run_heap_allocator_baseline_check import load_sector_map  # noqa: E402
from heap_allocator_sim import simulate_heap_allocation  # noqa: E402
import pandas as pd  # noqa: E402

EXIT_VARIANTS = {
    "Locked_live: TP80 + strike-breach hard stop": dict(tp=True, ma150_stop=False, strike_stop=True),
    "NoStop: TP80 only, hold-to-expiry (absorb ITM loss)": dict(tp=True, ma150_stop=False, strike_stop=False),
}


def simulate_one_tagged(entry, price_df, k_long, exit_variant):
    """Same math as loosened_entry_full_pipeline.simulate_one, but also
    tags HOW the trade closed (TP / STOP / EXPIRY_PROFIT / EXPIRY_LOSS) so
    we can answer the actual INTC/CDNS question: how often does removing
    the stop let a would-have-been-cut trade recover by expiry, vs how
    often does it just let a real breakdown run to a bigger loss?"""
    from options_portfolio import bs_price, iv_smile_adjustment
    from loosened_entry_full_pipeline import RISK_FREE, IV_SKEW, IV_CURVATURE

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
        if exit_variant["tp"] and captured >= 0.80:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit, "reason": "TP"}
        if exit_variant["strike_stop"] and S < k_short:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit, "reason": "STOP"}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    reason = "EXPIRY_PROFIT" if payoff >= 0 else "EXPIRY_LOSS"
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit, "reason": reason}


def run_combo_tagged(entry_label, entries, exit_label, exit_variant, sector_map, months=36.0):
    rows_by_key = {}
    reason_counts = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_one_tagged(entry, pdf, k_long, exit_variant)
            if r is None:
                continue
            if w == 10.0:
                reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
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
    merged = pd.DataFrame(merged_rows)
    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                       commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    print(f"\n=== {entry_label} x {exit_label} ===")
    print(f"width=10 exit-reason breakdown (all R/R-eligible, pre-allocator): {reason_counts}")
    print(f"Allocated: {result.allocated_count}  Blocked: {len(result.blocked)}  Win rate: {wr:.1%}")
    print(f"Total P&L: ${result.total_pnl:,.2f}   Monthly: ${result.total_pnl/months:,.2f}")
    return result, reason_counts


def main():
    sector_map = load_sector_map()
    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    print(f"Entry: B_loose_candle (any green close), {len(entries)} raw signals ({len(entries)/36.0:.1f}/mo)")
    for exit_label, exit_variant in EXIT_VARIANTS.items():
        run_combo_tagged("B_loose_candle", entries, exit_label, exit_variant, sector_map)


if __name__ == "__main__":
    main()
