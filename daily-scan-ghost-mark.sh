#!/usr/bin/env bash
# ARIA Ghost System — daily open position marking and exit runner.
# Runs on a separate, later cron time than daily-scan-ghost.sh (end-of-day pricing).
# Placeholder cron: 19:30 IDT.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ ! -f "$ARIA_HOME/.env" ] || source "$ARIA_HOME/.env"
export TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-}"
# Fix paths and grants after local configuration has loaded.
export ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ARIA_HOME/logs"
STATE_DIR="$ARIA_HOME/state"
PROMPT_FILE="$ARIA_HOME/prompts/bull-put-spread-ghost-mark.md"
LOCK_FILE="$STATE_DIR/daily-scan-ghost-mark.lock"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"
# NOTE: Read/Edit path rules must start with "/" + the absolute path (=> "//home/...") to be ABSOLUTE; a bare
# "/home/..." is resolved relative to the project root and never matches (first real mark run 2026-10-01: every
# scratch write and logger call was denied). daily-scan-ghost.sh uses the same form.
readonly GHOST_MARK_ALLOWED_TOOLS="\
Read(/${ARIA_HOME}/state/scratch/ghost_open_positions_*.json),\
Edit(/${ARIA_HOME}/state/scratch/ghost_mark_*.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot,\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_exit_logger.py --input:*),\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/jev_shadow_classify.py:*),\
Bash(${ARIA_HOME}/scripts/backtest/.venv/bin/python3 scripts/ghost/jev_shadow_classify.py:*),\
Bash(scripts/backtest/.venv/bin/python3 ${ARIA_HOME}/scripts/ghost/jev_shadow_classify.py:*),\
Bash(scripts/backtest/.venv/bin/python3 scripts/ghost/jev_shadow_classify.py:*)"

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
  local why
  why="$(tail -n 8 "$ERR_FILE" 2>/dev/null | tr '\r' '\n' | awk '!/%/ && NF' | tail -n 3 | tr '\n' '|' | head -c 300)"
  notify "ARIA Ghost System (Marks) FAILED" "exit $ec — see $ERR_FILE"
  send_telegram "ARIA Ghost System (Marks) FAILED — ${TODAY} (exit $ec). Last log lines: ${why:-none}. See $ERR_FILE." || true
  exit "$ec"
}
trap on_err ERR
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another ghost mark run holds the lock; exiting." >>"$ERR_FILE"
  exit 0
