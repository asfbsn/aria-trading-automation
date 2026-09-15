"""Exit-aware bull-put-spread backtest on the full production universe.

Non-production research artifact -- same footing as choke_point_test.py /
run_bps_backtest_cushion_cooldown_slope.py. NOT wired into daily-scan.sh or
any live path. Does not modify signal_core.py, bps_signal_engine.py or
options_portfolio.py; the engine below is a copy of
BullPutSpreadSignalEngine.generate() that additionally emits "close" signals.

Why: the 36-ticker sweeps held every spread to expiration. The live system
does not -- prompts/bull-put-spread-exit.md section 6 hard-closes on three
deterministic triggers. Holding through a strike breach overstates loss
(BA 2025-03-03 breached the next day and was held a month at near-max-loss).
This run answers two questions separately:
  run 1  hold      751-ticker universe, hold-to-expiry  (universe effect only)
  run 2  exit      same universe + the 3 hard-CLOSE triggers (exit effect only)
  run 3  exit_reentry  optional: run 2 + symbol re-enterable after a close
         ("live-faithful"; entry set diverges from runs 1/2, so it is NOT the
         paired comparison -- reported separately).

Exit rule modelled (ONLY the three deterministic hard-CLOSE triggers; the
RECOMMEND EXIT tier -- MA150_BREACH, EARNINGS_RISK, TIME_RISK, STRIKE_UNKNOWN
-- is human judgment in the live system and is NOT modelled):
  1. thesis_invalidated : settled close < short_strike
  2. profit target      : pct_max_profit_captured >= 0.80
  3. time stop          : DTE < 0 (cannot occur here: the simulator settles at
                          expiry), or 0 <= DTE <= 7 AND captured >= 0
Trigger is evaluated on day T's settled close (what exit-guard reads pre-open);
the close fills at day T+1's open (price_mode "open"), the same T+1-open
convention bps_signal_engine.py uses for entries. If several triggers fire on
the same day the position closes once; the recorded reason follows the order
above. No close is emitted with a fill date on/after expiry (the simulator
auto-settles at expiry in step 2b before signals execute).

Pricing consistency: the engine's credit estimate and daily cost-to-close use
options_portfolio.bs_price / iv_smile_adjustment / historical_volatility with
the same CONFIG as the simulator, replicating its two call shapes exactly
(entry: spot = T+1 open, iv = HV at T+1, T = (expiry-ts).days/365 floored at
0.001; mark: spot = close, iv = HV that day). The report cross-checks the
engine's credit against trades.csv's sell-buy prices for every entry; any
mismatch > 1e-4 is a blocker, not a footnote.

Entry set: runs 1 and 2 keep open_until = expiry even after an early close so
both runs open the identical (code, entry_date) set and the exit effect is a
paired per-spread delta. Run 3 sets open_until = close-fill date.

Data: scripts/backtest/mega750_ohlcv_cache.pkl (748 of the 751 tickers in
data/universe.csv, 2023-07-26..2026-07-24). The three missing (NVDA -- added
to the universe after the cache was built; BRKB/BFB -- yfinance spells them
BRK-B/BF-B) are pulled live once and keyed back to their universe.csv
spelling. Cache frames carry ma150/hv30 columns with leading NaNs; they are
sliced to OHLCV BEFORE dropna so no leading rows are lost.

Sizing caveat: initial_cash is 1,000,000 (not the 36-ticker runs' 100,000)
so the vendored engine's margin-reservation gate never rejects an entry --
at 100k, ~100 concurrent spreads x ~$850 reserve would bind exactly during
crash-time signal clusters and drop trades first-come. The report prints the
rejected_margin count (must be 0) and the peak reserve so the reader can see
whether 100k would have bound. Consequences: total return / %DD are on a 1M
base; Sharpe is scale-invariant. None of this is the live risk profile
(no Global-Heap 50%/25% caps, no sector guard, 1 contract per signal).

IS/OOS: IS 2023-07-26..2025-01-26, OOS 2025-01-27..2026-07-26, trades assigned
by entry_date, equity-curve metrics on the window slice (peak reset at window
start) -- same as run_bps_backtest_cushion_cooldown_slope.py.

Usage (each mode is one process; run them in parallel, then report):
    python3 exit_aware_full_universe_backtest.py --mode hold
    python3 exit_aware_full_universe_backtest.py --mode exit
    python3 exit_aware_full_universe_backtest.py --mode exit_reentry
    python3 exit_aware_full_universe_backtest.py --report
Outputs: run_out_exit_aware_full/<mode>/artifacts/*, entries.csv, exits.csv,
run_out_exit_aware_full/summary.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from options_portfolio import (  # noqa: E402
    bs_price, historical_volatility, iv_smile_adjustment, run_options_backtest,
)
from signal_core import MIN_BARS, entry_checks, rsi_series  # noqa: E402

UNIVERSE_CSV = BASE.parent.parent / "data" / "universe.csv"
CACHE = BASE / "mega750_ohlcv_cache.pkl"
OUT_DIR = BASE / "run_out_exit_aware_full"
YF_ALIASES = {"BRKB": "BRK-B", "BFB": "BF-B"}

START = "2023-07-26"
END = "2026-07-26"
SPREAD_WIDTH = 10.0
TARGET_DTE = 30
INITIAL_CASH = 1_000_000
BARS_PER_YEAR = 252
IS_START, IS_END = "2023-07-26", "2025-01-26"
OOS_START, OOS_END = "2025-01-27", "2026-07-26"

PROFIT_TARGET = 0.80
TIME_STOP_DTE = 7

OPTIONS_CONFIG = {
    "risk_free_rate": 0.045,
    "contract_multiplier": 100.0,
    "exercise_style": "american",
    "iv_skew": -0.15,
    "iv_curvature": 0.05,
}
MODES = {
    "hold": dict(exit_aware=False, reentry=False),
    "exit": dict(exit_aware=True, reentry=False),
    "exit_reentry": dict(exit_aware=True, reentry=True),
}


# --- data ---------------------------------------------------------------------

def load_universe() -> List[str]:
    with open(UNIVERSE_CSV) as f:
        return [r["ticker"].strip() for r in csv.DictReader(f) if r.get("ticker", "").strip()]


class CacheLoader:
    """loader.fetch(codes, start, end) -> {code: OHLCV DataFrame}; cache first,
    one live yfinance pull for whatever the cache lacks."""

    def __init__(self):
        self._data: Dict[str, pd.DataFrame] | None = None

    def fetch(self, codes, start, end):
        if self._data is not None:
            return self._data
        with open(CACHE, "rb") as f:
            raw = pickle.load(f)
        data_map: Dict[str, pd.DataFrame] = {}
        for code in codes:
            df = raw.get(code)
            if df is None:
                continue
            df = df[["open", "high", "low", "close", "volume"]].dropna().copy()
            df.index = pd.to_datetime(df.index)
            data_map[code] = df.sort_index()
        missing = [c for c in codes if c not in data_map]
        if missing:
            import yfinance as yf
            yf_syms = [YF_ALIASES.get(c, c) for c in missing]
            print(f"Cache lacks {missing}; pulling {yf_syms} from yfinance", flush=True)
            dl = yf.download(yf_syms, start=start, end=end, progress=False, auto_adjust=True,
                             group_by="ticker", threads=True)
            for code, sym in zip(missing, yf_syms):
                try:
                    df = dl[sym].copy() if isinstance(dl.columns, pd.MultiIndex) else dl.copy()
                except KeyError:
                    print(f"WARN: no data for {code} ({sym})", file=sys.stderr)
                    continue
                if df.empty or df["Close"].dropna().empty:
                    print(f"WARN: no data for {code} ({sym})", file=sys.stderr)
                    continue
                df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                        "Close": "close", "Volume": "volume"})
                df.index = pd.to_datetime(df.index)
                data_map[code] = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
        print(f"Universe loaded: {len(data_map)} of {len(codes)} tickers", flush=True)
        self._data = data_map
        return data_map


# --- engine -------------------------------------------------------------------

class ExitAwareBPSEngine:
    def __init__(self, exit_aware: bool, reentry: bool, width: float = SPREAD_WIDTH,
                 target_dte: int = TARGET_DTE):
        self.exit_aware = exit_aware
        self.reentry = reentry
        self.width = width
        self.target_dte = target_dte
        self.entries: List[Dict[str, Any]] = []
        self.exits: List[Dict[str, Any]] = []

    def _spread_value(self, spot, iv, T, k_short, k_long):
        """Simulator call shape: smile-adjust per leg, BS per leg, short - long."""
        r = OPTIONS_CONFIG["risk_free_rate"]
        sk, cv = OPTIONS_CONFIG["iv_skew"], OPTIONS_CONFIG["iv_curvature"]
        iv_s = iv_smile_adjustment(spot, k_short, iv, sk, cv) if (sk != 0 or cv != 0) else iv
        iv_l = iv_smile_adjustment(spot, k_long, iv, sk, cv) if (sk != 0 or cv != 0) else iv
        return bs_price(spot, k_short, T, r, iv_s, "put") - bs_price(spot, k_long, T, r, iv_l, "put")

    def generate(self, data_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        self.entries, self.exits = [], []

        for code, df in data_map.items():
            df = df.sort_index()
            closes = df["close"].tolist()
            opens = df["open"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            volumes = df["volume"].tolist()
            dates = list(df.index)
            hv = historical_volatility(df["close"])  # identical to the simulator's iv_map
            hv_list = hv.tolist()
            rsis_full = rsi_series(closes)  # Wilder recursion is causal: prefix == recompute
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

                bars_slice = [
                    {"open": opens[j], "high": highs[j], "low": lows[j], "close": closes[j]}
                    for j in (i - 1, i)
                ]
                _, entry_confirmed = entry_checks(closes[: i + 1], volumes[: i + 1], bars_slice,
                                                  rsis=rsis_full[: i + 1])
                if not entry_confirmed:
                    continue

                ma150 = sum(closes[i - 149: i + 1]) / 150.0
                close = closes[i]
                short_strike = math.floor(ma150 / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((min(ma150, close) - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width
                expiry_ts = entry_ts + pd.Timedelta(days=self.target_dte)
                expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
                expiry_str = str(expiry_ts.date())
                group_id = f"{code}-{date_str}"

                legs = [
                    {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                    {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                ]
                signals.append({"date": date_str, "action": "open", "underlying": code,
                                "price_mode": "open", "legs": legs, "group_id": group_id})

                # Credit estimate, replicating the simulator's entry pricing.
                fill_spot = opens[i + 1]
                fill_iv = hv_list[i + 1] if not math.isnan(hv_list[i + 1]) else 0.3
                T0 = max((expiry_ts - entry_ts).days / 365.0, 0.001)
                credit = self._spread_value(fill_spot, fill_iv, T0, short_strike, long_strike)

                entry_rec = {"code": code, "entry_date": date_str, "signal_close": close,
                             "ma150": ma150, "short_strike": short_strike, "long_strike": long_strike,
                             "expiry": expiry_str, "credit_est": credit}
                self.entries.append(entry_rec)
                open_until = expiry_str

                if not self.exit_aware or credit <= 0:
                    continue

                # Walk forward: evaluate the three hard-CLOSE triggers on each
                # settled close from the fill day on; fill the close next open.
                for j in range(i + 1, len(dates) - 1):
                    day = dates[j]
                    if day >= expiry_ts:
                        break
                    fill_day = dates[j + 1]
                    if fill_day >= expiry_ts:
                        break  # simulator settles at expiry before signals run
                    S = closes[j]
                    iv_j = hv_list[j] if not math.isnan(hv_list[j]) else 0.3
                    Tj = max((expiry_ts - day).days / 365.0, 0.001)
                    cost = self._spread_value(S, iv_j, Tj, short_strike, long_strike)
                    captured = (credit - cost) / credit
                    dte = (expiry_ts - day).days
                    reason = None
                    if S < short_strike:
                        reason = "STRIKE_BREACH"
                    elif captured >= PROFIT_TARGET:
                        reason = "PROFIT_TARGET"
                    elif dte < 0 or (0 <= dte <= TIME_STOP_DTE and captured >= 0):
                        reason = "TIME_STOP"
                    if reason is None:
                        continue
                    fill_str = str(fill_day.date())
                    signals.append({"date": fill_str, "action": "close", "underlying": code,
                                    "price_mode": "open", "group_id": group_id,
                                    "legs": [{"type": "put", "strike": short_strike, "expiry": expiry_str},
                                             {"type": "put", "strike": long_strike, "expiry": expiry_str}]})
                    self.exits.append({"code": code, "entry_date": date_str, "trigger_date": str(day.date()),
                                       "exit_fill_date": fill_str, "reason": reason, "captured_at_trigger": captured,
                                       "close_at_trigger": S, "dte_at_trigger": dte, "expiry": expiry_str})
                    if self.reentry:
                        open_until = fill_str
                    break

        return signals


# --- metrics ------------------------------------------------------------------

CLOSE_SIDES = ["expire", "exercise", "early_exercise", "close"]


def spread_level_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    c = trades[trades["side"].isin(CLOSE_SIDES)]
    g = c.groupby(["code", "entry_date"]).agg(spread_pnl=("pnl", "sum"), exit_date=("timestamp", "max"),
                                              exit_side=("side", "first")).reset_index()
    return g


def window_equity_metrics(equity_df: pd.DataFrame, start: str, end: str) -> Dict[str, float | None]:
    ts = pd.to_datetime(equity_df["timestamp"])
    eq = equity_df.loc[(ts >= start) & (ts <= end), "equity"].astype(float).reset_index(drop=True)
    out: Dict[str, float | None] = {"sharpe": None, "max_dd": None, "max_dd_usd": None}
    if len(eq) < 2:
        return out
    returns = eq.pct_change(fill_method=None).iloc[1:]
    vol = float(returns.std())
    if np.isfinite(vol) and vol > 1e-12:
        out["sharpe"] = float(returns.mean() / vol * np.sqrt(BARS_PER_YEAR))
    peak = eq.cummax()
    if bool((peak > 0).all()):
        out["max_dd"] = float(((eq - peak) / peak).min())
        out["max_dd_usd"] = float((eq - peak).min())
    return out


def window_trade_stats(grouped: pd.DataFrame, start: str, end: str) -> Dict[str, float]:
    ed = pd.to_datetime(grouped["entry_date"])
    g = grouped[(ed >= start) & (ed <= end)]
    n = len(g)
    total = float(g["spread_pnl"].sum())
    return {"n": n, "win_rate": float((g["spread_pnl"] > 0).mean()) if n else float("nan"),
            "per_trade": total / n if n else float("nan"), "total_pnl": total,
            "total_return": total / INITIAL_CASH}


# --- run one mode -------------------------------------------------------------

def run_mode(mode: str) -> None:
    flags = MODES[mode]
    run_dir = OUT_DIR / mode
    run_dir.mkdir(parents=True, exist_ok=True)
    codes = load_universe()
    config = {"codes": codes, "start_date": START, "end_date": END, "initial_cash": INITIAL_CASH,
              "commission": 0.0, "options_config": dict(OPTIONS_CONFIG)}
    engine = ExitAwareBPSEngine(**flags)
    print(f"RUNNING {mode} {flags} on {len(codes)} tickers", flush=True)
    metrics = run_options_backtest(config, CacheLoader(), engine, run_dir)
    art = run_dir / "artifacts"
    pd.DataFrame(engine.entries).to_csv(art / "entries.csv", index=False)
    pd.DataFrame(engine.exits, columns=["code", "entry_date", "trigger_date", "exit_fill_date", "reason",
                                        "captured_at_trigger", "close_at_trigger", "dte_at_trigger",
                                        "expiry"]).to_csv(art / "exits.csv", index=False)
    with open(run_dir / "metrics_full.json", "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"DONE {mode}: entries={len(engine.entries)} exits_emitted={len(engine.exits)} "
          f"total_return={metrics.get('total_return')} sharpe={metrics.get('sharpe')}", flush=True)


# --- report -------------------------------------------------------------------

def fmt_pct(v):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.2%}"


def fmt_num(v, spec=".2f"):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:{spec}}"


def load_mode(mode: str) -> Dict[str, Any] | None:
    art = OUT_DIR / mode / "artifacts"
    if not (art / "trades.csv").exists():
        return None
    trades = pd.read_csv(art / "trades.csv")
    grouped = spread_level_pnl(trades)
    equity = pd.read_csv(art / "equity.csv")
    entries = pd.read_csv(art / "entries.csv")
    exits = pd.read_csv(art / "exits.csv")
    greeks = pd.read_csv(art / "greeks.csv")
    with open(OUT_DIR / mode / "metrics_full.json") as f:
        full = json.load(f)
    s: Dict[str, Any] = {"mode": mode, "trades": trades, "grouped": grouped, "entries": entries,
                         "exits": exits, "full": full, "greeks": greeks}
    for label, (a, b) in {"IS": (IS_START, IS_END), "OOS": (OOS_START, OOS_END)}.items():
        s[label] = {**window_trade_stats(grouped, a, b), **window_equity_metrics(equity, a, b)}
    # credit cross-check: engine estimate vs simulator fills
    opens = trades[trades["side"].isin(["sell", "buy"])]
    sells = opens[opens["side"] == "sell"][["code", "entry_date", "price"]].rename(columns={"price": "sell"})
    buys = opens[opens["side"] == "buy"][["code", "entry_date", "price"]].rename(columns={"price": "buy"})
    fills = sells.merge(buys, on=["code", "entry_date"])
    chk = entries.merge(fills, on=["code", "entry_date"], how="left")
    chk["credit_sim"] = chk["sell"] - chk["buy"]
    chk["diff"] = (chk["credit_est"] - chk["credit_sim"]).abs()
    s["credit_check"] = {"n_entries": len(entries), "n_filled": int(chk["credit_sim"].notna().sum()),
                         "max_abs_diff": float(chk["diff"].max()) if len(chk) else 0.0,
                         "n_diff_gt_1e-4": int((chk["diff"] > 1e-4).sum())}
    s["rejected_margin"] = int((trades["side"] == "rejected_margin").sum())
    # peak reserve estimate: sum over open groups of (width - credit) * 100, from entries x exit dates
    g = grouped.merge(entries[["code", "entry_date", "credit_est"]], on=["code", "entry_date"], how="left")
    ev = []
    for _, r in g.iterrows():
        res = (SPREAD_WIDTH - float(r["credit_est"])) * 100.0
        ev.append((r["entry_date"], res)); ev.append((r["exit_date"], -res))
    ev.sort()
    cur = peak = 0.0
    for _, d in ev:
        cur += d; peak = max(peak, cur)
    s["peak_reserve_usd"] = peak
    s["peak_positions"] = int(greeks["num_positions"].max()) // 2
    return s


def report() -> None:
    stats = {m: load_mode(m) for m in MODES}
    stats = {m: s for m, s in stats.items() if s is not None}
    if not stats:
        print("No completed runs found."); return

    rows = []
    for m, s in stats.items():
        i, o = s["IS"], s["OOS"]
        rows.append({"run": m, "N IS/OOS": f"{i['n']}/{o['n']}",
                     "win IS/OOS": f"{fmt_pct(i['win_rate'])}/{fmt_pct(o['win_rate'])}",
                     "$/trade IS/OOS": f"{fmt_num(i['per_trade'])}/{fmt_num(o['per_trade'])}",
                     "P&L$ IS/OOS": f"{i['total_pnl']:.0f}/{o['total_pnl']:.0f}",
                     "ret IS/OOS": f"{fmt_pct(i['total_return'])}/{fmt_pct(o['total_return'])}",
                     "DD% IS/OOS": f"{fmt_pct(i['max_dd'])}/{fmt_pct(o['max_dd'])}",
                     "DD$ IS/OOS": f"{fmt_num(i['max_dd_usd'], '.0f')}/{fmt_num(o['max_dd_usd'], '.0f')}",
                     "Sharpe IS/OOS": f"{fmt_num(i['sharpe'], '.3f')}/{fmt_num(o['sharpe'], '.3f')}",
                     "full N": len(s["grouped"]), "full ret": fmt_pct(s["full"].get("total_return")),
                     "full Sharpe": fmt_num(s["full"].get("sharpe"), ".3f")})
    print(f"\n{'=' * 130}\nSUMMARY  (1 contract/signal, initial_cash={INITIAL_CASH:,}; ret/DD% on that base, "
          f"NOT the live Heap-Allocator risk profile)\n{'=' * 130}")
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n-- integrity --")
    for m, s in stats.items():
        cc = s["credit_check"]
        print(f"{m:13s} rejected_margin={s['rejected_margin']}  peak_reserve=${s['peak_reserve_usd']:,.0f} "
              f"(peak concurrent spreads={s['peak_positions']}; 100k gate {'WOULD' if s['peak_reserve_usd'] > 100_000 else 'would not'} have bound)  "
              f"credit check: {cc['n_filled']}/{cc['n_entries']} filled, max|est-sim|={cc['max_abs_diff']:.2e}, "
              f"n>1e-4={cc['n_diff_gt_1e-4']}")
        print(f"{'':13s} simulator close-side counts: {s['trades'][s['trades']['side'].isin(CLOSE_SIDES)]['side'].value_counts().to_dict()}")

    for m in ("exit", "exit_reentry"):
        s = stats.get(m)
        if s is None:
            continue
        ex = s["exits"].copy()
        ex["entry_date"] = pd.to_datetime(ex["entry_date"])
        ex["window"] = np.where(ex["entry_date"] <= IS_END, "IS", "OOS")
        print(f"\n-- {m}: engine-emitted exit triggers --")
        print(pd.crosstab(ex["reason"], ex["window"], margins=True).to_string())
        # per-reason realised P&L (simulator side)
        g = s["grouped"].merge(s["exits"][["code", "entry_date", "reason"]], on=["code", "entry_date"], how="left")
        g["reason"] = g["reason"].fillna("HELD_TO_EXPIRY")
        g["entry_date"] = pd.to_datetime(g["entry_date"])
        g["window"] = np.where(g["entry_date"] <= IS_END, "IS", "OOS")
        agg = g.groupby(["reason", "window"])["spread_pnl"].agg(n="size", win=lambda x: (x > 0).mean(),
                                                                per_trade="mean", total="sum")
        print(f"\n-- {m}: realised spread P&L by exit reason (simulator side; exit_side shows how it settled) --")
        print(agg.round(2).to_string())
        print(pd.crosstab(g["reason"], g["exit_side"]).to_string())
        ba = ex[(ex["code"] == "BA") & (ex["entry_date"] == "2025-03-03")]
        print(f"\n-- {m}: BA 2025-03-03 mechanism check --")
        print(ba.to_string(index=False) if len(ba) else "  no BA 2025-03-03 entry in this run")
        ba_t = s["trades"][(s["trades"]["code"] == "BA") & (s["trades"]["entry_date"] == "2025-03-03")]
        print(ba_t.to_string(index=False) if len(ba_t) else "  (no BA rows in trades.csv)")

    if "hold" in stats and "exit" in stats:
        h, e = stats["hold"]["grouped"], stats["exit"]["grouped"]
        p = h.merge(e, on=["code", "entry_date"], suffixes=("_hold", "_exit"), how="outer", indicator=True)
        print(f"\n-- paired hold vs exit on identical entries: both={int((p['_merge'] == 'both').sum())} "
              f"hold_only={int((p['_merge'] == 'left_only').sum())} exit_only={int((p['_merge'] == 'right_only').sum())} --")
        p = p[p["_merge"] == "both"].copy()
        p["d"] = p["spread_pnl_exit"] - p["spread_pnl_hold"]
        p["entry_date"] = pd.to_datetime(p["entry_date"])
        for w, msk in [("IS", p["entry_date"] <= IS_END), ("OOS", p["entry_date"] >= OOS_START)]:
            q = p[msk]
            print(f"  [{w}] n={len(q)} improved={int((q.d > 0).sum())} worsened={int((q.d < 0).sum())} "
                  f"same={int((q.d == 0).sum())} mean_delta={q.d.mean():.2f} sum_delta={q.d.sum():.0f}")
        # Feb-Mar 2025 cluster
        fm = p[(p["entry_date"] >= "2025-02-01") & (p["entry_date"] <= "2025-03-31")]
        print(f"  Feb-Mar 2025 entries: n={len(fm)} hold_total={fm.spread_pnl_hold.sum():.0f} "
              f"exit_total={fm.spread_pnl_exit.sum():.0f}")

    flat = []
    for m, s in stats.items():
        r = {"run": m, "rejected_margin": s["rejected_margin"], "peak_reserve_usd": s["peak_reserve_usd"],
             **{f"credit_{k}": v for k, v in s["credit_check"].items()}}
        for w in ("IS", "OOS"):
            for k, v in s[w].items():
                r[f"{w}_{k}"] = v
        for k in ("total_return", "max_drawdown", "sharpe", "win_rate", "profit_loss_ratio"):
            r[f"full_{k}"] = s["full"].get(k)
        flat.append(r)
    pd.DataFrame(flat).to_csv(OUT_DIR / "summary.csv", index=False)
    print(f"\nWrote {OUT_DIR / 'summary.csv'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODES))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(exist_ok=True)
    if a.mode:
        run_mode(a.mode)
    if a.report:
        report()
    if not a.mode and not a.report:
        ap.error("pass --mode <hold|exit|exit_reentry> and/or --report")


if __name__ == "__main__":
    main()
