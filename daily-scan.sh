#!/usr/bin/env bash
#
# daily-scan.sh — ARIA "Adi option swing 2.0" Bull Put Spread daily scanner
# ---------------------------------------------------------------------------
# Runs a headless Claude scan against data/universe.csv (a static large/mid-cap
# US universe standing in for the live TradingView screener — see
# scripts/refresh_universe.py for why), computes the entry signal locally via
# scripts/compute_signal.py against IBKR bars, verifies R/R against the live
# IBKR option chain, writes a dated log, and pings the desktop when finished.
#
# No TradingView dependency: this used to drive TradingView Desktop via the
# tradingview-bridge MCP for both the screener list and the entry-signal
# dashboard read. Both were replaced (2026-08-24) — the screener list by
# data/universe.csv, the dashboard read by compute_signal.py — after repeated
# unattended-session failures (TradingView Desktop crashing/hanging under
# cron; see git log for scripts/refresh_universe.py and this file). TV stays
# useful for interactive/ad-hoc work, just not as a hard dependency here.
#
# Designed to run from cron (headless), so it explicitly re-establishes the GUI
# session env (DISPLAY / DBUS / XDG_RUNTIME_DIR) and a sane PATH — kept even
# without TradingView since notify-send still needs it.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

# --- minimal cron PATH (cron starts with almost nothing) -------------------
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ===========================================================================
# 0. Configuration  (override anything in ~/aria-trading/.env)
# ===========================================================================
export ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
STATE_DIR="${STATE_DIR:-$ARIA_HOME/state}"
PROMPT_FILE="${PROMPT_FILE:-$ARIA_HOME/prompts/bull-put-spread.md}"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
# Phase B token-protection cap (scripts/prescreen.py --top-k): max entry_confirmed
# tickers forwarded to IBKR verification. See prescreen.py's --top-k help for the
# ranking rule (RSI20 ascending, tie-broken by MA150 proximity).
PRESCREEN_TOP_K="${PRESCREEN_TOP_K:-12}"
LOCK_FILE="${LOCK_FILE:-$ARIA_HOME/state/daily-scan.lock}"

# Claude project dir = where data/universe.csv and scripts/compute_signal.py
# live (this session's cwd). Run from here so relative paths resolve.
CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$HOME/Projects/aria-trading}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Read-only IBKR option-chain tools (to verify PRIME R/R against LIVE prices)
# + Read on the static universe file + the scoped compute_signal.py Bash prefix.
# We deliberately do NOT allow any order-placement tools (the broker MCP's
# create_order_instruction / delete_order_instruction).
#
# Entry-signal computation: the "Premium Trading Dashboard - Adi Radmy Edition"
# Pine indicator is protected/invite-only (source unavailable — confirmed
# 2026-07-09) and required a live TradingView chart render to read at all.
# scripts/compute_signal.py replaces it entirely: a local proxy built from the
# indicator's declared inputs (not its exact formulas) computed straight from
# IBKR bars, no TradingView dependency. The former SIGNAL_COMPARISON_MODE trial
# (running both in parallel) was retired 2026-08-24 — it never completed a
# single successful comparison run in 5+ weeks (TradingView kept crashing
# under cron), so there was nothing to validate against; compute_signal.py is
# now the sole signal source.
#
# Edit(.../state/scratch/signal_input_*.json) exists ONLY so compute_signal.py can be fed
# via `--input <path>` instead of a heredoc. Confirmed 2026-08-25: a Bash
# command whose argument literally contains JSON (any `{`/`"` mix — a heredoc
# body, an echo pipe, anything) is auto-denied by Claude Code's own
# command-safety heuristic as "expansion obfuscation" — this is NOT the
# allowedTools gate, it can't be worked around by editing this list, and it
# silently ate every ticker on every run for as long as the prompt asked for
# a heredoc (the run just sits with zero output until CLAUDE_TIMEOUT kills
# it).
# NOTE: the grant is `Edit(path)`, not `Write(path)` — confirmed directly from
# Claude Code's own permission-check error: "Write(path) is not matched by
# file permission checks — only Edit(path) rules are ... Edit rules cover all
# file-editing tools." The model still calls the Write tool; the allow-rule
# just has a different name than the tool it covers.
# Widened to a *.json glob (2026-08-25, was a single exact-path file): the
# original per-ticker sequential loop reused ONE file, which is fine one at a
# time but can't be parallelized — concurrent writes to the same path race.
# Batched scanning needs one file per ticker (signal_input_<TICKER>.json), so
# many tickers' price-history writes + compute_signal.py reads can happen in
# the same turn without clobbering each other. Still scoped to one
# subdirectory only, nothing else under $ARIA_HOME is writable.
#
# WebSearch + WebFetch: qualitative research gate for names surviving R/R
# verification (earnings timing, analyst sentiment, news catalysts, SEC filings;
# 0–3 PRIME-eligible names per run). Read-only research only — no order-placement
# capability added.
#
# get_account_summary + get_account_positions: read-only account data for
# trade-directive sizing ("Global Heap" dynamic allocator model, updated
# 2026-09-02: PRIME survivors ranked by R/R, allocated top-down against a
# shared 50%-of-net-liq pool with per-trade blocks sized by spread geometry,
# dynamic chain-driven width) and portfolio guards (sector diversification;
# position count now floats by geometry instead of a fixed cap); still zero
# order-placement capability.
CLAUDE_ALLOWED_TOOLS_BASE="\
Read(/${ARIA_HOME}/data/universe.csv),\
Read(/${ARIA_HOME}/state/scratch/prescreen_*.json),\
Edit(/${ARIA_HOME}/state/scratch/signal_input_*.json),\
WebSearch,\
WebFetch,\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_account_summary,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_account_positions,\
Bash(python3 ${ARIA_HOME}/scripts/compute_signal.py:*)"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-$CLAUDE_ALLOWED_TOOLS_BASE}"

