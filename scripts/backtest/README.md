# Bull Put Spread rule backtest

Manual research tool — **not** wired into `daily-scan.sh` and not part of the
unattended pipeline. Answers one question: does the entry rule in
`scripts/compute_signal.py` / `prompts/bull-put-spread.md` (rule 1/2) have
directional edge, tested against history?

## What it is / isn't

- `options_portfolio.py` is vendored from
  [vibe-trading-ai](https://github.com/HKUDS/Vibe-Trading) v0.1.12 (MIT
  License, see `LICENSE`) — a Black-Scholes multi-leg options backtest
  engine. Trimmed to drop its `run_card.py` dependency; otherwise unmodified.
- `bps_signal_engine.py` imports the shared `entry_checks` rule straight from
  `scripts/signal_core.py` (close>MA150, RSI(20)<50-and-rising, close 0%-10%
  below MA50 AND 0%-10% above MA150, volume>avg, bullish candle) plus rule 2
  (short strike below MA150, rounded to $5, $10-wide spread, ~30 DTE), so
  results track what the live scanner would actually flag — not a hand-tuned
  strategy.
- `run_bps_backtest.py` fetches OHLCV via `yfinance` (free, no key) and runs
  it through the engine.

**Premiums are theoretical (Black-Scholes off historical volatility), not
real historical bid/ask.** This tells you whether the *entry rule* has edge
on the underlying's price path — it does not replace the live IBKR R/R gate
in `daily-scan.sh`, which prices against real broker quotes before a name is
called PRIME.

## Setup

```bash
cd scripts/backtest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 run_bps_backtest.py
```

Edit `TICKERS` / `START` / `END` in `run_bps_backtest.py` to change scope.

## Reference run (2026-08-24, 36 liquid large-caps, 2023-07-26 → 2026-07-26)

24 spreads triggered on the confirmed dual-MA band rule (`MA50_BAND` /
`MA150_BAND` in `signal_core.py` — close 0%-10% below MA50 AND 0%-10% above
MA150, read off the live screener filter panel 2026-08-24). 21/24 wins
(87.5%), total P&L +$2,291.84 on 1 contract/spread, avg +$95.49/spread.

Supersedes the prior run (19 spreads, 84.2%, +$805.60) which used a guessed
2% single-MA `near_ma_support` band, undisclosed by the source indicator. The
real, jointly-required (AND) dual-MA band both fires more often and wins more
— it wasn't a hand-tune, it's what the live screener panel actually applies.

N=24 is still thin — read this as "the rule isn't obviously broken," not as
a validated win rate.

## R&D breakthrough (2026-09-11): VRP entry + GEX-regime exit — NOT wired to production

Sandboxed draft architecture, `_v2` suffix throughout, with nothing
wired into the live scanner or exit guard. Two independent pieces, both verified
against real (non-synthetic) data this session:

- **`bps_signal_engine_v2.py`** — replaces the RSI/near-MA150-band entry gate
  with a Volatility Risk Premium filter: only enter when
  `iv_current / hv_current >= 1.1` (real DoltHub-sourced IV vs HV, not the old
  engine's synthetic HV-as-IV). MA150 kept as the strike-derivation anchor
  (short strike = MA150 rounded down to $5) — not optional, strike selection
  breaks without it. Configurable `mode` (`baseline` / `vrp_only` /
  `vrp_plus_baseline`) so entry-effect and exit-effect stay separable.
- **`dix_fetcher_v2.py`** — free, live, no-key daily net-SPX-dealer-gamma-exposure
  series from SqueezeMetrics (`squeezemetrics.com/monitor/static/DIX.csv`,
  2011-05-02 to present, academically replicated against OptionMetrics —
  Baltussen/Da/Lammers/Martens, JFE 2021). Raw `gex < 0` was tested and
  falsified as a crash-window trigger (caught only 7.5% of Feb-Mar 2025's
  days); the shipped version uses trailing-252-session GEX **percentile rank**
  at a 10th-percentile threshold instead (catches 52.5% of that window, 5x
  the ~10.7% base rate) — still an incomplete crash detector, not a solved one.
- **`compute_exit_signal_v2.py`** — regime-gated exit: NEGATIVE regime keeps
  the existing hard `close < short_strike` stop; POSITIVE regime relaxes to a
  premium-multiple stop (`current_loss > 2.0 × initial_credit`). Profit-target
  (80%) and time-stop (DTE≤7) unchanged in both regimes. Paired with
  `prompts/bull-put-spread-exit_v2.md` and `exit-guard_v2.sh` (isolated lock
  file/logs, cannot collide with the real `exit-guard.sh`).

**Backtest result** (563-ticker real-IV-covered universe, same IS/OOS split as
the exit-aware run above, `scripts/backtest/run_out_exit_aware_regime/`,
gitignored): decomposed 4-way comparison isolates entry-effect from
exit-effect rather than reporting one confounded number.

| | OOS $/trade | Full return | Full MaxDD | Full Sharpe |
|---|---|---|---|---|
| old baseline, hold-to-expiry | $6.63 | 5.46% | -7.94% | 0.419 |
| old baseline, static hard stop | $12.33 | 4.87% | -2.16% | 0.989 |
| VRP entry, hold-to-expiry | $21.48 | 5.78% | -4.38% | 0.725 |
| VRP entry + regime exit | $24.11 | 4.71% | -1.42% | **1.144** |

VRP entry is the larger driver of expectancy (cuts trade count 40%, more than
triples OOS $/trade). Regime/premium exit is the larger driver of drawdown
compression on top of that. All four cells independently re-verified against
raw CSVs (not just the delegate's summary) before being trusted.

**Explicitly not yet validated — the next session's mandatory focus before
any of this goes near production:** every number above is frictionless
(no slippage, no bid/ask spread — the vendored engine prices at theoretical
Black-Scholes mid) and uncapped ($1M notional, no per-trade or portfolio
position-size limit). A Sharpe >1.1 with <1.5% max drawdown on a short-premium
strategy is exactly the profile that tends to evaporate under those two
frictions — treat it as a promising lead, not a validated edge, until it's
re-run with realistic slippage/spread assumptions and real margin-constrained
position sizing.
