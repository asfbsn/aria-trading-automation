#!/usr/bin/env bash
# ARIA Ghost watchdog — alerts via Telegram if today's Ghost System scan did
# NOT produce a COMPLETE report by check time. Pure bash + curl (NO Claude
# usage), so it always runs and catches silent misses: machine off, cron
# didn't fire, usage exhausted, or a stuck run. Mirrors watchdog.sh's exact
# pattern for the v1 daily scan -- see that file for the same rationale.
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
  # URL is visible to any other local user via `ps`/`/proc/<pid>/cmdline`
  # (same pattern already used by daily-scan-ghost.sh's send_telegram; this
  # file had copied watchdog.sh's older, pre-fix argv version instead).
  # --fail turns an HTTP error response into a nonzero curl exit instead of
  # "successfully" delivering an error body; no `|| true` here -- this
  # function's ENTIRE JOB is the alert, so a failed send must propagate
  # (via this script's own `set -e`) rather than looking like a clean,
  # silent exit when the one thing it exists to do didn't happen
  # (CodeRabbit finding, 2026-09-17).
  curl -s -m 30 --fail -K <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tok") \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null
}

# A complete run exists iff daily-scan-ghost.sh itself logged its own "Done"
# line -- written only after CLAUDE_EC==0 AND the observation-reconciliation
# checks all pass (see that script). Checking for this bash-authored ERR_FILE
# line, not a PROCESSED/ACCEPTED/REJECTED pattern grepped out of the LOG_FILE,
# avoids a false "complete" read if Claude's free-form report text happened
# to contain a look-alike line without the run actually having reached and
# passed the wrapper's own success gate.
ERR_FILE="$ARIA_HOME/logs/${TODAY}_daily-scan-ghost-stderr.log"
if [ -f "$ERR_FILE" ] && grep -qF '] Done → ' "$ERR_FILE"; then
  exit 0   # scan completed -- nothing to alert
fi

tg "⚠️ ARIA Ghost watchdog: today's (${TODAY}) Ghost System scan did NOT complete by $(date '+%H:%M %Z'). Likely the machine was off, cron didn't fire, or a stuck run. Check ${ARIA_HOME}/logs/${TODAY}_daily-scan-ghost*.log and run manually with: ${ARIA_HOME}/daily-scan-ghost.sh"
