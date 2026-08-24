# ARIA — Daily Bull Put Spread Scanner

Automated, unattended scanner that evaluates a static large/mid-cap US universe
every trading day and delivers a two-table Bull Put Spread report (desktop
notification + Telegram). Entry signal and option-chain verification run
against **IBKR** data only, via a headless Claude process — no TradingView
dependency in the automated path.

> This used to drive TradingView Desktop live (via the `tradingview-bridge`
> MCP) for both the screener list and the entry-signal dashboard read. Both
> were replaced 2026-08-24 after TradingView Desktop repeatedly crashed or
> hung under cron for 5+ weeks straight. See `scripts/refresh_universe.py`
> and `scripts/compute_signal.py` for what replaced them. TradingView Desktop
> is still useful interactively (ad-hoc chart/Pine work) — it's just no
> longer a hard dependency for the daily scan.

> ⚠️ **Advisory only.** The scan never places trades. Broker/order tools are
> deliberately excluded from its allow-list. Live IBKR orders are always *staged*
> for manual review/submit by a human (see `ibkr-live-workflow.md`).

## What it does (each weekday 19:00 Israel)

1. **Read** `data/universe.csv` — every ticker in it, no live fetch, no sampling.
2. **Scan** every ticker on the Daily (1D) interval: pull IBKR bars, run
   `scripts/compute_signal.py` locally (RSI(20), MA50/MA150, volume, candle
   pattern — a deterministic proxy for the entry signal).
3. **Verify** R/R on the live IBKR option chain for PRIME-eligible names only.
4. **Report** into two clearly separated tables:
   - 🟢 **PRIME** — `entry_confirmed: true` + strong structure that passes the MA-150 and
     1:1.5–2.5 R/R rules (the only execution-ready names).
   - 🟡 **RADAR** — "setups in the making" (strong structure / weak trigger) plus any
     discretionary "hidden gems" flagged from independent TA. Watch-only.
5. **Deliver** a dated log + desktop `notify-send` + Telegram (report inline + attached).

A **watchdog** (`watchdog.sh`, pure bash+curl, no Claude usage) runs at 19:45 and
Telegram-alerts if a complete report wasn't produced — so a silent miss never goes
unnoticed.

## Files

| File | Purpose |
|---|---|
| `daily-scan.sh` | Main wrapper: env, lock, skip logic, headless Claude run, alerting |
| `watchdog.sh` | Safety net — alerts if the day's report didn't complete |
| `data/universe.csv` | Static candidate universe (ticker, sector, approx market cap) — replaces the live screener list |
| `scripts/refresh_universe.py` | Regenerates `data/universe.csv` from the iShares Russell 1000 (IWB) holdings CSV, filtered to $10B–$5T approx market cap. Re-run every 1–3 months (see the script's docstring for why and how to update the calibration constant) |
| `scripts/compute_signal.py` | Local entry-signal proxy (RSI/MA/volume/candle) computed from IBKR bars — replaces the TradingView dashboard read |
| `scripts/signal_core.py` | Shared entry-rule logic between the live scanner and the backtest engine |
| `prompts/bull-put-spread.md` | The scan prompt (rules, two-table output, settled-vs-provisional) |
| `prompts/bull-put-spread-ror50.md` | On-demand IBKR-only strike selector: user-given tickers, dynamic spread widths, ROR≥50% gate, lowest-strike-that-clears rule |
| `prompts/verify-rr-gate.md` | Ad-hoc wiring check for the IBKR R/R gate (1:1.5–2.5 band) |
| `prompts/smoke-test.md` | Lightweight plumbing/auth check (one ticker) |
| `us-market-holidays.txt` | NYSE full-day closures to skip (update yearly) |
| `.env.example` | Template for `.env` (GUI, Claude auth, Telegram) |
| `priority-today.md` | *(gitignored)* optional date-gated daily priority overlay |
| `logs/`, `state/` | *(gitignored)* runtime output + hashes |

## Setup

```bash
cp .env.example .env && chmod 600 .env      # then fill in tokens
chmod +x daily-scan.sh watchdog.sh

# one-off plumbing/auth check (bypasses weekend/holiday guard):
FORCE_RUN=true PROMPT_FILE="$PWD/prompts/smoke-test.md" ./daily-scan.sh

# on-demand ROR>=50% strike check for specific tickers (fill in section 0 first):
FORCE_RUN=true PROMPT_FILE="$PWD/prompts/bull-put-spread-ror50.md" ./daily-scan.sh

# install the schedule:
( crontab -l 2>/dev/null; cat <<'CRON'
CRON_TZ=Asia/Jerusalem
0  19 * * 1-5 $HOME/Projects/aria-trading/daily-scan.sh >> $HOME/Projects/aria-trading/logs/cron.log 2>&1
45 19 * * 1-5 $HOME/Projects/aria-trading/watchdog.sh   >> $HOME/Projects/aria-trading/logs/watchdog.log 2>&1
CRON
) | crontab -
```

### Auth note
Headless Claude needs auth in `.env`. A subscription `CLAUDE_CODE_OAUTH_TOKEN`
(`claude setup-token`) works but has a **monthly usage cap**; for set-and-forget
automation prefer a pay-per-use `ANTHROPIC_API_KEY` (console.anthropic.com).

### Daily priority overlay (optional)
Drop a `priority-today.md` with first line `PRIORITY_DATE: YYYY-MM-DD` and a list of
tickers to surface them at the top of that day's report. It auto-expires (date-gated).

## Operational notes / gotchas
- The machine must be powered on at 19:00; cron can't wake a sleeping box (the watchdog flags it).
- `data/universe.csv` goes stale slowly (market-cap band is wide, Russell 1000 only
  reconstitutes semi-annually) — refresh every 1–3 months with
  `python3 scripts/refresh_universe.py`, and update its `RUSSELL_1000_TOTAL_MKTCAP`
  constant from FTSE Russell's latest published reconstitution figure first.

## Strategy rules (summary)
Above MA-150; short put OTM **below** support; target R/R 1:2 (band 1:1.5–2.5);
reject if strike intervals can't fit R/R with the short below support; check every
`compute_signal.py` factor, not just the aggregate; two-table PRIME/RADAR output;
strict scope (`data/universe.csv` constituents only).
