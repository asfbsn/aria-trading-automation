# ARIA — Daily Bull Put Spread Scanner

Automated, unattended scanner that evaluates a static large/mid-cap US universe
every trading day and delivers a two-table Bull Put Spread report (desktop
notification + Telegram), via a headless Claude process — no TradingView
dependency in the automated path. Two-phase signal source: the bulk of the
universe is classified locally from yfinance data (Phase A); **IBKR** is the
sole authoritative source for the finalist set — every name that can reach a
PRIME classification or a trade directive is IBKR-verified (Phase B), and all
option-chain/pricing data is always IBKR, never yfinance.

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

## What it does (each weekday 19:00 Israel — enforced 19:00–20:00 execution window)

1. **Prescreen locally** (`scripts/prescreen.py`, yfinance, zero Claude/IBKR usage):
   cuts the ~654-ticker universe to a shortlist (~130–140 live-tested) using
   deliberately widened MA-band rules, and computes full entry-signal checks
   locally for every shortlisted name via `signal_core.entry_checks()`.
   Over-inclusive by design — a name yfinance can't evaluate passes through to
   the shortlist rather than being dropped; only a confirmed band failure
   filters a name before the scan stage.
2. **Two-Phase Scan** on the Daily (1D) interval:
   - **Phase A (Local classification, zero IBKR calls):** Claude classifies
     ~125–130 non-finalist shortlisted names into REJECT/RADAR directly from
     prescreen's local data (zero tool calls, near-zero tokens). Tradeoff stated
     plainly: REJECT/RADAR decisions for the bulk of the universe are now made
     on yfinance data, not IBKR — a same-session validation (49 real tickers)
     found 49/49 agreement on `entry_confirmed`, but this is a real tradeoff,
     not a transparent no-op.
   - **Phase B (Authoritative IBKR verification):** Only the finalist set
     (local `entry_confirmed: true` candidates + prescreen data failures,
     typically <=15/day) pulls IBKR bars and runs `scripts/compute_signal.py`
     (settled & provisional passes). IBKR remains the sole authoritative signal
     source for all finalists and any name that can reach a directive.
3. **Verify** R/R on the live IBKR option chain for PRIME-eligible names only.
4. **Research-gate** each surviving PRIME name (WebSearch/WebFetch): earnings
   timing vs expiry, analyst sentiment, news catalysts, SEC filings. A red flag on
   the first three downgrades to RADAR; a material SEC finding (incl. a Form 4
   insider-selling cluster: 3+ distinct insiders in a 30-day window over 90 days)
   hard-rejects. One scan-start macro check (VIX / SPY-vs-MA150 / scheduled events)
   heads the report as Market Context.
5. **Emit a trade directive** per final PRIME name: exact strikes/expiry (a
   dynamic, chain-driven width — see Position & exit rules below, not a fixed
   $10), entry limit credit + minimum (1:2.5 floor), EXECUTE-NOW-vs-HOLD trigger
   from the provisional bar (must confirm at support with volume), position size
   at 10% of net-liq max loss with narrower-width fallback (0 contracts at every
   tested width ⇒ BLOCKED), 5-position cap + sector diversification guards on
   validated leg pairs, and mandatory exits: GTC buy-to-close at 20% of credit
   (80% capture), stop below short strike/MA150, DTE≤7 time stop. Advisory only
   — a human places every order.
6. **Report** into two clearly separated tables:
   - 🟢 **PRIME** — `entry_confirmed: true` + strong structure + verified
     1:1.5–2.5 R/R + clean research gate (the only execution-ready names).
   - 🟡 **RADAR** — "setups in the making", research-gate downgrades, plus any
     discretionary "hidden gems" flagged from independent TA. Watch-only.
7. **Deliver** a dated log + desktop `notify-send` + Telegram (report inline + attached).

Two companion guards (separate schedules/locks, both read-only, Telegram-delivered):
- **`gtc-guard.sh`** — pre-open listing of EVERY live order on the account
  (added after a forgotten GTC close order gap-filled at the 2026-08-25 open).
  Delivery failure fails the run loudly; long lists warn on truncation.
