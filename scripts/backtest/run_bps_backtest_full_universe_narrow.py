"""Backtests the Bull Put Spread entry rule against the full universe
(data/universe.csv) across historical OHLCV for narrower spread widths ($1, $2.5, $5)
and tests tradeability for a $3,000 account ($300 max loss budget).

KNOWN LIMITATION (CodeRabbit finding, 2026-09-02, not fixed here): the
reported total/monthly P&L sums every tradeable signal independently with no
concurrency or capital-capacity constraint — it does NOT model the $3k
account's actual $1,500 max concurrent-risk ceiling (10% x 5-position cap)
or overlapping open positions competing for the same capital. Treat the
P&L figures this script prints as an unconstrained per-signal screen /
upper bound, not a realized-portfolio simulation. A true portfolio
simulation (sequential capital reservation/release by entry/exit date)
would very likely report a LOWER number, since some concurrent signals
would have to be skipped for lack of capital.
"""
import contextlib
import csv
import math
import sys
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


def extract_tier_spreads(trades_csv_path: Path) -> pd.DataFrame:
    """Extracts spread-level metrics from a tier's trades.csv."""
    trades = pd.read_csv(trades_csv_path)

    # Spread-level P&L (1-contract)
    closes = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
    pnl_df = (
        closes.groupby(["code", "entry_date"])["pnl"]
        .sum()
        .reset_index()
        .rename(columns={"pnl": "spread_pnl"})
    )

    # Leg info from opening trades
    opens = trades[trades["side"].isin(["sell", "buy"])]
    sells = (
        opens[opens["side"] == "sell"][["code", "entry_date", "strike", "price"]]
        .drop_duplicates(subset=["code", "entry_date"])
        .rename(columns={"strike": "short_strike", "price": "short_price"})
    )
    buys = (
        opens[opens["side"] == "buy"][["code", "entry_date", "strike", "price"]]
        .drop_duplicates(subset=["code", "entry_date"])
        .rename(columns={"strike": "long_strike", "price": "long_price"})
    )

    leg_df = pd.merge(sells, buys, on=["code", "entry_date"], how="inner")
    leg_df["credit"] = leg_df["short_price"] - leg_df["long_price"]
    leg_df["width"] = leg_df["short_strike"] - leg_df["long_strike"]
    leg_df["max_loss_per_contract"] = (leg_df["width"] - leg_df["credit"]) * 100.0

    tier_df = pd.merge(leg_df, pnl_df, on=["code", "entry_date"], how="inner")
    tier_df = tier_df[
        [
            "code",
            "entry_date",
            "width",
            "short_strike",
            "long_strike",
            "credit",
            "max_loss_per_contract",
            "spread_pnl",
        ]
    ]
    return tier_df


def test_tradeable_candidate(max_loss: float, credit: float) -> bool:
    """Test if a candidate spread clears:
    1. max_loss_per_contract <= $300
    2. R/R (max_loss / (credit * 100)) is within [1.5, 2.5]
    """
    if max_loss > 300.0 + 1e-6:
        return False
    if credit <= 0:
        return False
    rr = max_loss / (credit * 100.0)
    return (1.5 - 1e-6) <= rr <= (2.5 + 1e-6)


