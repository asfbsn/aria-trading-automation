"""Widths 1/2.5/5 for the same 1591-ticker expanded universe (width=10 already
run in run_out_expanded_universe/). Needed to feed heap_allocator_sim.py with
a full 4-width candidate set for the $3k account, same methodology as the
750-ticker baseline's narrow-width run.
"""
import contextlib
from pathlib import Path

from run_bps_backtest_expanded_universe import (
    TICKERS, START, END, YFLoader, BullPutSpreadSignalEngine, run_options_backtest,
)


def main():
    loader = YFLoader()
    base_run_dir = Path(__file__).parent / "run_out_expanded_universe_narrow"
    base_run_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "codes": TICKERS,
        "start_date": START,
        "end_date": END,
        "initial_cash": 100_000,
        "commission": 0.0,
        "options_config": {
            "risk_free_rate": 0.045,
            "contract_multiplier": 100.0,
            "exercise_style": "american",
            "iv_skew": -0.15,
            "iv_curvature": 0.05,
        },
    }

    for width in [5.0, 2.5, 1.0]:
        suffix = str(width).rstrip("0").rstrip(".").replace(".", "_")
        tier_dir = base_run_dir / f"width_{suffix}"
        tier_dir.mkdir(parents=True, exist_ok=True)
        tier_log = tier_dir / "tier.log"
        print(f"Running width={width} on {len(TICKERS)} tickers...", flush=True)
        engine = BullPutSpreadSignalEngine(width=width)
        with open(tier_log, "w", encoding="utf-8") as f:
            with contextlib.redirect_stdout(f):
                run_options_backtest(config, loader, engine, tier_dir)
        print(f"width={width} done.", flush=True)

    print("All widths complete.")


if __name__ == "__main__":
    main()