- **`exit-guard.sh`** — monitors open bull-put-spread positions (validated leg
  pairs only) via `scripts/compute_exit_signal.py` across four verdict tiers:
  CLOSE (thesis broken, ≥80% profit captured, DTE≤7 not underwater),
  RECOMMEND EXIT (imminent earnings within 5% of strike, DTE≤7 underwater),
  WATCH (RSI>70, bearish candle), or HOLD, with live premarket price freshness annotations.

A **watchdog** (`watchdog.sh`, pure bash+curl, no Claude usage) runs at 19:45 and
Telegram-alerts if a complete report wasn't produced — so a silent miss never goes
unnoticed.

## Files

| File | Purpose |
|---|---|
| `daily-scan.sh` | Main wrapper: env, lock, skip logic + 19:00–20:00 window check, local prescreen, headless Claude run, shortlist-membership validation, alerting |
| `gtc-guard.sh` | Pre-open safety check: Telegram-lists every live order for human review (prompt: `prompts/gtc-order-guard.md`) |
| `exit-guard.sh` | Open-position exit monitor: CLOSE/RECOMMEND EXIT/WATCH/HOLD verdicts per spread + live premarket price check (prompt: `prompts/bull-put-spread-exit.md`) |
| `watchdog.sh` | Safety net — alerts if the day's report didn't complete |
| `research-reminder.sh` | Quarterly nudge (Telegram/desktop) to re-run the entry-gate research tool — never runs the analysis itself, see `docs/research-methodology.md` |
| `data/universe.csv` | Static candidate universe (ticker, sector, approx market cap) — replaces the live screener list |
| `scripts/prescreen.py` | Local yfinance prescreen: universe → shortlist JSON (`state/scratch/prescreen_<date>.json`) with full `entry_checks()`; authoritative `entry_confirmed` for non-finalists, pass-through on data failures |
| `scripts/compute_exit_signal.py` | Exit-signal computation for open positions (thesis invalidation vs short strike / MA150) |
| `scripts/refresh_universe.py` | Regenerates `data/universe.csv` from a 4-source union (S&P 500, S&P MidCap 400, Russell 3000, Nasdaq-100) with real per-ticker market cap via yfinance, filtered to $10B–$5T. Re-run every 1–3 months |
| `scripts/compute_signal.py` | Local entry-signal proxy (RSI/MA/volume/candle) computed from IBKR bars — replaces the TradingView dashboard read |
| `scripts/signal_core.py` | Shared entry + exit rule logic between the live scanner and the backtest engine |
| `scripts/research/` | Standalone statistical research tooling (walk-forward / permutation testing against entry-gate hypotheses), isolated from the live pipeline — see [`docs/research-methodology.md`](docs/research-methodology.md) |
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

# Pre-open guards, target 09:00-09:14 America/New_York. CRON_TZ does NOT work
# on this box's cron (classic Debian/Ubuntu 3.0pl1 -- confirmed via syslog
# 2026-08-31: a "CRON_TZ=America/New_York, 0 9" entry fired at 09:00
# Israel-local, 7h early, not 09:00 ET -- CRON_TZ is silently accepted as an
# inert env var, never used to reinterpret the schedule fields). Fix: fire
# every 15min across the full Israel-local range "09:00 ET" can fall in
# across the year (15:00-17:14 IDT/IST, wider during the ~2-4wk/year
# Israel/US DST transition dates don't coincide); gtc-guard.sh/exit-guard.sh
# each have their own internal NY-time check (TZ='America/New_York' date)
# that skips-and-exits-0 outside the real window, so only one of these ~20
# firings/day per script actually runs -- the rest are a no-op exit, no
# Claude/API cost:
CRON_TZ=Asia/Jerusalem
*/15 14-18 * * 1-5 $HOME/Projects/aria-trading/gtc-guard.sh  >> $HOME/Projects/aria-trading/logs/gtc-guard.log 2>&1
*/15 14-18 * * 1-5 $HOME/Projects/aria-trading/exit-guard.sh >> $HOME/Projects/aria-trading/logs/exit-guard.log 2>&1

