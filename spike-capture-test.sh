#!/usr/bin/env bash
# THROWAWAY SPIKE -- capture-feasibility test only, not pipeline.
# Tests whether `claude --print --output-format stream-json --verbose` captures
# every tool_use/tool_result event independently of the model's own narration,
# for the exact hosted IBKR MCP tools the real pipeline uses. Deletable after
# this question is answered. Never add to cron. Never called by any other script.
set -Eeuo pipefail
ARIA_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ARIA_HOME"

MODE="${1:-complete}"   # complete | interrupt
OUT_DIR="$ARIA_HOME/state/scratch/spike_capture_out"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STREAM_FILE="$OUT_DIR/spike_stream_${MODE}_${STAMP}.jsonl"
STDERR_FILE="$OUT_DIR/spike_stderr_${MODE}_${STAMP}.log"

# Exactly the three read-only IBKR tools under test, copied verbatim from
# daily-scan.sh's grant list (not reconstructed), plus scoped Read of the
# spike's own scratch input. --restricted + --tools "Read" (2026-09-19,
# tested config) actually removes Bash/Edit/Write/Grep as available tools --
# not just ungranted -- so this is no longer just a claim in the prompt text.
readonly SPIKE_ALLOWED_TOOLS="\
Read(${ARIA_HOME}/state/scratch/spike_capture_input.json),\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_history,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_parameters,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_option_data,\
mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot"

PROMPT="$(cat "$ARIA_HOME/prompts/spike-capture-test.md")"

echo "[spike] mode=$MODE stream=$STREAM_FILE"

if [ "$MODE" = "interrupt" ]; then
  TIMEOUT="30s"   # first attempt at 15s was killed during session startup,
                  # before any tool call -- raised so the kill lands mid-run.
else
  TIMEOUT="5m"
fi

START_TS=$(date +%s)
set +e
printf '%s' "$PROMPT" | timeout "$TIMEOUT" claude \
  --print \
  --output-format stream-json \
  --verbose \
  --permission-mode default \
  --restricted \
  --tools "Read" \
  --allowedTools "$SPIKE_ALLOWED_TOOLS" \
  --settings '{"enabledPlugins":{"claude-mem@thedotmack":false}}' \
  >"$STREAM_FILE" 2>"$STDERR_FILE"
EC=$?
set -e
END_TS=$(date +%s)

echo "[spike] exit_code=$EC elapsed_s=$((END_TS - START_TS)) lines=$(wc -l < "$STREAM_FILE")"
