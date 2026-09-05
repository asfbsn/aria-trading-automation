"""Sanity-check the rebuilt heap_allocator_sim.py against the 2026-09-02
session's reported baseline (7-of-7 gate, $3k account, ranked heap
allocator): 59/145 candidates allocated, -$62.18 total P&L over 36 months
(-$1.73/month). Uses the 7-of-7 trades.csv files already on disk for widths
1/2.5/5/10 -- no re-backtest needed.
"""
import csv
from pathlib import Path

import pandas as pd

from heap_allocator_sim import simulate_heap_allocation

BASE = Path(__file__).parent
UNIVERSE_CSV = BASE.parent.parent / "data" / "universe.csv"

TIER_PATHS = {
    10.0: BASE / "run_out_full_universe" / "current" / "artifacts" / "trades.csv",
    5.0: BASE / "run_out_full_universe_narrow" / "width_5_0" / "artifacts" / "trades.csv",
    2.5: BASE / "run_out_full_universe_narrow" / "width_2_5" / "artifacts" / "trades.csv",
    1.0: BASE / "run_out_full_universe_narrow" / "width_1_0" / "artifacts" / "trades.csv",
}


def load_sector_map() -> dict:
    m = {}
    with open(UNIVERSE_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = row.get("ticker", "").strip()
            if t:
                m[t] = row.get("sector", "UNKNOWN").strip()
    return m


def extract_tier_spreads_with_exit(trades_csv_path: Path) -> pd.DataFrame:
    if not trades_csv_path.exists() or trades_csv_path.stat().st_size == 0:
        return pd.DataFrame(columns=[
            "code", "entry_date", "width", "credit", "max_loss_per_contract",
            "spread_pnl", "exit_date",
        ])
    trades = pd.read_csv(trades_csv_path)
    if trades.empty:
        return pd.DataFrame(columns=[
            "code", "entry_date", "width", "credit", "max_loss_per_contract",
            "spread_pnl", "exit_date",
        ])

    closes = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
    pnl_df = closes.groupby(["code", "entry_date"])["pnl"].sum().reset_index().rename(columns={"pnl": "spread_pnl"})
    exit_df = closes.groupby(["code", "entry_date"])["timestamp"].max().reset_index().rename(columns={"timestamp": "exit_date"})

    opens = trades[trades["side"].isin(["sell", "buy"])]
    sells = opens[opens["side"] == "sell"][["code", "entry_date", "strike", "price"]].drop_duplicates(
        subset=["code", "entry_date"]
    ).rename(columns={"strike": "short_strike", "price": "short_price"})
    buys = opens[opens["side"] == "buy"][["code", "entry_date", "strike", "price"]].drop_duplicates(
        subset=["code", "entry_date"]
    ).rename(columns={"strike": "long_strike", "price": "long_price"})

    leg_df = pd.merge(sells, buys, on=["code", "entry_date"], how="inner")
    leg_df["credit"] = leg_df["short_price"] - leg_df["long_price"]
    leg_df["width"] = leg_df["short_strike"] - leg_df["long_strike"]
    leg_df["max_loss_per_contract"] = (leg_df["width"] - leg_df["credit"]) * 100.0

    tier_df = pd.merge(leg_df, pnl_df, on=["code", "entry_date"], how="inner")
    tier_df = pd.merge(tier_df, exit_df, on=["code", "entry_date"], how="left")
    return tier_df[["code", "entry_date", "width", "credit", "max_loss_per_contract", "spread_pnl", "exit_date"]]


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

    print(f"\nTotal merged signals: {len(merged)}")

    rr_compliant = merged.copy()
    from heap_allocator_sim import _primary_rr
    rr_compliant["rr_rank"] = rr_compliant.apply(_primary_rr, axis=1)
    n_rr = rr_compliant["rr_rank"].notna().sum()
    print(f"R/R-compliant (any width): {n_rr}")

    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map)
    print(f"\nAllocated: {result.allocated_count}")
    print(f"Blocked: {len(result.blocked)}")
    from collections import Counter
    print("Blocked reasons:", Counter(b["reason"] for b in result.blocked))
    print(f"Total P&L: ${result.total_pnl:,.2f}")
    print(f"Win rate: {result.wins}/{result.allocated_count}")
    months = 36.0
    print(f"Avg monthly P&L: ${result.total_pnl / months:,.2f}")
    print("\n(Compare to 2026-09-02 session baseline: 59/145 allocated, -$62.18 total, -$1.73/month)")


if __name__ == "__main__":
    main()
