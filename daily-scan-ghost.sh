#!/usr/bin/env bash
# ARIA Ghost System — isolated read-only quote collection runner.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ ! -f "$ARIA_HOME/.env" ] || source "$ARIA_HOME/.env"
# Fix paths and grants after local configuration has loaded.
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ARIA_HOME/logs"
STATE_DIR="$ARIA_HOME/state"
PROMPT_FILE="$ARIA_HOME/prompts/bull-put-spread-ghost.md"
LOCK_FILE="$STATE_DIR/daily-scan-ghost.lock"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"
readonly GHOST_ALLOWED_TOOLS="\
Read(/${ARIA_HOME}/state/scratch/ghost_prescreen_*.json),\
Edit(/${ARIA_HOME}/state/scratch/ghost_quote_*.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
Bash(python3 ${ARIA_HOME}/scripts/ghost/ghost_fill_logger.py:*)"

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
      -F "caption=ARIA Ghost System — ${TODAY}" >/dev/null 2>>"$ERR_FILE" || true
  fi
}

mkdir -p "$LOG_DIR" "$STATE_DIR/scratch" "$STATE_DIR/ghost"
TODAY="$(TZ=America/New_York date +%F)"
RUN_TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
LOG_FILE="$LOG_DIR/${TODAY}_daily-scan-ghost.md"
ERR_FILE="$LOG_DIR/${TODAY}_daily-scan-ghost-stderr.log"
on_err() {
  local ec=$?
  trap - ERR
  notify "ARIA Ghost System FAILED" "exit $ec — see $ERR_FILE"
  send_telegram "ARIA Ghost System FAILED — ${TODAY}. See $ERR_FILE." || true
  exit "$ec"
}
trap on_err ERR
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another ghost run holds the lock; exiting." >>"$ERR_FILE"
  exit 0
fi

cd "$ARIA_HOME"
PRESCREEN_FILE="$STATE_DIR/scratch/ghost_prescreen_${TODAY}.json"
# Validate local metadata and capture the raw-row baseline before the scan.
RUN_ID="$(python3 - "$PRESCREEN_FILE" "$TODAY" <<'PY'
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
with open(sys.argv[1]) as handle:
    data = json.load(handle)
assert datetime.fromisoformat(data['run_ts_utc'].replace('Z', '+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat() == sys.argv[2], 'Stale prescreen'
assert isinstance(data['candidates'], list)
assert data['run_id']
print(data['run_id'])
PY
)"
RAW_BASELINE="$(python3 - <<'PY'
from scripts.ghost.ghost_fill_logger import ROOT, rows
print(len(rows(ROOT / 'state/ghost/ghost_observations_raw.csv')))
PY
)"
GIT_HEAD="$(git rev-parse HEAD)"
GIT_DIRTY=false
if [ -n "$(git status --porcelain)" ]; then GIT_DIRTY=true; fi
CODE_VERSION_HASH="$(sha256sum scripts/ghost/ghost_fill_logger.py | cut -d' ' -f1)"
PROMPT="$(cat "$PROMPT_FILE")

## Run Metadata
SCAN_DATE: $TODAY
RUN_ID: $RUN_ID
CODE_VERSION_HASH: $CODE_VERSION_HASH
GIT_HEAD: $GIT_HEAD
GIT_DIRTY: $GIT_DIRTY"
{
  echo "# ARIA Ghost System — $TODAY"
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
  --allowedTools "$GHOST_ALLOWED_TOOLS" \
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
python3 - "$PRESCREEN_FILE" "$RUN_OUTPUT" "$RAW_BASELINE" <<'PY'
import json, re, sys
from collections import Counter
from pathlib import Path
from scripts.ghost.ghost_fill_logger import ROOT, rows
prescreen = json.loads(Path(sys.argv[1]).read_text())
observations = rows(ROOT / 'state/ghost/ghost_observations_raw.csv')[int(sys.argv[3]):]
assert all(row['run_id'] == prescreen['run_id'] for row in observations), 'Unexpected run id'
# Reconcile by DISTINCT candidate_id, not raw row multiplicity: a retried
# candidate (e.g. a transient quote-capture failure followed by a working
# attempt) legitimately logs more than one raw observation row for the same
# candidate_id -- per the design (see the plan's R5 revision), that's fine
# as long as every candidate got at least one attempt logged and nothing
# unexpected shows up. A raw Counter-multiset equality check would fail a
# perfectly successful run the moment any retry occurred (CodeRabbit
# finding, 2026-09-15).
observed_ids = {row['candidate_id'] for row in observations}
expected_ids = {c['candidate_id'] for c in prescreen['candidates']}
missing = expected_ids - observed_ids
assert not missing, f'Missing observations for candidates: {sorted(missing)}'
unexpected = observed_ids - expected_ids
assert not unexpected, f'Unexpected candidate_ids in observations: {sorted(unexpected)}'
counts = Counter(row['outcome'] for row in observations)
lines = Path(sys.argv[2]).read_text().strip().splitlines()
summary = re.fullmatch(r'PROCESSED: (\d+) ACCEPTED: (\d+) REJECTED: (\d+)', lines[-1] if lines else '')
assert summary and tuple(map(int, summary.groups())) == (len(observations), counts['accepted'], counts['rejected']), 'Missing or incorrect summary'
PY

# Distinct post-scan step. Exclusive creation preserves an existing dated copy.
python3 - "$TODAY" <<'PY'
import fcntl, io, csv, shutil, sys
from scripts.ghost.ghost_fill_logger import ROOT, ENTRY_FIELDS
state = ROOT / 'state/ghost'
with (state / '.logger.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    snapshot = state / f'ghost_entries_{sys.argv[1]}.csv'
    try:
        handle = snapshot.open('xb')
    except FileExistsError:
        print(f'Snapshot already exists; preserved: {snapshot}')
    else:
        with handle:
            source = state / 'ghost_entries.csv'
            if source.exists():
                with source.open('rb') as source_handle:
                    shutil.copyfileobj(source_handle, handle)
            else:
                header = io.StringIO(newline='')
                csv.writer(header).writerow(ENTRY_FIELDS)
                handle.write(header.getvalue().encode('utf-8'))
PY
notify "ARIA Ghost System complete" "$TODAY"
send_telegram "ARIA Ghost System — ${TODAY}. $(tail -n 1 "$RUN_OUTPUT")" "$LOG_FILE"
echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
