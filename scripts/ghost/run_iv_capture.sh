#!/usr/bin/env bash
# ARIA Ghost System — IBKR IV calibration data capture runner.
set -Eeuo pipefail

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -z "${ARIA_HOME:-}" ]; then
  ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
[ ! -f "$ARIA_HOME/.env" ] || source "$ARIA_HOME/.env"
if [ -z "${ARIA_HOME:-}" ]; then
  ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
export ARIA_HOME

cd "$ARIA_HOME"

SCAN_DATE="${1:-$(TZ=America/New_York date +%F)}"
POOL_FILE="$ARIA_HOME/state/scratch/ghost_ivpool_${SCAN_DATE}.json"

# Step 1: Check pool file existence and tickers
if [ ! -f "$POOL_FILE" ]; then
  echo "iv_capture: no pool file, skipping"
  exit 0
fi

PARSED="$(python3 - "$POOL_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        pool = json.load(f)
except Exception as e:
    sys.stderr.write(f"iv_capture: failed to read pool file: {e}\n")
    sys.exit(1)

tickers = [t['ticker'] if isinstance(t, dict) else str(t) for t in pool.get('tickers', [])]
if not tickers:
    pool_size = pool.get('pool_size_pre_sample', 0)
    gex = pool.get('gex')
    if isinstance(gex, dict):
        gex_regime = gex.get('regime', gex)
    else:
        gex_regime = gex
    print(f"EMPTY:pool_size_pre_sample={pool_size}, gex_regime={gex_regime}")
    sys.exit(0)

pool_id = pool.get('pool_id', '')
print(f"OK:{pool_id}")
for t in tickers:
    print(t)
PY
)"

status_line="$(printf '%s\n' "$PARSED" | head -n 1)"
if [[ "$status_line" == EMPTY:* ]]; then
  reason="${status_line#EMPTY:}"
  echo "iv_capture: pool empty ($reason), skipping"
  exit 0
fi

pool_id="${status_line#OK:}"
ticker_list="$(printf '%s\n' "$PARSED" | tail -n +2)"

# Step 2: Build PROMPT with embedded tickers (no Read grant needed)
PROMPT_BASE="$(cat "$ARIA_HOME/prompts/ghost-iv-capture.md")"
PROMPT="$(printf '%s\n\n## Run Metadata\nSCAN_DATE: %s\nPOOL_ID: %s\nTICKERS (in order, one per line):\n%s\n' \
  "$PROMPT_BASE" "$SCAN_DATE" "$pool_id" "$ticker_list")"

# Step 3: Invoke claude headless session with minimal IBKR MCP tools
# Hardcoded allowedTools ensures ambient CLAUDE_ALLOWED_TOOLS cannot widen capabilities.
readonly IV_CAPTURE_ALLOWED_TOOLS="\
mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot"

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# Not haiku: the headless session loads ~288 MCP tool definitions from every claude.ai connector
# (--restricted does not drop them), which overflows haiku (prompt_too_long, 2026-10-01).
IV_CAPTURE_MODEL="${IV_CAPTURE_MODEL:-claude-sonnet-5}"
IV_CAPTURE_TIMEOUT="${IV_CAPTURE_TIMEOUT:-15m}"

RAW_DIR="$ARIA_HOME/state/ghost/iv_raw"
mkdir -p "$RAW_DIR" "$ARIA_HOME/state/ghost"
# The claude.ai connectors connect asynchronously: a session can start with the IBKR connector still
# "pending" (observed 2026-10-01: model replied "no IBKR tools", 0 tool calls, exit 0). Retry a session
# that made zero IBKR tool calls, up to IV_CAPTURE_MAX_ATTEMPTS, with a short wait for the connector.
IV_CAPTURE_MAX_ATTEMPTS="${IV_CAPTURE_MAX_ATTEMPTS:-3}"
attempt=1
while :; do
  UTC_TS="$(date -u +%Y%m%d_%H%M%S)"
  STREAM_FILE="$RAW_DIR/ivcap_${SCAN_DATE}_${UTC_TS}_a${attempt}.jsonl"

  set +e
# --permission-mode default is copied from tested spike config; accepted by CLI at parse time.
printf '%s' "$PROMPT" | timeout "$IV_CAPTURE_TIMEOUT" "$CLAUDE_BIN" \
  --print \
  --output-format stream-json \
  --verbose \
  --permission-mode default \
  --restricted \
  --tools "Read" \
  --allowedTools "$IV_CAPTURE_ALLOWED_TOOLS" \
  --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
  --model "$IV_CAPTURE_MODEL" \
  >"$STREAM_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
set -e

  IBKR_CALLS="$(python3 - "$STREAM_FILE" <<'PY'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        o = json.loads(line)
    except ValueError:
        continue
    if o.get("type") == "assistant":
        for b in (o.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and "Interactive_Brokers" in str(b.get("name")):
                n += 1
print(n)
PY
)"
  if [ "${IBKR_CALLS:-0}" -gt 0 ] || [ "$attempt" -ge "$IV_CAPTURE_MAX_ATTEMPTS" ]; then
    break
  fi
  echo "iv_capture: attempt $attempt made 0 IBKR tool calls (connector likely still pending); retrying in 20s" >&2
  attempt=$((attempt + 1))
  sleep 20
done

if [ "$CLAUDE_EC" -ne 0 ]; then
  echo "iv_capture: claude exited with code $CLAUDE_EC (continuing with partial stream)" >&2
fi

# Step 4: Deterministic extraction & CSV logging
set +e
EXTRACT_SUMMARY="$(python3 scripts/ghost/ghost_iv_extract.py \
  --stream "$STREAM_FILE" \
  --pool "$POOL_FILE" \
  --out-csv state/ghost/iv_ibkr_log.csv)"
EXTRACT_EC=$?
set -e

if [ "$EXTRACT_EC" -ne 0 ]; then
  echo "iv_capture: ghost_iv_extract crashed with exit code $EXTRACT_EC" >&2
  exit 1
fi

# Step 5: Best-effort calibration report backfill
CALIB_SCRIPT="$ARIA_HOME/scripts/ghost/ghost_iv_calib_report.py"
CALIB_PY="$ARIA_HOME/scripts/backtest/.venv/bin/python3"
[ -x "$CALIB_PY" ] || CALIB_PY="python3"

if [ -f "$CALIB_SCRIPT" ]; then
  set +e
  CALIB_OUT="$("$CALIB_PY" "$CALIB_SCRIPT" --backfill 2>&1)"
  CALIB_EC=$?
  set -e
  if [ "$CALIB_EC" -ne 0 ]; then
    echo "iv_capture: ghost_iv_calib_report --backfill failed (exit $CALIB_EC, ignored): $CALIB_OUT" >&2
  fi
fi

# Print one-line summary as the LAST line of stdout
printf '%s claude_ec=%s\n' "$EXTRACT_SUMMARY" "$CLAUDE_EC"
exit 0
