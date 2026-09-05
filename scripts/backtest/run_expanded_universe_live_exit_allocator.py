"""The real question: what does the S&P 400/600 universe expansion yield at
$3k, through the ACTUAL live exit rule (80% TP + strike-breach-only hard
stop -- MA150 demoted per the 2026-09-05 signal_core.py change), through the
Global Heap Allocator (sector diversification on, $3k net_liq)?

Every other "expanded universe" number tonight (1191 signals, 67 R/R-
compliant, $514/mo) assumed hold-to-expiry, which is NOT what's live.
This script re-simulates every entry from the 4-width expanded-universe
backtest through the day-by-day exit-rule engine (same methodology as
run_exit_rule_ab_test.py, validated against the mega-cap baseline earlier
tonight), then feeds the result through heap_allocator_sim.py.

Excludes: the 15 index/sector-ETF tickers (tested negative, dropped per
user decision), and 2 known garbage entries (CFFN 2024-03-18, JBLU
2026-06-10 -- $0 short strikes, a data artifact, not real signals).
"""
import csv
from pathlib import Path

import pandas as pd

from options_portfolio import bs_price, historical_volatility, iv_smile_adjustment
from heap_allocator_sim import simulate_heap_allocation
from run_heap_allocator_baseline_check import load_sector_map

BASE = Path(__file__).parent
RISK_FREE = 0.045
IV_SKEW = -0.15
IV_CURVATURE = 0.05
COMMISSION_PER_SPREAD = 1.30

INDEX_TICKERS_EXCLUDE = {
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLY",
    "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
}
GARBAGE_EXCLUDE = {("CFFN", "2024-03-18"), ("JBLU", "2026-06-10")}

WIDTH_PATHS = {
    10.0: BASE / "run_out_expanded_universe" / "artifacts" / "trades.csv",
    5.0: BASE / "run_out_expanded_universe_narrow" / "width_5" / "artifacts" / "trades.csv",
    2.5: BASE / "run_out_expanded_universe_narrow" / "width_2_5" / "artifacts" / "trades.csv",
    1.0: BASE / "run_out_expanded_universe_narrow" / "width_1" / "artifacts" / "trades.csv",
}

VARIANTS = {
    "1_tp_only_no_stop": dict(tp=True, ma150_stop=False, strike_stop=False),
    "2_current_live_strike_stop_only": dict(tp=True, ma150_stop=False, strike_stop=True),
    "3_old_logic_strike_plus_ma150": dict(tp=True, ma150_stop=True, strike_stop=True),
}


def load_entries(trades_csv: Path) -> pd.DataFrame:
    trades = pd.read_csv(trades_csv)
    opens = trades[trades["side"].isin(["sell", "buy"])]
    sells = opens[opens["side"] == "sell"][["code", "entry_date", "strike", "price", "expiry"]].drop_duplicates(
        subset=["code", "entry_date"]
    ).rename(columns={"strike": "short_strike", "price": "short_price"})
    buys = opens[opens["side"] == "buy"][["code", "entry_date", "strike", "price"]].drop_duplicates(
        subset=["code", "entry_date"]
    ).rename(columns={"strike": "long_strike", "price": "long_price"})
    df = pd.merge(sells, buys, on=["code", "entry_date"], how="inner")
    df["credit"] = df["short_price"] - df["long_price"]
    df = df[df["short_strike"] > 0]  # drop $0-strike garbage
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    df = df[~df["code"].isin(INDEX_TICKERS_EXCLUDE)]
    df = df[~df.apply(lambda r: (r["code"], r["entry_date"].strftime("%Y-%m-%d")) in GARBAGE_EXCLUDE, axis=1)]
    return df


def fetch_price_data(tickers, start, end):
    import yfinance as yf
    data_map = {}
    raw = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True,
                       group_by="ticker", threads=True)
    for code in tickers:
        try:
            df = raw[code].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
        except KeyError:
            continue
        if df is None or df.empty or df["Close"].dropna().empty:
            continue
        df = df.rename(columns={"Close": "close"})
        df.index = pd.to_datetime(df.index)
        df = df[["close"]].dropna()
        df["ma150"] = df["close"].rolling(150, min_periods=150).mean()
        df["hv30"] = historical_volatility(df["close"])
        data_map[code] = df
    return data_map


