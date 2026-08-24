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
# Edit(.../state/scratch/signal_input.json) exists ONLY so compute_signal.py
# can be fed via `--input <path>` instead of a heredoc. Confirmed 2026-08-25:
# a Bash command whose argument literally contains JSON (any `{`/`"` mix — a
# heredoc body, an echo pipe, anything) is auto-denied by Claude Code's own
# command-safety heuristic as "expansion obfuscation" — this is NOT the
# allowedTools gate, it can't be worked around by editing this list, and it
# silently ate every ticker on every run for as long as the prompt asked for
# a heredoc (the run just sits with zero output until CLAUDE_TIMEOUT kills
# it). The file gets overwritten once per ticker; scoped to one exact path,
# not a directory glob, so nothing else can be written under $ARIA_HOME.
# NOTE: the grant is `Edit(path)`, not `Write(path)` — confirmed directly from
# Claude Code's own permission-check error: "Write(path) is not matched by
# file permission checks — only Edit(path) rules are ... Edit rules cover all
# file-editing tools." The model still calls the Write tool; the allow-rule
# just has a different name than the tool it covers.
CLAUDE_ALLOWED_TOOLS_BASE="\
Read(/${ARIA_HOME}/data/universe.csv),\
Edit(/${ARIA_HOME}/state/scratch/signal_input.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
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
# 2. Skip logic — weekends (belt & braces) + US market holidays
# ===========================================================================
DOW="$(date +%u)"  # 1=Mon … 7=Sun
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "[$RUN_TS] FORCE_RUN=true — bypassing weekend/holiday skip (manual test)." >>"$ERR_FILE"
else
  if [ "$DOW" -ge 6 ]; then
    echo "[$RUN_TS] Weekend — US market closed. Skip." >>"$ERR_FILE"; exit 0
  fi
  if [ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE"; then
    echo "[$RUN_TS] $TODAY is a US market holiday. Skip." >>"$ERR_FILE"
    notify "ARIA scan skipped" "$TODAY — US market holiday"; exit 0
  fi
fi

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
# every universe ticker was actually processed — a loose text-headline check
# (e.g. grepping for "PRIME|RADAR|REJECT") is bypassable: a clean run that
# reports an IBKR error as a REJECT for every ticker, while still emitting
# SCREENER_CONSTITUENTS, would pass a headline check without processing any
# real signal, and the watchdog would never fire. So require:
#   1. SIGNALS_COMPLETED + SIGNALS_FAILED both present and parse as integers
#   2. SIGNALS_FAILED == 0            (any tool/data failure = hard fail)
#   3. SIGNALS_COMPLETED == universe row count (data/universe.csv minus header)
set +e   # bulletproof the alert/exit path: never let a stray non-zero (notify-send
         # failing under cron, grep -c returning 1, etc.) trip set -e and skip the alert.
trap - ERR
UNIVERSE_COUNT="$(($(wc -l < "$CLAUDE_PROJECT_DIR/data/universe.csv") - 1))"
SIGNALS_COMPLETED="$(grep -m1 '^SIGNALS_COMPLETED:' "$LOG_FILE" | grep -oE '[0-9]+' | head -1)"
SIGNALS_FAILED="$(grep -m1 '^SIGNALS_FAILED:' "$LOG_FILE" | grep -oE '[0-9]+' | head -1)"
if [ "${CLAUDE_EC:-1}" = "0" ] \
  && grep -q '^SCREENER_CONSTITUENTS:' "$LOG_FILE" \
  && [ -n "$SIGNALS_COMPLETED" ] && [ -n "$SIGNALS_FAILED" ] \
  && [ "$SIGNALS_FAILED" = "0" ] \
  && [ "$SIGNALS_COMPLETED" = "$UNIVERSE_COUNT" ]; then
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
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}, marker=$(grep -c '^SCREENER_CONSTITUENTS:' "$LOG_FILE" 2>/dev/null || echo 0), signals_completed=${SIGNALS_COMPLETED:-?}, signals_failed=${SIGNALS_FAILED:-?}, universe_count=${UNIVERSE_COUNT:-?}" >>"$ERR_FILE"
  exit 1
fi
