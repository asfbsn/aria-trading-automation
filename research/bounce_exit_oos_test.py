#!/usr/bin/env python3
"""Bifurcated in-sample/out-of-sample validation of the Bounce Exit
hypothesis (bounce_exit_test.py). Research only -- isolated from
scripts/signal_core.py and every live prompt; 05d60b2 stays the frozen
Monday config regardless of the result here.

The single-run +6.1% edge found in bounce_exit_test.py ($123.34/mo vs a
$116.21/mo baseline) has one free parameter -- the circuit-breaker
threshold (2.5%) -- that was picked, not fit. A threshold tuned and
scored on the same data it was chosen from cannot tell you whether the
edge is real or just curve-fit to this specific 36-month tape. This
script splits the window and never lets the test half see the tuning:

  IN-SAMPLE   2023-07-26 -> 2025-07-26 (24mo): sweep the circuit-breaker
    threshold over a grid, pick the single best performer by monthly P&L.
  OUT-OF-SAMPLE 2025-07-26 -> 2026-07-26 (12mo): freeze that one threshold
    (no re-optimization) and run it against the baseline -- entries in
    this window, and the threshold's own selection, never influence each
    other.

PRE-REGISTERED CRITERION (written before this script is executed):
  The Bounce Exit hypothesis is OOS-VALIDATED only if the frozen,
  IS-selected threshold beats the strike-only-immediate-stop baseline on
  OOS monthly P&L. An IS win with an OOS loss means the single-run edge
  was noise/overfit and the hypothesis is dead pending a different
  design -- it does NOT get retuned on the OOS window after the fact.

Caveat stated up front: ~24mo/~12mo split of one 750-ticker mega-cap
universe is one regime split, not a robust walk-forward with many folds.
A pass here is grounds for a live shadow-mode trial, not proof.
"""
import sys
from functools import partial
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounce_exit_test import (  # noqa: E402
    MONTHS, baseline_sim, build_merged, load_sector_map, simulate_bounce_exit,
)
from loosened_entry_full_pipeline import ENTRY_VARIANTS, generate_entries  # noqa: E402
from heap_allocator_sim import simulate_heap_allocation  # noqa: E402
from loosened_entry_full_pipeline import COMMISSION_PER_SPREAD  # noqa: E402

OOS_SPLIT = pd.Timestamp("2025-07-26")
IS_MONTHS = 24.0
OOS_MONTHS = MONTHS - IS_MONTHS  # 12.0
CIRCUIT_BREAKER_GRID = [0.010, 0.015, 0.020, 0.025, 0.030, 0.040, 0.050]


def run_allocator(merged, sector_map):
    result = simulate_heap_allocation(merged, net_liq=3000.0, sector_map=sector_map,
                                       commission_per_spread=COMMISSION_PER_SPREAD, rank_ascending=True)
    wr = result.wins / result.allocated_count if result.allocated_count else 0
    return result, wr


