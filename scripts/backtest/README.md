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
