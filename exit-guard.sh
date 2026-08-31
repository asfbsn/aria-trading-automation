#!/usr/bin/env bash
#
# exit-guard.sh — open Bull Put Spread exit & profit-target guard
# ---------------------------------------------------------------------------
# Evaluates open bull-put-spread positions against technical invalidation,
# profit capture targets (80% of max profit — the default GTC baseline), and
# time stops (DTE <= 7).
# This is a read-only, no-judgment listing Telegram-delivered to alert on
# exit conditions — the human decides whether/how to execute orders.
#
# Deliberately separate from daily-scan.sh and gtc-guard.sh: different schedule
# and scope (monitoring existing open positions rather than scanning for new
# entries or checking pending orders), and runs independently with its own
# locks and logs so it never blocks or is blocked by them.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

export ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
STATE_DIR="${STATE_DIR:-$ARIA_HOME/state}"
PROMPT_FILE="${PROMPT_FILE:-$ARIA_HOME/prompts/bull-put-spread-exit.md}"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
LOCK_FILE="${LOCK_FILE:-$ARIA_HOME/state/exit-guard.lock}"

CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$HOME/Projects/aria-trading}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Read-only IBKR tools for positions, price history, and option snapshots.
# search_contracts added 2026-08-27: get_price_history needs a resolved
# contract_id first (same two-step dependency as daily-scan.sh's Phase B) -
# missing here since the original build, never caught because this path
# never ran until a real open position existed to test against.
# + scoped write to scratch for JSON input + scoped compute_exit_signal.py Bash prefix.
# We deliberately do NOT allow any order-placement/modification tools.
# WebSearch: earnings-proximity check for the new RECOMMEND EXIT escalation layer
# (read-only, best-effort; still zero order-placement capability).
CLAUDE_ALLOWED_TOOLS_BASE="\
Edit(/${ARIA_HOME}/state/scratch/signal_input_*.json),\
WebSearch,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_account_positions,\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
Bash(python3 ${ARIA_HOME}/scripts/compute_exit_signal.py:*)"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-$CLAUDE_ALLOWED_TOOLS_BASE}"

# Load user overrides if present. Auth note: see .env's own comment — do NOT
# export CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY here, same reason the
# daily scan broke for 5+ weeks (connector access needs the ambient login).
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Exit Guard" "$1" "${2:-}" || true
}

send_telegram() {
  local msg="$1"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  curl -s -m 30 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
}

mkdir -p "$LOG_DIR" "$STATE_DIR" "$STATE_DIR/scratch"

TODAY="$(date +%F)"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_FILE="$LOG_DIR/${TODAY}_exit-guard.md"
ERR_FILE="$LOG_DIR/${TODAY}_exit-guard-stderr.log"

on_err() { local ec=$?; notify "ARIA Exit Guard FAILED" "exit $ec — see $ERR_FILE"; exit "$ec"; }
trap on_err ERR

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another run holds the lock — exiting." >>"$ERR_FILE"
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
  # This box's cron (classic Debian/Ubuntu 3.0pl1) silently ignores CRON_TZ —
  # confirmed via syslog 2026-08-31: a "CRON_TZ=America/New_York, 0 9" entry
  # fired at 09:00 system-local (Asia/Jerusalem) time, 7h early, not 09:00 ET.
  # Fix: crontab fires this on a wide Israel-local window every 15min (see
  # crontab comment); this check gates the real work to the true NY-local
  # pre-open window regardless of DST state on either side.
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
  echo "# Bull Put Spread Exit Guard — $TODAY"
  echo "_Run ${RUN_TS}_"
  echo
} >"$LOG_FILE"

set +e
printf '%s' "$PROMPT" | timeout "${CLAUDE_TIMEOUT:-5m}" "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$CLAUDE_ALLOWED_TOOLS" \
  >>"$LOG_FILE" 2>>"$ERR_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
set -e

set +e
trap - ERR
if [ "${CLAUDE_EC:-1}" = "0" ] && grep -q '^POSITIONS_CHECKED:' "$LOG_FILE"; then
  BODY="$(sed -n '3,40p' "$LOG_FILE" | head -c 3800)"
  send_telegram "$(printf '🛡️ %s\n\n%s' "$TODAY" "$BODY")"
  notify "Exit Guard sent" "$TODAY"
  echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
  exit 0
else
  send_telegram "🔴 Exit Guard FAILED — ${TODAY}. Check open positions manually. Log: $LOG_FILE"
  notify "Exit Guard FAILED" "check positions manually"
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}" >>"$ERR_FILE"
  exit 1
fi
