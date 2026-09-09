#!/bin/bash
# Lets Claude Code (the Orchestrator) delegate a task to a coding agent backend.
#
# Usage:
#   ./run_agent.sh [--backend opencode|agy|codex] [--model <id>] "<prompt>"
#
# Defaults to the free OpenCode model, so the legacy form
#   ./run_agent.sh "<prompt>"
# keeps working unchanged.
#
# codex runs via the `codex` CLI, authenticated against a ChatGPT Plus login
# (not an API key). Model is fixed to whatever ~/.codex/config.toml sets
# (gpt-5.6-terra as of 2026-09-08); --model here selects *reasoning effort*
# (low|medium|high), not a model id. Sandboxed workspace-write (-s
# workspace-write), not the full-bypass posture opencode/agy use -- confined
# to this repo, no network egress. That last part matters here specifically:
# a delegated task that needs to fetch fresh yfinance/IBKR data (most
# research/backtest work) CANNOT use codex -- route those to opencode/agy.
# codex is the right lane for pure code edits/refactors on already-present
# data or files (e.g. tonight's CodeRabbit-fix pattern), and it draws on an
# independent ChatGPT Plus quota, not Google's (agy) or Sonnet's (this
# session) -- ported from ketosense's run_agent.sh 2026-09-09, no aria-
# specific track record yet, treat its first few real runs as calibration.
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
# codex's model id itself is fixed by ~/.codex/config.toml (not overridable
# per-call the way opencode/agy swap model ids) -- these three values select
# model_reasoning_effort instead.
CODEX_MODELS=(
  "low"
  "medium"
  "high"
)

BACKEND="opencode"
MODEL=""
PROMPT=""

usage() {
  echo "Usage: ./run_agent.sh [--backend opencode|agy|codex] [--model <id>] \"<prompt>\"" >&2
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
  codex)    ALLOWED=("${CODEX_MODELS[@]}");    DEFAULT_MODEL="medium" ;;
  *)
    echo "ERROR: unknown backend '$BACKEND' (expected: opencode, agy, codex)." >&2
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
  codex)
    # -s workspace-write: real sandbox (writes confined to -C's dir), not a
    # full bypass -- still runs to completion unattended, no approval
    # prompts, same as the other two backends.
    # network_access defaults to false (verified 2026-09-09: curl inside this
    # sandbox fails DNS resolution, exit 6; a Playwright-launched Chromium
    # can't even start -- syscall blocked before it gets to networking).
    # Overridable per-call via CODEX_NETWORK_ACCESS=true when agy's quota is
    # exhausted and this backend needs to do real web research instead of
    # pure code edits (2026-09-09) -- know what this trades away: the
    # sandbox no longer blocks outbound network for anything the delegated
    # agent runs, same exposure agy already has. Default stays false; this
    # is an explicit, deliberate opt-in per call, not a new default.
    CODEX_NETWORK_ACCESS="${CODEX_NETWORK_ACCESS:-false}"
    exec codex exec \
      -C "$PWD" \
      -s workspace-write \
      -c sandbox_workspace_write.network_access="$CODEX_NETWORK_ACCESS" \
      -c model_reasoning_effort="$MODEL" \
      "$PROMPT"
    ;;
esac
