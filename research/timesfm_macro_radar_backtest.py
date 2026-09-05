#!/usr/bin/env python3
"""Historical backtest of timesfm_macro_radar.py's exact gate logic.

Per advisor review: a shadow-mode log without a pre-registered scoring
criterion, checked against real history first, is how a 51%-precision
signal ends up wired into a live gate in six weeks "because the log
looked promising." This asks the past what the gate would have said,
before the daily shadow log is trusted for anything -- same discipline
already applied to Concept #3 (per-trade exit validation) earlier
tonight, now applied to Concept #2 (macro-gating).

Replays evaluate_macro_signal() on >=40 NON-OVERLAPPING 30-trading-day
windows across the available SPY/VIX history, scores each HALT/SAFE call
against the PRE-REGISTERED criterion frozen in timesfm_macro_radar.py's
docstring (>=5% realized 30-day forward drawdown = HALT was correct),
and reports precision/recall. Does not modify any live file.
"""
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from timesfm_macro_radar import (  # noqa: E402
    CONTEXT_MAX, HORIZON, SPY_DRAWDOWN_HALT_PCT, build_model,
    evaluate_macro_signal, fetch_close_series,
)

MAX_HISTORY_PERIOD = "10y"  # more than enough for CONTEXT_MAX + many windows


def main():
    print("Fetching full SPY/VIX history for backtest...", flush=True)
    spy_full = fetch_close_series("SPY", period=MAX_HISTORY_PERIOD)
    vix_full = fetch_close_series("^VIX", period=MAX_HISTORY_PERIOD)
    n = min(len(spy_full), len(vix_full))
    spy_full, vix_full = spy_full[-n:], vix_full[-n:]
    print(f"History length: {n} trading days", flush=True)

    print("Loading TimesFM 2.5...", flush=True)
    model = build_model()

    # Non-overlapping windows: earliest possible evaluation date needs
    # CONTEXT_MAX prior bars; latest needs HORIZON trailing bars to score.
    first_idx = CONTEXT_MAX
    last_idx = n - HORIZON - 1
    window_starts = list(range(first_idx, last_idx, HORIZON))
    print(f"Non-overlapping {HORIZON}-day windows: {len(window_starts)}", flush=True)
    if len(window_starts) < 40:
        print(f"WARNING: only {len(window_starts)} windows available (<40 target). "
              f"Results below are directional, not the full pre-registered sample size.")

    results = []
    for i, idx in enumerate(window_starts):
        spy_ctx = spy_full[max(0, idx - CONTEXT_MAX):idx]
        vix_ctx = vix_full[max(0, idx - CONTEXT_MAX):idx]
        r = evaluate_macro_signal(model, spy_ctx, vix_ctx)

        eval_close = float(spy_full[idx - 1])
        forward_window = spy_full[idx: idx + HORIZON]
        realized_min = float(np.min(forward_window))
        realized_drawdown = (eval_close - realized_min) / eval_close
        realized_halt_correct = realized_drawdown >= SPY_DRAWDOWN_HALT_PCT

        results.append({
            "idx": idx, "verdict": r["verdict"], "realized_drawdown": realized_drawdown,
            "ground_truth_halt": realized_halt_correct,
        })
        print(f"  window {i+1}/{len(window_starts)}: verdict={r['verdict']:4s}  "
              f"realized_30d_drawdown={realized_drawdown:.1%}  "
              f"ground_truth={'HALT-deserved' if realized_halt_correct else 'SAFE'}", flush=True)

    n_ground_truth_halt = sum(1 for x in results if x["ground_truth_halt"])
    n = len(results)
    base_rate = n_ground_truth_halt / n
    print(f"\nBase rate of real >={SPY_DRAWDOWN_HALT_PCT:.0%} drawdowns across {n} windows: {base_rate:.1%}")
    print(f"(counts: {n_ground_truth_halt} windows had a real drawdown, {n - n_ground_truth_halt} did not)")

    tp = sum(1 for x in results if x["verdict"] == "HALT" and x["ground_truth_halt"])
    fp = sum(1 for x in results if x["verdict"] == "HALT" and not x["ground_truth_halt"])
    fn = sum(1 for x in results if x["verdict"] == "SAFE" and x["ground_truth_halt"])
    tn = sum(1 for x in results if x["verdict"] == "SAFE" and not x["ground_truth_halt"])

    print("\n=== Confusion matrix (gate verdict vs pre-registered ground truth) ===")
    print(f"                 GT: real drawdown   GT: no drawdown")
    print(f"Gate HALT:       TP={tp:<15}   FP={fp}")
    print(f"Gate SAFE:       FN={fn:<15}   TN={tn}")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    gate_accuracy = (tp + tn) / n
    # "Always SAFE" (never halt) is correct on exactly the windows where no
    # real drawdown occurred -- i.e. the ground-truth-SAFE population
    # (FP+TN), NOT (FN+TN). Getting this backwards understates how good
    # doing nothing is relative to the gate.
    always_safe_accuracy = (fp + tn) / n
    halt_fire_rate = (tp + fp) / n
    print(f"\nHALT precision: {precision:.1%}  (of the times the gate said HALT, how often was it right)")
    print(f"HALT recall:    {recall:.1%}  (of real drawdowns, how many did the gate catch)")
    print(f"Gate fires HALT on {tp+fp}/{n} = {halt_fire_rate:.1%} of all windows")
    print(f"Gate overall accuracy: {gate_accuracy:.1%}")
    print(f"'Always SAFE' (never halt, do nothing) baseline accuracy: {always_safe_accuracy:.1%}")
    beats_baseline = (not np.isnan(precision)) and gate_accuracy > always_safe_accuracy
    print(f"\nVERDICT: {'gate beats doing nothing' if beats_baseline else 'gate is DOMINATED by doing nothing -- treat as dead'}")


if __name__ == "__main__":
    main()
