"""Backtests the Bull Put Spread entry rule (scripts/compute_signal.py +
prompts/bull-put-spread.md rule 1/2) against historical OHLCV, using a
vendored Black-Scholes options-portfolio engine (see options_portfolio.py
header for provenance). Theoretical premiums, not real fills -- this checks
whether the entry rule has directional edge on the underlying, not exact
live-fill economics (that's what the daily-scan.sh IBKR R/R gate is for).

Usage:
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python3 run_bps_backtest.py
"""
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from options_portfolio import run_options_backtest
from bps_signal_engine import BullPutSpreadSignalEngine

TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V",
    "MA", "UNH", "HD", "PG", "KO", "XOM", "CVX", "JCI", "FCX", "CAT",
    "DE", "BA", "LMT", "NKE", "MCD", "DIS", "NFLX", "CRM", "ADBE", "ORCL",
    "CSCO", "AMD", "QCOM", "IBM", "WMT", "GS",
]
START = "2023-07-26"
END = "2026-07-26"


class YFLoader:
    def fetch(self, codes, start, end):
        data_map = {}
        raw = yf.download(
            codes, start=start, end=end, progress=False, auto_adjust=True,
            group_by="ticker", threads=True,
        )
        for code in codes:
            try:
                df = raw[code].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
            except KeyError:
                print(f"WARN: no data for {code}", file=sys.stderr)
                continue
            if df is None or df.empty or df["Close"].dropna().empty:
                print(f"WARN: no data for {code}", file=sys.stderr)
                continue
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df.index = pd.to_datetime(df.index)
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            data_map[code] = df
        return data_map


def main():
    loader = YFLoader()
    engine = BullPutSpreadSignalEngine()
    run_dir = Path(__file__).parent / "run_out"
    run_dir.mkdir(exist_ok=True)

    config = {
        "codes": TICKERS,
        "start_date": START,
        "end_date": END,
        "initial_cash": 100_000,
        "commission": 0.0,
        "options_config": {
            "risk_free_rate": 0.045,
            "contract_multiplier": 100.0,
            "exercise_style": "american",
            # Equity put skew: OTM puts trade richer than flat IV implies.
            # Without this the short leg is underpriced and spread credit
            # (hence P&L) is understated.
            "iv_skew": -0.15,
            "iv_curvature": 0.05,
        },
    }

    metrics = run_options_backtest(config, loader, engine, run_dir)

    print("=== ENTRIES ===")
    for e in engine.entries:
        print(e)

    print("\n=== METRICS ===")
    print(metrics)

    trades_path = run_dir / "artifacts" / "trades.csv"
    if trades_path.exists():
        trades = pd.read_csv(trades_path)
        print("\n=== TRADES (raw legs) ===")
        print(trades.to_string())

        # Group leg-level trades into spread-level P&L by (code, entry_date)
        closes_only = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
        grouped = closes_only.groupby(["code", "entry_date"])["pnl"].sum().reset_index()
        grouped = grouped.rename(columns={"pnl": "spread_pnl"})
        print("\n=== SPREAD-LEVEL P&L (short+long leg combined per trade) ===")
        print(grouped.to_string())

        n = len(grouped)
        wins = (grouped["spread_pnl"] > 0).sum()
        total_pnl = grouped["spread_pnl"].sum()
        avg_pnl = grouped["spread_pnl"].mean() if n else 0
        print(f"\nSpreads opened: {n}")
        if n:
            print(f"Win rate: {wins}/{n} = {wins/n:.1%}")
            print(f"Total P&L: {total_pnl:.2f}")
            print(f"Avg P&L per spread: {avg_pnl:.2f}")


if __name__ == "__main__":
    main()
