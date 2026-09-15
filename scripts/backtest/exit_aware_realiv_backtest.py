"""Exit-aware bull-put-spread backtest re-priced with REAL market implied vol.

Non-production research artifact -- same footing as
exit_aware_full_universe_backtest.py (the "baseline"), which it imports and
does NOT modify. options_portfolio.py is not modified either.

Why: options_portfolio.run_options_backtest builds its iv_map as
historical_volatility(df["close"]) -- every credit and every daily mark in
the baseline was priced off 30-day realised vol, not implied vol. This script
substitutes DoltHub post-no-preference/options.volatility_history.iv_current
(pulled by dolthub_iv_pull.py) as the base ATM vol fed into the same smile
model, at both call sites (the simulator's iv_map and the engine copy's
credit / cost-to-close), via one injection point: both modules resolve
`historical_volatility` through their module globals, so it is replaced
there with a function that reads the ticker from the Series' .attrs (set by
the loader; pandas 3 propagates DataFrame.attrs to df["close"]).

Four runs, all on the SAME covered sub-universe so pricing and universe
effects are separable (entry signals are price-only, so hv_* and iv_* runs
open the identical (code, entry_date) set):
    hv_hold / hv_exit   baseline pricing (HV30 as IV) on the covered subset
    iv_hold / iv_exit   real DoltHub IV on the covered subset
  baseline-751 (run_out_exit_aware_full) vs hv_*  = universe-shrinkage effect
  hv_* vs iv_*                                    = pricing effect

Coverage / gap policy (measured 2026-09-11 on the first 88 pulled symbols):
  DoltHub skips ~10.8% of trading days -- almost all Tuesdays/Thursdays, a
  collection cadence -- with a max consecutive gap of 9 trading days for
  normally-covered names; a few names (MRVL, DELL, PLTR, CRWD, SNDK, GEV in
  that sample) have 55..584-day holes or only recent rows.
  * A ticker is INCLUDED iff non-null iv_current exists on >= COVER_MIN of
    the OHLCV trading days in the pricing window (2024-03-01..2026-07-24,
    first possible signal is 2024-03-07 because MIN_BARS=155 from 2023-07-26)
    AND its longest run of missing trading days is <= MAX_GAP_DAYS.
  * Within an included ticker, IV is forward-filled across gaps (last real
    value); trading days before the first IV row are back-filled from it.
    Every (code, date) carries a source flag (exact / ffill / bfill) and the
    report counts entries whose fill-day IV was not an exact observation.
  * Excluded tickers are dropped from ALL FOUR runs (never mixed HV/IV
    per-day -- a level switch mid-trade would fire spurious PROFIT_TARGET
    closes). The report lists them and how many baseline trades they carried.

Known limitations, stated not smoothed:
  * iv_current is one scalar per symbol-day of undocumented tenor and
    construction. It is used exactly where HV30 was: as the base ATM vol
    into iv_smile_adjustment(skew=-0.15, curvature=0.05). No term structure.
  * The entry fill at T+1 open is priced with date-T+1 IV (an end-of-day
    snapshot). That is the same lookahead shape the HV baseline has (HV
    through T+1 close); kept for parity.
  * DoltHub's row for date D is taken to be D's end-of-day observation.

Usage (each run is one process; run them in parallel, then report):
    python3 exit_aware_realiv_backtest.py --coverage          # writes covered_universe.csv
    python3 exit_aware_realiv_backtest.py --run hv_hold       # etc. for the 4 runs
    python3 exit_aware_realiv_backtest.py --report
Outputs: run_out_exit_aware_realiv/<run>/artifacts/*, entries.csv, exits.csv,
entries_iv.csv (per-entry fill-day IV/HV/source), covered_universe.csv,
summary.csv, comparison printed to stdout.
"""
from __future__ import annotations

import argparse
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

import exit_aware_full_universe_backtest as base  # noqa: E402
import options_portfolio  # noqa: E402

IV_CACHE = BASE / "run_out_dolthub_iv" / "volatility_history.pkl"
OUT_DIR = BASE / "run_out_exit_aware_realiv"
BASELINE_DIR = base.OUT_DIR  # run_out_exit_aware_full (751 tickers, HV pricing)

