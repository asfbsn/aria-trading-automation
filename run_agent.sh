#!/bin/bash
# Lets Claude Code (the Orchestrator) delegate a task to a coding agent backend.
#
# Usage:
#   ./run_agent.sh [--backend opencode|agy] [--model <id>] "<prompt>"
#
# Defaults to the free OpenCode model, so the legacy form
#   ./run_agent.sh "<prompt>"
# keeps working unchanged.
#
# Guards against nested delegation: if a delegated agent tries to call this
# script again, it exits immediately instead of recursing.

set -uo pipefail

if [ -n "${ORCHESTRATOR_AGENT_RUNNING:-}" ]; then
  echo "ERROR: run_agent.sh called from inside a delegated agent (nested delegation blocked)." >&2
  echo "The agent should implement the task directly, not re-delegate." >&2
  exit 1
fi

OPENCODE_MODELS=(
  "opencode/deepseek-v4-flash-free"
  "opencode/deepseek-v4-flash"
  "opencode/gemini-3.6-flash"
  "opencode/claude-sonnet-4-6"
)
AGY_MODELS=(
  "gemini-3.6-flash-low"
  "gemini-3.6-flash-medium"
  "gemini-3.6-flash-high"
  "gemini-3.5-flash-low"
  "gemini-3.5-flash-medium"
  "gemini-3.5-flash-high"
  "gemini-3.1-pro-low"
  "gemini-3.1-pro-high"
  "gpt-oss-120b-medium"
  "claude-sonnet-4-6"
  "claude-opus-4-6-thinking"
)

BACKEND="opencode"
MODEL=""
PROMPT=""

usage() {
  echo "Usage: ./run_agent.sh [--backend opencode|agy] [--model <id>] \"<prompt>\"" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --backend)
      if [ $# -lt 2 ]; then
        echo "ERROR: --backend requires a value." >&2
        usage; exit 2
      fi
      BACKEND="$2"; shift 2 ;;
    --backend=*)
      BACKEND="${1#*=}"; shift ;;
    --model)
      if [ $# -lt 2 ]; then
        echo "ERROR: --model requires a value." >&2
        usage; exit 2
      fi
      MODEL="$2"; shift 2 ;;
    --model=*)
      MODEL="${1#*=}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      if [ -n "$PROMPT" ]; then
        echo "ERROR: unexpected extra argument: $1" >&2
        usage
        exit 2
      fi
      PROMPT="$1"; shift ;;
  esac
done

if [ -z "$PROMPT" ]; then
  echo "ERROR: no prompt given." >&2
  usage
  exit 2
fi

case "$BACKEND" in
  opencode) ALLOWED=("${OPENCODE_MODELS[@]}"); DEFAULT_MODEL="opencode/deepseek-v4-flash-free" ;;
  agy)      ALLOWED=("${AGY_MODELS[@]}");      DEFAULT_MODEL="gemini-3.6-flash-high" ;;
  *)
    echo "ERROR: unknown backend '$BACKEND' (expected: opencode, agy)." >&2
    exit 2 ;;
esac

[ -z "$MODEL" ] && MODEL="$DEFAULT_MODEL"

MATCHED=0
for m in "${ALLOWED[@]}"; do
  if [ "$m" = "$MODEL" ]; then MATCHED=1; break; fi
done
if [ "$MATCHED" -ne 1 ]; then
  echo "ERROR: model '$MODEL' is not allowed for backend '$BACKEND'." >&2
  echo "Allowed models for $BACKEND:" >&2
  printf '  %s\n' "${ALLOWED[@]}" >&2
  exit 2
fi

echo "Delegating to backend=$BACKEND model=$MODEL"
echo "Task: $PROMPT"

# Marks the child process as an agent run, so a nested call sees the guard above.
export ORCHESTRATOR_AGENT_RUNNING=1

case "$BACKEND" in
  opencode)
    exec opencode run "$PROMPT" --model "$MODEL" --auto
    ;;
  agy)
    # agy's `--model` flag is ignored by `agy --print` (verified: cli.log always shows
    # "Propagating selected model override to backend" using the *persisted* model,
    # never the flag value). The only thing that actually selects the model is the
    # "model" field in its settings.json. So: swap it in, run, always swap the
    # original back out — even on failure/interrupt — and serialize with flock so
    # concurrent run_agent.sh calls can't stomp each other's model selection.
    AGY_SETTINGS="$HOME/.gemini/antigravity-cli/settings.json"
    AGY_LOCK="$HOME/.gemini/antigravity-cli/.settings.lock"

    if ! command -v jq >/dev/null 2>&1; then
      echo "ERROR: agy backend needs jq (model swap in settings.json); jq not on PATH." >&2
      exit 3
    fi
    if [ ! -f "$AGY_SETTINGS" ]; then
      echo "ERROR: agy settings file not found at $AGY_SETTINGS" >&2
      exit 3
    fi

    exec 9>"$AGY_LOCK"
    flock 9

    ORIGINAL_MODEL="$(jq -r '.model' "$AGY_SETTINGS")"
    if [ -z "$ORIGINAL_MODEL" ] || [ "$ORIGINAL_MODEL" = "null" ]; then
      echo "ERROR: no '.model' field in $AGY_SETTINGS; refusing to swap." >&2
      exit 3
    fi
    restore_model() {
      jq --arg m "$ORIGINAL_MODEL" '.model = $m' "$AGY_SETTINGS" > "$AGY_SETTINGS.tmp" \
        && mv "$AGY_SETTINGS.tmp" "$AGY_SETTINGS"
    }
    trap restore_model EXIT

    jq --arg m "$MODEL" '.model = $m' "$AGY_SETTINGS" > "$AGY_SETTINGS.tmp" \
      && mv "$AGY_SETTINGS.tmp" "$AGY_SETTINGS"

    # NOTE: agy's flag parser is order-sensitive — any flag placed *after* --print
    # gets mis-swallowed (observed: --dangerously-skip-permissions after --print
    # made agy treat the flag name itself as the prompt). --print must be last.
    agy --dangerously-skip-permissions \
      --add-dir "$PWD" \
      --print-timeout 30m \
      --print \
      "$PROMPT"
    STATUS=$?

    exit "$STATUS"
    ;;
esac
