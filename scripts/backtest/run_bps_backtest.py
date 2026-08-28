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
    def __init__(self):
        self._cache = {}

    def fetch(self, codes, start, end):
        key = (tuple(sorted(codes)), start, end)
        if key in self._cache:
            return self._cache[key]
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
        self._cache[key] = data_map
        return data_map


def run_tier(tier_name: str, engine: BullPutSpreadSignalEngine, loader: YFLoader, run_dir: Path, config: dict):
    print(f"\n{'=' * 80}")
    print(f"RUNNING TIER: {tier_name}")
    print(f"{'=' * 80}")
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = run_options_backtest(config, loader, engine, run_dir)

    print("\n=== ENTRIES ===")
    for e in engine.entries:
        print(e)

    print("\n=== METRICS ===")
    print(metrics)

    trades_path = run_dir / "artifacts" / "trades.csv"
    stats = {
        "tier": tier_name,
        "spreads_opened": 0,
        "wins": 0,
        "win_rate_str": "0/0 = 0.0%",
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
    }
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
        wins = int((grouped["spread_pnl"] > 0).sum())
        total_pnl = float(grouped["spread_pnl"].sum())
        avg_pnl = float(grouped["spread_pnl"].mean()) if n else 0.0
        win_rate = (wins / n) if n else 0.0
        win_rate_str = f"{wins}/{n} = {win_rate:.1%}" if n else "0/0 = 0.0%"

        stats.update({
            "spreads_opened": n,
            "wins": wins,
            "win_rate_str": win_rate_str,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
        })
        print(f"\nSpreads opened: {n}")
        if n:
            print(f"Win rate: {win_rate_str}")
            print(f"Total P&L: {total_pnl:.2f}")
            print(f"Avg P&L per spread: {avg_pnl:.2f}")

    return stats


def main():
    loader = YFLoader()
    base_run_dir = Path(__file__).parent / "run_out"
    base_run_dir.mkdir(exist_ok=True)

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

    tiers = [
        ("current", {}),
        ("structural_1of3", {"min_structural": 4, "min_confirm": 1}),
        ("structural_2of3", {"min_structural": 4, "min_confirm": 2}),
    ]
    all_stats = []
    for name, kwargs in tiers:
        engine = BullPutSpreadSignalEngine(**kwargs)
        all_stats.append(run_tier(name, engine, loader, base_run_dir / name, config))

    # Final side-by-side comparison table
    print(f"\n\n{'=' * 100}")
    print("FINAL COMPARISON: CURRENT (ALL-7) vs STRUCTURAL + 1-OF-3 vs STRUCTURAL + 2-OF-3")
    print(f"{'=' * 100}")
    col_w = 24
    header = f"{'Metric':<25}" + "".join(f" | {s['tier']:<{col_w}}" for s in all_stats)
    print(header)
    print("-" * len(header))

    def fmt(v, spec):
        return f"{v:{spec}}" if v is not None else "N/A"

    rows = [
        ("Spreads opened", lambda s: str(s["spreads_opened"])),
        ("Win rate", lambda s: s["win_rate_str"]),
        ("Total P&L ($)", lambda s: fmt(s["total_pnl"], ".2f")),
        ("Avg P&L per spread ($)", lambda s: fmt(s["avg_pnl"], ".2f")),
        ("Max Drawdown", lambda s: fmt(s["max_drawdown"], ".6f")),
        ("Sharpe", lambda s: fmt(s["sharpe"], ".4f")),
    ]
    for label, fn in rows:
        print(f"{label:<25}" + "".join(f" | {fn(s):<{col_w}}" for s in all_stats))
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
