#!/usr/bin/env bash
# DELIBERATE HARVEST -- NOT PIPELINE. Manual, one-time (rerunnable) capture of
# real IBKR connector responses across VALID/TOOL_ERROR/NO_DATA/MALFORMED_DATA
# shapes, for building a balanced fixture set to calibrate the Jev
# tool-response-triage classifier (see prompts/harvest-ibkr-edge-cases.md).
# Read-only market data only. Never add to cron. Never called by any other
# script. Run manually during market hours (weekday RTH) -- some target
# shapes (frozen/partial quotes) depend on real market state.
set -Eeuo pipefail
ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ARIA_HOME"

OUT_DIR="$ARIA_HOME/state/scratch/harvest_capture_out"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STREAM_FILE="$OUT_DIR/harvest_stream_${STAMP}.jsonl"
STDERR_FILE="$OUT_DIR/harvest_stderr_${STAMP}.log"

# Same 4 read-only IBKR tools as spike-capture-test.sh's grant list, plus
# scoped Read of this harvest's own input file. --restricted + --tools "Read"
# (tested 2026-09-19) actually removes Bash/Edit/Write/Grep as available
# tools -- the harvest agent cannot write anything itself; capture happens
# entirely via --output-format stream-json, extracted afterward by a separate
# trusted local script (scripts/harvest_extract_raw_responses.py), not by the
# agent.
readonly HARVEST_ALLOWED_TOOLS="\
Read(${ARIA_HOME}/state/scratch/spike_capture_input.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot"

PROMPT="$(cat "$ARIA_HOME/prompts/harvest-ibkr-edge-cases.md")"

echo "[harvest] stream=$STREAM_FILE"

START_TS=$(date +%s)
set +e
printf '%s' "$PROMPT" | timeout "5m" claude \
  --print \
  --output-format stream-json \
  --verbose \
  --permission-mode default \
  --restricted \
  --tools "Read" \
  --allowedTools "$HARVEST_ALLOWED_TOOLS" \
  --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
  >"$STREAM_FILE" 2>"$STDERR_FILE"
EC=$?
set -e
END_TS=$(date +%s)

echo "[harvest] exit_code=$EC elapsed_s=$((END_TS - START_TS)) lines=$(wc -l < "$STREAM_FILE")"
echo "[harvest] next: python3 scripts/harvest_extract_raw_responses.py $STREAM_FILE"
