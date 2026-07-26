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
- `bps_signal_engine.py` re-implements `compute_signal.py`'s `entry_confirmed`
  logic (RSI(20)<50-and-rising, near MA support within 2%, volume>avg,
  bullish candle) plus rule 1 (close>MA150) and rule 2 (short strike below
  MA150, rounded to $5, $10-wide spread, ~30 DTE), so results track what the
  live scanner would actually flag — not a hand-tuned strategy.
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

## Reference run (2026-07-26, 36 liquid large-caps, 2023-07-26 → 2026-07-26)

19 spreads triggered (rule is selective — some tickers, e.g. AMZN, fired
zero times in 3 years). 16/19 wins (84.2%), profit/loss ratio 1.44, total
P&L +$805.60 on 1 contract/spread. The 3 losses (BA, MSFT, and a near-zero
one on V) were cases where MA-150 support broke *after* entry, inside the
30 DTE hold — expected tail risk for a short-premium rule, not a bug.

N=19 is still thin — read this as "the rule isn't obviously broken," not as
a validated win rate. Re-run periodically, especially after touching
`compute_signal.py`'s guessed `MA_SUPPORT_BAND` (2%, flagged in that file's
own comments as undisclosed/best-effort) to see if loosening it changes the
edge, not just the trade count.
