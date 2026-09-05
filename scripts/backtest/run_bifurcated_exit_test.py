"""Tests the user's bifurcated-exit hypothesis: mega-cap segment (original
750-ticker universe) gets strike-breach-only hard stop (MA150 advisory);
mid/small-cap segment (S&P 400/600 additions) gets the old strike+MA150
hard-stop logic. Both segments compete for the same $3k heap.

Reuses the cached price/MA150/HV data and entries already built for the
uniform-rule comparison (run_expanded_universe_live_exit_allocator.py) --
no re-fetch needed. Compares three configurations:
  A. Uniform strike-only (current live rule) everywhere       -- already computed: -$18.72/mo
  B. Uniform strike+MA150 (old rule) everywhere                -- already computed: +$71.11/mo
  C. Bifurcated: mega=strike-only, mid/small=strike+MA150      -- new, this script
"""
import csv
import pickle
from pathlib import Path

import pandas as pd

from run_expanded_universe_live_exit_allocator import WIDTH_PATHS, load_entries, simulate_one
from heap_allocator_sim import simulate_heap_allocation
from run_heap_allocator_baseline_check import load_sector_map

BASE = Path(__file__).parent
COMMISSION_PER_SPREAD = 1.30

MEGA_ONLY_VARIANT = dict(tp=True, ma150_stop=False, strike_stop=True)   # current live rule
MIDSMALL_VARIANT = dict(tp=True, ma150_stop=True, strike_stop=True)     # old rule


def load_mega_tickers():
    with open(BASE.parent.parent / "data" / "universe.csv") as f:
        return {row["ticker"].strip() for row in csv.DictReader(f)}


def main():
    with open(BASE / "price_data_cache.pkl", "rb") as f:
        price_data = pickle.load(f)

    all_entries = {w: load_entries(p) for w, p in WIDTH_PATHS.items()}
    mega_tickers = load_mega_tickers()

    sector_map = load_sector_map()
    with open(BASE / "universe_midsmall_validated.csv") as f:
        for row in csv.DictReader(f):
            sector_map[row["ticker"]] = row["sector"]

    months = 36.0

    def run_config(label, variant_for):
        rows_by_key = {}
        for w, df in all_entries.items():
            for _, entry in df.iterrows():
                pdf = price_data.get(entry["code"])
                if pdf is None:
                    continue
                variant = variant_for(entry["code"])
                r = simulate_one(entry, pdf, variant)
                if r is None:
                    continue
                key = (entry["code"], entry["entry_date"])
                rows_by_key.setdefault(key, {})[w] = {
                    "max_loss": (w - entry["credit"]) * 100.0,
                    "credit": entry["credit"],
                    "pnl": r["pnl"],
                    "exit_date": r["exit_date"],
                }
        merged_rows = []
        for (code, entry_date), widths in rows_by_key.items():
            row = {"code": code, "entry_date": entry_date}
            for w in [10.0, 5.0, 2.5, 1.0]:
                suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
                d = widths.get(w)
                row[f"max_loss_{suffix}"] = d["max_loss"] if d else None
                row[f"credit_{suffix}"] = d["credit"] if d else None
                row[f"spread_pnl_{suffix}"] = d["pnl"] if d else None
                row[f"exit_date_{suffix}"] = d["exit_date"] if d else None
            merged_rows.append(row)
        merged = pd.DataFrame(merged_rows)

        result = simulate_heap_allocation(
            merged, net_liq=3000.0, sector_map=sector_map,
            commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True,
        )
        wr = result.wins / result.allocated_count if result.allocated_count else 0
        mega_alloc = sum(1 for a in result.allocated if a["code"] in mega_tickers)
        midsmall_alloc = result.allocated_count - mega_alloc
        print(f"\n=== {label} ===")
        print(f"Allocated: {result.allocated_count} (mega={mega_alloc}, mid/small={midsmall_alloc})  Blocked: {len(result.blocked)}")
        print(f"Win rate: {result.wins}/{result.allocated_count} = {wr:.1%}")
        print(f"Total P&L: ${result.total_pnl:,.2f}   Monthly: ${result.total_pnl/months:,.2f}")
        return result

    run_config("C. BIFURCATED (mega=strike-only, mid/small=strike+MA150)",
               lambda code: MEGA_ONLY_VARIANT if code in mega_tickers else MIDSMALL_VARIANT)

    # Re-verify the two uniform baselines against the SAME merged/allocator code path,
    # for a clean apples-to-apples (earlier numbers used a slightly different script).
    run_config("A. UNIFORM strike-only everywhere (current live rule)",
               lambda code: MEGA_ONLY_VARIANT)
    run_config("B. UNIFORM strike+MA150 everywhere (old rule)",
               lambda code: MIDSMALL_VARIANT)


if __name__ == "__main__":
    main()
