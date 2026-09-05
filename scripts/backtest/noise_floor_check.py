"""Advisor's proposed decisive number: is the distance TimesFM needed to
resolve at breach (how far below strike) smaller than its own honest
uncertainty band (q90-q10) at that horizon? If so, the flat precision
curve isn't the model failing -- it's correctly reporting "I don't know"
because the question sits below its noise floor.
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path("/home/assaf/Projects/aria-trading/scripts/backtest")
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import ENTRY_VARIANTS, generate_entries, data_map  # noqa: E402
from hold_to_expiry_test import simulate_one_tagged  # noqa: E402

LOCKED_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
W_REF = 10.0
HORIZON = 30
CONTEXT_MAX = 512
BATCH_SIZE = 32


def main():
    import timesfm
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        "google/timesfm-2.5-200m-pytorch", torch_compile=False
    )
    model.compile(timesfm.ForecastConfig(
        max_context=CONTEXT_MAX, max_horizon=HORIZON, normalize_inputs=True,
        use_continuous_quantile_head=True, force_flip_invariance=True,
        infer_is_positive=True, fix_quantile_crossing=True,
    ))
    print("Model loaded.", flush=True)

    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    events = []
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        k_long = entry["short_strike"] - W_REF
        r = simulate_one_tagged(entry, pdf, k_long, LOCKED_EXIT)
        if r is None or r["reason"] != "STOP":
            continue
        breach_date = r["exit_date"]
        breach_close = pdf.at[breach_date, "close"]
        future_dates = pdf.index[(pdf.index > breach_date) & (pdf.index <= entry["expiry"])]
        if len(future_dates) == 0:
            continue
        events.append({
            "entry": entry, "breach_date": breach_date, "breach_close": breach_close,
            "remaining_trading_days": len(future_dates),
        })

    print(f"Events: {len(events)}", flush=True)

    breach_pcts, band_pcts = [], []
    for start in range(0, len(events), BATCH_SIZE):
        batch = events[start:start + BATCH_SIZE]
        contexts = []
        for ev in batch:
            pdf = data_map[ev["entry"]["code"]]
            closes_up_to = pdf.loc[pdf.index <= ev["breach_date"], "close"].dropna()
            contexts.append(closes_up_to.values[-CONTEXT_MAX:].astype(np.float32))
        _, qf = model.forecast(horizon=HORIZON, inputs=contexts)
        for i, ev in enumerate(batch):
            day_idx = min(ev["remaining_trading_days"] - 1, HORIZON - 1)
            q10, q50, q90 = qf[i, day_idx, 1], qf[i, day_idx, 5], qf[i, day_idx, 9]
            k_short = ev["entry"]["short_strike"]
            breach_pct = abs(ev["breach_close"] - k_short) / k_short
            band_pct = (q90 - q10) / q50 if q50 else float("nan")
            breach_pcts.append(breach_pct)
            band_pcts.append(band_pct)
        print(f"  batch {start}-{start+len(batch)}/{len(events)} done", flush=True)

    breach_pcts = np.array(breach_pcts)
    band_pcts = np.array(band_pcts)
    print(f"\nMean |breach_close - short_strike| / short_strike: {np.mean(breach_pcts):.1%}")
    print(f"Median: {np.median(breach_pcts):.1%}")
    print(f"\nMean (q90-q10)/q50 at matched horizon (TimesFM's own uncertainty band): {np.mean(band_pcts):.1%}")
    print(f"Median: {np.median(band_pcts):.1%}")
    ratio = np.mean(breach_pcts) / np.mean(band_pcts)
    print(f"\nRatio (breach distance / uncertainty band width): {ratio:.2f}")
    print("If well under 0.5: the breach distance sits inside the model's honest noise floor.")


if __name__ == "__main__":
    main()