def main():
    sector_map = load_sector_map()
    all_entries = generate_entries(ENTRY_VARIANTS["B_loose_candle"])
    is_entries = [e for e in all_entries if e["entry_date"] < OOS_SPLIT]
    oos_entries = [e for e in all_entries if e["entry_date"] >= OOS_SPLIT]
    print(f"Total entries: {len(all_entries)}  IS(<{OOS_SPLIT.date()}): {len(is_entries)}  "
          f"OOS(>={OOS_SPLIT.date()}): {len(oos_entries)}\n")

    # ---- Step 1: IN-SAMPLE -- sweep circuit-breaker threshold, pick the best ----
    print("=== IN-SAMPLE (24mo) circuit-breaker threshold sweep ===")
    best_t, best_monthly = None, float("-inf")
    for t in CIRCUIT_BREAKER_GRID:
        merged, reasons = build_merged(is_entries, partial(simulate_bounce_exit, circuit_breaker_pct=t))
        result, wr = run_allocator(merged, sector_map)
        monthly = result.total_pnl / IS_MONTHS
        print(f"  threshold={t:.1%}: allocated={result.allocated_count:4d} win_rate={wr:.1%} "
              f"monthly=${monthly:,.2f}  reasons={dict(reasons)}")
        if monthly > best_monthly:
            best_t, best_monthly = t, monthly

    is_baseline_merged, _ = build_merged(is_entries, baseline_sim)
    is_baseline_result, is_baseline_wr = run_allocator(is_baseline_merged, sector_map)
    is_baseline_monthly = is_baseline_result.total_pnl / IS_MONTHS
    print(f"\nIN-SAMPLE baseline (immediate stop): allocated={is_baseline_result.allocated_count} "
          f"win_rate={is_baseline_wr:.1%} monthly=${is_baseline_monthly:,.2f}")
    print(f"IN-SAMPLE best bounce threshold: {best_t:.1%} (monthly=${best_monthly:,.2f}, "
          f"beats baseline by {(best_monthly - is_baseline_monthly):+,.2f}/mo)")

    # ---- Step 2: OUT-OF-SAMPLE -- freeze best_t, no re-optimization ----
    print(f"\n=== OUT-OF-SAMPLE (12mo, {OOS_SPLIT.date()} -> end) -- threshold frozen at {best_t:.1%} ===")
    oos_baseline_merged, _ = build_merged(oos_entries, baseline_sim)
    oos_baseline_result, oos_baseline_wr = run_allocator(oos_baseline_merged, sector_map)
    oos_baseline_monthly = oos_baseline_result.total_pnl / OOS_MONTHS
    print(f"OOS baseline (immediate stop):        allocated={oos_baseline_result.allocated_count:4d} "
          f"win_rate={oos_baseline_wr:.1%} monthly=${oos_baseline_monthly:,.2f}")

    oos_bounce_merged, oos_reasons = build_merged(oos_entries, partial(simulate_bounce_exit, circuit_breaker_pct=best_t))
    oos_bounce_result, oos_bounce_wr = run_allocator(oos_bounce_merged, sector_map)
    oos_bounce_monthly = oos_bounce_result.total_pnl / OOS_MONTHS
    print(f"OOS bounce (frozen {best_t:.1%} threshold): allocated={oos_bounce_result.allocated_count:4d} "
          f"win_rate={oos_bounce_wr:.1%} monthly=${oos_bounce_monthly:,.2f}  reasons={dict(oos_reasons)}")

    # Bonus robustness check: does the ORIGINAL hardcoded 2.5% (from the
    # first, non-optimized bounce_exit_test.py run) also hold OOS, independent
    # of whatever the IS sweep happened to pick? Reported, not scored against
    # the pre-registered criterion above.
    if best_t != 0.025:
        oos_25_merged, _ = build_merged(oos_entries, partial(simulate_bounce_exit, circuit_breaker_pct=0.025))
        oos_25_result, oos_25_wr = run_allocator(oos_25_merged, sector_map)
        oos_25_monthly = oos_25_result.total_pnl / OOS_MONTHS
        print(f"OOS bounce (original 2.5%, unselected): allocated={oos_25_result.allocated_count:4d} "
              f"win_rate={oos_25_wr:.1%} monthly=${oos_25_monthly:,.2f}  [informational only]")

    delta = oos_bounce_monthly - oos_baseline_monthly
    pct = (delta / abs(oos_baseline_monthly)) * 100 if oos_baseline_monthly else float("nan")
    print(f"\nOOS delta (frozen threshold vs baseline): ${delta:,.2f} ({pct:+.1f}%)")
    print("\n=== PRE-REGISTERED VERDICT ===")
    if oos_bounce_monthly > oos_baseline_monthly:
        print("OOS-VALIDATED: the IS-selected, frozen threshold beats the immediate-stop "
              "baseline on unseen data. Edge survived the split -- worth a live shadow-mode trial, "
              "still not a signal_core.py change.")
    else:
        print("OOS-FAILED: the in-sample edge did not survive out-of-sample. Per the "
              "pre-registered criterion, the single-run +6.1% result is treated as noise/overfit. "
              "Bounce Exit is dead as designed -- 05d60b2's immediate strike-only stop stands.")


if __name__ == "__main__":
    main()
