"""Runs the heap allocator against the loosened-gate (structural_2of3)
4-width backtest output, mirroring run_heap_allocator_baseline_check.py.
Reports both ranking directions (richest-first / safest-first), with
commission applied, sector diversification ON (the live spec).
"""
from pathlib import Path

import pandas as pd

from heap_allocator_sim import simulate_heap_allocation, _primary_rr
from run_heap_allocator_baseline_check import load_sector_map, extract_tier_spreads_with_exit

BASE = Path(__file__).parent

TIER_PATHS = {
    10.0: BASE / "run_out_structural2of3" / "width_10" / "artifacts" / "trades.csv",
    5.0: BASE / "run_out_structural2of3" / "width_5" / "artifacts" / "trades.csv",
    2.5: BASE / "run_out_structural2of3" / "width_2_5" / "artifacts" / "trades.csv",
    1.0: BASE / "run_out_structural2of3" / "width_1" / "artifacts" / "trades.csv",
}


def main():
    sector_map = load_sector_map()
    tier_dfs = {w: extract_tier_spreads_with_exit(p) for w, p in TIER_PATHS.items()}
    for w, df in tier_dfs.items():
        print(f"width {w}: {len(df)} closed spreads")

    merged = None
    for w in [10.0, 5.0, 2.5, 1.0]:
        suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
        df = tier_dfs[w].rename(columns={
            "credit": f"credit_{suffix}",
            "max_loss_per_contract": f"max_loss_{suffix}",
            "spread_pnl": f"spread_pnl_{suffix}",
            "exit_date": f"exit_date_{suffix}",
        }).drop(columns=["width"])
        merged = df if merged is None else pd.merge(merged, df, on=["code", "entry_date"], how="outer")

    print(f"\nTotal merged signals (structural_2of3): {len(merged)}")

    rr_check = merged.copy()
    rr_check["rr_rank"] = rr_check.apply(_primary_rr, axis=1)
    n_rr = rr_check["rr_rank"].notna().sum()
    print(f"R/R-compliant [1.5,2.5] (any width): {n_rr}")

    # De-dup: count distinct clusters (same code, entry dates within 5 trading days)
    rr_df = rr_check[rr_check["rr_rank"].notna()][["code", "entry_date"]].copy()
    rr_df["entry_date"] = pd.to_datetime(rr_df["entry_date"])
    rr_df = rr_df.sort_values(["code", "entry_date"])
    clusters = 0
    for code, g in rr_df.groupby("code"):
        dates = g["entry_date"].tolist()
        last = None
        for d in dates:
            if last is None or (d - last).days > 5:
                clusters += 1
            last = d
    print(f"De-duplicated clusters (>5 trading days apart): {clusters}")

    months = 36.0
    print(f"\nRaw signal frequency: {len(merged)/months:.1f}/month (undeduped), {clusters/months:.1f}/month (deduped clusters)")

    for label, kwargs in [
        ("richest-first (current spec)", dict(rank_ascending=True)),
        ("safest-first (flip)", dict(rank_ascending=False)),
    ]:
        r = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map, commission_per_spread=1.30, **kwargs)
        wr = (r.wins / r.allocated_count) if r.allocated_count else 0.0
        print(f"\n[structural_2of3] {label}: allocated={r.allocated_count} blocked={len(r.blocked)} "
              f"win_rate={r.wins}/{r.allocated_count}={wr:.1%} total_pnl=${r.total_pnl:,.2f} monthly=${r.total_pnl/months:,.2f}")


if __name__ == "__main__":
    main()
