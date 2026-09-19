#!/usr/bin/env bash
# ARIA Ghost System — one-time audited contract ID recovery runner.
# THIS SCRIPT IS RUN MANUALLY ON DEMAND. NEVER ADD TO CRONTAB.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ ! -f "$ARIA_HOME/.env" ] || source "$ARIA_HOME/.env"
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ARIA_HOME/logs"
STATE_DIR="$ARIA_HOME/state"
PROMPT_FILE="$ARIA_HOME/prompts/bull-put-spread-ghost-recover-conids.md"
LOCK_FILE="$STATE_DIR/recover-ghost-conids.lock"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Narrowly scoped tool grants: read-only contract resolution + scoped scratch I/O ONLY.
# Deliberately excludes get_price_snapshot and routine mark/fill tools.
readonly GHOST_RECOVER_ALLOWED_TOOLS="\
Read(${ARIA_HOME}/state/ghost/ghost_entries.csv),\
Edit(${ARIA_HOME}/state/scratch/ghost_recovery_*.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Ghost System (Conid Recovery)" "$1" "${2:-}" || true
}

send_telegram() {
  local msg="$1" file="${2:-}"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"
    return 0
  fi
  curl -s -m 30 -K <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tok") \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
  if [ -n "$file" ] && [ -f "$file" ]; then
    curl -s -m 60 -K <(printf 'url = "https://api.telegram.org/bot%s/sendDocument"\n' "$tok") \
      -F "chat_id=${chat}" -F "document=@${file}" \
      -F "caption=ARIA Ghost System (Conid Recovery) — ${TODAY}" >/dev/null 2>>"$ERR_FILE" || true
  fi
}

mkdir -p "$LOG_DIR" "$STATE_DIR/scratch" "$STATE_DIR/ghost"
TODAY="$(TZ=America/New_York date +%F)"
RUN_TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/${TODAY}_recover-ghost-conids.md"
ERR_FILE="$LOG_DIR/${TODAY}_recover-ghost-conids-stderr.log"

on_err() {
  local ec=$?
  trap - ERR
  notify "ARIA Ghost System (Conid Recovery) FAILED" "exit $ec — see $ERR_FILE"
  send_telegram "ARIA Ghost System (Conid Recovery) FAILED — ${TODAY}. See $ERR_FILE." || true
  exit "$ec"
}
trap on_err ERR

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another recovery run holds the lock; exiting." >>"$ERR_FILE"
  exit 0
fi

cd "$ARIA_HOME"

ENTRIES_FILE="$STATE_DIR/ghost/ghost_entries.csv"
if [ ! -f "$ENTRIES_FILE" ]; then
  echo "[$RUN_TS] $ENTRIES_FILE absent; nothing to recover." >>"$ERR_FILE"
  exit 0
fi

RUN_ID="ghost_recover_$(date -u +%Y%m%d_%H%M%S)"
SCRATCH_FILE="$STATE_DIR/scratch/ghost_recovery_${TODAY}_${RUN_ID}.json"

GIT_HEAD="$(git rev-parse HEAD)"
GIT_DIRTY=false
if [ -n "$(git status --porcelain)" ]; then GIT_DIRTY=true; fi
CODE_VERSION_HASH="$(sha256sum scripts/ghost/update_ghost_entry_conids.py | cut -d' ' -f1)"

PROMPT="$(cat "$PROMPT_FILE")

## Run Metadata
SCAN_DATE: $TODAY
RUN_ID: $RUN_ID
SCRATCH_FILE: $SCRATCH_FILE
CODE_VERSION_HASH: $CODE_VERSION_HASH
GIT_HEAD: $GIT_HEAD
GIT_DIRTY: $GIT_DIRTY"

{
  echo "# ARIA Ghost System — Conid Recovery — $TODAY"
  echo "_Run ${RUN_TS}_"
  echo
} >>"$LOG_FILE"

RUN_OUTPUT="$(mktemp)"
trap 'rm -f "$RUN_OUTPUT"' EXIT

trap - ERR
set +e
printf '%s' "$PROMPT" | timeout "${CLAUDE_TIMEOUT:-15m}" "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$GHOST_RECOVER_ALLOWED_TOOLS" \
  --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
  >"$RUN_OUTPUT" 2>>"$ERR_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
set -e
trap on_err ERR

cat "$RUN_OUTPUT" >>"$LOG_FILE"
if [ "$CLAUDE_EC" -ne 0 ]; then
  echo "[$RUN_TS] FAILURE: claude_ec=$CLAUDE_EC" >>"$ERR_FILE"
  false
fi

# Apply recovery updates atomically in place to ghost_entries.csv
if [ -f "$SCRATCH_FILE" ]; then
  python3 "$ARIA_HOME/scripts/ghost/update_ghost_entry_conids.py" \
    --recovery-file "$SCRATCH_FILE" \
    --entries-path "$ENTRIES_FILE" \
    >>"$LOG_FILE" 2>>"$ERR_FILE"
else
  echo "[$RUN_TS] No scratch file produced at $SCRATCH_FILE" >>"$ERR_FILE"
  false
fi

notify "ARIA Ghost System (Conid Recovery) complete" "$TODAY"
TELEGRAM_MSG="ARIA Ghost System (Conid Recovery) — ${TODAY}. $(tail -n 1 "$RUN_OUTPUT")"
send_telegram "$TELEGRAM_MSG" "$LOG_FILE"
echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
