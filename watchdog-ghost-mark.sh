#!/usr/bin/env bash
# ARIA Ghost Mark watchdog — alerts via Telegram if today's Ghost System marking scan
# did NOT produce a COMPLETE report by check time. Pure bash + curl (NO Claude
# usage), so it always runs and catches silent misses: machine off, cron didn't
# fire, usage exhausted, or a stuck run. Mirrors watchdog-ghost.sh's exact pattern.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

ARIA_HOME="${ARIA_HOME:-$HOME/Projects/aria-trading}"
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

TODAY="$(date +%F)"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"

# No run is expected on weekends / US market holidays -- stay quiet.
DOW="$(date +%u)"; [ "$DOW" -ge 6 ] && exit 0
[ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE" && exit 0

tg() {
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Telegram not configured -- watchdog cannot alert." >&2
    return 1
  fi
  # Token via -K process substitution, not argv -- a literal "bot<TOKEN>/..."
  # URL is visible to any other local user via ps/proc cmdline.
  # --fail turns an HTTP error response into a nonzero curl exit.
  curl -s -m 30 --fail -K <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tok") \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null
}

# A complete run exists iff daily-scan-ghost-mark.sh itself logged its own "Done"
# line -- written only after CLAUDE_EC==0 AND the observation-reconciliation
# checks all pass (see that script). Checking for this bash-authored ERR_FILE
# line, not a PROCESSED/MARKED/EXITED pattern grepped out of the LOG_FILE,
# avoids a false "complete" read.
ERR_FILE="$ARIA_HOME/logs/${TODAY}_daily-scan-ghost-mark-stderr.log"
if [ -f "$ERR_FILE" ] && grep -qF '] Done → ' "$ERR_FILE"; then
  exit 0   # marking scan completed -- nothing to alert
fi

tg "⚠️ ARIA Ghost Mark watchdog: today's (${TODAY}) Ghost System marking scan did NOT complete by $(date '+%H:%M %Z'). Likely the machine was off, cron didn't fire, or a stuck run. Check ${ARIA_HOME}/logs/${TODAY}_daily-scan-ghost-mark*.log and run manually with: FORCE_RUN=true ${ARIA_HOME}/daily-scan-ghost-mark.sh"