PRICING_WINDOW = ("2024-03-01", "2026-07-24")
COVER_MIN = 0.85
MAX_GAP_DAYS = 10

RUNS = {
    "hv_hold": dict(iv_source="hv", exit_aware=False, reentry=False),
    "hv_exit": dict(iv_source="hv", exit_aware=True, reentry=False),
    "iv_hold": dict(iv_source="iv", exit_aware=False, reentry=False),
    "iv_exit": dict(iv_source="iv", exit_aware=True, reentry=False),
}

_ORIG_HV = options_portfolio.historical_volatility
IV_SERIES: Dict[str, pd.Series] = {}
IV_SOURCE: Dict[str, pd.Series] = {}
ACTIVE_SOURCE = "hv"


# --- coverage -----------------------------------------------------------------

def load_iv_cache() -> Dict[str, pd.DataFrame]:
    if not IV_CACHE.exists():
        sys.exit(f"IV cache missing: {IV_CACHE} -- run dolthub_iv_pull.py first")
    with open(IV_CACHE, "rb") as f:
        return pickle.load(f)


def load_ohlcv_index() -> Dict[str, pd.DatetimeIndex]:
    """Trading-day index per code from the same OHLCV cache the runs use
    (the 3 cache-missing names NVDA/BRKB/BFB come through the loader's
    yfinance pull; for coverage we approximate their calendar with SPY's
    if present, else AAPL's)."""
    with open(base.CACHE, "rb") as f:
        raw = pickle.load(f)
    out = {}
    for code, df in raw.items():
        idx = pd.to_datetime(df[["open", "high", "low", "close", "volume"]].dropna().index)
        out[code] = idx.sort_values()
    ref = out.get("SPY", out.get("AAPL"))
    for code in base.load_universe():
        if code not in out:
            out[code] = ref
    return out


def coverage_table(iv_cache: Dict[str, pd.DataFrame], calendars: Dict[str, pd.DatetimeIndex]) -> pd.DataFrame:
    rows = []
    a, b = PRICING_WINDOW
    for code in base.load_universe():
        cal = calendars.get(code)
        tdays = cal[(cal >= a) & (cal <= b)] if cal is not None else pd.DatetimeIndex([])
        rec: Dict[str, Any] = {"code": code, "trading_days": len(tdays), "iv_rows": 0, "iv_nonnull": 0,
                               "cover_frac": 0.0, "max_gap": len(tdays), "first_iv": None, "last_iv": None,
                               "iv_median": float("nan"), "iv_min": float("nan"), "iv_max": float("nan")}
        df = iv_cache.get(code)
        if df is not None and len(df):
            iv = df["iv_current"]
            iv = iv[(iv > 0)]  # non-positive IV is treated as missing
            rec["iv_rows"] = len(df)
            rec["iv_nonnull"] = int(iv.notna().sum())
            have = iv.dropna().index
            present = tdays.isin(have)
            rec["cover_frac"] = float(present.mean()) if len(tdays) else 0.0
            runs, c = [], 0
            for p in present:
                if p:
                    if c:
                        runs.append(c)
                    c = 0
                else:
                    c += 1
            if c:
                runs.append(c)
            rec["max_gap"] = max(runs) if runs else 0
            if len(have):
                rec["first_iv"] = str(have.min().date()); rec["last_iv"] = str(have.max().date())
                rec["iv_median"] = float(iv.median()); rec["iv_min"] = float(iv.min()); rec["iv_max"] = float(iv.max())
        rec["included"] = bool(rec["cover_frac"] >= COVER_MIN and rec["max_gap"] <= MAX_GAP_DAYS)
        rows.append(rec)
    return pd.DataFrame(rows)


