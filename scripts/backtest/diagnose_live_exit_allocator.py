"""Validate run_expanded_universe_live_exit_allocator.py before trusting any
of its output -- the exit-reason counts (86-99.9% TP/stop before expiry on a
30-DTE spread) are not physically plausible. Three checks, in order.
"""
import pickle
from pathlib import Path

import pandas as pd

from run_expanded_universe_live_exit_allocator import (
    WIDTH_PATHS, load_entries, fetch_price_data, simulate_one,
)
from options_portfolio import bs_price, iv_smile_adjustment
from heap_allocator_sim import _primary_rr

BASE = Path(__file__).parent
CACHE = BASE / "price_data_cache.pkl"


def get_price_data(all_entries):
    if CACHE.exists():
        print("Loading cached price data...")
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    all_tickers = sorted(set().union(*[set(df["code"]) for df in all_entries.values()]))
    min_date = min(df["entry_date"].min() for df in all_entries.values() if len(df))
    max_date = max(df["expiry"].max() for df in all_entries.values() if len(df))
    start = (min_date - pd.Timedelta(days=250)).strftime("%Y-%m-%d")
    end = (max_date + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"Fetching price data for {len(all_tickers)} tickers...")
    price_data = fetch_price_data(all_tickers, start, end)
    with open(CACHE, "wb") as f:
        pickle.dump(price_data, f)
    return price_data


def main():
    all_entries = {w: load_entries(p) for w, p in WIDTH_PATHS.items()}
    price_data = get_price_data(all_entries)
    print(f"Price data: {len(price_data)} tickers cached.\n")

    # --- Check 1: hold-to-expiry reproduction against the engine's own trades.csv ---
    print("=" * 80)
    print("CHECK 1: hold-to-expiry reproduction (width=10) vs run_out_expanded_universe")
    print("=" * 80)
    w10_entries = all_entries[10.0]
    variant_hold = dict(tp=False, ma150_stop=False, strike_stop=False)
    sim_pnls = {}
    for _, entry in w10_entries.iterrows():
        pdf = price_data.get(entry["code"])
        if pdf is None:
            continue
        r = simulate_one(entry, pdf, variant_hold)
        if r is not None:
            sim_pnls[(entry["code"], entry["entry_date"])] = r["pnl"]
    sim_total = sum(sim_pnls.values())
    print(f"Simulator hold-to-expiry total P&L (width=10): ${sim_total:,.2f} over {len(sim_pnls)} trades")

    engine_trades = pd.read_csv(BASE / "run_out_expanded_universe" / "artifacts" / "trades.csv")
    closes = engine_trades[engine_trades["side"].isin(["expire", "exercise", "early_exercise"])]
    eng_pnl = closes.groupby(["code", "entry_date"])["pnl"].sum()
    eng_total = eng_pnl.sum()
    print(f"Engine's own hold-to-expiry total P&L (width=10, all entries incl. excluded): ${eng_total:,.2f} over {len(eng_pnl)} trades")
    print(f"NOTE: engine total includes indices+garbage rows the simulator excludes -- expect a gap, not a match.")
    print()

    # --- Check 2: days-held distribution for variant 1 (TP-only) ---
    print("=" * 80)
    print("CHECK 2: days-to-exit distribution, variant 1 (TP-only, no stop)")
    print("=" * 80)
    variant1 = dict(tp=True, ma150_stop=False, strike_stop=False)
    days_held = []
    reasons = []
    sample_fast_tp = []
    for w, df in all_entries.items():
        for _, entry in df.iterrows():
            pdf = price_data.get(entry["code"])
            if pdf is None:
                continue
            r = simulate_one(entry, pdf, variant1)
            if r is None:
                continue
            d = (r["exit_date"] - entry["entry_date"]).days
            days_held.append(d)
            reasons.append(r["reason"])
            if r["reason"] == "TP" and d <= 5:
                sample_fast_tp.append((entry["code"], entry["entry_date"], entry["short_strike"], entry["long_strike"], entry["credit"], r["exit_date"], d, w))

    days_s = pd.Series(days_held)
    print(f"n={len(days_held)}")
    print(f"days-held: min={days_s.min()} p25={days_s.quantile(.25):.0f} median={days_s.median():.0f} p75={days_s.quantile(.75):.0f} max={days_s.max()}")
    print(f"reason counts: {pd.Series(reasons).value_counts().to_dict()}")
    print(f"Entries that hit TP within 5 days of entry: {len(sample_fast_tp)}")
    print()

    # --- Check 3: spot/strike sanity on the fastest TP entries ---
    print("=" * 80)
    print("CHECK 3: spot/short_strike ratio at entry, for entries that TP'd within 5 days")
    print("=" * 80)
    for code, entry_date, k_short, k_long, credit, exit_date, d, w in sample_fast_tp[:20]:
        pdf = price_data[code]
        entry_dates_avail = pdf.index[pdf.index > entry_date]
        if len(entry_dates_avail) == 0:
            continue
        first_day = entry_dates_avail[0]
        spot = pdf.at[first_day, "close"]
        ratio = spot / k_short if k_short else None
        print(f"{code} entry={entry_date.date()} width={w} short_strike={k_short:.1f} spot(day1)={spot:.2f} "
              f"ratio={ratio:.2f} credit={credit:.3f} exit_in={d}d")


if __name__ == "__main__":
    main()