def simulate_one(entry, price_df, variant):
    code = entry["code"]
    entry_date = entry["entry_date"]
    expiry = entry["expiry"]
    k_short, k_long = entry["short_strike"], entry["long_strike"]
    credit = entry["credit"]
    if credit <= 0 or pd.isna(credit):
        return None
    dates = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry)]
    if len(dates) == 0:
        return None
    for d in dates:
        S = price_df.at[d, "close"]
        ma150 = price_df.at[d, "ma150"]
        hv = price_df.at[d, "hv30"]
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
            return {"exit_date": d, "reason": "TP", "pnl": (credit - cost_to_close) * 100.0}
        ma150_breach = variant["ma150_stop"] and not pd.isna(ma150) and S < ma150
        strike_breach = variant["strike_stop"] and S < k_short
        if ma150_breach or strike_breach:
            reason = "STOP_MA150" if ma150_breach and not strike_breach else (
                "STOP_STRIKE" if strike_breach and not ma150_breach else "STOP_BOTH")
            return {"exit_date": d, "reason": reason, "pnl": (credit - cost_to_close) * 100.0}
    last_date = dates[-1]
    S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "reason": "EXPIRY", "pnl": payoff * 100.0}


def main():
    all_entries = {w: load_entries(p) for w, p in WIDTH_PATHS.items()}
    for w, df in all_entries.items():
        print(f"width {w}: {len(df)} entries (post-exclusion)")

    all_tickers = sorted(set().union(*[set(df["code"]) for df in all_entries.values()]))
    min_date = min(df["entry_date"].min() for df in all_entries.values() if len(df))
    max_date = max(df["expiry"].max() for df in all_entries.values() if len(df))
    start = (min_date - pd.Timedelta(days=250)).strftime("%Y-%m-%d")
    end = (max_date + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"Fetching price data for {len(all_tickers)} tickers...", flush=True)
    price_data = fetch_price_data(all_tickers, start, end)
    print(f"Price data fetched for {len(price_data)}/{len(all_tickers)} tickers.", flush=True)

    sector_map = load_sector_map()
    with open(BASE / "universe_midsmall_validated.csv") as f:
        for row in csv.DictReader(f):
            sector_map[row["ticker"]] = row["sector"]

    months = 36.0
    from collections import Counter

    for variant_name, variant in VARIANTS.items():
        rows_by_key = {}
        for w, df in all_entries.items():
            for _, entry in df.iterrows():
                pdf = price_data.get(entry["code"])
                if pdf is None:
                    continue
                r = simulate_one(entry, pdf, variant)
                if r is None:
                    continue
                key = (entry["code"], entry["entry_date"])
                rows_by_key.setdefault(key, {})[w] = {
                    "max_loss": (w - entry["credit"]) * 100.0,
                    "credit": entry["credit"],
                    "pnl": r["pnl"],
                    "exit_date": r["exit_date"],
                    "reason": r["reason"],
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
        reasons = Counter()
        for w, df in all_entries.items():
            for _, entry in df.iterrows():
                pdf = price_data.get(entry["code"])
                if pdf is None:
                    continue
                r = simulate_one(entry, pdf, variant)
                if r:
                    reasons[r["reason"]] += 1

        print(f"\n{'=' * 90}\n[{variant_name}]\n{'=' * 90}")
        print(f"Total merged signals (pre-allocator, any width R/R-eligible): {len(merged)}")
        print(f"Exit reason breakdown (all widths, all entries): {dict(reasons)}")
        print(f"$3k Heap Allocator (sector diversification ON, commission $1.30/spread):")
        print(f"  Allocated: {result.allocated_count}  Blocked: {len(result.blocked)}")
        print(f"  Blocked reasons: {dict(Counter(b['reason'] for b in result.blocked))}")
        print(f"  Win rate: {result.wins}/{result.allocated_count} = {wr:.1%}")
        print(f"  Total P&L: ${result.total_pnl:,.2f}")
        print(f"  Monthly: ${result.total_pnl/months:,.2f}")


if __name__ == "__main__":
    main()
