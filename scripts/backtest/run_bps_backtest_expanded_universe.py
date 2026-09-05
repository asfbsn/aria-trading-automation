"""Universe-expansion test: does scanning mid-cap (S&P 400) + small-cap
(S&P 600) names in addition to the existing $10B+ universe produce more
entry-signal frequency through the SAME unchanged 7-of-7 gate?

Not touching data/universe.csv (the live scan's universe) -- this reads a
separate universe_expanded.csv (1591 tickers: 750 existing + 841 new
mid/small-cap names from S&P 400/600, deduped) for backtest-only comparison.
Same width (10), same gate (current, 7-of-7 default), same 36-month window
as the existing 750-ticker baseline, so the signal counts are directly
comparable.
"""
import contextlib
import csv
from pathlib import Path

import pandas as pd
import yfinance as yf

from options_portfolio import run_options_backtest
from bps_signal_engine import BullPutSpreadSignalEngine

UNIVERSE_CSV = Path(__file__).parent / "universe_expanded.csv"
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


def extract_tier_spreads(trades_csv_path: Path) -> pd.DataFrame:
    cols = ["code", "entry_date", "width", "credit", "max_loss_per_contract", "spread_pnl"]
    if not trades_csv_path.exists() or trades_csv_path.stat().st_size == 0:
        return pd.DataFrame(columns=cols)
    trades = pd.read_csv(trades_csv_path)
    if trades.empty:
        return pd.DataFrame(columns=cols)
    closes = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
    pnl_df = closes.groupby(["code", "entry_date"])["pnl"].sum().reset_index().rename(columns={"pnl": "spread_pnl"})
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
    return tier_df[cols]


def test_rr_band(max_loss, credit):
    if credit is None or credit <= 0:
        return False
    rr = max_loss / (credit * 100.0)
    return (1.5 - 1e-6) <= rr <= (2.5 + 1e-6)


def main():
    loader = YFLoader()
    run_dir = Path(__file__).parent / "run_out_expanded_universe"
    run_dir.mkdir(parents=True, exist_ok=True)

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

    print(f"Running expanded-universe backtest: {len(TICKERS)} tickers, width=10, 7-of-7 gate...", flush=True)
    engine = BullPutSpreadSignalEngine(width=10.0)
    log_path = run_dir / "tier.log"
    with open(log_path, "w", encoding="utf-8") as f:
        with contextlib.redirect_stdout(f):
            metrics = run_options_backtest(config, loader, engine, run_dir)
    print(f"Done. Metrics: {metrics}", flush=True)

    tier_df = extract_tier_spreads(run_dir / "artifacts" / "trades.csv")
    n_total = len(tier_df)
    tier_df["rr_ok"] = tier_df.apply(lambda r: test_rr_band(r["max_loss_per_contract"], r["credit"]), axis=1)
    n_rr = int(tier_df["rr_ok"].sum())
    wins = int((tier_df["spread_pnl"] > 0).sum())
    total_pnl = float(tier_df["spread_pnl"].sum())
    months = 36.0

    print("\n" + "=" * 80)
    print("EXPANDED UNIVERSE ($10B+ mega-cap + S&P 400 mid-cap + S&P 600 small-cap)")
    print("=" * 80)
    print(f"Universe size: {len(TICKERS)} tickers (baseline was 750, mega-cap only)")
    print(f"Total raw entry signals (7-of-7 gate, width=10): {n_total} (baseline: 597)")
    print(f"  -> {n_total/months:.1f}/month raw (baseline: {597/36:.1f}/month)")
    print(f"R/R-compliant [1.5,2.5] at width=10: {n_rr} (baseline: need width-10-only recount for fair compare)")
    print(f"  -> {n_rr/months:.1f}/month R/R-compliant at width=10 only")
    print(f"Unconstrained win rate: {wins}/{n_total} = {wins/n_total:.1%}" if n_total else "n=0")
    print(f"Unconstrained total P&L (width=10, no capacity limit): ${total_pnl:,.2f} (${total_pnl/months:,.2f}/month)")
    print("=" * 80)


if __name__ == "__main__":
    main()
