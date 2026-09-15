#!/usr/bin/env bash
#
# research-reminder.sh — periodic nudge to re-run the IAF gate-hypothesis
# research tool (docs/research-methodology.md).
# ---------------------------------------------------------------------------
# Deliberately does NOT run the analysis itself: interpreting a walk-forward
# survival/permutation result needs a human read, not an unattended cron
# job. This only sends a reminder — quarterly by default (matches
# scripts/refresh_universe.py's own "re-run every 1-3 months" cadence), and
# "when needed" ad hoc use (a new gate-tuning idea) can't be scheduled at
# all — that part is on you to remember to invoke, this only covers the
# periodic half.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

export ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
mkdir -p "$LOG_DIR"

RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
ERR_FILE="$LOG_DIR/research-reminder-stderr.log"

[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Research Reminder" "$1" "${2:-}" || true
}

send_telegram() {
  local msg="$1"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 1
  fi
  # Token via -K/process-substitution, not a literal argv URL: a bare
  # "bot<TOKEN>/sendMessage" URL as a curl argument is visible to any other
  # local user via `ps`/`/proc/<pid>/cmdline` for curl's runtime.
  # CodeRabbit finding, 2026-08-31.
  # Without --fail, curl treats an HTTP 4xx/5xx response as a normal
  # (exit-0) transfer -- it just downloads the error body -- and the old
  # `|| true` also swallowed real transport errors (DNS/timeout/refused).
  # Check the actual HTTP status so a caller can tell whether this reminder
  # genuinely reached Telegram before logging "sent" (CodeRabbit finding).
  local response http_code curl_ec
  response="$(curl -sS -m 30 -w $'\n%{http_code}' \
    -K <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tok") \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" 2>>"$ERR_FILE")"
  curl_ec=$?
  if [ "$curl_ec" -ne 0 ]; then
    echo "[$RUN_TS] TELEGRAM SEND FAILED -- curl transport error (exit $curl_ec)." >>"$ERR_FILE"
    return 1
  fi
  http_code="${response##*$'\n'}"
  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    echo "[$RUN_TS] TELEGRAM SEND FAILED -- HTTP $http_code. Response: ${response%$'\n'*}" >>"$ERR_FILE"
    return 1
  fi
  return 0
}

MSG="$(cat <<EOF
📊 ARIA Research Reminder — $(date +%F)

Quarterly nudge: re-run the entry-gate structural survival test, or any
time you've got a new gate-tuning idea to sanity-check before touching
live logic.

  source ~/venvs/trading-eval/bin/activate
  python3 ~/Projects/aria-trading/scripts/research/iaf_ma150_survival_bridge.py

Full writeup: docs/research-methodology.md
EOF
)"

notify "ARIA Research Reminder" "Time to re-run the gate research script — see Telegram/docs/research-methodology.md"
if send_telegram "$MSG"; then
  echo "[$RUN_TS] Reminder sent." >>"$ERR_FILE"
else
  # send_telegram already logged the specific reason (not configured / HTTP
  # error / transport error) right above this line.
  echo "[$RUN_TS] Reminder did NOT reach Telegram -- see the line above. Desktop notify still fired." >>"$ERR_FILE"
fi
