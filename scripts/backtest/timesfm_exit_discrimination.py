"""Concept #3 (Smart Exit Validation) discrimination test.

For every historical strike-breach event under the LOCKED live exit rule
(750-ticker mega-cap universe, loosened "any green close" entry, strike-
breach hard stop), ask: at the exact moment of breach, does TimesFM 2.5's
short-term forecast correctly distinguish trades that would go on to
recover (49-ish, the INTC/CDNS pattern) from trades that would keep
bleeding to a real loss (284-ish), if we asked it to predict?

Ground truth for each breached entry = its outcome under the NO-STOP
variant (TP80-or-hold-to-expiry) -- i.e. what actually happens if nobody
stops it. TimesFM's call = its own median forecast at the entry's actual
remaining-days-to-expiry: predicts RECOVER if median forecast ends back
above the short strike, else predicts BLEED.

Reports a confusion matrix (TP/FP/TN/FN) and the resulting $3k Global
Heap Allocator P&L if exits were conditioned on TimesFM's call instead of
the blanket strike-breach stop. Research only -- signal_core.py and the
live config files are untouched.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path("/home/assaf/Projects/aria-trading/scripts/backtest")
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))

from loosened_entry_full_pipeline import (  # noqa: E402
    ENTRY_VARIANTS, generate_entries, data_map, WIDTHS, COMMISSION_PER_SPREAD,
    RISK_FREE, IV_SKEW, IV_CURVATURE,
)
from hold_to_expiry_test import simulate_one_tagged  # noqa: E402
from run_heap_allocator_baseline_check import load_sector_map  # noqa: E402
from heap_allocator_sim import simulate_heap_allocation  # noqa: E402

LOCKED_EXIT = dict(tp=True, ma150_stop=False, strike_stop=True)
NO_STOP_EXIT = dict(tp=True, ma150_stop=False, strike_stop=False)
HORIZON = 30
CONTEXT_MAX = 512
BATCH_SIZE = 32


def main():
    import timesfm

    print("Loading TimesFM 2.5 200M (torch, CPU)...", flush=True)
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        "google/timesfm-2.5-200m-pytorch", torch_compile=False
    )
    model.compile(
        timesfm.ForecastConfig(
            max_context=CONTEXT_MAX,
            max_horizon=HORIZON,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
        )
    )
    print("Model loaded.", flush=True)

    entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    print(f"Entry population: {len(entries)} signals (loosened candle, mega-cap-only)", flush=True)

    # width=10 is the reference: short_strike (and hence breach date/outcome
    # classification) is identical across widths for a given entry.
    w_ref = 10.0
    breach_events = []  # list of dict: entry, breach_date, remaining_days, no_stop_outcome
    for entry in entries:
        pdf = data_map.get(entry["code"])
        if pdf is None:
            continue
        k_long = entry["short_strike"] - w_ref
        r_locked = simulate_one_tagged(entry, pdf, k_long, LOCKED_EXIT)
        if r_locked is None or r_locked["reason"] != "STOP":
            continue
        r_nostop = simulate_one_tagged(entry, pdf, k_long, NO_STOP_EXIT)
        if r_nostop is None:
            continue
        breach_date = r_locked["exit_date"]
        remaining_days = max((entry["expiry"] - breach_date).days, 1)
        breach_events.append({
            "entry": entry, "breach_date": breach_date,
            "remaining_days": remaining_days,
            "ground_truth_recover": r_nostop["reason"] in ("TP", "EXPIRY_PROFIT"),
            "no_stop_outcome": r_nostop["reason"],
        })

    print(f"Matched strike-breach events: {len(breach_events)}", flush=True)
    n_recover_gt = sum(1 for e in breach_events if e["ground_truth_recover"])
    print(f"Ground truth: {n_recover_gt} recover, {len(breach_events) - n_recover_gt} bleed "
          f"(ratio 1:{(len(breach_events)-n_recover_gt)/max(n_recover_gt,1):.1f})", flush=True)

    # Batch TimesFM forecasts.
    predictions = [None] * len(breach_events)
    for start in range(0, len(breach_events), BATCH_SIZE):
        batch = breach_events[start:start + BATCH_SIZE]
        contexts = []
        for ev in batch:
            pdf = data_map[ev["entry"]["code"]]
            closes_up_to = pdf.loc[pdf.index <= ev["breach_date"], "close"].dropna()
            ctx = closes_up_to.values[-CONTEXT_MAX:].astype(np.float32)
            contexts.append(ctx)
        _, quantile_forecast = model.forecast(horizon=HORIZON, inputs=contexts)
        for i, ev in enumerate(batch):
            day_idx = min(ev["remaining_days"] - 1, HORIZON - 1)
            median_at_horizon = quantile_forecast[i, day_idx, 5]  # index 5 = q50
            predictions[start + i] = median_at_horizon > ev["entry"]["short_strike"]
        print(f"  forecast batch {start}-{start+len(batch)}/{len(breach_events)} done", flush=True)

    for ev, pred in zip(breach_events, predictions):
        ev["timesfm_predicts_recover"] = pred

    tp = sum(1 for e in breach_events if e["timesfm_predicts_recover"] and e["ground_truth_recover"])
    fp = sum(1 for e in breach_events if e["timesfm_predicts_recover"] and not e["ground_truth_recover"])
    tn = sum(1 for e in breach_events if not e["timesfm_predicts_recover"] and not e["ground_truth_recover"])
    fn = sum(1 for e in breach_events if not e["timesfm_predicts_recover"] and e["ground_truth_recover"])

    print("\n=== Confusion matrix (predict RECOVER vs ground truth) ===")
    print(f"                 GT: Recover   GT: Bleed")
    print(f"Pred Recover:    TP={tp:<10}   FP={fp}")
    print(f"Pred Bleed:      FN={fn:<10}   TN={tn}")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    baseline_recover_rate = n_recover_gt / len(breach_events)
    print(f"\nPrecision (of predicted recoveries, how many really recovered): {precision:.1%}")
    print(f"Recall (of real recoveries, how many did it catch): {recall:.1%}")
    print(f"Base rate (always predict recover): {baseline_recover_rate:.1%} would be 'precision' by chance")

    # Build the TimesFM-conditioned exit outcome per entry (all entries, not
    # just breach events), then re-run the $3k allocator.
    breach_by_key = {(e["entry"]["code"], e["entry"]["entry_date"]): e for e in breach_events}

    sector_map = load_sector_map()

    def run_variant(label, decision_fn):
        rows_by_key = {}
        for entry in entries:
            pdf = data_map.get(entry["code"])
            if pdf is None:
                continue
            key = (entry["code"], entry["entry_date"])
            ev = breach_by_key.get(key)
            for w in WIDTHS:
                k_long = entry["short_strike"] - w
                if ev is None:
                    # never breached under locked rule -- outcome unchanged
                    r = simulate_one_tagged(entry, pdf, k_long, LOCKED_EXIT)
                else:
                    hold = decision_fn(ev)
                    r = simulate_one_tagged(entry, pdf, k_long, NO_STOP_EXIT if hold else LOCKED_EXIT)
                if r is None:
                    continue
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
        months = 36.0
        print(f"\n=== {label} ===")
        print(f"Allocated: {result.allocated_count}  Win rate: {wr:.1%}  "
              f"Total P&L: ${result.total_pnl:,.2f}  Monthly: ${result.total_pnl/months:,.2f}")

    run_variant("Locked live (strike-only, always cut on breach)", lambda ev: False)
    run_variant("No-stop (always hold through breach)", lambda ev: True)
    run_variant("TimesFM-conditioned (hold iff model predicts recover)", lambda ev: ev["timesfm_predicts_recover"])


if __name__ == "__main__":
    main()