# If true, exit early (no scan) when the screener constituents are byte-identical
# to the previous run. Default false: name lists rarely change day-to-day even on
# valid trading days (the dashboard *values* do), so a hard skip would suppress
# good runs. Kept as an opt-in guard against a stale/frozen feed.
HARD_SKIP_ON_UNCHANGED="${HARD_SKIP_ON_UNCHANGED:-false}"

# Load user overrides if present.
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

# ===========================================================================
# 1. GUI session env so a headless cron shell can reach notify-send
# ===========================================================================
export DISPLAY="${DISPLAY:-:0}"
UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

notify() {  # no-op if notify-send is missing
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Scan" "$1" "${2:-}" || true
}

# Send a status line + (optionally) attach a file to Telegram. No-op if the bot
# token / chat id aren't set. Plain text (no parse_mode) to avoid markdown-entity
# 400s; the full report goes as a document so the 4096-char limit doesn't truncate.
send_telegram() {
  local msg="$1" file="${2:-}"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  curl -s -m 30 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
  if [ -n "$file" ] && [ -f "$file" ]; then
    curl -s -m 60 "https://api.telegram.org/bot${tok}/sendDocument" \
      -F "chat_id=${chat}" -F "document=@${file}" \
      -F "caption=ARIA Bull Put Spread report — ${TODAY}" >/dev/null 2>>"$ERR_FILE" || true
  fi
}

mkdir -p "$LOG_DIR" "$STATE_DIR" "$STATE_DIR/scratch"

TODAY="$(date +%F)"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_FILE="$LOG_DIR/${TODAY}_bull-put-spread.md"
ERR_FILE="$LOG_DIR/${TODAY}_stderr.log"

on_err() { local ec=$?; notify "ARIA scan FAILED" "exit $ec — see $ERR_FILE"; exit "$ec"; }
trap on_err ERR

# Prevent overlapping runs (a hung TradingView/Claude shouldn't stack up).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another run holds the lock — exiting." >>"$ERR_FILE"
  exit 0
fi

