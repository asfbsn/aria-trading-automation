#!/usr/bin/env bash
#
# exit-guard_v2.sh — open Bull Put Spread exit & profit-target guard (v2 Draft)
# ---------------------------------------------------------------------------
# DRAFT SCRIPT FOR HUMAN REVIEW — DO NOT WIRE INTO CRON OR PRODUCTION DIRECTLY.
#
# Evaluates open bull-put-spread positions against regime-aware technical
# invalidation (via prompts/bull-put-spread-exit_v2.md and compute_exit_signal_v2.py),
# profit capture targets (80% of max profit — default GTC baseline), and
# time stops (DTE <= 7).
#
# This is a read-only, advisory monitoring script delivering alerts via Telegram.
# The human decides whether and how to execute any orders.
#
# Deliberately isolated from production exit-guard.sh:
# Uses distinct lock file (state/exit-guard-v2.lock) and distinct log files
# (logs/*_exit-guard-v2.md) to ensure a manual test or review execution NEVER
# contends with, overwrites, or alters the state of the live production pipeline.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

export ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
STATE_DIR="${STATE_DIR:-$ARIA_HOME/state}"
PROMPT_FILE="${PROMPT_FILE:-$ARIA_HOME/prompts/bull-put-spread-exit_v2.md}"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
LOCK_FILE="${LOCK_FILE:-$ARIA_HOME/state/exit-guard-v2.lock}"

CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$HOME/Projects/aria-trading}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Read-only IBKR tools for positions, price history, and option snapshots.
# + scoped write to scratch for JSON input + scoped compute_exit_signal_v2.py Bash prefix.
# We deliberately do NOT allow any order-placement/modification tools.
# WebSearch: earnings-proximity check for RECOMMEND EXIT escalation tier.
CLAUDE_ALLOWED_TOOLS_BASE="\
Edit(/${ARIA_HOME}/state/scratch/signal_input_*.json),\
WebSearch,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_account_positions,\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
Bash(python3 ${ARIA_HOME}/scripts/compute_exit_signal_v2.py:*)"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-$CLAUDE_ALLOWED_TOOLS_BASE}"

# Load user overrides if present. Auth note: see .env's own comment — do NOT
# export CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY here.
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Exit Guard v2" "$1" "${2:-}" || true
}

send_telegram() {
  local msg="$1"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  local response http_code body curl_ec
  response="$(curl -sS -m 30 -w $'\n%{http_code}' \
    "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" 2>>"$ERR_FILE")"
  curl_ec=$?
  if [ "$curl_ec" -ne 0 ]; then
    echo "[$RUN_TS] TELEGRAM SEND FAILED — curl transport error (exit $curl_ec: network/DNS/timeout). Alerts are NOT reaching Telegram." >>"$ERR_FILE"
    return 1
  fi
  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi
  echo "[$RUN_TS] TELEGRAM SEND FAILED — HTTP ${http_code:-none}. Alerts are NOT reaching Telegram. Response: ${body}" >>"$ERR_FILE"
  return 1
}

mkdir -p "$LOG_DIR" "$STATE_DIR" "$STATE_DIR/scratch"

TODAY="$(date +%F)"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_FILE="$LOG_DIR/${TODAY}_exit-guard-v2.md"
ERR_FILE="$LOG_DIR/${TODAY}_exit-guard-v2-stderr.log"

on_err() { local ec=$?; notify "ARIA Exit Guard v2 FAILED" "exit $ec — see $ERR_FILE"; exit "$ec"; }
trap on_err ERR

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another run holds the lock ($LOCK_FILE) — exiting." >>"$ERR_FILE"
  exit 0
fi

DOW="$(date +%u)"  # 1=Mon … 7=Sun
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "[$RUN_TS] FORCE_RUN=true — bypassing weekend/holiday skip (manual test)." >>"$ERR_FILE"
else
  if [ "$DOW" -ge 6 ]; then
    echo "[$RUN_TS] Weekend — US market closed. Skip." >>"$ERR_FILE"; exit 0
  fi
  if [ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE"; then
    echo "[$RUN_TS] $TODAY is a US market holiday. Skip." >>"$ERR_FILE"
    exit 0
  fi
  # 10#$(...) forces base-10 parsing — "0900" as a bare int is invalid octal.
  NY_HHMM=$((10#$(TZ='America/New_York' date +%H%M)))
  if [ "$NY_HHMM" -lt 900 ] || [ "$NY_HHMM" -ge 915 ]; then
    echo "[$RUN_TS] Outside 09:00-09:14 America/New_York (NY time now: $NY_HHMM) — skip." >>"$ERR_FILE"
    exit 0
  fi
fi

cd "$CLAUDE_PROJECT_DIR"
PROMPT="$(cat "$PROMPT_FILE")"

{
  echo "# Bull Put Spread Exit Guard (v2 Draft) — $TODAY"
  echo "_Run ${RUN_TS}_"
  echo
} >"$LOG_FILE"

set +e
RUN_OUTPUT="$(mktemp)"
trap 'rm -f "$RUN_OUTPUT"' EXIT
printf '%s' "$PROMPT" | timeout "${CLAUDE_TIMEOUT:-5m}" "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$CLAUDE_ALLOWED_TOOLS" \
  --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
  >"$RUN_OUTPUT" 2>>"$ERR_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
cat "$RUN_OUTPUT" >>"$LOG_FILE"
set -e

set +e
trap - ERR
# Requires the marker to be the LAST non-empty line, with an actual digit after it
if [ "${CLAUDE_EC:-1}" = "0" ] \
  && awk 'NF { last = $0 } END { exit last !~ /^POSITIONS_CHECKED: [0-9]+$/ }' "$RUN_OUTPUT"; then
  FULL_BODY="$(sed -n '3,$p' "$LOG_FILE")"
  BODY="$(printf '%s' "$FULL_BODY" | head -c 3800)"
  if [ "${#FULL_BODY}" -gt 3800 ]; then
    BODY="$BODY
⚠️ TRUNCATED — full report in $LOG_FILE, review it before the open."
  fi
  if send_telegram "$(printf '🛡️ [v2 Draft] %s\n\n%s' "$TODAY" "$BODY")"; then
    notify "Exit Guard v2 sent" "$TODAY"
  else
    notify "Exit Guard v2: Telegram FAILED" "report is in $LOG_FILE — review manually"
  fi
  echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
  exit 0
else
  send_telegram "🔴 Exit Guard v2 FAILED — ${TODAY}. Check open positions manually. Log: $LOG_FILE" || true
  notify "Exit Guard v2 FAILED" "check positions manually"
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}" >>"$ERR_FILE"
  exit 1
fi
