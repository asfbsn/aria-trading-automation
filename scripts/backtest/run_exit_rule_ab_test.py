"""A/B test: does the live exit-guard's hard stop-loss (settled-close MA150
breach OR short-strike breach) help or hurt a small account, versus the
backtest's existing hold-to-expiry-only assumption?

Reuses the entries already opened in the 7-of-7, $10-wide "current" tier
(run_out_full_universe/current for the 750-ticker universe, run_out/current
for the 36-ticker universe) -- same signals, same entry credit, same width --
and re-simulates each one day-by-day against four exit-rule variants:

  0. Baseline (already known from the original backtest run): hold to expiry,
     no TP, no stop. Not re-simulated here, just quoted for context.
  1. TP-only:        80% profit-take, no stop-loss at all (ride out any breach).
  2. TP + full stop: 80% profit-take + MA150 breach + short-strike breach
                      (the live exit-guard's actual rule, minus the time-stop
                      -- the user's variant spec didn't include DTE<=7, so it's
                      excluded here to test exactly what was asked).
  2a. TP + MA150-only stop   (isolates the MA150 leg)
  2b. TP + strike-only stop  (isolates the short-strike-breach leg)

All four use settled (prior) daily close only -- same cadence as
bull-put-spread-exit.md -- never intraday. Daily marks use the same BS +
historical-volatility + IV-smile pricing as options_portfolio.py itself
(iv_skew=-0.15, iv_curvature=0.05, r=0.045), so this is priced consistently
with the original backtest, not a different pricing model.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from options_portfolio import bs_price, historical_volatility, iv_smile_adjustment

BASE = Path(__file__).parent
RISK_FREE = 0.045
IV_SKEW = -0.15
IV_CURVATURE = 0.05
COMMISSION_PER_SPREAD = 1.30  # open-only, matches heap_allocator_sim.py's floor

UNIVERSES = {
    "750-ticker": BASE / "run_out_full_universe" / "current" / "artifacts" / "trades.csv",
    "36-ticker": BASE / "run_out" / "current" / "artifacts" / "trades.csv",
}

VARIANTS = {
    "0_hold_to_expiry": dict(tp=False, ma150_stop=False, strike_stop=False),
    "1_tp_only": dict(tp=True, ma150_stop=False, strike_stop=False),
    "2_tp_full_stop": dict(tp=True, ma150_stop=True, strike_stop=True),
    "2a_tp_ma150_only": dict(tp=True, ma150_stop=True, strike_stop=False),
    "2b_tp_strike_only": dict(tp=True, ma150_stop=False, strike_stop=True),
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
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def fetch_price_data(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
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


def simulate_one(entry: pd.Series, price_df: pd.DataFrame, variant: dict) -> dict | None:
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
            pnl = (credit - cost_to_close) * 100.0 - COMMISSION_PER_SPREAD
            return {"code": code, "entry_date": entry_date, "exit_date": d,
                    "exit_reason": "TP", "pnl": pnl}

        ma150_breach = variant["ma150_stop"] and not pd.isna(ma150) and S < ma150
        strike_breach = variant["strike_stop"] and S < k_short
        if ma150_breach or strike_breach:
            reason = "STOP_MA150" if ma150_breach and not strike_breach else (
                "STOP_STRIKE" if strike_breach and not ma150_breach else "STOP_BOTH")
            pnl = (credit - cost_to_close) * 100.0 - COMMISSION_PER_SPREAD
            return {"code": code, "entry_date": entry_date, "exit_date": d,
                    "exit_reason": reason, "pnl": pnl}

    # Never triggered TP or stop -> settle at expiry via intrinsic payoff
    last_date = dates[-1]
    S_T = price_df.at[last_date, "close"]
    payoff_per_share = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    pnl = payoff_per_share * 100.0 - COMMISSION_PER_SPREAD
    return {"code": code, "entry_date": entry_date, "exit_date": last_date,
            "exit_reason": "EXPIRY", "pnl": pnl}


def run_universe(label: str, trades_csv: Path):
    print(f"\n{'=' * 90}\n{label}\n{'=' * 90}")
    entries = load_entries(trades_csv)
    tickers = sorted(entries["code"].unique())
    print(f"{len(entries)} entries across {len(tickers)} tickers. Fetching price history...", flush=True)

    start = (entries["entry_date"].min() - pd.Timedelta(days=250)).strftime("%Y-%m-%d")
    end = (entries["expiry"].max() + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    price_data = fetch_price_data(tickers, start, end)
    print(f"Price data fetched for {len(price_data)}/{len(tickers)} tickers.", flush=True)

    months = 36.0
    for vname, vcfg in VARIANTS.items():
        results = []
        for _, entry in entries.iterrows():
            pdf = price_data.get(entry["code"])
            if pdf is None:
                continue
            r = simulate_one(entry, pdf, vcfg)
            if r is not None:
                results.append(r)

        n = len(results)
        if n == 0:
            print(f"[{vname}] no results")
            continue
        rdf = pd.DataFrame(results)
        wins = int((rdf["pnl"] > 0).sum())
        total = float(rdf["pnl"].sum())
        avg = total / n
        reasons = rdf["exit_reason"].value_counts().to_dict()
        print(f"[{vname}] n={n} win_rate={wins}/{n}={wins/n:.1%} total_pnl=${total:,.2f} "
              f"avg=${avg:,.2f} monthly=${total/months:,.2f} exit_reasons={reasons}")


def main():
    for label, path in UNIVERSES.items():
        if not path.exists():
            print(f"SKIP {label}: {path} not found")
            continue
        run_universe(label, path)


if __name__ == "__main__":
    main()
