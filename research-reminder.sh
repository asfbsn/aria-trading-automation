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
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  curl -s -m 30 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
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
send_telegram "$MSG"
echo "[$RUN_TS] Reminder sent." >>"$ERR_FILE"