def write_coverage() -> pd.DataFrame:
    OUT_DIR.mkdir(exist_ok=True)
    cov = coverage_table(load_iv_cache(), load_ohlcv_index())
    cov.to_csv(OUT_DIR / "covered_universe.csv", index=False)
    inc = cov[cov["included"]]
    print(f"universe {len(cov)}: included {len(inc)}, excluded {len(cov) - len(inc)} "
          f"(no IV rows: {int((cov['iv_rows'] == 0).sum())}, cover<{COVER_MIN}: "
          f"{int(((cov['iv_rows'] > 0) & (cov['cover_frac'] < COVER_MIN)).sum())}, "
          f"gap>{MAX_GAP_DAYS} only: {int(((cov['cover_frac'] >= COVER_MIN) & (cov['max_gap'] > MAX_GAP_DAYS)).sum())})")
    print(f"included: cover_frac min/median {inc['cover_frac'].min():.3f}/{inc['cover_frac'].median():.3f}, "
          f"max_gap max {inc['max_gap'].max()}, iv_median across names median {inc['iv_median'].median():.3f}, "
          f"iv_max overall {inc['iv_max'].max():.2f}, iv_min overall {inc['iv_min'].min():.3f}")
    return cov


def covered_codes() -> List[str]:
    p = OUT_DIR / "covered_universe.csv"
    cov = pd.read_csv(p) if p.exists() else write_coverage()
    return cov[cov["included"]]["code"].tolist()


# --- IV injection -------------------------------------------------------------

def build_iv_series(code: str, df: pd.DataFrame, iv_cache: Dict[str, pd.DataFrame]) -> None:
    """Align DoltHub iv_current to df.index (exact / ffill / bfill) and cache it."""
    iv = iv_cache[code]["iv_current"]
    iv = iv[iv > 0].dropna()
    idx = df.index
    exact = iv.reindex(idx)
    ff = iv.reindex(idx, method="ffill")
    bf = iv.reindex(idx, method="bfill")
    src = pd.Series(np.where(exact.notna(), "exact", np.where(ff.notna(), "ffill", "bfill")), index=idx)
    out = exact.fillna(ff).fillna(bf)
    if out.isna().any():
        raise RuntimeError(f"{code}: {int(out.isna().sum())} unfillable IV days")
    IV_SERIES[code] = out.astype(float)
    IV_SOURCE[code] = src


def patched_historical_volatility(close: pd.Series, window: int = 30) -> pd.Series:
    """Drop-in for options_portfolio.historical_volatility. In 'iv' mode it
    returns the real-IV series for close.attrs['code']; in 'hv' mode the original."""
    if ACTIVE_SOURCE == "hv":
        return _ORIG_HV(close, window)
    code = close.attrs.get("code")
    if code is None or code not in IV_SERIES:
        raise RuntimeError(f"real-IV lookup failed for code={code!r} (attrs={dict(close.attrs)})")
    s = IV_SERIES[code]
    if not s.index.equals(close.index):
        raise RuntimeError(f"{code}: IV series index != close index")
    return s


class CoveredCacheLoader(base.CacheLoader):
    """Baseline loader restricted to the covered subset; tags each frame with
    its code (attrs) and builds the aligned IV series."""

    def __init__(self, iv_cache: Dict[str, pd.DataFrame] | None):
        super().__init__()
        self.iv_cache = iv_cache

    def fetch(self, codes, start, end):
        data_map = super().fetch(codes, start, end)
        for code, df in data_map.items():
            df.attrs["code"] = code
            if self.iv_cache is not None:
                build_iv_series(code, df, self.iv_cache)
        return data_map


# --- run one configuration ----------------------------------------------------

