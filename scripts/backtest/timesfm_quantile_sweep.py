"""Corrected, final TimesFM exit-discrimination test. Fixes two bugs from
the first pass (timesfm_exit_discrimination.py): (1) horizon was indexed by
calendar days, not trading days, inflating it ~40%; (2) only the median
(q50) was used, discarding the other 8 quantile bands. Here: score =
fraction of the 9 quantile bands (q10..q90) above the short strike --
a real probability estimate -- horizon indexed by actual trading days
remaining in the price series. Threshold swept 0.1-0.9.

Pre-committed kill criterion (stated before running): if the best
threshold's precision doesn't beat the cheap RSI-median heuristic's 56.2%
precision (at similar ~50% recall) by >=5 points, this is dead permanently.
Research only -- no live files touched.
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

LOCKED_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
NO_STOP_EXIT = dict(tp=True, ma150_stop=False, strike_stop=False)
W_REF = 10.0
HORIZON = 30
CONTEXT_MAX = 512
BATCH_SIZE = 32


def main():
    import timesfm

    print("Loading TimesFM 2.5 200M (cached)...", flush=True)
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        "google/timesfm-2.5-200m-pytorch", torch_compile=False
    )
    model.compile(
        timesfm.ForecastConfig(
            max_context=CONTEXT_MAX, max_horizon=HORIZON, normalize_inputs=True,
            use_continuous_quantile_head=True, force_flip_invariance=True,
            infer_is_positive=True, fix_quantile_crossing=True,
        )
    )
    print("Model loaded.", flush=True)

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
        # trading-day-correct remaining horizon: count actual sessions in
        # this ticker's own price index between breach and expiry.
        future_dates = pdf.index[(pdf.index > breach_date) & (pdf.index <= entry["expiry"])]
        if len(future_dates) == 0:
            continue
        remaining_trading_days = len(future_dates)
        events.append({
            "entry": entry, "breach_date": breach_date,
            "remaining_trading_days": remaining_trading_days,
            "recover": r_nostop["reason"] in ("TP", "EXPIRY_PROFIT"),
        })

    print(f"Matched breach events: {len(events)}", flush=True)

    for start in range(0, len(events), BATCH_SIZE):
        batch = events[start:start + BATCH_SIZE]
        contexts = []
        for ev in batch:
            pdf = data_map[ev["entry"]["code"]]
            closes_up_to = pdf.loc[pdf.index <= ev["breach_date"], "close"].dropna()
            contexts.append(closes_up_to.values[-CONTEXT_MAX:].astype(np.float32))
        _, quantile_forecast = model.forecast(horizon=HORIZON, inputs=contexts)
        for i, ev in enumerate(batch):
            day_idx = min(ev["remaining_trading_days"] - 1, HORIZON - 1)
            deciles = quantile_forecast[i, day_idx, 1:10]  # q10..q90, 9 bands
            ev["prob_recover_score"] = float(np.mean(deciles > ev["entry"]["short_strike"]))
        print(f"  batch {start}-{start+len(batch)}/{len(events)} done", flush=True)

    n_recover = sum(1 for e in events if e["recover"])
    base_rate = n_recover / len(events)
    print(f"\nBase rate: {base_rate:.1%} ({n_recover}/{len(events)})")

    print("\n=== Threshold sweep (score = fraction of 9 quantile bands above strike) ===")
    best = None
    for t in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        preds = [e["prob_recover_score"] >= t for e in events]
        tp = sum(1 for e, p in zip(events, preds) if p and e["recover"])
        fp = sum(1 for e, p in zip(events, preds) if p and not e["recover"])
        fn = sum(1 for e, p in zip(events, preds) if not p and e["recover"])
        n_hold = tp + fp
        prec = tp / n_hold if n_hold else float("nan")
        rec = tp / (tp + fn) if (tp + fn) else float("nan")
        print(f"threshold={t:.1f}: n_hold={n_hold:4d}  precision={prec:.1%}  recall={rec:.1%}")
        if n_hold >= 20 and (best is None or prec > best[1]):
            best = (t, prec, rec, n_hold)

    print(f"\nBest threshold (n_hold>=20): t={best[0]}, precision={best[1]:.1%}, "
          f"recall={best[2]:.1%}, n_hold={best[3]}")
    cheap_best_precision = 0.562
    delta = best[1] - cheap_best_precision
    print(f"\nCheap RSI-median heuristic precision: 56.2% (recall 50.4%)")
    print(f"TimesFM best precision: {best[1]:.1%}  (delta: {delta*100:+.1f} points)")
    if delta >= 0.05:
        print("VERDICT: TimesFM clears the pre-committed +5pt bar. Worth a P&L rerun.")
    else:
        print("VERDICT: TimesFM does NOT clear the pre-committed +5pt bar. Dead permanently.")


if __name__ == "__main__":
    main()
