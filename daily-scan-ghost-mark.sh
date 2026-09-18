#!/usr/bin/env bash
# ARIA Ghost System — daily open position marking and exit runner.
# Runs on a separate, later cron time than daily-scan-ghost.sh (end-of-day pricing).
# Placeholder cron: 19:30 IDT.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ ! -f "$ARIA_HOME/.env" ] || source "$ARIA_HOME/.env"
# Fix paths and grants after local configuration has loaded.
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ARIA_HOME/logs"
STATE_DIR="$ARIA_HOME/state"
PROMPT_FILE="$ARIA_HOME/prompts/bull-put-spread-ghost-mark.md"
LOCK_FILE="$STATE_DIR/daily-scan-ghost-mark.lock"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"
readonly GHOST_MARK_ALLOWED_TOOLS="\
Read(/${ARIA_HOME}/state/scratch/ghost_open_positions_*.json),\
Edit(/${ARIA_HOME}/state/scratch/ghost_mark_*.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_exit_logger.py --input:*)"

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Ghost System" "$1" "${2:-}" || true
}

send_telegram() {
  local msg="$1" file="${2:-}"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"
    return 0
  fi
  # The token-bearing URL goes through -K (a curl config file, here a
  # process substitution) instead of argv -- "bot<TOKEN>/..." as a literal
  # curl argument is visible to any other local user via `ps`/
  # `/proc/<pid>/cmdline` for curl's runtime (CodeRabbit finding, same
  # pattern already fixed in research-reminder.sh 2026-08-31).
  curl -s -m 30 -K <(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tok") \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
  if [ -n "$file" ] && [ -f "$file" ]; then
    curl -s -m 60 -K <(printf 'url = "https://api.telegram.org/bot%s/sendDocument"\n' "$tok") \
      -F "chat_id=${chat}" -F "document=@${file}" \
      -F "caption=ARIA Ghost System (Marks) — ${TODAY}" >/dev/null 2>>"$ERR_FILE" || true
  fi
}

mkdir -p "$LOG_DIR" "$STATE_DIR/scratch" "$STATE_DIR/ghost"
TODAY="$(TZ=America/New_York date +%F)"
RUN_TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/${TODAY}_daily-scan-ghost-mark.md"
ERR_FILE="$LOG_DIR/${TODAY}_daily-scan-ghost-mark-stderr.log"
on_err() {
  local ec=$?
  trap - ERR
  notify "ARIA Ghost System (Marks) FAILED" "exit $ec — see $ERR_FILE"
  send_telegram "ARIA Ghost System (Marks) FAILED — ${TODAY}. See $ERR_FILE." || true
  exit "$ec"
}
trap on_err ERR
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another ghost mark run holds the lock; exiting." >>"$ERR_FILE"
  exit 0
fi

# Weekend / US market holiday skip -- cron's own "1-5" already excludes
# weekends, but this stays defense-in-depth (same belt-and-braces reasoning
# as daily-scan-ghost.sh) since a bare invocation or a manual FORCE_RUN=
# omission shouldn't burn a real IBKR-quote-capture pass against a closed
# market.
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
if [ "${FORCE_RUN:-false}" != "true" ]; then
  DOW="$(TZ=America/New_York date +%u)"
  if [ "$DOW" -ge 6 ]; then
    echo "[$RUN_TS] Weekend — US market closed. Skip." >>"$ERR_FILE"
    exit 0
  fi
  if [ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE"; then
    echo "[$RUN_TS] $TODAY is a US market holiday. Skip." >>"$ERR_FILE"
    notify "ARIA Ghost System (Marks) skipped" "$TODAY — US market holiday"
    exit 0
  fi
  # 10#$(...) forces base-10 parsing — "0900" as a bare int is invalid octal.
  # Target ET window: 15:45-15:59 America/New_York (end-of-day pricing within RTH).
  # Matches the gtc-guard.sh/exit-guard.sh pattern to handle box cron CRON_TZ bug.
  NY_HHMM=$((10#$(TZ='America/New_York' date +%H%M)))
  if [ "$NY_HHMM" -lt 1545 ] || [ "$NY_HHMM" -ge 1600 ]; then
    echo "[$RUN_TS] Outside 15:45-15:59 America/New_York (NY time now: $NY_HHMM) — skip." >>"$ERR_FILE"
    exit 0
  fi
fi

cd "$ARIA_HOME"
# Fetch real settled closes for current open positions via yfinance.
# Settled closes reflect the last completed trading session (yesterday's close),
# serving as lagged structural check evidence, kept explicitly separate from today's
# live hypothetical-exit quotes captured by the marking prompt.
SETTLED_CLOSES_FILE="$STATE_DIR/ghost/settled_closes.json"
echo "[$RUN_TS] Fetching settled closes -> $SETTLED_CLOSES_FILE" >>"$ERR_FILE"
timeout 5m "$ARIA_HOME/scripts/backtest/.venv/bin/python3" \
  "$ARIA_HOME/scripts/ghost/fetch_settled_closes.py" \
  --out "$SETTLED_CLOSES_FILE" \
  --as-of-date "$TODAY" \
  >>"$ERR_FILE" 2>&1 || {
    echo "[$RUN_TS] WARNING: fetch_settled_closes.py failed (exit $?); continuing with unmeasured structural check" >>"$ERR_FILE"
}

OPEN_POSITIONS_FILE="$STATE_DIR/scratch/ghost_open_positions_${TODAY}.json"
# Generate today's open positions ourselves. ghost_exit_logger.py needs pandas/
# signal_core/dix_fetcher_v2 (the backtest venv). 10m timeout matches
# daily-scan-ghost.sh guard.
timeout 10m "$ARIA_HOME/scripts/backtest/.venv/bin/python3" \
  "$ARIA_HOME/scripts/ghost/ghost_exit_logger.py" --list-open \
  >"$OPEN_POSITIONS_FILE" 2>>"$ERR_FILE"
echo "[$RUN_TS] Open positions listed -> $OPEN_POSITIONS_FILE" >>"$ERR_FILE"

# Validate local metadata and capture the raw-row baseline before the scan.
RUN_ID="$(python3 - "$OPEN_POSITIONS_FILE" <<'PY'
import json, sys, uuid
from datetime import datetime, timezone
with open(sys.argv[1]) as handle:
    data = json.load(handle)
assert isinstance(data, list), 'Open positions must be a JSON array'
print(f"ghost_mark_{uuid.uuid4().hex[:8]}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
PY
)"
RAW_BASELINE="$(python3 - <<'PY'
from scripts.ghost.ghost_exit_logger import ROOT, rows
print(len(rows(ROOT / 'state/ghost/ghost_mark_observations_raw.csv')))
PY
)"
GIT_HEAD="$(git rev-parse HEAD)"
GIT_DIRTY=false
if [ -n "$(git status --porcelain)" ]; then GIT_DIRTY=true; fi
CODE_VERSION_HASH="$(sha256sum scripts/ghost/ghost_exit_logger.py | cut -d' ' -f1)"
PROMPT="$(cat "$PROMPT_FILE")

## Run Metadata
SCAN_DATE: $TODAY
RUN_ID: $RUN_ID
CODE_VERSION_HASH: $CODE_VERSION_HASH
GIT_HEAD: $GIT_HEAD
GIT_DIRTY: $GIT_DIRTY"
{
  echo "# ARIA Ghost System (Marks) — $TODAY"
  echo "_Run ${RUN_TS}_"
  echo
} >>"$LOG_FILE"
RUN_OUTPUT="$(mktemp)"
trap 'rm -f "$RUN_OUTPUT"' EXIT
# Handle the pipeline failure explicitly before restoring the error trap.
trap - ERR
set +e
printf '%s' "$PROMPT" | timeout "${CLAUDE_TIMEOUT:-15m}" "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$GHOST_MARK_ALLOWED_TOOLS" \
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

# Success requires actual observations for every candidate, not only model text.
python3 - "$OPEN_POSITIONS_FILE" "$RUN_OUTPUT" "$RAW_BASELINE" "$RUN_ID" <<'PY'
import json, re, sys
from collections import Counter
from pathlib import Path
from scripts.ghost.ghost_exit_logger import ROOT, rows
positions = json.loads(Path(sys.argv[1]).read_text())
assert isinstance(positions, list), 'Expected JSON array of open positions'
raw_baseline = int(sys.argv[3])
run_id = sys.argv[4]
observations = rows(ROOT / 'state/ghost/ghost_mark_observations_raw.csv')[raw_baseline:]
assert all(row['run_id'] == run_id for row in observations), 'Unexpected run id'
# Reconcile by DISTINCT candidate_id, not raw row multiplicity: a retried
# candidate (e.g. a transient quote-capture failure followed by a working
# attempt) legitimately logs more than one raw observation row for the same
# candidate_id.
observed_ids = {row['candidate_id'] for row in observations}
expected_ids = {p['candidate_id'] for p in positions}
missing = expected_ids - observed_ids
assert not missing, f'Missing observations for candidates: {sorted(missing)}'
unexpected = observed_ids - expected_ids
assert not unexpected, f'Unexpected candidate_ids in observations: {sorted(unexpected)}'
# Reconcile MARKED/EXITED against the TERMINAL outcome per candidate_id (last
# row wins), not raw row multiplicity -- a retried candidate legitimately logs
# more than one raw row (e.g. one rejected attempt then one marked retry), and
# comparing PROCESSED/MARKED/EXITED against raw-row counts fails a perfectly
# successful run the moment any retry occurs (same class of bug CodeRabbit
# flagged in daily-scan-ghost.sh's matching reconciliation, 2026-09-17).
terminal = {row['candidate_id']: row for row in observations}
counts = Counter(row['outcome'] for row in terminal.values())
lines = Path(sys.argv[2]).read_text().strip().splitlines()
summary = re.fullmatch(r'PROCESSED: (\d+) MARKED: (\d+) EXITED: (\d+)', lines[-1] if lines else '')
assert summary and tuple(map(int, summary.groups())) == (len(terminal), counts['marked'], counts['exited']), 'Missing or incorrect summary'
PY

notify "ARIA Ghost System (Marks) complete" "$TODAY"
send_telegram "ARIA Ghost System (Marks) — ${TODAY}. $(tail -n 1 "$RUN_OUTPUT")" "$LOG_FILE"
echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