def run_one(run: str) -> None:
    global ACTIVE_SOURCE
    flags = RUNS[run]
    ACTIVE_SOURCE = flags["iv_source"]
    options_portfolio.historical_volatility = patched_historical_volatility
    base.historical_volatility = patched_historical_volatility

    codes = covered_codes()
    run_dir = OUT_DIR / run
    run_dir.mkdir(parents=True, exist_ok=True)
    iv_cache = load_iv_cache() if ACTIVE_SOURCE == "iv" else None
    loader = CoveredCacheLoader(iv_cache)
    engine = base.ExitAwareBPSEngine(exit_aware=flags["exit_aware"], reentry=flags["reentry"])
    config = {"codes": codes, "start_date": base.START, "end_date": base.END, "initial_cash": base.INITIAL_CASH,
              "commission": 0.0, "options_config": dict(base.OPTIONS_CONFIG)}
    print(f"RUNNING {run} {flags} on {len(codes)} covered tickers", flush=True)
    metrics = base.run_options_backtest(config, loader, engine, run_dir)
    art = run_dir / "artifacts"
    pd.DataFrame(engine.entries).to_csv(art / "entries.csv", index=False)
    pd.DataFrame(engine.exits, columns=["code", "entry_date", "trigger_date", "exit_fill_date", "reason",
                                        "captured_at_trigger", "close_at_trigger", "dte_at_trigger",
                                        "expiry"]).to_csv(art / "exits.csv", index=False)
    # per-entry vol actually used on the fill day (entry_date == fill day), plus what the other source would be
    data_map = loader.fetch(codes, base.START, base.END)
    recs = []
    for e in engine.entries:
        code, d = e["code"], pd.Timestamp(e["entry_date"])
        df = data_map[code]
        hv = float(_ORIG_HV(df["close"]).at[d]) if d in df.index else float("nan")
        iv = float(IV_SERIES[code].at[d]) if code in IV_SERIES and d in IV_SERIES[code].index else float("nan")
        src = IV_SOURCE[code].at[d] if code in IV_SOURCE and d in IV_SOURCE[code].index else "n/a"
        recs.append({"code": code, "entry_date": e["entry_date"], "vol_used": iv if ACTIVE_SOURCE == "iv" else hv,
                     "hv30": hv, "iv_real": iv, "iv_source": src, "credit_est": e["credit_est"]})
    pd.DataFrame(recs).to_csv(art / "entries_iv.csv", index=False)
    with open(run_dir / "metrics_full.json", "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"DONE {run}: entries={len(engine.entries)} exits_emitted={len(engine.exits)} "
          f"total_return={metrics.get('total_return')} sharpe={metrics.get('sharpe')}", flush=True)


# --- report -------------------------------------------------------------------

def paired(h: pd.DataFrame, e: pd.DataFrame) -> pd.DataFrame:
    p = h.merge(e, on=["code", "entry_date"], suffixes=("_hold", "_exit"), how="inner")
    p["d"] = p["spread_pnl_exit"] - p["spread_pnl_hold"]
    p["entry_date"] = pd.to_datetime(p["entry_date"])
    return p


def tstat(x: pd.Series) -> float:
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))) if len(x) > 1 and x.std(ddof=1) > 0 else float("nan")


def exit_effect_block(label: str, h: pd.DataFrame, e: pd.DataFrame) -> Dict[str, Any]:
    p = paired(h, e)
    oos = p[p["entry_date"] >= base.OOS_START]
    is_ = p[p["entry_date"] <= base.IS_END]
    crash_mask = (oos["entry_date"] >= "2025-02-01") & (oos["entry_date"] <= "2025-03-31")
    crash, ex = oos[crash_mask], oos[~crash_mask]
    r = {"label": label, "n_pairs": len(p),
         "IS_n": len(is_), "IS_sum_delta": is_["d"].sum(), "IS_t": tstat(is_["d"]),
         "OOS_n": len(oos), "OOS_sum_delta": oos["d"].sum(), "OOS_mean_delta": oos["d"].mean(), "OOS_t": tstat(oos["d"]),
         "FebMar25_n": len(crash), "FebMar25_hold": crash["spread_pnl_hold"].sum(),
         "FebMar25_exit": crash["spread_pnl_exit"].sum(), "FebMar25_delta": crash["d"].sum(),
         "OOS_excrash_n": len(ex), "OOS_excrash_delta": ex["d"].sum(), "OOS_excrash_t": tstat(ex["d"])}
    print(f"\n-- exit effect, {label}: paired hold vs exit on identical entries (n={len(p)}) --")
    print(f"  [IS]  n={len(is_)} sum_delta={r['IS_sum_delta']:.0f} t={r['IS_t']:.2f}")
    print(f"  [OOS] n={len(oos)} sum_delta={r['OOS_sum_delta']:.0f} mean={r['OOS_mean_delta']:.2f} t={r['OOS_t']:.2f}  "
          f"improved={int((oos.d > 0).sum())} worsened={int((oos.d < 0).sum())} same={int((oos.d == 0).sum())}")
    print(f"  Feb-Mar 2025 entries: n={len(crash)} hold={r['FebMar25_hold']:.0f} exit={r['FebMar25_exit']:.0f} "
          f"delta={r['FebMar25_delta']:.0f}")
    print(f"  OOS ex-crash: n={len(ex)} delta={r['OOS_excrash_delta']:.0f} t={r['OOS_excrash_t']:.2f}")
    return r


