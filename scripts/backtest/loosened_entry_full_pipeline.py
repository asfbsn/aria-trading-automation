"""Full R/R + $3k Global Heap Allocator + exit-rule P&L test on loosened
entry variants (B: any-green-candle, D: green-candle + no rsi_rising),
750-ticker mega-cap-only universe. Compares against the strict baseline (A)
entry, under both mega-cap exit configs (A: strike-only, MA150 advisory;
B: strike+MA150 hard stop) -- same universe, so this isolates the entry-side
effect cleanly against the already-known expanded-universe numbers.
"""
import csv
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))
from signal_core import sma, rsi_series, entry_checks, MIN_BARS
from options_portfolio import bs_price, historical_volatility, iv_smile_adjustment
from heap_allocator_sim import simulate_heap_allocation
from run_heap_allocator_baseline_check import load_sector_map

UNIVERSE_CSV = BASE.parent.parent / "data" / "universe.csv"
CACHE = BASE / "mega750_ohlcv_cache.pkl"

START = "2023-07-26"
END = "2026-07-26"
TARGET_DTE = 30
RISK_FREE = 0.045
IV_SKEW = -0.15
IV_CURVATURE = 0.05
COMMISSION_PER_SPREAD = 1.30
WIDTHS = [10.0, 5.0, 2.5, 1.0]

with open(UNIVERSE_CSV) as f:
    TICKERS = [row["ticker"].strip() for row in csv.DictReader(f) if row.get("ticker", "").strip()]

if CACHE.exists():
    print("Loading cached OHLCV...", flush=True)
    with open(CACHE, "rb") as f:
        data_map = pickle.load(f)
else:
    print(f"Fetching OHLCV for {len(TICKERS)} tickers...", flush=True)
    raw = yf.download(TICKERS, start=START, end=END, progress=False, auto_adjust=True,
                       group_by="ticker", threads=True)
    data_map = {}
    for code in TICKERS:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw[code].copy()
            elif len(TICKERS) == 1:
                df = raw.copy()
            else:
                # yfinance returned flat (non-MultiIndex) columns for a
                # >1-ticker request -- raw.copy() would silently assign the
                # SAME frame to every remaining code, running this 750-ticker
                # comparison on N identical series with no warning.
                print(f"WARN: unexpected flat columns for {code}", file=sys.stderr)
                continue
        except KeyError:
            continue
        if df is None or df.empty or df["Close"].dropna().empty:
            continue
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        df.index = pd.to_datetime(df.index)
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df["ma150"] = df["close"].rolling(150, min_periods=150).mean()
        df["hv30"] = historical_volatility(df["close"])
        data_map[code] = df
    with open(CACHE, "wb") as f:
        pickle.dump(data_map, f)
print(f"Universe loaded: {len(data_map)} tickers.", flush=True)

ENTRY_VARIANTS = {
    "A_strict": dict(loosen_candle=False, drop_momentum=False),
    "B_loose_candle": dict(loosen_candle=True, drop_momentum=False),
    "D_both_loose": dict(loosen_candle=True, drop_momentum=True),
}

EXIT_VARIANTS = {
    "ExitA_strike_only": dict(tp=True, ma150_stop=False, strike_stop=True),
    "ExitB_strike_plus_ma150": dict(tp=True, ma150_stop=True, strike_stop=True),
}


def generate_entries(cfg):
    entries = []
    for code, df in data_map.items():
        closes = df["close"].tolist()
        opens = df["open"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        volumes = df["volume"].tolist()
        dates = list(df.index)
        rsis = rsi_series(closes)
        bars = [{"open": o, "high": h, "low": l, "close": c}
                for o, h, l, c in zip(opens, highs, lows, closes)]
        open_until = None
        for i in range(len(df)):
            if i < MIN_BARS or i < 1 or i + 1 >= len(dates):
                continue
            entry_ts = dates[i + 1]
            date_str = str(entry_ts.date())
            if open_until is not None:
                if date_str <= open_until:
                    continue
                open_until = None

            close = closes[i]
            ma150 = sma(closes[: i + 1], 150)

            # Reuse signal_core.entry_checks() for every structural/momentum/
            # volume/candle computation instead of re-deriving the MA-band/
            # RSI/volume formulas locally -- a later change to the live gate
            # (signal_core.entry_checks) now reaches this comparison
            # automatically instead of silently going stale against a frozen
            # copy of the constants.
            checks, _ = entry_checks(
                closes[: i + 1], volumes[: i + 1], bars[: i + 1], rsis=rsis[: i + 1]
            )
            structural_ok = (
                checks["above_ma150"] and checks["rsi_below_50"]
                and checks["near_ma50_pullback"] and checks["near_ma150_support"]
            )
            if not structural_ok:
                continue
            if not checks["volume_above_avg"]:
                continue
            momentum_ok = True if cfg["drop_momentum"] else checks["rsi_rising"]
            if not momentum_ok:
                continue
            # entry_checks()'s own "bullish_candle" key is always the
            # loosened any-green-close rule (locked 2026-09-05); the strict
            # variant here instead requires its separately-returned
            # candle_pattern (hammer/bullish-engulfing) to be non-"none".
            candle_ok = checks["bullish_candle"] if cfg["loosen_candle"] else checks["candle_pattern"] != "none"
            if not candle_ok:
                continue

            short_strike = np.floor(ma150 / 5.0) * 5.0
            if short_strike >= close:
                short_strike = np.floor((min(ma150, close) - 0.01) / 5.0) * 5.0
            expiry_ts = entry_ts + pd.Timedelta(days=TARGET_DTE)
            expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)

            entries.append({
                "code": code, "entry_date": entry_ts, "expiry": expiry_ts,
                "short_strike": short_strike,
            })
            open_until = str(expiry_ts.date())
    return entries


