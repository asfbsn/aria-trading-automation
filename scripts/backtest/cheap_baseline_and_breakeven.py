"""Advisor-directed follow-up before reopening TimesFM: (1) compute the
breakeven precision any hold/cut classifier needs to beat the blanket
strike-breach stop on the 641 matched breach events, from data already in
hand; (2) test cheap stdlib heuristics already computed in signal_core.py
(breach volume vs avg, RSI at breach, MA150 co-breach) against that bar,
before spending any more compute on a model. No live files touched.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/assaf/Projects/aria-trading/scripts/backtest")
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import ENTRY_VARIANTS, generate_entries, data_map  # noqa: E402
from hold_to_expiry_test import simulate_one_tagged  # noqa: E402
from signal_core import rsi_series, sma, VOLUME_MA_LENGTH  # noqa: E402

LOCKED_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
NO_STOP_EXIT = dict(tp=True, ma150_stop=False, strike_stop=False)
W_REF = 10.0


def main():
    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    events = []
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        k_long = entry["short_strike"] - W_REF
        r_locked = simulate_one_tagged(entry, pdf, k_long, LOCKED_EXIT)
        if r_locked is None or r_locked["reason"] != "STOP":
            continue
        r_nostop = simulate_one_tagged(entry, pdf, k_long, NO_STOP_EXIT)
        if r_nostop is None:
            continue
        breach_date = r_locked["exit_date"]
        closes_up_to = pdf.loc[pdf.index <= breach_date, "close"]
        volumes_up_to = pdf.loc[pdf.index <= breach_date, "volume"] if "volume" in pdf.columns else None
        ma150_at_breach = pdf.at[breach_date, "ma150"] if "ma150" in pdf.columns else sma(closes_up_to.tolist(), 150)
        rsi_at_breach = rsi_series(closes_up_to.tolist())[-1]
        vol_ma20 = sma(volumes_up_to.tolist(), VOLUME_MA_LENGTH) if volumes_up_to is not None else None
        vol_at_breach = volumes_up_to.iloc[-1] if volumes_up_to is not None else None
        events.append({
            "recover": r_nostop["reason"] in ("TP", "EXPIRY_PROFIT"),
            "pnl_locked": r_locked["pnl"],
            "pnl_nostop": r_nostop["pnl"],
            "light_volume": (vol_ma20 is not None and vol_at_breach is not None and vol_at_breach < vol_ma20),
            "rsi_at_breach": rsi_at_breach,
            "ma150_co_breach": (ma150_at_breach is not None and closes_up_to.iloc[-1] < ma150_at_breach),
        })

    n = len(events)
    n_recover = sum(1 for e in events if e["recover"])
    n_bleed = n - n_recover
    base_rate = n_recover / n
    print(f"Matched breach events: {n}  (recover={n_recover}, bleed={n_bleed}, base_rate={base_rate:.1%})")

    # --- Breakeven precision ---
    gains = [e["pnl_nostop"] - e["pnl_locked"] for e in events if e["recover"]]
    costs = [e["pnl_locked"] - e["pnl_nostop"] for e in events if not e["recover"]]
    avg_gain = float(np.mean(gains))
    avg_cost = float(np.mean(costs))
    breakeven_p = avg_cost / (avg_gain + avg_cost)
    print(f"\nAvg gain from correctly holding a recoverer: ${avg_gain:,.2f}")
    print(f"Avg cost from incorrectly holding a bleeder:  ${avg_cost:,.2f}")
    print(f"BREAKEVEN PRECISION required on 'hold' calls to beat blanket-stop: {breakeven_p:.1%}")
    print(f"(base rate is {base_rate:.1%} -- a classifier needs to clear {breakeven_p:.1%}, not just beat 50/50)")

    # --- Cheap stdlib heuristics ---
    def precision_recall(predict_fn, label):
        preds = [predict_fn(e) for e in events]
        tp = sum(1 for e, p in zip(events, preds) if p and e["recover"])
        fp = sum(1 for e, p in zip(events, preds) if p and not e["recover"])
        fn = sum(1 for e, p in zip(events, preds) if not p and e["recover"])
        n_hold = tp + fp
        prec = tp / n_hold if n_hold else float("nan")
        rec = tp / (tp + fn) if (tp + fn) else float("nan")
        print(f"{label}: n_hold={n_hold}  precision={prec:.1%}  recall={rec:.1%}  "
              f"{'CLEARS breakeven' if prec >= breakeven_p else 'below breakeven'} "
              f"({'above' if prec > base_rate else 'at/below'} base rate)")

    print("\n--- Cheap stdlib heuristics (no model) ---")
    precision_recall(lambda e: e["light_volume"], "Hold if breach on LIGHT volume (below 20d avg)")
    precision_recall(lambda e: not e["ma150_co_breach"], "Hold if MA150 NOT also breached (strike-only breach)")
    rsis = [e["rsi_at_breach"] for e in events if e["rsi_at_breach"] is not None]
    median_rsi = float(np.median(rsis))
    precision_recall(lambda e: e["rsi_at_breach"] is not None and e["rsi_at_breach"] > median_rsi,
                      f"Hold if RSI at breach > median ({median_rsi:.1f})")
    precision_recall(lambda e: e["light_volume"] and not e["ma150_co_breach"],
                      "Hold if LIGHT volume AND MA150 not co-breached (combined)")


if __name__ == "__main__":
    main()
