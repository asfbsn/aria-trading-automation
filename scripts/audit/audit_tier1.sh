#!/bin/bash
# Tier-1 screener parity audit -- standalone, on-demand, manual-trigger only.
# NOT called by daily-scan.sh, NOT in cron. Run this by hand when you want a
# parity check, nothing else invokes it.
#
# Usage:
#   ./audit_tier1.sh --ground-truth fixtures/adi_screener_2026-09-10.json
#   ./audit_tier1.sh --ground-truth <file> --refresh
#
# Uses `uv run` so this never touches the system/venv Python that
# prescreen.py and compute_signal.py run under in production -- this script
# has zero extra dependencies (stdlib only), so it's really just isolation
# discipline, not because it needs anything special.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v uv >/dev/null 2>&1; then
  exec uv run --no-project -- python3 "$SCRIPT_DIR/audit_tier1.py" "$@"
else
  exec python3 "$SCRIPT_DIR/audit_tier1.py" "$@"
fi