def main():
    loader = YFLoader()
    base_run_dir = Path(__file__).parent / "run_out_full_universe_narrow"
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

    tiers = [
        ("width_1_0", 1.0),
        ("width_2_5", 2.5),
        ("width_5_0", 5.0),
    ]

    all_stats = []
    tier_dfs = {}

    for name, width in tiers:
        tier_dir = base_run_dir / name
        tier_dir.mkdir(parents=True, exist_ok=True)
        tier_log = tier_dir / "tier.log"
        print(f"Running tier {name} (width={width})...", flush=True)
        engine = BullPutSpreadSignalEngine(width=width)
        with open(tier_log, "w", encoding="utf-8") as f:
            with contextlib.redirect_stdout(f):
                stats = run_tier(name, engine, loader, tier_dir, config)
        all_stats.append(stats)
        trades_csv = tier_dir / "artifacts" / "trades.csv"
        tier_dfs[name] = extract_tier_spreads(trades_csv)
        print(f"Tier {name} finished ({len(tier_dfs[name])} closed spreads).", flush=True)

    # Step 3b: Merge across tiers
    counts = {k: len(v) for k, v in tier_dfs.items()}
    if len(set(counts.values())) > 1:
        print(f"WARNING: Row counts differ across tiers: {counts}", file=sys.stderr)

    df_5 = tier_dfs["width_5_0"].rename(
        columns={
            "width": "width_5",
            "short_strike": "short_strike_5",
            "long_strike": "long_strike_5",
            "credit": "credit_5",
            "max_loss_per_contract": "max_loss_5",
            "spread_pnl": "spread_pnl_5",
        }
    )
    df_2 = tier_dfs["width_2_5"].rename(
        columns={
            "width": "width_2_5",
            "short_strike": "short_strike_2_5",
            "long_strike": "long_strike_2_5",
            "credit": "credit_2_5",
            "max_loss_per_contract": "max_loss_2_5",
            "spread_pnl": "spread_pnl_2_5",
        }
    )
    df_1 = tier_dfs["width_1_0"].rename(
        columns={
            "width": "width_1",
            "short_strike": "short_strike_1",
            "long_strike": "long_strike_1",
            "credit": "credit_1",
            "max_loss_per_contract": "max_loss_1",
            "spread_pnl": "spread_pnl_1",
        }
    )

    # Outer join: a signal missing from one tier's trades.csv (e.g. a pricing
    # edge case at that width) must still count in the denominator and still
    # be eligible via the OTHER tiers, not silently dropped. Missing tier
    # columns come back NaN; test_tradeable_candidate() is NaN-safe (every
    # comparison against NaN is False in Python/numpy) so a missing width is
    # correctly treated as "doesn't clear", not skipped.
    merged = pd.merge(df_5, df_2, on=["code", "entry_date"], how="outer")
    merged = pd.merge(merged, df_1, on=["code", "entry_date"], how="outer")

    # Step 3c & 3d: Selection rule & position sizing
    tradeable_rows = []
    width_counts = {5.0: 0, 2.5: 0, 1.0: 0}

    for _, row in merged.iterrows():
        selected_width = None
        selected_max_loss = None
        selected_credit = None
        selected_pnl = None

        if test_tradeable_candidate(row["max_loss_5"], row["credit_5"]):
            selected_width = 5.0
            selected_max_loss = row["max_loss_5"]
            selected_credit = row["credit_5"]
            selected_pnl = row["spread_pnl_5"]
        elif test_tradeable_candidate(row["max_loss_2_5"], row["credit_2_5"]):
            selected_width = 2.5
            selected_max_loss = row["max_loss_2_5"]
            selected_credit = row["credit_2_5"]
            selected_pnl = row["spread_pnl_2_5"]
        elif test_tradeable_candidate(row["max_loss_1"], row["credit_1"]):
            selected_width = 1.0
            selected_max_loss = row["max_loss_1"]
            selected_credit = row["credit_1"]
            selected_pnl = row["spread_pnl_1"]

        if selected_width is not None:
            width_counts[selected_width] += 1
            contracts = math.floor(300.0 / selected_max_loss)
            scaled_pnl = selected_pnl * contracts
            tradeable_rows.append({
                "code": row["code"],
                "entry_date": row["entry_date"],
                "selected_width": selected_width,
                "max_loss_per_contract": selected_max_loss,
                "credit": selected_credit,
                "contracts": contracts,
                "pnl_1contract": selected_pnl,
                "scaled_pnl": scaled_pnl,
            })

    tradeable_df = pd.DataFrame(tradeable_rows)

    # Step 4: Aggregate numbers
    total_signals = len(merged)
    tradeable_count = len(tradeable_df)
    tradeable_pct = (tradeable_count / total_signals * 100.0) if total_signals else 0.0

    baseline_path = Path(__file__).parent / "run_out_full_universe" / "current" / "artifacts" / "trades.csv"
    baseline_count = None
    if baseline_path.exists():
        b_df = pd.read_csv(baseline_path)
        b_closes = b_df[b_df["side"].isin(["expire", "exercise", "early_exercise"])]
        baseline_count = len(b_closes.groupby(["code", "entry_date"]))

    total_scaled_pnl = float(tradeable_df["scaled_pnl"].sum()) if tradeable_count else 0.0
    wins = int((tradeable_df["scaled_pnl"] > 0).sum()) if tradeable_count else 0
    win_rate = (wins / tradeable_count * 100.0) if tradeable_count else 0.0
    avg_scaled_pnl = float(tradeable_df["scaled_pnl"].mean()) if tradeable_count else 0.0

    start_dt = pd.to_datetime(START)
    end_dt = pd.to_datetime(END)
    num_months = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month) + (end_dt.day - start_dt.day) / 30.4375
    expected_monthly_pnl = (total_scaled_pnl / num_months) if num_months else 0.0

    w5 = width_counts[5.0]
    w25 = width_counts[2.5]
    w1 = width_counts[1.0]

    w5_pct = (w5 / tradeable_count * 100.0) if tradeable_count else 0.0
    w25_pct = (w25 / tradeable_count * 100.0) if tradeable_count else 0.0
    w1_pct = (w1 / tradeable_count * 100.0) if tradeable_count else 0.0

    print("\n" + "=" * 80)
    print("NARROW WIDTH BPS BACKTEST SUMMARY ($3k ACCOUNT, $300 MAX LOSS BUDGET)")
    print("=" * 80)
    if baseline_count is not None:
        print(f"Total signals generated: {total_signals} (baseline in run_out_full_universe: {baseline_count})")
    else:
        print(f"Total signals generated: {total_signals}")
    print(f"Tradeable signals at $3k: {tradeable_count} ({tradeable_pct:.1f}%)")
    print(f"Width selection breakdown:")
    print(f"  - Width 5.0: {w5} ({w5_pct:.1f}%)")
    print(f"  - Width 2.5: {w25} ({w25_pct:.1f}%)")
    print(f"  - Width 1.0: {w1} ({w1_pct:.1f}%)")
    print(f"Realized total P&L across all tradeable signals: ${total_scaled_pnl:,.2f}")
    print(f"Win rate on tradeable signals: {wins}/{tradeable_count} = {win_rate:.1f}%")
    print(f"Average scaled P&L per tradeable signal: ${avg_scaled_pnl:,.2f}")
    print(f"Expected average monthly P&L: ${expected_monthly_pnl:,.2f} (over {num_months:.1f} months)")
    print("=" * 80)


if __name__ == "__main__":
    main()
