"""Loosened-gate backtest: structural_2of3 (min_structural=4, min_confirm=2,
vs. the live system's current 7-of-7) across all four candidate spread
widths (1/2.5/5/10) on the full 750-ticker universe.

This is the backtest-only, legitimate way to test "lower the guards": a
comparison tier, never a live prompt edit. Reuses one YFLoader across all
4 width tiers so the universe is only downloaded once.
"""
import contextlib
import csv
from pathlib import Path

import pandas as pd
import yfinance as yf

from options_portfolio import run_options_backtest
from bps_signal_engine import BullPutSpreadSignalEngine

UNIVERSE_CSV = Path(__file__).resolve().parent.parent.parent / "data" / "universe.csv"
tickers = []
with open(UNIVERSE_CSV, mode="r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        t = row.get("ticker", "").strip()
        if t:
            tickers.append(t)
TICKERS = tickers

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
                continue
            if df is None or df.empty or df["Close"].dropna().empty:
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
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = run_options_backtest(config, loader, engine, run_dir)
    print(f"Tier {tier_name} metrics: {metrics}", flush=True)


def extract_tier_spreads(trades_csv_path: Path) -> pd.DataFrame:
    cols = ["code", "entry_date", "width", "credit", "max_loss_per_contract", "spread_pnl", "exit_date"]
    if not trades_csv_path.exists() or trades_csv_path.stat().st_size == 0:
        return pd.DataFrame(columns=cols)
    trades = pd.read_csv(trades_csv_path)
    if trades.empty:
        return pd.DataFrame(columns=cols)

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
    return tier_df[cols]


def main():
    loader = YFLoader()
    base_run_dir = Path(__file__).parent / "run_out_structural2of3"
    base_run_dir.mkdir(parents=True, exist_ok=True)

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
            "iv_skew": -0.15,
            "iv_curvature": 0.05,
        },
    }

    widths = [10.0, 5.0, 2.5, 1.0]

    for w in widths:
        suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
        tier_name = f"width_{suffix}"
        tier_dir = base_run_dir / tier_name
        tier_dir.mkdir(parents=True, exist_ok=True)
        tier_log = tier_dir / "tier.log"
        print(f"Running tier {tier_name} (width={w}, structural_2of3)...", flush=True)
        engine = BullPutSpreadSignalEngine(width=w, min_structural=4, min_confirm=2)
        with open(tier_log, "w", encoding="utf-8") as f:
            with contextlib.redirect_stdout(f):
                run_tier(tier_name, engine, loader, tier_dir, config)
        n = len(extract_tier_spreads(tier_dir / "artifacts" / "trades.csv"))
        print(f"Tier {tier_name} finished ({n} closed spreads).", flush=True)

    print("\nAll 4 tiers complete. Run the allocator analysis script next.")


if __name__ == "__main__":
    main()