def simulate_one(entry, price_df, k_long, exit_variant):
    entry_date = entry["entry_date"]; expiry = entry["expiry"]
    k_short = entry["short_strike"]
    dates = price_df.index[(price_df.index > entry_date) & (price_df.index <= expiry)]
    entry_idx_arr = price_df.index[price_df.index <= entry_date]
    if len(entry_idx_arr) == 0 or len(dates) == 0:
        return None
    entry_hv = price_df.at[entry_idx_arr[-1], "hv30"]
    entry_S = price_df.at[entry_idx_arr[-1], "close"]
    if pd.isna(entry_hv) or entry_hv <= 0:
        return None
    T0 = max((expiry - entry_date).days / 365.0, 0.001)
    iv_s0 = iv_smile_adjustment(entry_S, k_short, entry_hv, IV_SKEW, IV_CURVATURE)
    iv_l0 = iv_smile_adjustment(entry_S, k_long, entry_hv, IV_SKEW, IV_CURVATURE)
    credit = bs_price(entry_S, k_short, T0, RISK_FREE, iv_s0, "put") - bs_price(entry_S, k_long, T0, RISK_FREE, iv_l0, "put")
    if credit <= 0 or pd.isna(credit):
        return None

    for d in dates:
        S = price_df.at[d, "close"]; ma150 = price_df.at[d, "ma150"]; hv = price_df.at[d, "hv30"]
        if pd.isna(S) or pd.isna(hv) or hv <= 0:
            continue
        T = max((expiry - d).days / 365.0, 0.001)
        iv_short = iv_smile_adjustment(S, k_short, hv, IV_SKEW, IV_CURVATURE)
        iv_long = iv_smile_adjustment(S, k_long, hv, IV_SKEW, IV_CURVATURE)
        cost_to_close = bs_price(S, k_short, T, RISK_FREE, iv_short, "put") - bs_price(S, k_long, T, RISK_FREE, iv_long, "put")
        captured = (credit - cost_to_close) / credit
        if exit_variant["tp"] and captured >= 0.80:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
        ma150_breach = exit_variant["ma150_stop"] and not pd.isna(ma150) and S < ma150
        strike_breach = exit_variant["strike_stop"] and S < k_short
        if ma150_breach or strike_breach:
            return {"exit_date": d, "pnl": (credit - cost_to_close) * 100.0, "credit": credit}
    last_date = dates[-1]; S_T = price_df.at[last_date, "close"]
    payoff = credit - max(0.0, k_short - S_T) + max(0.0, k_long - S_T)
    return {"exit_date": last_date, "pnl": payoff * 100.0, "credit": credit}


def run_combo(entry_label, entries, exit_label, exit_variant, sector_map, months=36.0):
    rows_by_key = {}
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        for w in WIDTHS:
            k_long = entry["short_strike"] - w
            r = simulate_one(entry, pdf, k_long, exit_variant)
            if r is None:
                continue
            key = (entry["code"], entry["entry_date"])
            suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
            rows_by_key.setdefault(key, {})[w] = {
                "max_loss": (w - r["credit"]) * 100.0, "credit": r["credit"],
                "pnl": r["pnl"], "exit_date": r["exit_date"],
            }
    merged_rows = []
    for (code, entry_date), widths in rows_by_key.items():
        row = {"code": code, "entry_date": entry_date}
        for w in WIDTHS:
            suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
            d = widths.get(w)
            row[f"max_loss_{suffix}"] = d["max_loss"] if d else None
            row[f"credit_{suffix}"] = d["credit"] if d else None
            row[f"spread_pnl_{suffix}"] = d["pnl"] if d else None
            row[f"exit_date_{suffix}"] = d["exit_date"] if d else None
        merged_rows.append(row)
    merged = pd.DataFrame(merged_rows)
    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                       commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    print(f"{entry_label} x {exit_label}: raw_signals={len(entries)} R/R_merged={len(merged)} "
          f"allocated={result.allocated_count} blocked={len(result.blocked)} win_rate={wr:.1%} "
          f"total_pnl=${result.total_pnl:,.2f} monthly=${result.total_pnl/months:,.2f}")
    return result


def main():
    sector_map = load_sector_map()
    entries_by_variant = {}
    for label, cfg in ENTRY_VARIANTS.items():
        entries = generate_entries(cfg)
        entries_by_variant[label] = entries
        print(f"{label}: {len(entries)} raw signals ({len(entries)/36.0:.1f}/mo)")

    print()
    for entry_label, entries in entries_by_variant.items():
        for exit_label, exit_variant in EXIT_VARIANTS.items():
            run_combo(entry_label, entries, exit_label, exit_variant, sector_map)


if __name__ == "__main__":
    main()