def load_run(out_dir: Path, run: str) -> Dict[str, Any] | None:
    saved = base.OUT_DIR
    base.OUT_DIR = out_dir
    try:
        s = base.load_mode(run)
    finally:
        base.OUT_DIR = saved
    if s is None:
        return None
    p = out_dir / run / "artifacts" / "entries_iv.csv"
    s["entries_iv"] = pd.read_csv(p) if p.exists() else None
    return s


def summary_row(name: str, s: Dict[str, Any]) -> Dict[str, Any]:
    i, o = s["IS"], s["OOS"]
    f = s["full"]
    return {"run": name, "N IS/OOS": f"{i['n']}/{o['n']}",
            "win IS/OOS": f"{base.fmt_pct(i['win_rate'])}/{base.fmt_pct(o['win_rate'])}",
            "$/trade IS/OOS": f"{base.fmt_num(i['per_trade'])}/{base.fmt_num(o['per_trade'])}",
            "P&L$ IS/OOS": f"{i['total_pnl']:.0f}/{o['total_pnl']:.0f}",
            "ret IS/OOS": f"{base.fmt_pct(i['total_return'])}/{base.fmt_pct(o['total_return'])}",
            "DD% IS/OOS": f"{base.fmt_pct(i['max_dd'])}/{base.fmt_pct(o['max_dd'])}",
            "Sharpe IS/OOS": f"{base.fmt_num(i['sharpe'], '.3f')}/{base.fmt_num(o['sharpe'], '.3f')}",
            "full N": len(s["grouped"]), "full win": base.fmt_pct(f.get("win_rate")),
            "full ret": base.fmt_pct(f.get("total_return")), "full DD": base.fmt_pct(f.get("max_drawdown")),
            "full Sharpe": base.fmt_num(f.get("sharpe"), ".3f")}


