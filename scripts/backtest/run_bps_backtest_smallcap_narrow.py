"""Backtests the Bull Put Spread entry rule against a small-cap/high-volatility
universe subset across historical OHLCV for narrow spread widths ($2.5, $1.0),
testing $3k account economics under both unconstrained and capacity-constrained
(max 5 concurrent positions, $1,500 max committed capital) portfolio models.
"""
from __future__ import annotations

import contextlib
import csv
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import yfinance as yf

from bps_signal_engine import BullPutSpreadSignalEngine
from options_portfolio import historical_volatility, run_options_backtest

UNIVERSE_CSV = Path(__file__).resolve().parent.parent.parent / "data" / "universe.csv"
START = "2023-07-26"
END = "2026-07-26"


class YFLoader:
    def __init__(self):
        self._cache = {}

    def fetch(self, codes: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        key = (tuple(sorted(codes)), start, end)
        if key in self._cache:
            return self._cache[key]
        try:
            from curl_cffi import requests as cffi_requests
            from yfinance.data import YfData, cookie_jar

            s = cffi_requests.Session(impersonate="chrome120")
            s.get("https://fc.yahoo.com", timeout=10)
            data = YfData()
            jar = cookie_jar(data._session)
            for c in s.cookies.jar:
                jar.set_cookie(c)
            data._save_cookie_curlCffi()
        except Exception:
            pass

        data_map = {}
        raw = yf.download(
            codes,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        )
        for code in codes:
            try:
                df = raw[code].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
            except KeyError:
                continue
            if df is None or df.empty or df["Close"].dropna().empty:
                continue
            df = df.rename(
                columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            df.index = pd.to_datetime(df.index)
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            data_map[code] = df
        self._cache[key] = data_map
        return data_map


class PreloadedLoader:
    """Provides subsetted in-memory data to avoid refetching over network."""

    def __init__(self, data_map: Dict[str, pd.DataFrame]):
        self._data_map = data_map

    def fetch(self, codes: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        return {c: self._data_map[c] for c in codes if c in self._data_map}


def run_tier(
    tier_name: str,
    engine: BullPutSpreadSignalEngine,
    loader: Any,
    run_dir: Path,
    config: dict,
) -> Dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = run_options_backtest(config, loader, engine, run_dir)

    trades_path = run_dir / "artifacts" / "trades.csv"
    stats: Dict[str, Any] = {
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
    if trades_path.exists() and trades_path.stat().st_size > 0:
        trades = pd.read_csv(trades_path)
        if not trades.empty:
            closes_only = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
            grouped = closes_only.groupby(["code", "entry_date"])["pnl"].sum().reset_index()
            grouped = grouped.rename(columns={"pnl": "spread_pnl"})

            n = len(grouped)
            wins = int((grouped["spread_pnl"] > 0).sum())
            total_pnl = float(grouped["spread_pnl"].sum())
            avg_pnl = float(grouped["spread_pnl"].mean()) if n else 0.0
            win_rate = (wins / n) if n else 0.0
            win_rate_str = f"{wins}/{n} = {win_rate:.1%}" if n else "0/0 = 0.0%"

            stats.update(
                {
                    "spreads_opened": n,
                    "wins": wins,
                    "win_rate_str": win_rate_str,
                    "win_rate": win_rate,
                    "total_pnl": total_pnl,
                    "avg_pnl": avg_pnl,
                }
            )
    return stats


def extract_tier_spreads(trades_csv_path: Path) -> pd.DataFrame:
    """Extracts spread-level metrics from a tier's trades.csv."""
    cols = [
        "code",
        "entry_date",
        "width",
        "short_strike",
        "long_strike",
        "credit",
        "max_loss_per_contract",
        "spread_pnl",
        "actual_close_date",
    ]
    if not trades_csv_path.exists() or trades_csv_path.stat().st_size == 0:
        return pd.DataFrame(columns=cols)

    trades = pd.read_csv(trades_csv_path)
    if trades.empty:
        return pd.DataFrame(columns=cols)

    # Spread-level P&L (1-contract). Also capture the ACTUAL close date (not
    # the planned expiry) — early_exercise can close a position before its
    # expiry, and using planned expiry for capacity release would hold that
    # capital reserved longer than it really was, understating capacity for
    # later signals (CodeRabbit finding, 2026-09-02).
    closes = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
    if closes.empty:
        return pd.DataFrame(columns=cols)

    pnl_df = (
        closes.groupby(["code", "entry_date"])
        .agg(spread_pnl=("pnl", "sum"), actual_close_date=("timestamp", "max"))
        .reset_index()
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
    return tier_df[cols]


def test_tradeable_candidate(max_loss: float, credit: float) -> bool:
    """Test if a candidate spread clears:
    1. max_loss_per_contract <= $300
    2. R/R (max_loss / (credit * 100)) is within [1.5, 2.5]
    """
    if pd.isna(max_loss) or pd.isna(credit):
        return False
    if max_loss > 300.0 + 1e-6:
        return False
    if credit <= 0:
        return False
    rr = max_loss / (credit * 100.0)
    return (1.5 - 1e-6) <= rr <= (2.5 + 1e-6)


def main():
    base_run_dir = Path(__file__).resolve().parent / "run_out_smallcap_narrow"
    base_run_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Load all tickers from universe.csv and fetch OHLCV
    tickers = []
    with open(UNIVERSE_CSV, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = row.get("ticker", "").strip()
            if t:
                tickers.append(t)

    loader = YFLoader()
    fetch_log_path = base_run_dir / "fetch.log"
    with open(fetch_log_path, "w", encoding="utf-8") as f_log:
        with contextlib.redirect_stdout(f_log), contextlib.redirect_stderr(f_log):
            data_map = loader.fetch(tickers, START, END)

    # Step 2: Universe filter (10.0 <= mean_close <= 50.0 AND ann_hv >= 0.50)
    price_filtered_hvs = []
    filtered_items = []
    valid_count = 0

    for code, df in data_map.items():
        if len(df) < 30:
            continue
        valid_count += 1
        mean_close = float(df["close"].mean())
        hv_series = historical_volatility(df["close"], window=30)
        ann_hv = float(hv_series.dropna().mean())

        if 10.0 <= mean_close <= 50.0:
            price_filtered_hvs.append((code, mean_close, ann_hv))
            if ann_hv >= 0.50:
                filtered_items.append((code, mean_close, ann_hv))

    # Sort descending by ann_hv
    filtered_items.sort(key=lambda x: x[2], reverse=True)
    filtered_codes = [x[0] for x in filtered_items]

    # HV distribution among price-filtered subset
    pf_hvs = [x[2] for x in price_filtered_hvs]
    min_hv = min(pf_hvs) if pf_hvs else 0.0
    median_hv = float(np.median(pf_hvs)) if pf_hvs else 0.0
    max_hv = max(pf_hvs) if pf_hvs else 0.0

    print("=" * 80)
    print("SMALL-CAP / HIGH-VOLATILITY UNIVERSE FILTER")
    print("=" * 80)
    print(f"Total tickers with valid data: {valid_count}")
    print(f"Price-filtered ($10-$50) subset count: {len(price_filtered_hvs)}")
    print(f"Price-filtered HV distribution: min={min_hv:.1%}, median={median_hv:.1%}, max={max_hv:.1%}")
    print(f"Combined filter matched ($10-$50 & HV >= 50%): {len(filtered_codes)}")
    if len(filtered_codes) <= 40:
        print(f"Matched tickers ({len(filtered_codes)}): {', '.join(filtered_codes)}")
    else:
        top10_str = ", ".join([f"{c} ({hv:.1%})" for c, _, hv in filtered_items[:10]])
        print(f"Top 10 matched tickers by HV: {top10_str}")

    # Step 3: Run two tiers (width 2.5 and width 1.0) on filtered subset
    filtered_data_map = {c: data_map[c] for c in filtered_codes if c in data_map}
    sub_loader = PreloadedLoader(filtered_data_map)

    config = {
        "codes": filtered_codes,
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
        ("width_2_5", 2.5),
        ("width_1_0", 1.0),
    ]

    tier_dfs = {}
    expiry_map = {}

    for name, width in tiers:
        tier_dir = base_run_dir / name
        tier_dir.mkdir(parents=True, exist_ok=True)
        tier_log = tier_dir / "tier.log"
        engine = BullPutSpreadSignalEngine(width=width)
        with open(tier_log, "w", encoding="utf-8") as f_log:
            with contextlib.redirect_stdout(f_log), contextlib.redirect_stderr(f_log):
                run_tier(name, engine, sub_loader, tier_dir, config)

        for e in engine.entries:
            expiry_map[(e["code"], e["date"])] = e["expiry"]

        trades_csv = tier_dir / "artifacts" / "trades.csv"
        tier_dfs[name] = extract_tier_spreads(trades_csv)

    # Step 4: Outer join across the two tiers and apply selection rule
    df_2 = tier_dfs["width_2_5"].rename(
        columns={
            "width": "width_2_5",
            "short_strike": "short_strike_2_5",
            "long_strike": "long_strike_2_5",
            "credit": "credit_2_5",
            "max_loss_per_contract": "max_loss_2_5",
            "spread_pnl": "spread_pnl_2_5",
            "actual_close_date": "actual_close_date_2_5",
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
            "actual_close_date": "actual_close_date_1",
        }
    )

    merged = pd.merge(df_2, df_1, on=["code", "entry_date"], how="outer")

    tradeable_rows = []
    width_counts = {2.5: 0, 1.0: 0}

    for _, row in merged.iterrows():
        selected_width = None
        selected_max_loss = None
        selected_credit = None
        selected_pnl = None

        selected_close_date = None
        if test_tradeable_candidate(row.get("max_loss_2_5"), row.get("credit_2_5")):
            selected_width = 2.5
            selected_max_loss = float(row["max_loss_2_5"])
            selected_credit = float(row["credit_2_5"])
            selected_pnl = float(row["spread_pnl_2_5"])
            selected_close_date = row.get("actual_close_date_2_5")
        elif test_tradeable_candidate(row.get("max_loss_1"), row.get("credit_1")):
            selected_width = 1.0
            selected_max_loss = float(row["max_loss_1"])
            selected_credit = float(row["credit_1"])
            selected_pnl = float(row["spread_pnl_1"])
            selected_close_date = row.get("actual_close_date_1")

        if selected_width is not None:
            width_counts[selected_width] += 1
            contracts = math.floor(300.0 / selected_max_loss)
            scaled_pnl = selected_pnl * contracts

            # Prefer the ACTUAL close date (early_exercise can close before
            # planned expiry); only fall back to planned expiry / a computed
            # approximation when the actual close date is unavailable
            # (CodeRabbit finding, 2026-09-02 -- using planned expiry alone
            # over-reserves capacity and can wrongly capacity_block later
            # signals).
            if pd.notna(selected_close_date):
                exit_date = selected_close_date
            else:
                exit_date = expiry_map.get((row["code"], row["entry_date"]))
                if exit_date is None:
                    entry_ts = pd.to_datetime(row["entry_date"])
                    exp_ts = entry_ts + pd.Timedelta(days=30)
                    exp_ts += pd.Timedelta(days=(4 - exp_ts.weekday()) % 7)
                    exit_date = str(exp_ts.date())

            tradeable_rows.append(
                {
                    "code": row["code"],
                    "entry_date": row["entry_date"],
                    "exit_date": exit_date,
                    "selected_width": selected_width,
                    "max_loss_per_contract": selected_max_loss,
                    "credit": selected_credit,
                    "contracts": contracts,
                    "pnl_1contract": selected_pnl,
                    "scaled_pnl": scaled_pnl,
                }
            )

    # Step 5: Capacity-constrained portfolio simulation
    tradeable_rows.sort(key=lambda r: (r["entry_date"], r["code"]))

    open_positions: List[Dict[str, Any]] = []
    taken_signals: List[Dict[str, Any]] = []
    blocked_signals: List[Dict[str, Any]] = []

    for r in tradeable_rows:
        entry_date = r["entry_date"]
        cap_needed = r["max_loss_per_contract"] * r["contracts"]

        # Release positions whose exit_date <= this signal's entry_date
        open_positions = [pos for pos in open_positions if pos["exit_date"] > entry_date]
        curr_reserved = sum(pos["capital_reserved"] for pos in open_positions)

        if len(open_positions) < 5 and (curr_reserved + cap_needed <= 1500.0 + 1e-6):
            r["status"] = "taken"
            open_positions.append(
                {
                    "exit_date": r["exit_date"],
                    "capital_reserved": cap_needed,
                }
            )
            taken_signals.append(r)
        else:
            r["status"] = "capacity_blocked"
            blocked_signals.append(r)

    # Save detailed tradeable records for offline inspection
    pd.DataFrame(tradeable_rows).to_csv(base_run_dir / "tradeable_signals.csv", index=False)

    # Step 6: Summary Metrics
    total_signals = len(merged)
    tradeable_count = len(tradeable_rows)
    tradeable_pct = (tradeable_count / total_signals * 100.0) if total_signals else 0.0

    w25 = width_counts[2.5]
    w1 = width_counts[1.0]
    w25_pct = (w25 / tradeable_count * 100.0) if tradeable_count else 0.0
    w1_pct = (w1 / tradeable_count * 100.0) if tradeable_count else 0.0

    start_dt = pd.to_datetime(START)
    end_dt = pd.to_datetime(END)
    num_months = (
        (end_dt.year - start_dt.year) * 12
        + (end_dt.month - start_dt.month)
        + (end_dt.day - start_dt.day) / 30.4375
    )

    # Unconstrained metrics
    unconstrained_pnl = sum(r["scaled_pnl"] for r in tradeable_rows)
    unconstrained_wins = sum(1 for r in tradeable_rows if r["scaled_pnl"] > 0)
    unconstrained_win_rate = (
        (unconstrained_wins / tradeable_count * 100.0) if tradeable_count else 0.0
    )
    unconstrained_avg = (unconstrained_pnl / tradeable_count) if tradeable_count else 0.0
    unconstrained_monthly = (unconstrained_pnl / num_months) if num_months else 0.0

    # Constrained metrics
    taken_count = len(taken_signals)
    blocked_count = len(blocked_signals)
    blocked_pct = (blocked_count / tradeable_count * 100.0) if tradeable_count else 0.0

    constrained_pnl = sum(r["scaled_pnl"] for r in taken_signals)
    constrained_wins = sum(1 for r in taken_signals if r["scaled_pnl"] > 0)
    constrained_win_rate = (constrained_wins / taken_count * 100.0) if taken_count else 0.0
    constrained_monthly = (constrained_pnl / num_months) if num_months else 0.0

    print("\n" + "=" * 80)
    print("SMALL-CAP / HIGH-VOLATILITY NARROW BPS BACKTEST SUMMARY ($3k ACCOUNT)")
    print("=" * 80)
    print(f"Total signals generated: {total_signals}")
    print(f"Tradeable signals at $3k: {tradeable_count} ({tradeable_pct:.1f}%)")
    print(f"Width selection breakdown:")
    print(f"  - Width 2.5: {w25} ({w25_pct:.1f}%)")
    print(f"  - Width 1.0: {w1} ({w1_pct:.1f}%)")
    print("\nUNCONSTRAINED (ALL TRADEABLE SIGNALS):")
    print(f"  - Total P&L: ${unconstrained_pnl:,.2f}")
    print(f"  - Win rate: {unconstrained_wins}/{tradeable_count} = {unconstrained_win_rate:.1f}%")
    print(f"  - Avg P&L per signal: ${unconstrained_avg:,.2f}")
    print(f"  - Avg monthly P&L: ${unconstrained_monthly:,.2f} (over {num_months:.1f} months)")
    print("\nCONSTRAINED (MAX 5 POSITIONS, $1,500 COMMITTED CAPITAL):")
    print(f"  - Taken signals: {taken_count}/{tradeable_count} ({(taken_count / tradeable_count * 100.0) if tradeable_count else 0.0:.1f}%)")
    print(f"  - Capacity blocked signals: {blocked_count} ({blocked_pct:.1f}%)")
    print(f"  - Total P&L: ${constrained_pnl:,.2f}")
    print(f"  - Win rate on taken signals: {constrained_wins}/{taken_count} = {constrained_win_rate:.1f}%" if taken_count else "  - Win rate on taken signals: 0/0 = 0.0%")
    print(f"  - Avg monthly P&L: ${constrained_monthly:,.2f} (over {num_months:.1f} months)")
    print("=" * 80)


if __name__ == "__main__":
    main()
