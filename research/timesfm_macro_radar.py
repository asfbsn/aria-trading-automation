#!/usr/bin/env python3
"""V2 Macro-Gate prototype -- SHADOW MODE ONLY.

Daily radar: does TimesFM's own forecast see a severe SPY drawdown coming
in the next 30 days? Logs a [SAFE]/[HALT] verdict plus the raw numbers
behind it to a CSV so a real track record builds up over time. It does
NOT gate, block, or influence any live trade -- it has no wiring into
signal_core.py, the entry/exit prompts, daily-scan.sh, or data/universe.csv,
and it never will unless someone explicitly adds that wiring later, after
the shadow-mode log has enough history to judge whether this has skill.

Background (2026-09-05 session): a rigorous same-night test found TimesFM
has zero measurable skill discriminating individual-stock mean-reversion
at a strike-breach moment -- flat ~55-57% precision across every
confidence threshold, no better than free stdlib heuristics already in
signal_core.py. The mechanistic reason: the question asked (resolve a
~1.8%-from-strike move) was ~10x finer than the model's own honest
30-day uncertainty band (~21% width) on a univariate daily-close input.
This script asks a different, coarser question -- "is a broad-index
drawdown coming," not "will this specific stock's 1.8% dip resolve up or
down" -- which may or may not sit inside the model's actual resolution.
Shadow-mode logging is how you find out without ever risking capital on
an unproven premise.

Model: TimesFM 2.5 (google/timesfm-2.5-200m-pytorch), Apache-2.0,
self-hosted. Deliberately NOT TimesFM 3.0 -- its weights are
non-commercial-license only, and a real trading operation's monitoring
tooling is not personal research use. Stick to 2.5 for anything ARIA-
adjacent, shadow mode or not.

Note on "multivariate": TimesFM 3.0's marketing page advertises native
multivariate forecasting with side-information covariates. The 2.5
Python API this script actually uses (`model.forecast(horizon, inputs=[...])`)
forecasts each series independently in one batched call -- it does NOT
jointly condition SPY's forecast on VIX. This script combines SPY's own
downside forecast and VIX's own upside forecast as two independent
signals (OR'd together), not a true multivariate model. Said explicitly
so nobody mistakes this for the covariate-based multivariate feature.

Usage (cron-friendly, no interactive prompts, no live pipeline coupling):
    source /home/assaf/Projects/aria-trading/scripts/backtest/.venv/bin/activate
    python3 /home/assaf/Projects/aria-trading/research/timesfm_macro_radar.py

Exit code is always 0 (informational tool, never a hard failure) unless
data fetch itself fails, so a cron wrapper can treat non-zero as a real
error worth alerting on without ever conflating that with a HALT verdict.
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "state" / "research" / "timesfm_macro_radar_log.csv"

MODEL_CHECKPOINT = "google/timesfm-2.5-200m-pytorch"  # Apache-2.0 -- not 3.0
CONTEXT_MAX = 1024   # TimesFM 2.5's own max_context
HORIZON = 30         # trading days forward
FETCH_PERIOD = "5y"  # comfortably more than CONTEXT_MAX trading days

# Downside/upside thresholds -- deliberately conservative starting points,
# meant to be tuned once the shadow log has real history to judge against,
# not treated as tuned/validated numbers today.
SPY_DRAWDOWN_HALT_PCT = 0.05   # HALT if SPY's q10 (or configured quantile) forecast
                               # implies a >5% decline from today's close
VIX_SPIKE_HALT_LEVEL = 30.0    # HALT if VIX's q80 forecast at horizon exceeds this
DOWNSIDE_QUANTILE_IDX = 1      # index into the 9-quantile block: 1=q10, 2=q20, ... 9=q90
UPSIDE_QUANTILE_IDX = 8        # 8 = q80, for VIX's upside read


def fetch_close_series(ticker: str) -> np.ndarray:
    df = yf.download(ticker, period=FETCH_PERIOD, progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    close = df["Close"].dropna()
    if hasattr(close, "squeeze"):
        close = close.squeeze()
    values = close.values.astype(np.float32).reshape(-1)
    if len(values) < 30:
        raise RuntimeError(f"Suspiciously short history for {ticker}: {len(values)} points")
    return values[-CONTEXT_MAX:]


def main() -> int:
    import timesfm

    spy_context = fetch_close_series("SPY")
    vix_context = fetch_close_series("^VIX")
    spy_last = float(spy_context[-1])
    vix_last = float(vix_context[-1])

    print(f"Loading TimesFM 2.5 ({MODEL_CHECKPOINT})...", flush=True)
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_CHECKPOINT, torch_compile=False)
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

    _, quantile_forecast = model.forecast(horizon=HORIZON, inputs=[spy_context, vix_context])
    # quantile_forecast shape: (2, HORIZON, 10) -- index 0 = SPY, 1 = VIX;
    # last dim: 0=mean, 1..9 = q10..q90.

    spy_downside_path = quantile_forecast[0, :, DOWNSIDE_QUANTILE_IDX]
    spy_worst_case = float(np.min(spy_downside_path))
    spy_worst_day = int(np.argmin(spy_downside_path)) + 1
    spy_drawdown_pct = (spy_last - spy_worst_case) / spy_last

    vix_upside_path = quantile_forecast[1, :, UPSIDE_QUANTILE_IDX]
    vix_peak_forecast = float(np.max(vix_upside_path))
    vix_peak_day = int(np.argmax(vix_upside_path)) + 1

    spy_trigger = spy_drawdown_pct >= SPY_DRAWDOWN_HALT_PCT
    vix_trigger = vix_peak_forecast >= VIX_SPIKE_HALT_LEVEL
    verdict = "HALT" if (spy_trigger or vix_trigger) else "SAFE"

    reasons = []
    if spy_trigger:
        reasons.append(
            f"SPY q{DOWNSIDE_QUANTILE_IDX*10} forecast implies {spy_drawdown_pct:.1%} "
            f"drawdown by day {spy_worst_day} (threshold {SPY_DRAWDOWN_HALT_PCT:.0%})"
        )
    if vix_trigger:
        reasons.append(
            f"VIX q{UPSIDE_QUANTILE_IDX*10} forecast peaks at {vix_peak_forecast:.1f} "
            f"by day {vix_peak_day} (threshold {VIX_SPIKE_HALT_LEVEL:.1f})"
        )
    reason_str = "; ".join(reasons) if reasons else "no threshold breached"

    print(f"\n[{verdict}] {reason_str}")
    print(f"SPY last close: ${spy_last:.2f}  |  30d q{DOWNSIDE_QUANTILE_IDX*10} worst-case: "
          f"${spy_worst_case:.2f} ({spy_drawdown_pct:+.1%})")
    print(f"VIX last close: {vix_last:.2f}  |  30d q{UPSIDE_QUANTILE_IDX*10} peak forecast: "
          f"{vix_peak_forecast:.1f}")
    print("\nSHADOW MODE: this verdict does not affect any live trade. "
          "No wiring into signal_core.py, the exit/entry prompts, or the scan pipeline.")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "run_timestamp_utc", "spy_close", "spy_drawdown_pct_q10_30d",
                "spy_worst_case_price", "spy_worst_case_day", "vix_close",
                "vix_peak_forecast_q80_30d", "vix_peak_day", "verdict", "reasons",
            ])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), f"{spy_last:.2f}",
            f"{spy_drawdown_pct:.4f}", f"{spy_worst_case:.2f}", spy_worst_day,
            f"{vix_last:.2f}", f"{vix_peak_forecast:.2f}", vix_peak_day,
            verdict, reason_str,
        ])
    print(f"\nLogged to {LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