fi
# Redundant cron fires (the backup minute, or the other DST-offset hour) must be harmless: if today's
# marks already completed, do nothing.
if [ "${FORCE_RUN:-false}" != "true" ] && [ -f "$ERR_FILE" ] && grep -qF '] Done → ' "$ERR_FILE"; then
  echo "[$RUN_TS] Already completed today; skip." >>"$ERR_FILE"
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
  # A run may START between 14:45 and 15:35 America/New_York. Marks are captured one position at a time (~1 minute
  # each, 27+ positions and growing), so the session needs a long runway BEFORE the 16:00 close: every quote taken
  # after the close is rejected as quote_outside_regular_hours. The session itself is capped at 15:55 ET below.
  # Matches the gtc-guard.sh/exit-guard.sh pattern to handle box cron CRON_TZ bug.
  NY_HHMM=$((10#$(TZ='America/New_York' date +%H%M)))
  if [ "$NY_HHMM" -lt 1445 ] || [ "$NY_HHMM" -ge 1535 ]; then
    echo "[$RUN_TS] Outside the 14:45-15:35 America/New_York start window (NY time now: $NY_HHMM) — skip." >>"$ERR_FILE"
    exit 0
  fi
fi

cd "$ARIA_HOME"
# Stale per-position mark files from earlier runs (e.g. 2026-09-18) must not be replayable: on 2026-10-01 a
# session that could not write a fresh file ran the logger on an old one and wrote a junk row to the raw ledger.
mkdir -p "$STATE_DIR/scratch/ghost_mark_archive"
find "$STATE_DIR/scratch" -maxdepth 1 -name 'ghost_mark_*.json' -exec mv --backup=numbered -t "$STATE_DIR/scratch/ghost_mark_archive" {} + 2>>"$ERR_FILE" || true
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

# Wrapper-only FULL list (large: ~1.8K chars per position). The Claude session must NOT read it: the Read tool
# truncates a single long line at ~42K chars, which aborted the 2026-10-05 and 2026-10-06 runs (33/37 positions).
OPEN_POSITIONS_FILE="$STATE_DIR/scratch/ghost_open_full_${TODAY}.json"
# Compact worklist the session reads (name matches the Read grant and the prompt): ONE position per line, only
# the fields needed to quote a position.
SESSION_POSITIONS_FILE="$STATE_DIR/scratch/ghost_open_positions_${TODAY}.json"
# Generate today's open positions ourselves. ghost_exit_logger.py needs pandas/
# signal_core/dix_fetcher_v2 (the backtest venv). 10m timeout matches
# daily-scan-ghost.sh guard.
timeout 10m "$ARIA_HOME/scripts/backtest/.venv/bin/python3" \
  "$ARIA_HOME/scripts/ghost/ghost_exit_logger.py" --list-open \
  >"$OPEN_POSITIONS_FILE" 2>>"$ERR_FILE"
echo "[$RUN_TS] Open positions listed -> $OPEN_POSITIONS_FILE" >>"$ERR_FILE"
python3 - "$OPEN_POSITIONS_FILE" "$SESSION_POSITIONS_FILE" <<'PY' 2>>"$ERR_FILE"
import json, os, sys
KEEP = ('ticker', 'candidate_id', 'resolution_status', 'underlying_contract_id', 'short_contract_id',
        'long_contract_id', 'resolved_short_strike', 'resolved_long_strike', 'resolved_expiry')
with open(sys.argv[1]) as handle:
    data = json.load(handle)
assert isinstance(data, list), 'Open positions must be a JSON array'
lines = [json.dumps({key: row.get(key) for key in KEEP}, separators=(',', ':')) for row in data]
body = '[\n' + ',\n'.join(lines) + '\n]\n' if lines else '[]\n'
tmp = sys.argv[2] + '.tmp'
with open(tmp, 'w') as handle:
    handle.write(body)
os.replace(tmp, sys.argv[2])
assert json.load(open(sys.argv[2])) == [{key: row.get(key) for key in KEEP} for row in data]
PY
echo "[$RUN_TS] Session worklist written -> $SESSION_POSITIONS_FILE" >>"$ERR_FILE"

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
RAW_BASELINE="$("$ARIA_HOME/scripts/backtest/.venv/bin/python3" - <<'PY'
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
# The claude.ai IBKR connector connects asynchronously: a headless session can start with it still
# "pending", so the model reports "no IBKR tools" and exits 0 having captured nothing (seen 2026-10-01 on the
# IV capture runner). Retry a session that logged ZERO observations for this run. Do not switch CLAUDE_MODEL
# to haiku: it overflows its context with the connector tool definitions.
N_OPEN="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$OPEN_POSITIONS_FILE" 2>>"$ERR_FILE")"
N_OPEN="${N_OPEN:-0}"
MARK_MAX_ATTEMPTS="${MARK_MAX_ATTEMPTS:-3}"
case "$MARK_MAX_ATTEMPTS" in ''|*[!0-9]*) MARK_MAX_ATTEMPTS=3 ;; esac
count_run_observations() {
  "$ARIA_HOME/scripts/backtest/.venv/bin/python3" - "$RAW_BASELINE" "$RUN_ID" <<'PY'
import sys
from scripts.ghost.ghost_exit_logger import ROOT, rows
base, rid = int(sys.argv[1]), sys.argv[2]
print(sum(1 for r in rows(ROOT / 'state/ghost/ghost_mark_observations_raw.csv')[base:] if r['run_id'] == rid))
PY
}
attempt=1
CLAUDE_EC=1   # stays 1 (=> loud failure) if the loop never starts a session
while :; do
  ATTEMPT_TIMEOUT="${MARK_SESSION_MAX_SECS:-2700}"
  if [ "${FORCE_RUN:-false}" != "true" ]; then
    # Never let a session run past 15:55 ET: quotes after the 16:00 close are rejected as outside regular hours.
    REMAIN="$(( $(TZ=America/New_York date -d '15:55' +%s) - $(date +%s) ))"
    if [ "$REMAIN" -lt 90 ]; then
      echo "[$RUN_TS] less than 90 s left before 15:55 ET; not starting a session" >>"$ERR_FILE"
      break
    fi
    [ "$REMAIN" -lt "$ATTEMPT_TIMEOUT" ] && ATTEMPT_TIMEOUT="$REMAIN"
  fi
  : >"$RUN_OUTPUT"
  printf '%s' "$PROMPT" | timeout "${ATTEMPT_TIMEOUT}s" "$CLAUDE_BIN" \
    --print \
    --model "$CLAUDE_MODEL" \
    --permission-mode default \
    --allowedTools "$GHOST_MARK_ALLOWED_TOOLS" \
    --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
    >"$RUN_OUTPUT" 2>>"$ERR_FILE"
  CLAUDE_EC=${PIPESTATUS[1]}
  cat "$RUN_OUTPUT" >>"$LOG_FILE"
  N_OBS="$(count_run_observations 2>>"$ERR_FILE")"
  if [ -z "$N_OBS" ]; then
    echo "[$RUN_TS] observation count failed; not retrying" >>"$ERR_FILE"
    break
  fi
  if [ "$CLAUDE_EC" -ne 0 ] || [ "$N_OPEN" -eq 0 ] || [ "$N_OBS" -gt 0 ] || [ "$attempt" -ge "$MARK_MAX_ATTEMPTS" ]; then
    break
  fi
  if [ "${FORCE_RUN:-false}" != "true" ] && [ "$((10#$(TZ='America/New_York' date +%H%M)))" -ge 1550 ]; then
    echo "[$RUN_TS] mark attempt $attempt logged 0 observations but NY time >= 15:50; not retrying" >>"$ERR_FILE"
    break
  fi
  echo "[$RUN_TS] mark attempt $attempt logged 0 observations (connector likely still pending); retrying in 20s" >>"$ERR_FILE"
  attempt=$((attempt + 1))
  sleep 20
done
set -e
trap on_err ERR
if [ "$CLAUDE_EC" -ne 0 ]; then
  echo "[$RUN_TS] FAILURE: claude_ec=$CLAUDE_EC" >>"$ERR_FILE"
  false
fi

# Success requires actual observations for every candidate, not only model text.
"$ARIA_HOME/scripts/backtest/.venv/bin/python3" - "$OPEN_POSITIONS_FILE" "$RUN_OUTPUT" "$RAW_BASELINE" "$RUN_ID" <<'PY' 2>>"$ERR_FILE"
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
# A run in which every mark was rejected (or any rejection was systemic, e.g. outside market hours) is a
# FAILED run, not a "Done" run (2026-09-18: 10/10 outside-hours rejections still wrote the Done line).
from scripts.ghost.ghost_reconcile import systemic_failure
failed, message = systemic_failure(list(terminal.values()), 'mark')
if failed:
    raise SystemExit(f'SYSTEMIC FAILURE: {message}')
PY

notify "ARIA Ghost System (Marks) complete" "$TODAY"
send_telegram "ARIA Ghost System (Marks) — ${TODAY}. $(tail -n 1 "$RUN_OUTPUT")" "$LOG_FILE"
echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
