#!/usr/bin/env python3
"""V2 Macro-Gate prototype -- SHADOW MODE ONLY.

Daily radar: does TimesFM's own forecast see a severe SPY drawdown coming
in the next 30 days? Logs a [SAFE]/[HALT] verdict plus the raw numbers
behind it to a CSV so a real track record builds up over time. It does
NOT gate, block, or influence any live trade -- it has no wiring into
signal_core.py, the entry/exit prompts, daily-scan.sh, or data/universe.csv,
and it never will unless someone explicitly adds that wiring later, after
enough history exists to judge whether this has skill. The CSV logs
`verdict` only -- never a `halt_active` flag or anything a downstream
script could read as a switch. Keep it that way deliberately.

Background (2026-09-05 session): a rigorous same-night test found TimesFM
has zero measurable skill discriminating individual-stock mean-reversion
at a strike-breach moment. This script asks a different, coarser question
-- "is a broad-index drawdown coming" -- which may sit differently
relative to the model's actual resolution. It does NOT get to skip the
same rigor: see timesfm_macro_radar_backtest.py, which replays this exact
logic against 5 years of real SPY history before the daily shadow log is
trusted for anything.

PRE-REGISTERED SCORING CRITERION (frozen before ever looking at backtest
results -- do not edit after seeing them, that's how a 51%-precision
signal becomes a live gate in six weeks):
  A HALT is CORRECT if SPY's realized minimum close over the following
  HORIZON trading days is >= SPY_DRAWDOWN_HALT_PCT below the evaluation
  date's close. A SAFE is CORRECT if it isn't. Evaluated on >=40 NON-
  OVERLAPPING HORIZON-day windows (overlapping windows pseudo-replicate
  and inflate apparent sample size). Precision/recall on HALT calls is
  the number that matters -- not raw accuracy, since SAFE-every-day
  already scores ~88-92% accuracy for free (SPY drops >=5% in a 30-day
  window roughly 8-12% of the time historically).

Model: TimesFM 2.5 (google/timesfm-2.5-200m-pytorch), Apache-2.0,
self-hosted. Deliberately NOT TimesFM 3.0 -- its weights are
non-commercial-license only, and a real trading operation's monitoring
tooling is not personal research use.

Note on "multivariate": TimesFM 3.0's marketing page advertises native
multivariate forecasting with side-information covariates. The 2.5
Python API this script actually uses (`model.forecast(horizon, inputs=[...])`)
forecasts each series independently in one batched call -- it does NOT
jointly condition SPY's forecast on VIX. This combines SPY's own tail
forecast and VIX's own forecast as two independent signals (OR'd
together), not a true multivariate model.

Signal design (revised 2026-09-05 after advisor review of the first
version -- see git history for what was wrong):
  - SPY trigger requires the q10 path to drop *more than the model's own
    q50 (central) path* by SPY_MIN_TAIL_ASYMMETRY_PCT, not just a q10
    decline from today's spot. Without this, a model that simply
    forecasts a downward *trend* (q10 and q50 both drift down together)
    would fire the gate -- but a shared-drift call isn't "elevated crash
    risk," it's a directional trend call the model has no particular
    right to make about SPY. Only a WIDENING left tail (q10 falling away
    from q50) is treated as a risk signal.
  - VIX trigger is a forecast *rise* from today's VIX level, not an
    absolute level. An absolute-level threshold (e.g. "VIX > 30") only
    fires once VIX is already elevated -- a lagging indicator wearing a
    forecasting model's clothes, not a leading one.

Usage (cron-friendly, no interactive prompts, no live pipeline coupling):
    source /home/assaf/Projects/aria-trading/scripts/backtest/.venv/bin/activate
    python3 /home/assaf/Projects/aria-trading/research/timesfm_macro_radar.py
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

# Frozen thresholds -- part of the pre-registered scoring criterion above.
# Do not retune after seeing backtest or shadow-log results.
SPY_DRAWDOWN_HALT_PCT = 0.05        # q10 must imply >=5% decline from spot
SPY_MIN_TAIL_ASYMMETRY_PCT = 0.02   # AND q10 must fall >=2pp further than q50
VIX_RISE_HALT_POINTS = 8.0          # VIX q80 forecast must RISE >=8pts from today
DOWNSIDE_QUANTILE_IDX = 1           # index into the 9-quantile block: 1=q10 ... 9=q90
MEDIAN_QUANTILE_IDX = 5             # 5 = q50
UPSIDE_QUANTILE_IDX = 8             # 8 = q80, for VIX's upside read


def fetch_close_series(ticker: str, period: str = FETCH_PERIOD) -> np.ndarray:
    df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    close = df["Close"].dropna()
    if hasattr(close, "squeeze"):
        close = close.squeeze()
    values = close.values.astype(np.float32).reshape(-1)
    if len(values) < 30:
        raise RuntimeError(f"Suspiciously short history for {ticker}: {len(values)} points")
    return values


def build_model():
    import timesfm
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
    return model


def evaluate_macro_signal(model, spy_context: np.ndarray, vix_context: np.ndarray) -> dict:
    """Shared evaluation logic -- used by both the live daily script and
    the historical backtest, so the two can never drift apart. Contexts
    are truncated to CONTEXT_MAX internally; pass full available history.
    """
    spy_ctx = spy_context[-CONTEXT_MAX:].astype(np.float32)
    vix_ctx = vix_context[-CONTEXT_MAX:].astype(np.float32)
    spy_last = float(spy_ctx[-1])
    vix_last = float(vix_ctx[-1])

    _, quantile_forecast = model.forecast(horizon=HORIZON, inputs=[spy_ctx, vix_ctx])
    # shape: (2, HORIZON, 10) -- index 0=SPY, 1=VIX; last dim 0=mean, 1..9=q10..q90.

    spy_q10_path = quantile_forecast[0, :, DOWNSIDE_QUANTILE_IDX]
    spy_q50_path = quantile_forecast[0, :, MEDIAN_QUANTILE_IDX]
    spy_q10_worst = float(np.min(spy_q10_path))
    spy_q10_worst_day = int(np.argmin(spy_q10_path)) + 1
    spy_q10_drawdown = (spy_last - spy_q10_worst) / spy_last
    # q50's own drawdown at the SAME day the q10 path bottoms out -- the
    # "how much of this is just a trend call" control.
    spy_q50_at_worst_day = float(spy_q50_path[spy_q10_worst_day - 1])
    spy_q50_drawdown_same_day = (spy_last - spy_q50_at_worst_day) / spy_last
    tail_asymmetry = spy_q10_drawdown - spy_q50_drawdown_same_day

    vix_q80_path = quantile_forecast[1, :, UPSIDE_QUANTILE_IDX]
    vix_q80_peak = float(np.max(vix_q80_path))
    vix_q80_peak_day = int(np.argmax(vix_q80_path)) + 1
    vix_rise = vix_q80_peak - vix_last

    spy_trigger = (spy_q10_drawdown >= SPY_DRAWDOWN_HALT_PCT
                   and tail_asymmetry >= SPY_MIN_TAIL_ASYMMETRY_PCT)
    vix_trigger = vix_rise >= VIX_RISE_HALT_POINTS
    verdict = "HALT" if (spy_trigger or vix_trigger) else "SAFE"

    reasons = []
    if spy_trigger:
        reasons.append(
            f"SPY q10 implies {spy_q10_drawdown:.1%} drawdown by day {spy_q10_worst_day} "
            f"({tail_asymmetry:+.1%} beyond its own q50 trend, threshold {SPY_MIN_TAIL_ASYMMETRY_PCT:.0%})"
        )
    if vix_trigger:
        reasons.append(
            f"VIX q80 forecast rises {vix_rise:+.1f}pts to {vix_q80_peak:.1f} "
            f"by day {vix_q80_peak_day} (threshold +{VIX_RISE_HALT_POINTS:.0f}pts)"
        )

    return {
        "spy_last": spy_last, "vix_last": vix_last,
        "spy_q10_drawdown": spy_q10_drawdown, "spy_q10_worst": spy_q10_worst,
        "spy_q10_worst_day": spy_q10_worst_day, "tail_asymmetry": tail_asymmetry,
        "vix_q80_peak": vix_q80_peak, "vix_q80_peak_day": vix_q80_peak_day,
        "vix_rise": vix_rise, "spy_trigger": spy_trigger, "vix_trigger": vix_trigger,
        "verdict": verdict, "reason_str": "; ".join(reasons) if reasons else "no threshold breached",
    }


def main() -> int:
    spy_context = fetch_close_series("SPY")
    vix_context = fetch_close_series("^VIX")

    print(f"Loading TimesFM 2.5 ({MODEL_CHECKPOINT})...", flush=True)
    model = build_model()

    r = evaluate_macro_signal(model, spy_context, vix_context)

    print(f"\n[{r['verdict']}] {r['reason_str']}")
    print(f"SPY last close: ${r['spy_last']:.2f}  |  30d q10 worst-case: "
          f"${r['spy_q10_worst']:.2f} ({r['spy_q10_drawdown']:+.1%}, "
          f"tail asymmetry vs its own q50: {r['tail_asymmetry']:+.1%})")
    print(f"VIX last close: {r['vix_last']:.2f}  |  30d q80 peak forecast: "
          f"{r['vix_q80_peak']:.1f} ({r['vix_rise']:+.1f}pts)")
    print("\nSHADOW MODE: this verdict does not affect any live trade. "
          "No wiring into signal_core.py, the exit/entry prompts, or the scan pipeline.")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "run_timestamp_utc", "spy_close", "spy_q10_drawdown_30d", "tail_asymmetry",
                "vix_close", "vix_q80_peak_30d", "vix_rise_30d", "verdict", "reasons",
            ])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), f"{r['spy_last']:.2f}",
            f"{r['spy_q10_drawdown']:.4f}", f"{r['tail_asymmetry']:.4f}",
            f"{r['vix_last']:.2f}", f"{r['vix_q80_peak']:.2f}", f"{r['vix_rise']:.2f}",
            r["verdict"], r["reason_str"],
        ])
    print(f"\nLogged to {LOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