# Quarterly nudge to re-run the entry-gate research tool (docs/research-methodology.md).
# Sends a reminder only -- never runs the analysis unattended.
CRON_TZ=Asia/Jerusalem
0 9 1 1,4,7,10 * $HOME/Projects/aria-trading/research-reminder.sh >> $HOME/Projects/aria-trading/logs/research-reminder.log 2>&1
CRON
) | crontab -
```

### Auth note
Run `claude login` once on this machine and export **nothing** for Claude auth
in `.env`. Confirmed 2026-08-24 (root cause of 5+ weeks of failed scans):
exporting `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) or `ANTHROPIC_API_KEY`
makes the headless `claude --print` authenticate via that token instead of the
machine's ambient login session — and a token/API-key session carries no
claude.ai connector access, so IBKR silently fails to attach on every
script-invoked run while direct-shell testing (which never exported these
vars) worked every time. Keep the machine logged in via `claude login`; cron
inherits the same ambient session.

### Daily priority overlay (optional)
Drop a `priority-today.md` with first line `PRIORITY_DATE: YYYY-MM-DD` and a list of
tickers to surface them at the top of that day's report. It auto-expires (date-gated).

## Operational notes / gotchas
- The machine must be powered on at 19:00; cron can't wake a sleeping box (the watchdog flags it).
- `data/universe.csv` goes stale slowly (market-cap band is wide, Russell 1000 only
  reconstitutes semi-annually) — refresh every 1–3 months with
  `python3 scripts/refresh_universe.py`, and update its `RUSSELL_1000_TOTAL_MKTCAP`
  constant from FTSE Russell's latest published reconstitution figure first.
- **Known open issue (2026-08-26): headless cron runs still don't see the IBKR
  connector** — every cron log 08-11 → 08-25 is an abort or empty header, while
  interactive sessions attach IBKR fine. Until root-caused, run scans from an
  interactive session (`FORCE_RUN=true ./daily-scan.sh`). The auth note below fixed
  one cause (exported tokens); something in the cron environment still breaks
  connector attach.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are not in `.env`/`.env.example` — all
  four scripts treat Telegram as optional and silently skip when unset. Set them
  (in `.env` or the cron environment) or delivery is desktop-notify only.
- Universe symbol hygiene: yfinance chokes on a few rows (`BRKB` needs `BRK-B`
  format; `XTSLA` is a cash fund; `HEIA`, `FDXF`, `HONA`, `SPCX`, `SUNB` also fail).
  Harmless — the prescreen passes them through to IBKR — but worth cleaning on the
  next `refresh_universe.py` pass.
- The execution window check skips any run whose local start hour isn't 19:xx
  (`FORCE_RUN=true` bypasses). Israel/US DST transitions don't coincide, so ~2–3
  weeks/year the window is 13:00 ET rather than 12:00 — defined in LOCAL time on
  purpose.

## Strategy rules (summary)
Above MA-150; short put OTM **below** support; target R/R 1:2 (band 1:1.5–2.5);
reject if strike intervals can't fit R/R with the short below support; check every
`compute_signal.py` factor, not just the aggregate; research gate on PRIME names
(earnings/analyst/news downgrade to RADAR, material SEC finding hard-rejects);
two-table PRIME/RADAR output plus per-PRIME trade directives; strict scope
(`data/universe.csv` constituents only, prescreen-shortlisted).

**Position & exit rules (updated 2026-09-02):** spread width is now **dynamic,
derived from each name's actual live chain spacing** (S, 2S, 4S — never a fixed
$10; many higher-priced/wide-interval names like JBHT/APD only ever list $10
apart, others list $1–2.50) — the widest width that clears both the liquidity
gate and the 1:1.5–2.5 R/R band is preferred, with narrower widths kept as
fallbacks for accounts too small to size the widest one. Max loss per spread =
**10% of net liquidation value** (contracts = floor(10% × net_liq /
(width−credit) × 100); retries narrower fallback widths before giving up; 0 ⇒
BLOCKED, never a 0-contract order); **max 5 concurrent spreads** (10% × 5 = same
**50% total portfolio risk** ceiling as before — only the per-trade/count split
changed) with **strict sector diversification** (one spread per sector);
default exit = **GTC buy-to-close at 20% of received credit (80% capture)**, plus
stop on a close below the short strike or MA150 and a DTE≤7 time stop — the same
thresholds `exit-guard.sh` monitors.
