# ARIA — Daily Bull Put Spread Scanner

Automated, unattended scanner that evaluates the TradingView **"Adi option swing 2.0"**
screener every trading day and delivers a two-table Bull Put Spread report (desktop
notification + Telegram). It drives a local **TradingView Desktop** through the
[`tradingview-bridge`](https://github.com/tradesdontlie/tradingview-mcp) MCP server,
run by a headless Claude process.

> ⚠️ **Advisory only.** The scan never places trades. Broker/order tools are
> deliberately excluded from its allow-list. Live IBKR orders are always *staged*
> for manual review/submit by a human (see `ibkr-live-workflow.md`).

## What it does (each weekday 19:00 Israel)

1. **Pre-warm** TradingView Desktop with the CDP debug port (`--remote-debugging-port`)
   and poll until it answers — deterministic, no reliance on a flaky cold-start.
2. **Open** the "Adi option swing 2.0" screener (`ui_click` the radar icon).
3. **Scan** every constituent on the Daily (1D) interval, parsing *every* row of the
   "Premium Trading Dashboard – Adi Radmy Edition" table (not just the entry row).
4. **Report** into two clearly separated tables:
   - 🟢 **PRIME** — full `יש אישור כניסה` + strong structure that passes the MA-150 and
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
| `daily-scan.sh` | Main wrapper: env, lock, skip logic, CDP pre-warm, headless Claude run, alerting |
| `watchdog.sh` | Safety net — alerts if the day's report didn't complete |
| `prompts/bull-put-spread.md` | The scan prompt (rules, two-table output, settled-vs-provisional) |
| `prompts/smoke-test.md` | Lightweight plumbing/auth check (one ticker) |
| `us-market-holidays.txt` | NYSE full-day closures to skip (update yearly) |
| `.env.example` | Template for `.env` (TradingView, GUI, Claude auth, Telegram) |
| `priority-today.md` | *(gitignored)* optional date-gated daily priority overlay |
| `logs/`, `state/` | *(gitignored)* runtime output + hashes |

## Setup

```bash
cp .env.example .env && chmod 600 .env      # then fill in tokens
chmod +x daily-scan.sh watchdog.sh

# one-off plumbing/auth check (bypasses weekend/holiday guard):
FORCE_RUN=true PROMPT_FILE="$PWD/prompts/smoke-test.md" ./daily-scan.sh

# install the schedule:
( crontab -l 2>/dev/null; cat <<'CRON'
CRON_TZ=Asia/Jerusalem
0  19 * * 1-5 $HOME/aria-trading/daily-scan.sh >> $HOME/aria-trading/logs/cron.log 2>&1
45 19 * * 1-5 $HOME/aria-trading/watchdog.sh   >> $HOME/aria-trading/logs/watchdog.log 2>&1
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
- **Launch with CDP, never plain `tradingview`** — a non-CDP instance can't be driven by the bridge.
- **Wayland env required** (`WAYLAND_DISPLAY`) for the Electron app to start under cron.
- The run lock closes the TradingView fd (`9>&-`) so the app can't strand the lock.
- The machine must be powered on at 19:00; cron can't wake a sleeping box (the watchdog flags it).

## Strategy rules (summary)
Above MA-150; short put OTM **below** support; target R/R 1:2 (band 1:1.5–2.5);
reject if strike intervals can't fit R/R with the short below support; deep-parse the
dashboard; two-table PRIME/RADAR output; strict scope (screener constituents only).