def data_quality_block(cov: pd.DataFrame, runs: Dict[str, Any]) -> None:
    """Checks that decide whether the real-IV numbers can be trusted:
    (a) cover_frac distribution vs the COVER_MIN threshold (is inclusion bimodal
        or arbitrary?), (b) DoltHub observation cadence by month and in the
        Feb-Mar 2025 crash window (how often the daily exit-trigger mark used a
        forward-filled, stale IV), (c) DoltHub hv_current vs the engine's HV30
        (the '0.999 correlation' claim, re-verified here), (d) IV source on the
        exit-trigger days actually emitted by iv_exit."""
    iv_cache = load_iv_cache()
    calendars = load_ohlcv_index()
    inc = cov[cov["included"]]["code"].tolist()
    a, b = PRICING_WINDOW

    print(f"\n-- (a) cover_frac distribution over the pricing window, tickers with any IV rows (COVER_MIN={COVER_MIN}) --")
    has = cov[cov["iv_rows"] > 0]
    bins = [0, 0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 1.001]
    print(pd.cut(has["cover_frac"], bins, right=False).value_counts().sort_index().to_string())
    print(f"  max_gap among tickers with cover>={COVER_MIN}: "
          f"{has[has['cover_frac'] >= COVER_MIN]['max_gap'].describe()[['50%', 'max']].round(0).to_dict()}")

    pres_all = []
    for code in inc:
        cal = calendars[code]
        cal = cal[(cal >= a) & (cal <= b)]
        iv = iv_cache[code]["iv_current"]
        iv = iv[iv > 0].dropna()
        pres_all.append(pd.Series(cal.isin(iv.index), index=cal))
    pres = pd.concat(pres_all)
    monthly = pres.groupby(pres.index.to_period("M")).mean()
    print(f"\n-- (b) DoltHub observation cadence: share of OHLCV trading days with an EXACT iv_current, pooled over "
          f"{len(inc)} included tickers --\n  overall {pres.mean():.3f}; by month:")
    print("  " + "  ".join(f"{str(k)}:{v:.2f}" for k, v in monthly.items()))
    for label, lo, hi in [("IS part of window", a, base.IS_END), ("OOS", base.OOS_START, b),
                          ("Feb-Mar 2025 crash window", "2025-02-01", "2025-03-31"),
                          ("Apr 2025 (tariff shock)", "2025-04-01", "2025-04-30")]:
        m = pres[(pres.index >= lo) & (pres.index <= hi)]
        print(f"  {label:28s}: exact on {m.mean():.3f} of trading days (n={len(m)})")

    print("\n-- (c) DoltHub hv_current vs engine historical_volatility(close, 30) on the same (ticker, date) --")
    with open(base.CACHE, "rb") as f:
        raw = pickle.load(f)
    pairs, per_code = [], []
    for code in inc:
        df = raw.get(code)
        if df is None:
            continue
        df = df[["open", "high", "low", "close", "volume"]].dropna().copy()
        df.index = pd.to_datetime(df.index)
        hv30 = _ORIG_HV(df["close"].sort_index()).dropna()
        d = iv_cache[code]["hv_current"].dropna()
        j = pd.concat([hv30.rename("hv30"), d.rename("dolt_hv")], axis=1, join="inner")
        j = j[(j.index >= a) & (j.index <= b)]
        if len(j) > 30:
            pairs.append(j)
            per_code.append(j["hv30"].corr(j["dolt_hv"]))
    if pairs:
        allp = pd.concat(pairs)
        ratio = allp["dolt_hv"] / allp["hv30"]
        print(f"  n={len(allp)} pairs over {len(per_code)} tickers: pooled corr={allp['hv30'].corr(allp['dolt_hv']):.4f}, "
              f"per-ticker corr median={float(np.median(per_code)):.4f} min={float(np.min(per_code)):.4f}; "
              f"ratio dolt_hv/hv30 median={ratio.median():.3f} p5={ratio.quantile(.05):.3f} p95={ratio.quantile(.95):.3f}; "
              f"mean abs diff={((allp['dolt_hv'] - allp['hv30']).abs()).mean():.4f}")

    s = runs.get("iv_exit")
    if s is not None and len(s["exits"]):
        ex = s["exits"].copy()
        ex["trigger_date"] = pd.to_datetime(ex["trigger_date"])
        src = []
        for _, r in ex.iterrows():
            iv = iv_cache[r["code"]]["iv_current"]
            src.append("exact" if (r["trigger_date"] in iv.index and float(iv.at[r["trigger_date"]]) > 0) else "ffill")
        ex["iv_src"] = src
        ex["crash"] = (ex["trigger_date"] >= "2025-02-01") & (ex["trigger_date"] <= "2025-03-31")
        print("\n-- (d) iv_exit: IV source on the trigger day of each emitted exit --")
        print(pd.crosstab([ex["crash"].map({True: "Feb-Mar25", False: "other"}), ex["reason"]], ex["iv_src"],
                          margins=True).to_string())


