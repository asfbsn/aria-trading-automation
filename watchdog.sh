#!/usr/bin/env bash
# ARIA watchdog — alerts via Telegram if the daily scan did NOT produce a
# COMPLETE report by check time. Pure bash + curl (NO Claude usage), so it
# always runs and catches silent misses: machine off, cron didn't fire, usage
# exhausted, or a stuck run. Runs a bit after the 19:00 scan.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

ARIA_HOME="${ARIA_HOME:-$HOME/aria-trading}"
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

TODAY="$(date +%F)"
LOG="$ARIA_HOME/logs/${TODAY}_bull-put-spread.md"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"

# No run is expected on weekends / US market holidays — stay quiet.
DOW="$(date +%u)"; [ "$DOW" -ge 6 ] && exit 0
[ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE" && exit 0

tg() {
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  { [ -z "$tok" ] || [ -z "$chat" ]; } && return 0
  curl -s -m 30 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# A complete report exists iff today's log has the completion marker.
if [ -f "$LOG" ] && grep -q '^SCREENER_CONSTITUENTS:' "$LOG"; then
  exit 0   # scan completed — nothing to alert
fi

tg "⚠️ ARIA watchdog: today's (${TODAY}) Bull Put Spread scan did NOT complete by $(date '+%H:%M %Z'). Likely the machine was off at 19:00, cron didn't fire, or Claude usage is exhausted. Check ~/aria-trading/logs/ and run manually with: FORCE_RUN=true ~/aria-trading/daily-scan.sh"
