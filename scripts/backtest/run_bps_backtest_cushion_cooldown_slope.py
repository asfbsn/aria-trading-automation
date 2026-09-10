"""One-axis-at-a-time sweep: strike cushion, per-symbol cooldown, MA150 slope.

Non-production research artifact -- same footing as choke_point_test.py /
noise_floor_check.py. NOT wired into daily-scan.sh or any live path. Does not
import-modify signal_core.py or bps_signal_engine.py; the engine below is a
COPY of BullPutSpreadSignalEngine.generate() with three research knobs added.

Question: does any cheap structural tweak beat the locked 5-key gate's
expectancy OUT OF SAMPLE? Baseline ("current" tier, run_out/current):
718 trades, 47.43% win, +1.59% total return, -6.73% max DD, Sharpe 0.137,
$2.21/trade. The bar is the OOS window, not IS, not combined, not win rate.

Pre-registered kill criterion (written before any run, not adjusted after):
  a config PASSES only if, on the OOS window,
    (1) $/trade   > baseline OOS $/trade
    (2) Sharpe    > baseline OOS Sharpe
    (3) max-DD    not worse (>=) than baseline OOS max-DD
  and it is flagged UNRELIABLE if OOS N < 100 regardless of (1)-(3).
  "baseline" = the cushion=0.0 run of THIS script (same data pull, same
  engine copy), so the comparison is apples-to-apples even if yfinance's
  adjusted closes have drifted since run_out/current was produced.

IS/OOS split: IS 2023-07-26..2025-01-26, OOS 2025-01-27..2026-07-26. One full-
window backtest per config; trades are assigned to a window by entry_date,
Sharpe/max-DD are computed on the equity curve sliced to the window (same
formula as options_portfolio._calc_options_metrics, peak reset at window
start). Positions straddling the boundary are counted as IS trades but their
mark-to-market lands in the OOS equity slice -- identical treatment for every
config, so it does not bias the comparison.

Run order (one varying axis per stage, no grid):
  1. cushion_pct in {0.0, 0.02, 0.04}; C* = max OOS total return among PASSes
     (falls back to 0.0 with a notice if nothing passes).
  2. C* + 10-session per-symbol cooldown.
  3. C* + MA150 slope filter (ma150_now > ma150 20 bars ago).
  4. C* + cooldown + slope, ONLY if 2 and 3 both PASS individually.

Cooldown semantics: the spec said "within 10 sessions of that symbol's last
ENTRY". The engine already blocks re-entry on a symbol until the open spread's
expiry (open_until), and expiry is >= 30 calendar days (~21 sessions) after
entry, so a 10-session cooldown anchored at ENTRY is satisfied by construction
and would be a no-op (the engine counts these as `cooldown_entry_noop_blocks`
to prove it -- expect 0). The cooldown is therefore anchored at the last
spread's EXPIRY: no new entry until 10 sessions after the previous spread on
that symbol expired. Flagged for the reviewer.

Slope filter: needs closes[i-169 : i-19], so it cannot fire before bar 169
(MIN_BARS is 155). That removes ~2 weeks of March 2024 from the slope config's
IS window and nothing from OOS.

Step 0 (free diagnostic, printed before any variant runs): the baseline's
718 spread P&Ls joined to engine.entries by (code, entry_date), bucketed by
entry cushion (close-ma150)/ma150, strike distance (close-short)/close, and
price bucket -- per window.

Usage:
    cd scripts/backtest && source .venv/bin/activate
    python3 run_bps_backtest_cushion_cooldown_slope.py
Outputs: run_out_cushion_sweep/<config>/artifacts/*, summary.csv, stdout table.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent))

from options_portfolio import run_options_backtest  # noqa: E402
from run_bps_backtest import END, START, TICKERS, YFLoader  # noqa: E402
from signal_core import MIN_BARS, entry_checks  # noqa: E402

SPREAD_WIDTH = 10.0
TARGET_DTE = 30
INITIAL_CASH = 100_000
BARS_PER_YEAR = 252

IS_START, IS_END = "2023-07-26", "2025-01-26"
OOS_START, OOS_END = "2025-01-27", "2026-07-26"
MIN_RELIABLE_OOS_N = 100

CUSHION_GRID = [0.0, 0.02, 0.04]
COOLDOWN_SESSIONS = 10
SLOPE_LOOKBACK = 20

OUT_DIR = BASE / "run_out_cushion_sweep"

CONFIG = {
    "codes": TICKERS,
    "start_date": START,
    "end_date": END,
    "initial_cash": INITIAL_CASH,
    "commission": 0.0,
    "options_config": {
        "risk_free_rate": 0.045,
        "contract_multiplier": 100.0,
        "exercise_style": "american",
        "iv_skew": -0.15,
        "iv_curvature": 0.05,
    },
}


class CushionCooldownSlopeEngine:
    """Copy of bps_signal_engine.BullPutSpreadSignalEngine.generate() with
    cushion_pct / cooldown_sessions / slope_filter knobs. Entry rule itself
    is still the locked signal_core.entry_checks default gate."""

    def __init__(
        self,
        cushion_pct: float = 0.0,
        cooldown_sessions: int = 0,
        slope_filter: bool = False,
        width: float = SPREAD_WIDTH,
        target_dte: int = TARGET_DTE,
    ):
        self.cushion_pct = cushion_pct
        self.cooldown_sessions = cooldown_sessions
        self.slope_filter = slope_filter
        self.width = width
        self.target_dte = target_dte
        self.entries: List[Dict[str, Any]] = []
        self.cooldown_blocks = 0            # blocked by the expiry-anchored cooldown
        self.cooldown_entry_noop_blocks = 0  # would-be blocks under the entry-anchored reading
        self.slope_blocks = 0

    def generate(self, data_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        self.entries = []
        self.cooldown_blocks = 0
        self.cooldown_entry_noop_blocks = 0
        self.slope_blocks = 0
        min_bars = max(MIN_BARS, 149 + SLOPE_LOOKBACK) if self.slope_filter else MIN_BARS

        for code, df in data_map.items():
            df = df.sort_index()
            closes = df["close"].tolist()
            opens = df["open"].tolist() if "open" in df.columns else closes
            highs = df["high"].tolist() if "high" in df.columns else closes
            lows = df["low"].tolist() if "low" in df.columns else closes
            volumes = df["volume"].tolist() if "volume" in df.columns else [0] * len(closes)
            dates = list(df.index)

            open_until = None
            last_entry_i = None
            released_i = None  # first bar index past the previous spread's expiry

            for i in range(len(df)):
                if i < min_bars or i < 1 or i + 1 >= len(dates):
                    continue

                entry_ts = dates[i + 1]
                date_str = str(entry_ts.date())

                if open_until is not None:
                    if date_str <= open_until:
                        continue
                    open_until = None
                    released_i = i

                bars_slice = [
                    {"open": opens[j], "high": highs[j], "low": lows[j], "close": closes[j]}
                    for j in (i - 1, i)
                ]
                checks, entry_confirmed = entry_checks(closes[: i + 1], volumes[: i + 1], bars_slice)
                if not entry_confirmed:
                    continue

                # --- research knobs (evaluated only on confirmed signals) ---
                if self.cooldown_sessions > 0:
                    if last_entry_i is not None and i - last_entry_i < self.cooldown_sessions:
                        self.cooldown_entry_noop_blocks += 1  # diagnostic only
                    if released_i is not None and i - released_i < self.cooldown_sessions:
                        self.cooldown_blocks += 1
                        continue

                ma150 = sum(closes[i - 149: i + 1]) / 150.0
                if self.slope_filter:
                    ma150_prev = sum(closes[i - 149 - SLOPE_LOOKBACK: i + 1 - SLOPE_LOOKBACK]) / 150.0
                    if not ma150 > ma150_prev:
                        self.slope_blocks += 1
                        continue

                close = closes[i]
                short_strike = math.floor(ma150 * (1.0 - self.cushion_pct) / 5.0) * 5.0
                if short_strike >= close:
                    short_strike = math.floor((min(ma150, close) - 0.01) / 5.0) * 5.0
                long_strike = short_strike - self.width

                expiry_ts = entry_ts + pd.Timedelta(days=self.target_dte)
                expiry_ts += pd.Timedelta(days=(4 - expiry_ts.weekday()) % 7)
                expiry_str = str(expiry_ts.date())

                signals.append({
                    "date": date_str,
                    "action": "open",
                    "underlying": code,
                    "price_mode": "open",
                    "legs": [
                        {"type": "put", "strike": short_strike, "expiry": expiry_str, "qty": -1},
                        {"type": "put", "strike": long_strike, "expiry": expiry_str, "qty": 1},
                    ],
                    "group_id": f"{code}-{date_str}",
                })
                self.entries.append({
                    "code": code, "entry_date": date_str, "signal_close": close,
                    "ma150": ma150, "short_strike": short_strike,
                    "long_strike": long_strike, "expiry": expiry_str,
                })
                open_until = expiry_str
                last_entry_i = i

        return signals


# --- metrics -----------------------------------------------------------------

def spread_level_pnl(trades_csv: Path) -> pd.DataFrame:
    """Same grouping as run_bps_backtest.run_tier(): sum close-side legs per
    (code, entry_date)."""
    trades = pd.read_csv(trades_csv)
    closes_only = trades[trades["side"].isin(["expire", "exercise", "early_exercise"])]
    grouped = closes_only.groupby(["code", "entry_date"])["pnl"].sum().reset_index()
    return grouped.rename(columns={"pnl": "spread_pnl"})


def window_equity_metrics(equity_df: pd.DataFrame, start: str, end: str) -> Dict[str, float | None]:
    """CodeRabbit (2026-09-10) flagged that this slices the shared whole-
    period equity curve by date, so a position opened just before the
    window boundary still contributes its mark-to-market swing to the
    window it closes in, not the window it opened in -- the trade
    population here isn't exactly window_trade_stats' population (which
    filters by entry_date). Already disclosed in this file's module
    docstring ("Positions straddling the boundary...") as a known,
    deliberate simplification applied identically to every config, so
    verdict PASS/FAIL comparisons (all relative to the same-treatment
    baseline) aren't biased by it -- only the absolute Sharpe/max-DD
    figures carry this caveat, not the $/trade and win-rate numbers from
    window_trade_stats (those filter by entry_date directly, unaffected).
    Not rebuilding this into a from-scratch windowed equity curve right
    now: doing that correctly means picking a new annualization basis
    (trade-level spacing, not daily bars) and reconciling it against the
    "full" column's daily-Sharpe, which is real design work, not a
    same-session hotfix. Flagged for a follow-up pass, not silently
    ignored.
    """
    ts = pd.to_datetime(equity_df["timestamp"])
    eq = equity_df.loc[(ts >= start) & (ts <= end), "equity"].astype(float).reset_index(drop=True)
    out: Dict[str, float | None] = {"sharpe": None, "max_dd": None}
    if len(eq) < 2:
        return out
    returns = eq.pct_change(fill_method=None).iloc[1:]
    vol = float(returns.std())
    if np.isfinite(vol) and vol > 1e-12:
        out["sharpe"] = float(returns.mean() / vol * np.sqrt(BARS_PER_YEAR))
    peak = eq.cummax()
    if bool((peak > 0).all()):
        out["max_dd"] = float(((eq - peak) / peak).min())
    return out


def window_trade_stats(grouped: pd.DataFrame, start: str, end: str) -> Dict[str, float]:
    ed = pd.to_datetime(grouped["entry_date"])
    g = grouped[(ed >= start) & (ed <= end)]
    n = len(g)
    wins = int((g["spread_pnl"] > 0).sum())
    total = float(g["spread_pnl"].sum())
    return {
        "n": n,
        "win_rate": wins / n if n else float("nan"),
        "per_trade": total / n if n else float("nan"),
        "total_pnl": total,
        "total_return": total / INITIAL_CASH,
    }


def run_config(name: str, engine: CushionCooldownSlopeEngine, loader: YFLoader) -> Dict[str, Any]:
    print(f"\n{'=' * 80}\nRUNNING: {name}\n{'=' * 80}", flush=True)
    run_dir = OUT_DIR / name
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = run_options_backtest(CONFIG, loader, engine, run_dir)
    art = run_dir / "artifacts"
    grouped = spread_level_pnl(art / "trades.csv")
    equity_df = pd.read_csv(art / "equity.csv")
    entries = pd.DataFrame(engine.entries)
    entries.to_csv(art / "entries.csv", index=False)

    stats: Dict[str, Any] = {"name": name, "full": metrics, "grouped": grouped, "entries": entries}
    for label, (s, e) in {"IS": (IS_START, IS_END), "OOS": (OOS_START, OOS_END)}.items():
        stats[label] = {**window_trade_stats(grouped, s, e), **window_equity_metrics(equity_df, s, e)}
    stats["blocks"] = {
        "cooldown": engine.cooldown_blocks,
        "cooldown_entry_noop": engine.cooldown_entry_noop_blocks,
        "slope": engine.slope_blocks,
    }
    print(f"  full-window: N={len(grouped)} total_return={metrics.get('total_return')} "
          f"maxDD={metrics.get('max_drawdown')} sharpe={metrics.get('sharpe')}  blocks={stats['blocks']}")
    return stats


def evaluate(stats: Dict[str, Any], base: Dict[str, Any]) -> str:
    o, b = stats["OOS"], base["OOS"]
    if stats["name"] == base["name"]:
        return "BASELINE"
    reasons = []
    if not (o["per_trade"] > b["per_trade"]):
        reasons.append("$/trade")
    if o["sharpe"] is None or b["sharpe"] is None or not (o["sharpe"] > b["sharpe"]):
        reasons.append("sharpe")
    if o["max_dd"] is None or b["max_dd"] is None or not (o["max_dd"] >= b["max_dd"]):
        reasons.append("maxDD")
    verdict = "PASS" if not reasons else "FAIL(" + ",".join(reasons) + ")"
    if o["n"] < MIN_RELIABLE_OOS_N:
        # Prefix (not suffix) so this never satisfies a downstream
        # .startswith("PASS") check -- an unreliable-N config must not be
        # selectable as C* (line ~397) or count toward the stage-4 combined
        # gate (line ~418), even if its raw pass/fail reasons look clean.
        verdict = f"UNRELIABLE(OOS N={o['n']}<{MIN_RELIABLE_OOS_N}) " + verdict
    return verdict


# --- step 0 diagnostic ---------------------------------------------------------

def step0_diagnostic(stats: Dict[str, Any]) -> None:
    df = stats["entries"].merge(stats["grouped"], on=["code", "entry_date"], how="inner")
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["cushion"] = (df["signal_close"] - df["ma150"]) / df["ma150"]
    df["strike_dist"] = (df["signal_close"] - df["short_strike"]) / df["signal_close"]
    df["cushion_b"] = pd.cut(df["cushion"], [-1e-9, 0.02, 0.05, 0.10], labels=["0-2%", "2-5%", "5-10%"])
    df["strike_b"] = pd.cut(df["strike_dist"], [-1e-9, 0.02, 0.05, 0.10, 1.0],
                            labels=["<2%", "2-5%", "5-10%", ">10%"])
    df["price_b"] = pd.cut(df["signal_close"], [0, 100, 300, 1e9], labels=["<$100", "$100-300", ">$300"])
    df["window"] = np.where(df["entry_date"] <= IS_END, "IS", "OOS")

    print(f"\n{'=' * 80}\nSTEP 0 DIAGNOSTIC (baseline {stats['name']}: {len(df)} spreads joined "
          f"to entries)\n{'=' * 80}")
    for col, title in [("cushion_b", "entry cushion (close-ma150)/ma150"),
                       ("strike_b", "strike distance (close-short)/close"),
                       ("price_b", "signal close price bucket")]:
        print(f"\n-- by {title} --")
        rows = []
        for w in ["IS", "OOS", "ALL"]:
            sub = df if w == "ALL" else df[df["window"] == w]
            agg = sub.groupby(col, observed=False)["spread_pnl"].agg(
                n="size", win=lambda s: (s > 0).mean(), per_trade="mean", total="sum")
            for b, r in agg.iterrows():
                rows.append({"window": w, "bucket": b, "N": int(r["n"]),
                             "win%": f"{r['win']:.1%}" if r["n"] else "-",
                             "$/trade": f"{r['per_trade']:.2f}" if r["n"] else "-",
                             "total$": f"{r['total']:.0f}"})
        print(pd.DataFrame(rows).pivot(index="bucket", columns="window",
                                       values=["N", "win%", "$/trade", "total$"]).to_string())


# --- report --------------------------------------------------------------------

def fmt_pct(v):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.2%}"


def fmt_num(v, spec=".2f"):
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:{spec}}"


def report(all_stats: List[Dict[str, Any]], base: Dict[str, Any]) -> None:
    rows = []
    for s in all_stats:
        i, o = s["IS"], s["OOS"]
        rows.append({
            "config": s["name"],
            "IS N/OOS N": f"{i['n']}/{o['n']}",
            "IS win/OOS win": f"{fmt_pct(i['win_rate'])}/{fmt_pct(o['win_rate'])}",
            "IS $/t / OOS $/t": f"{fmt_num(i['per_trade'])}/{fmt_num(o['per_trade'])}",
            "IS ret/OOS ret": f"{fmt_pct(i['total_return'])}/{fmt_pct(o['total_return'])}",
            "IS DD/OOS DD": f"{fmt_pct(i['max_dd'])}/{fmt_pct(o['max_dd'])}",
            "IS Sh/OOS Sh": f"{fmt_num(i['sharpe'], '.3f')}/{fmt_num(o['sharpe'], '.3f')}",
            "full N": len(s["grouped"]),
            "full ret": fmt_pct(s["full"].get("total_return")),
            "full Sh": fmt_num(s["full"].get("sharpe"), ".3f"),
            "verdict": evaluate(s, base),
        })
    table = pd.DataFrame(rows)
    print(f"\n\n{'=' * 120}\nSUMMARY  (kill criterion: OOS $/trade > baseline AND OOS Sharpe > baseline "
          f"AND OOS maxDD >= baseline; OOS N<{MIN_RELIABLE_OOS_N} => UNRELIABLE)\n{'=' * 120}")
    print(table.to_string(index=False))
    flat = []
    for s in all_stats:
        r = {"config": s["name"], "verdict": evaluate(s, base), **s["blocks"]}
        for w in ("IS", "OOS"):
            for k, v in s[w].items():
                r[f"{w}_{k}"] = v
        for k in ("total_return", "max_drawdown", "sharpe", "win_rate", "profit_loss_ratio"):
            r[f"full_{k}"] = s["full"].get(k)
        flat.append(r)
    pd.DataFrame(flat).to_csv(OUT_DIR / "summary.csv", index=False)
    print(f"\nWrote {OUT_DIR / 'summary.csv'}")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    loader = YFLoader()
    all_stats: List[Dict[str, Any]] = []

    # Stage 1: cushion sweep (0.0 is the in-script baseline).
    cushion_stats: Dict[float, Dict[str, Any]] = {}
    for c in CUSHION_GRID:
        s = run_config(f"cushion_{c:.2f}", CushionCooldownSlopeEngine(cushion_pct=c), loader)
        cushion_stats[c] = s
        all_stats.append(s)
    base = cushion_stats[0.0]
    step0_diagnostic(base)

    passing = [(c, s) for c, s in cushion_stats.items()
               if c != 0.0 and evaluate(s, base).startswith("PASS")]
    if passing:
        c_star, _ = max(passing, key=lambda cs: cs[1]["OOS"]["total_return"])
        print(f"\nC* = {c_star:.2f} (max OOS total return among PASSing cushions)")
    else:
        c_star = 0.0
        print("\nNo cushion variant cleared the kill criterion -> C* = 0.00 (baseline); "
              "stages 2-3 run on the baseline strike rule.")

    # Stage 2: cooldown on C*.
    s_cool = run_config(f"cushion_{c_star:.2f}+cooldown{COOLDOWN_SESSIONS}",
                        CushionCooldownSlopeEngine(cushion_pct=c_star, cooldown_sessions=COOLDOWN_SESSIONS),
                        loader)
    all_stats.append(s_cool)

    # Stage 3: slope on C*.
    s_slope = run_config(f"cushion_{c_star:.2f}+slope{SLOPE_LOOKBACK}",
                         CushionCooldownSlopeEngine(cushion_pct=c_star, slope_filter=True), loader)
    all_stats.append(s_slope)

    # Stage 4: combined, only if both pass individually.
    if evaluate(s_cool, base).startswith("PASS") and evaluate(s_slope, base).startswith("PASS"):
        s_both = run_config(f"cushion_{c_star:.2f}+cooldown{COOLDOWN_SESSIONS}+slope{SLOPE_LOOKBACK}",
                            CushionCooldownSlopeEngine(cushion_pct=c_star, cooldown_sessions=COOLDOWN_SESSIONS,
                                                       slope_filter=True), loader)
        all_stats.append(s_both)
    else:
        print("\nStage 4 (combined) skipped: cooldown and slope did not BOTH pass individually.")

    report(all_stats, base)


if __name__ == "__main__":
    main()