def report() -> None:
    cov = pd.read_csv(OUT_DIR / "covered_universe.csv")
    runs = {r: load_run(OUT_DIR, r) for r in RUNS}
    runs = {r: s for r, s in runs.items() if s is not None}
    basel = {m: load_run(BASELINE_DIR, m) for m in ("hold", "exit")}
    basel = {m: s for m, s in basel.items() if s is not None}
    if not runs:
        print("No completed runs found."); return

    excluded = cov[~cov["included"]]
    print(f"\n{'=' * 130}\nCOVERAGE  universe={len(cov)} included={int(cov['included'].sum())} "
          f"excluded={len(excluded)}  (COVER_MIN={COVER_MIN}, MAX_GAP_DAYS={MAX_GAP_DAYS}, window={PRICING_WINDOW})\n{'=' * 130}")
    if "hold" in basel:
        bg = basel["hold"]["grouped"]
        n_excl_trades = int(bg["code"].isin(excluded["code"]).sum())
        print(f"baseline-751 hold trades on excluded tickers: {n_excl_trades} of {len(bg)} "
              f"({n_excl_trades / len(bg):.1%}); excluded-ticker baseline P&L (hold): "
              f"{bg[bg['code'].isin(excluded['code'])]['spread_pnl'].sum():.0f}")
    print("excluded (reason):")
    for _, r in excluded.iterrows():
        why = "no IV rows" if r["iv_rows"] == 0 else (f"cover {r['cover_frac']:.2f}" if r["cover_frac"] < COVER_MIN
                                                      else f"max_gap {int(r['max_gap'])}")
        print(f"  {r['code']:6s} {why}")
    data_quality_block(cov, runs)

    rows = [summary_row(f"baseline751_{m}", s) for m, s in basel.items()] + [summary_row(r, s) for r, s in runs.items()]
    print(f"\n{'=' * 130}\nSUMMARY  (1 contract/signal, initial_cash={base.INITIAL_CASH:,}; ret/DD% on that base)\n{'=' * 130}")
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n-- integrity --")
    for r, s in runs.items():
        cc = s["credit_check"]
        print(f"{r:9s} rejected_margin={s['rejected_margin']}  peak_reserve=${s['peak_reserve_usd']:,.0f} "
              f"(peak concurrent spreads={s['peak_positions']})  credit check: {cc['n_filled']}/{cc['n_entries']} filled, "
              f"max|est-sim|={cc['max_abs_diff']:.2e}, n>1e-4={cc['n_diff_gt_1e-4']}  "
              f"close-side counts: {s['trades'][s['trades']['side'].isin(base.CLOSE_SIDES)]['side'].value_counts().to_dict()}")

    # --- positive verification that the injection took ---
    if "hv_hold" in runs and "iv_hold" in runs:
        a = runs["hv_hold"]["entries"][["code", "entry_date", "credit_est"]]
        b = runs["iv_hold"]["entries"][["code", "entry_date", "credit_est"]]
        m = a.merge(b, on=["code", "entry_date"], suffixes=("_hv", "_iv"), how="outer", indicator=True)
        same_set = (m["_merge"] == "both").all()
        m = m[m["_merge"] == "both"]
        diff = (m["credit_est_iv"] - m["credit_est_hv"])
        ei = runs["iv_hold"]["entries_iv"]
        print(f"\n-- injection check --\n  identical entry set hv vs iv: {same_set} (n={len(m)})")
        print(f"  credit_est differs (|d|>1e-6) on {int((diff.abs() > 1e-6).sum())}/{len(m)} entries; "
              f"mean credit hv={m['credit_est_hv'].mean():.3f} iv={m['credit_est_iv'].mean():.3f} "
              f"median ratio iv/hv={(m['credit_est_iv'] / m['credit_est_hv']).median():.3f}")
        if ei is not None:
            print(f"  fill-day vol used == 0.3 default on {int((ei['vol_used'] == 0.3).sum())} entries; "
                  f"vol_used == iv_real on {int(np.isclose(ei['vol_used'], ei['iv_real']).sum())}/{len(ei)}")
            print(f"  fill-day IV source: {ei['iv_source'].value_counts().to_dict()}")
            print(f"  fill-day IV vs HV30: median iv={ei['iv_real'].median():.3f} hv={ei['hv30'].median():.3f} "
                  f"median iv/hv={(ei['iv_real'] / ei['hv30']).median():.3f}; iv>hv on {(ei['iv_real'] > ei['hv30']).mean():.1%}")
    # hv_* on the subset must reproduce baseline-751 on the same tickers exactly
    if "hv_hold" in runs and "hold" in basel:
        bh = basel["hold"]["grouped"]; sh = runs["hv_hold"]["grouped"]
        inc = set(cov[cov["included"]]["code"])
        bh = bh[bh["code"].isin(inc)]
        m = bh.merge(sh, on=["code", "entry_date"], how="outer", suffixes=("_b", "_s"), indicator=True)
        both = m[m["_merge"] == "both"]
        print(f"\n-- hv_hold vs baseline-751 hold restricted to included tickers --\n  entry set: both={len(both)} "
              f"baseline_only={int((m['_merge'] == 'left_only').sum())} subset_only={int((m['_merge'] == 'right_only').sum())}; "
              f"max |pnl diff| on shared = {(both['spread_pnl_b'] - both['spread_pnl_s']).abs().max():.4f}")

    # --- exit effect under each pricing ---
    eff = []
    if "hold" in basel and "exit" in basel:
        eff.append(exit_effect_block("baseline-751 HV pricing", basel["hold"]["grouped"], basel["exit"]["grouped"]))
    if "hv_hold" in runs and "hv_exit" in runs:
        eff.append(exit_effect_block("covered subset, HV pricing", runs["hv_hold"]["grouped"], runs["hv_exit"]["grouped"]))
    if "iv_hold" in runs and "iv_exit" in runs:
        eff.append(exit_effect_block("covered subset, REAL IV pricing", runs["iv_hold"]["grouped"], runs["iv_exit"]["grouped"]))

    # --- pricing effect: same entries, HV vs IV, per mode ---
    for mode in ("hold", "exit"):
        a, b = runs.get(f"hv_{mode}"), runs.get(f"iv_{mode}")
        if a is None or b is None:
            continue
        p = a["grouped"].merge(b["grouped"], on=["code", "entry_date"], suffixes=("_hv", "_iv"))
        p["d"] = p["spread_pnl_iv"] - p["spread_pnl_hv"]
        p["entry_date"] = pd.to_datetime(p["entry_date"])
        print(f"\n-- pricing effect, {mode} mode: real IV minus HV on identical entries (n={len(p)}) --")
        for w, msk in [("IS", p["entry_date"] <= base.IS_END), ("OOS", p["entry_date"] >= base.OOS_START)]:
            q = p[msk]
            print(f"  [{w}] n={len(q)} hv_total={q.spread_pnl_hv.sum():.0f} iv_total={q.spread_pnl_iv.sum():.0f} "
                  f"delta={q.d.sum():.0f} t={tstat(q.d):.2f} win_hv={(q.spread_pnl_hv > 0).mean():.1%} "
                  f"win_iv={(q.spread_pnl_iv > 0).mean():.1%}")

    for r in ("hv_exit", "iv_exit"):
        s = runs.get(r)
        if s is None:
            continue
        ex = s["exits"].copy()
        ex["entry_date"] = pd.to_datetime(ex["entry_date"])
        ex["window"] = np.where(ex["entry_date"] <= base.IS_END, "IS", "OOS")
        print(f"\n-- {r}: engine-emitted exit triggers --")
        print(pd.crosstab(ex["reason"], ex["window"], margins=True).to_string())
        g = s["grouped"].merge(s["exits"][["code", "entry_date", "reason"]], on=["code", "entry_date"], how="left")
        g["reason"] = g["reason"].fillna("HELD_TO_EXPIRY")
        g["entry_date"] = pd.to_datetime(g["entry_date"])
        g["window"] = np.where(g["entry_date"] <= base.IS_END, "IS", "OOS")
        agg = g.groupby(["reason", "window"])["spread_pnl"].agg(n="size", win=lambda x: (x > 0).mean(),
                                                                per_trade="mean", total="sum")
        print(agg.round(2).to_string())

    flat = []
    for name, s in list(basel.items()) + list(runs.items()):
        label = f"baseline751_{name}" if name in ("hold", "exit") else name
        rr = {"run": label, "rejected_margin": s["rejected_margin"], "peak_reserve_usd": s["peak_reserve_usd"],
              **{f"credit_{k}": v for k, v in s["credit_check"].items()}}
        for w in ("IS", "OOS"):
            for k, v in s[w].items():
                rr[f"{w}_{k}"] = v
        for k in ("total_return", "max_drawdown", "sharpe", "win_rate", "profit_loss_ratio"):
            rr[f"full_{k}"] = s["full"].get(k)
        flat.append(rr)
    pd.DataFrame(flat).to_csv(OUT_DIR / "summary.csv", index=False)
    pd.DataFrame(eff).to_csv(OUT_DIR / "exit_effect.csv", index=False)
    print(f"\nWrote {OUT_DIR / 'summary.csv'} and {OUT_DIR / 'exit_effect.csv'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--run", choices=list(RUNS))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(exist_ok=True)
    if a.coverage:
        write_coverage()
    if a.run:
        run_one(a.run)
    if a.report:
        report()
    if not (a.coverage or a.run or a.report):
        ap.error("pass --coverage, --run <name> and/or --report")


if __name__ == "__main__":
    main()
