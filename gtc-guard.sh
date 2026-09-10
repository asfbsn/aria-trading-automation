#!/usr/bin/env bash
#
# gtc-guard.sh — pre-open live-order safety check
# ---------------------------------------------------------------------------
# Added 2026-08-25 after a forgotten GTC close order on CDNS gap-filled at the
# 9:30 ET open with zero warning. This is a read-only, no-judgment listing of
# every live order, Telegram-delivered before the open — the point is the
# human reviews all of them, not that this script decides which are "safe".
#
# Deliberately separate from daily-scan.sh: different schedule (pre-open, not
# mid-session), different scope (account orders, not the universe scan), and
# a failure here should never block or be blocked by the daily scan's lock.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

export ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
STATE_DIR="${STATE_DIR:-$ARIA_HOME/state}"
PROMPT_FILE="${PROMPT_FILE:-$ARIA_HOME/prompts/gtc-order-guard.md}"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
LOCK_FILE="${LOCK_FILE:-$ARIA_HOME/state/gtc-guard.lock}"

CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$HOME/Projects/aria-trading}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Single read-only tool — no Read/Edit/Bash grants needed, this never touches
# the filesystem or computes anything, just lists what IBKR already has.
CLAUDE_ALLOWED_TOOLS_BASE="mcp__claude_ai_Interactive_Brokers_IBKR__get_account_orders"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-$CLAUDE_ALLOWED_TOOLS_BASE}"

# Load user overrides if present. Auth note: see .env's own comment — do NOT
# export CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY here, same reason the
# daily scan broke for 5+ weeks (connector access needs the ambient login).
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA GTC Guard" "$1" "${2:-}" || true
}

# Unlike the other guards' fire-and-forget Telegram, delivery failure here must
# surface: an unseen pre-open order review IS the failure this script prevents.
# Unconfigured Telegram still returns 0 (opt-in channel); a configured-but-failed
# send returns curl's status for the caller to act on.
send_telegram() {
  local msg="$1"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  # -sS: silent but still emit curl's own error text on transport failure.
  # -w '\n%{http_code}': append the HTTP status as the response's last line,
  # so a 4xx/5xx (bad token, wrong chat_id, etc) is distinguishable from a
  # transport-level failure (DNS/timeout) and from success -- replaces the
  # old bare `--fail` (which caught HTTP errors via exit code but logged no
  # detail at all, since -s alone suppresses curl's own error text too).
  # Return code is unchanged in spirit (0/1) so the existing caller's
  # if/else still works -- it just now also logs WHY, loudly.
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

mkdir -p "$LOG_DIR" "$STATE_DIR"

TODAY="$(date +%F)"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_FILE="$LOG_DIR/${TODAY}_gtc-guard.md"
ERR_FILE="$LOG_DIR/${TODAY}_gtc-guard-stderr.log"

on_err() { local ec=$?; notify "ARIA GTC Guard FAILED" "exit $ec — see $ERR_FILE"; exit "$ec"; }
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
  echo "# GTC Order Guard — $TODAY"
  echo "_Run ${RUN_TS}_"
  echo
} >"$LOG_FILE"

set +e
# --settings disables claude-mem for this invocation only -- same cold-start
# hook race that hit exit-guard.sh today (2026-09-10); see daily-scan.sh for
# the full writeup and why --bare was rejected (breaks this box's OAuth auth).
#
# Output captured to its own RUN_OUTPUT file first, validated there, then
# appended to LOG_FILE -- see exit-guard.sh's identical change (CodeRabbit
# review, 2026-09-10) for why: not a real cross-run staleness bug (LOG_FILE
# is already truncated fresh each invocation), but `grep -q` has no
# positional awareness, so validating the isolated per-invocation capture
# instead of the cumulative display file is the more defensible pattern.
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
# Requires the marker to be the LAST non-empty line, with an actual digit
# after it -- see exit-guard.sh's identical fix for the full writeup
# (2026-09-10 hook incident + CodeRabbit review same day).
if [ "${CLAUDE_EC:-1}" = "0" ] \
  && awk 'NF { last = $0 } END { exit last !~ /^ORDERS_CHECKED: [0-9]+$/ }' "$RUN_OUTPUT"; then
  # Telegram caps messages at 4096 chars; if the order list doesn't fit, say so
  # loudly rather than silently dropping rows — a truncated pre-open order review
  # is exactly the missed-order failure mode this guard exists to prevent.
  FULL_BODY="$(sed -n '3,$p' "$LOG_FILE")"
  BODY="$(printf '%s' "$FULL_BODY" | head -c 3800)"
  if [ "${#FULL_BODY}" -gt 3800 ]; then
    BODY="$BODY
⚠️ TRUNCATED — full list in $LOG_FILE, review it before the open."
  fi
  # Stale REPLACED/STATUS-UNCLEAR order sitting >1 day (prompt step 6) gets a
  # distinct message-level banner too, not just a body line — the 2026-08-25
  # CDNS incident and the week-long INTC order both went unactioned inside the
  # routine ⏰ report, so the escalation must be visible before the body is read.
  HEADER_EMOJI="⏰"
  if grep -q "ACTION REQUIRED" "$LOG_FILE"; then
    HEADER_EMOJI="🚨🚨🚨"
  fi
  if send_telegram "$(printf '%s %s\n\n%s' "$HEADER_EMOJI" "$TODAY" "$BODY")"; then
    notify "GTC Guard sent" "$TODAY"
    echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
    exit 0
  else
    notify "GTC Guard: Telegram FAILED" "orders listed in $LOG_FILE — review before the open"
    echo "[$RUN_TS] FAILURE: report built but Telegram delivery failed" >>"$ERR_FILE"
    exit 1
  fi
else
  send_telegram "🔴 GTC Guard FAILED — ${TODAY}. Check orders manually before the open. Log: $LOG_FILE"
  notify "GTC Guard FAILED" "check orders manually"
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}" >>"$ERR_FILE"
  exit 1
fi