# ===========================================================================
# 2. Skip logic — weekends (belt & braces) + US market holidays + execution window
# ===========================================================================
DOW="$(date +%u)"  # 1=Mon … 7=Sun
HH="$(date +%H)"   # local hour (00-23)
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "[$RUN_TS] FORCE_RUN=true — bypassing weekend/holiday/window skip (manual test)." >>"$ERR_FILE"
else
  if [ "$DOW" -ge 6 ]; then
    echo "[$RUN_TS] Weekend — US market closed. Skip." >>"$ERR_FILE"; exit 0
  fi
  if [ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE"; then
    echo "[$RUN_TS] $TODAY is a US market holiday. Skip." >>"$ERR_FILE"
    notify "ARIA scan skipped" "$TODAY — US market holiday"; exit 0
  fi
  # Execution window: 19:00–20:00 system local (Israel). Israel/US DST transitions
  # don't coincide, so ~2–3 weeks/year 19:00 IDT is 13:00 ET instead of 12:00 — the
  # window is deliberately defined in LOCAL time per the user's instruction,
  # keeping cron (19:00 local) and this check consistent year-round.
  if [ "$HH" -ne 19 ]; then
    echo "[$RUN_TS] Outside 19:00–20:00 local execution window (current hour: $HH). Skip." >>"$ERR_FILE"
    exit 0
  fi
fi

# ===========================================================================
# 2.5. Local technical prescreen (token economy)
#      Claude was ingesting 220-bar histories for ~654 universe tickers when
#      ~550 fail basic MA support/pullback bands. The prescreen does that first
#      cut locally for free via yfinance. It is deliberately over-inclusive;
#      IBKR + compute_signal.py remain authoritative for every shortlisted name.
# ===========================================================================
PRESCREEN_FILE="$STATE_DIR/scratch/prescreen_${TODAY}.json"
set +e
timeout 10m python3 "$ARIA_HOME/scripts/prescreen.py" --output "$PRESCREEN_FILE" --top-k "$PRESCREEN_TOP_K" >>"$ERR_FILE" 2>&1
PRESCREEN_EC=$?
set -e
if [ "$PRESCREEN_EC" -ne 0 ]; then
  notify "ARIA scan FAILED" "prescreen failed — see stderr log"
  send_telegram "🔴 ARIA scan FAILED / prescreen failed — ${TODAY}. Check $ERR_FILE."
  echo "[$RUN_TS] FAILURE: prescreen exit code $PRESCREEN_EC" >>"$ERR_FILE"
  exit 1
fi
SHORTLIST_COUNT="$(python3 -c "import json; print(len(json.load(open('$PRESCREEN_FILE'))['shortlist']))")"
EXPECTED_FINALISTS="$(python3 -c "import json; d=json.load(open('$PRESCREEN_FILE')); print(sum(1 for v in d['per_ticker'].values() if v.get('entry_confirmed')) + len(d['failures']))")"

# ===========================================================================
# 3. Headless Claude scan against data/universe.csv
# ===========================================================================
cd "$CLAUDE_PROJECT_DIR"
PROMPT="$(cat "$PROMPT_FILE")"

# Optional TODAY-ONLY priority overlay (e.g. instructor's daily picks). Date-gated
# so it auto-expires: applied ONLY when its first line "PRIORITY_DATE: <today>"
# matches. Never alters the permanent strategy/prompt; just appended for one day.
PRIORITY_FILE="${PRIORITY_FILE:-$ARIA_HOME/priority-today.md}"
if [ -f "$PRIORITY_FILE" ] && grep -qx "PRIORITY_DATE: ${TODAY}" "$PRIORITY_FILE"; then
  PROMPT="${PROMPT}

=== TODAY-ONLY PRIORITY OVERLAY (applies only to ${TODAY}; obey permanent rules) ===
$(cat "$PRIORITY_FILE")"
  echo "[$RUN_TS] Priority overlay applied for ${TODAY}." >>"$ERR_FILE"
else
  echo "[$RUN_TS] No priority overlay active for ${TODAY}." >>"$ERR_FILE"
fi

{
  echo "# ARIA Bull Put Spread — $TODAY"
  echo "_Run $RUN_TS · 19:00 Asia/Jerusalem ≈ 12:00 ET (mid-session: today's bar is UNSETTLED)_"
  echo
} >"$LOG_FILE"

set +e
# Prompt goes via stdin: --allowedTools is variadic and would otherwise swallow a
# trailing positional prompt as a tool name.
# Hard cap on the headless run: a hung Claude/TradingView must not hold the
# lock into tomorrow's cron. timeout exit 124 is treated as a failure below.
printf '%s' "$PROMPT" | timeout "${CLAUDE_TIMEOUT:-45m}" "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$CLAUDE_ALLOWED_TOOLS" \
  >>"$LOG_FILE" 2>>"$ERR_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
set -e

# ===========================================================================
# 4. "Unchanged screener" guard (stale/frozen-feed detector)
#    The prompt emits a line:  SCREENER_CONSTITUENTS: SYM1,SYM2,...
# ===========================================================================
LAST_HASH_FILE="$STATE_DIR/last-constituents.sha"
SYMS_LINE="$(grep -m1 '^SCREENER_CONSTITUENTS:' "$LOG_FILE" || true)"
if [ -n "$SYMS_LINE" ]; then
  NORM="$(printf '%s' "${SYMS_LINE#SCREENER_CONSTITUENTS:}" | tr ',' '\n' | tr -d '[:space:]' | sort -u)"
  NEW_HASH="$(printf '%s' "$NORM" | sha256sum | cut -d' ' -f1)"
  if [ -f "$LAST_HASH_FILE" ] && [ "$NEW_HASH" = "$(cat "$LAST_HASH_FILE")" ]; then
    {
      echo
      echo "> ⚠️ **Screener constituents identical to the previous run.** Possible stale/frozen"
      echo "> feed or a non-trading day the holiday table missed — treat signals with caution."
    } >>"$LOG_FILE"
    if [ "$HARD_SKIP_ON_UNCHANGED" = "true" ]; then
      notify "ARIA scan skipped" "Screener unchanged (stale feed?)"
      printf '%s' "$NEW_HASH" >"$LAST_HASH_FILE"
      exit 0
    fi
  fi
  printf '%s' "$NEW_HASH" >"$LAST_HASH_FILE"
fi

# ===========================================================================
# 5. Done — desktop notification
# ===========================================================================
# Success requires a clean exit AND a structured completion record proving
# every shortlisted ticker was actually processed and all finalists were verified —
# a loose text-headline check (e.g. grepping for "PRIME|RADAR|REJECT") is bypassable:
# a clean run that reports an IBKR error as a REJECT for every ticker, while still
# emitting SCREENER_CONSTITUENTS, would pass a headline check without processing any
# real signal, and the watchdog would never fire. So require:
#   1. SIGNALS_COMPLETED + SIGNALS_FAILED + FINALISTS_VERIFIED present and parse as integers
#   2. SIGNALS_FAILED == 0            (any tool/data failure = hard fail)
#   3. SIGNALS_COMPLETED == shortlist count (from prescreen JSON)
#   4. FINALISTS_VERIFIED == expected finalists count (prescreen entry_confirmed + failures)
#      Why: closes the gap where Claude could under-route names to the expensive Phase B
#      verification phase to save tokens/time without the wrapper catching it — a run that
#      verifies fewer finalists than prescreen implies must fail loudly, same principle
#      as the SIGNALS_FAILED>0 hard-fail.
set +e   # bulletproof the alert/exit path: never let a stray non-zero (notify-send
         # failing under cron, grep -c returning 1, etc.) trip set -e and skip the alert.
trap - ERR
SIGNALS_COMPLETED="$(grep -m1 '^SIGNALS_COMPLETED:' "$LOG_FILE" | grep -oE '[0-9]+' | head -1)"
SIGNALS_FAILED="$(grep -m1 '^SIGNALS_FAILED:' "$LOG_FILE" | grep -oE '[0-9]+' | head -1)"
FINALISTS_VERIFIED="$(grep -m1 '^FINALISTS_VERIFIED:' "$LOG_FILE" | grep -oE '[0-9]+' | head -1)"
# Membership check, not just cardinality: SCREENER_CONSTITUENTS must be exactly
# the prescreen shortlist (sorted-list compare, so duplicates/substitutions fail
# too) — a count-only check can't catch the prompt silently swapping tickers.
CONSTITUENTS_MATCH="$(python3 -c "
import json, re, sys
log = open('$LOG_FILE').read()
m = re.search(r'^SCREENER_CONSTITUENTS:(.*)\$', log, re.M)
syms = sorted(s.strip() for s in m.group(1).split(',') if s.strip()) if m else None
short = sorted(json.load(open('$PRESCREEN_FILE'))['shortlist'])
print('yes' if syms == short else 'no')
" 2>>"$ERR_FILE")"
# Same membership check for finalists: FINALISTS_VERIFIED matching the expected
# COUNT isn't enough — a run could verify the right number of finalists while
# silently omitting one prescreen implied and substituting an unrelated ticker.
# FINALISTS_VERIFIED_TICKERS must be exactly (entry_confirmed==true tickers) +
# (failures tickers) from prescreen, sorted-list compare same as constituents.
FINALISTS_MATCH="$(python3 -c "
import json, re, sys
log = open('$LOG_FILE').read()
m = re.search(r'^FINALISTS_VERIFIED_TICKERS:(.*)\$', log, re.M)
syms = sorted(s.strip() for s in m.group(1).split(',') if s.strip()) if m else None
d = json.load(open('$PRESCREEN_FILE'))
expected = sorted([t for t, v in d['per_ticker'].items() if v.get('entry_confirmed')]
                   + [f['ticker'] for f in d['failures']])
print('yes' if syms == expected else 'no')
" 2>>"$ERR_FILE")"
if [ "${CLAUDE_EC:-1}" = "0" ] \
  && grep -q '^SCREENER_CONSTITUENTS:' "$LOG_FILE" \
  && [ "$CONSTITUENTS_MATCH" = "yes" ] \
  && [ -n "$SIGNALS_COMPLETED" ] && [ -n "$SIGNALS_FAILED" ] \
  && [ "$SIGNALS_FAILED" = "0" ] \
  && [ "$SIGNALS_COMPLETED" = "$SHORTLIST_COUNT" ] \
  && [ -n "$FINALISTS_VERIFIED" ] \
  && [ "$FINALISTS_VERIFIED" = "$EXPECTED_FINALISTS" ] \
  && grep -q '^FINALISTS_VERIFIED_TICKERS:' "$LOG_FILE" \
  && [ "$FINALISTS_MATCH" = "yes" ]; then
  notify "ARIA scan ready ✓" "$TODAY — log saved"
  # Put the ACTUAL report (headline + PRIME/RADAR tables) into the message body,
  # not a generic line — and still attach the full file. Trimmed to stay under
  # Telegram's 4096-char cap; the attachment always has the complete report.
  TG_MSG="$(printf '🟢 ARIA Bull Put Spread — %s\n\n%s\n\n…(full report attached)' \
    "$TODAY" "$(sed -n '1,80p' "$LOG_FILE" | head -c 3300)")"
  send_telegram "$TG_MSG" "$LOG_FILE"
  echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
  exit 0
else
  notify "ARIA scan FAILED" "incomplete — see log"
  REASON="$(sed -n '3,6p' "$LOG_FILE" | head -c 800)"
  send_telegram "🔴 ARIA scan FAILED / incomplete — ${TODAY}. Reason: ${REASON:-unknown}. Full log attached." "$LOG_FILE"
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}, marker=$(grep -c '^SCREENER_CONSTITUENTS:' "$LOG_FILE" 2>/dev/null || echo 0), constituents_match=${CONSTITUENTS_MATCH:-?}, signals_completed=${SIGNALS_COMPLETED:-?}, signals_failed=${SIGNALS_FAILED:-?}, shortlist_count=${SHORTLIST_COUNT:-?}, finalists_verified=${FINALISTS_VERIFIED:-?}, expected_finalists=${EXPECTED_FINALISTS:-?}, finalists_match=${FINALISTS_MATCH:-?}" >>"$ERR_FILE"
  exit 1
fi
