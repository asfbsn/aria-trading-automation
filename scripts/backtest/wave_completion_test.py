"""Wave-Completion (dynamic technical) TP test: does an early technical
mean-reversion exit (price reclaims MA20 + RSI14 > 60, floored at 30% profit
capture) beat waiting for the static 80% theta-decay TP -- on win rate and
capital velocity (days held) -- layered on top of both leading exit-rule
candidates (A: strike-only: B: strike+MA150)?
"""
import pickle
import numpy as np
import pandas as pd
from pathlib import Path

from run_expanded_universe_live_exit_allocator import WIDTH_PATHS, load_entries, RISK_FREE, IV_SKEW, IV_CURVATURE
from options_portfolio import bs_price, iv_smile_adjustment
from heap_allocator_sim import simulate_heap_allocation
from run_heap_allocator_baseline_check import load_sector_map
from run_bifurcated_exit_test import load_mega_tickers
import csv

BASE = Path(__file__).parent

def add_indicators(df):
    df = df.copy()
    df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    return df

def simulate_one_wave(entry, price_df, variant):
    code = entry["code"]; entry_date = entry["entry_date"]; expiry = entry["expiry"]
    k_short, k_long = entry["short_strike"], entry["long_strike"]
    credit = entry["credit"]
    if credit <= 0 or pd.isna(credit):
        return None
    dates = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry)]
    if len(dates) == 0:
        return None
    for d in dates:
        S = price_df.at[d, "close"]; ma150 = price_df.at[d, "ma150"]; hv = price_df.at[d, "hv30"]
        ma20 = price_df.at[d, "ma20"]; rsi14 = price_df.at[d, "rsi14"]
        if pd.isna(S) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        short_p = bs_price(S, k_short, T, RISK_FREE, iv_short, "put")
        long_p = bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        cost_to_close = short_p - long_p
        captured = (credit - cost_to_close) / credit
        if variant["tp"] and captured >= 0.80:
            return {"exit_date": d, "reason": "TP80", "pnl": (credit - cost_to_close) * 100.0, "days": (d - entry_date).days}
        if variant.get("wave_tp") and captured >= 0.30 and not pd.isna(ma20) and not pd.isna(rsi14):
            if S > ma20 and rsi14 > 60:
                return {"exit_date": d, "reason": "WAVE_TP", "pnl": (credit - cost_to_close) * 100.0, "days": (d - entry_date).days}
        ma150_breach = variant["ma150_stop"] and not pd.isna(ma150) and S < ma150
        strike_breach = variant["strike_stop"] and S < k_short
        if ma150_breach or strike_breach:
            return {"exit_date": d, "reason": "STOP", "pnl": (credit - cost_to_close) * 100.0, "days": (d - entry_date).days}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "reason": "EXPIRY", "pnl": payoff * 100.0, "days": (last_date - entry_date).days}

def build_and_run(label, variant, all_entries, price_data, sector_map, mega_tickers, months=36.0):
    rows_by_key = {}
    reason_counts = {}
    days_list = []
    for w, df in all_entries.items():
        for _, entry in df.iterrows():
            pdf = price_data.get(entry["code"])
            if pdf is None: continue
            r = simulate_one_wave(entry, pdf, variant)
            if r is None: continue
            reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
            days_list.append(r["days"])
            key = (entry["code"], entry["entry_date"])
            rows_by_key.setdefault(key, {})[w] = {
                "max_loss": (w - entry["credit"]) * 100.0, "credit": entry["credit"],
                "pnl": r["pnl"], "exit_date": r["exit_date"],
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
    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                       commission_per_spread=1.30, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    days_s = pd.Series(days_list)
    print(f"\n=== {label} ===")
    print(f"Exit reasons (pre-allocator): {reason_counts}")
    print(f"Days held: median={days_s.median():.0f} p75={days_s.quantile(.75):.0f}")
    print(f"Allocated: {result.allocated_count}  Win rate: {wr:.1%}  Total P&L: ${result.total_pnl:,.2f}  Monthly: ${result.total_pnl/months:,.2f}")
    return result

def main():
    with open(BASE / "price_data_cache.pkl", "rb") as f:
        price_data = pickle.load(f)
    price_data = {k: add_indicators(v) for k, v in price_data.items()}

    all_entries = {w: load_entries(p) for w, p in WIDTH_PATHS.items()}
    mega_tickers = load_mega_tickers()
    sector_map = load_sector_map()
    with open(BASE / "universe_midsmall_validated.csv") as f:
        for row in csv.DictReader(f):
            sector_map[row["ticker"]] = row["sector"]

    configs = {
        "A. strike-only, static 80% TP only (current live)": dict(tp=True, ma150_stop=False, strike_stop=True, wave_tp=False),
        "A+Wave: strike-only + wave-completion TP": dict(tp=True, ma150_stop=False, strike_stop=True, wave_tp=True),
        "B. strike+MA150, static 80% TP only (old rule)": dict(tp=True, ma150_stop=True, strike_stop=True, wave_tp=False),
        "B+Wave: strike+MA150 + wave-completion TP": dict(tp=True, ma150_stop=True, strike_stop=True, wave_tp=True),
    }
    for label, variant in configs.items():
        build_and_run(label, variant, all_entries, price_data, sector_map, mega_tickers)

if __name__ == "__main__":
    main()
