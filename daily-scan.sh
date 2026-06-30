#!/usr/bin/env bash
#
# daily-scan.sh — ARIA "Adi option swing 2.0" Bull Put Spread daily scanner
# ---------------------------------------------------------------------------
# Drives the LOCAL TradingView Desktop (via the tradingview-bridge MCP) through
# a headless Claude run, applies the saved Bull-Put-Spread rules, writes a dated
# log, and pings the desktop when finished.
#
# SCAFFOLD ONLY — nothing here is activated automatically. To enable later:
#   chmod +x ~/aria-trading/daily-scan.sh
#   # then install the crontab line shown in the review notes.
#
# Designed to run from cron (headless), so it explicitly re-establishes the GUI
# session env (DISPLAY / DBUS / XDG_RUNTIME_DIR) and a sane PATH.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

# --- minimal cron PATH (cron starts with almost nothing) -------------------
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ===========================================================================
# 0. Configuration  (override anything in ~/aria-trading/.env)
# ===========================================================================
ARIA_HOME="${ARIA_HOME:-$HOME/aria-trading}"
LOG_DIR="${LOG_DIR:-$ARIA_HOME/logs}"
STATE_DIR="${STATE_DIR:-$ARIA_HOME/state}"
PROMPT_FILE="${PROMPT_FILE:-$ARIA_HOME/prompts/bull-put-spread.md}"
HOLIDAYS_FILE="${HOLIDAYS_FILE:-$ARIA_HOME/us-market-holidays.txt}"
LOCK_FILE="${LOCK_FILE:-$ARIA_HOME/state/daily-scan.lock}"

# Claude project dir = where the tradingview-bridge MCP server is configured
# (this session's cwd). Run from here so the same MCP servers load.
CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$HOME/Projects/aria-baby}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"

# Read-only / navigation tools only. NOTE: we deliberately do NOT allow any
# order-placement tools (e.g. the broker MCP's create_order_instruction).
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-\
mcp__tradingview-bridge__tv_health_check,\
mcp__tradingview-bridge__tv_launch,\
mcp__tradingview-bridge__ui_find_element,\
mcp__tradingview-bridge__ui_click,\
mcp__tradingview-bridge__ui_evaluate,\
mcp__tradingview-bridge__chart_set_symbol,\
mcp__tradingview-bridge__chart_set_timeframe,\
mcp__tradingview-bridge__chart_get_state,\
mcp__tradingview-bridge__data_get_pine_tables,\
mcp__tradingview-bridge__data_get_study_values,\
mcp__tradingview-bridge__data_get_ohlcv,\
mcp__tradingview-bridge__quote_get}"

# TradingView Desktop process name + launch command (adjust to your install:
# native binary, AppImage path, or e.g. 'flatpak run com.tradingview.Desktop').
TV_PROC_NAME="${TV_PROC_NAME:-TradingView}"
TV_LAUNCH_CMD="${TV_LAUNCH_CMD:-tradingview}"
TV_WARMUP_SECS="${TV_WARMUP_SECS:-25}"

# If true, exit early (no scan) when the screener constituents are byte-identical
# to the previous run. Default false: name lists rarely change day-to-day even on
# valid trading days (the dashboard *values* do), so a hard skip would suppress
# good runs. Kept as an opt-in guard against a stale/frozen feed.
HARD_SKIP_ON_UNCHANGED="${HARD_SKIP_ON_UNCHANGED:-false}"

# Load user overrides if present.
[ -f "$ARIA_HOME/.env" ] && source "$ARIA_HOME/.env"

# ===========================================================================
# 1. GUI session env so a headless cron shell can launch GUI apps + notify-send
# ===========================================================================
export DISPLAY="${DISPLAY:-:0}"
UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
# Wayland session vars — TradingView's Electron runtime needs these to start
# headless (the app uses --ozone-platform=wayland). Without WAYLAND_DISPLAY the
# app fails to come up and CDP never binds.
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_SESSION_TYPE="${XDG_SESSION_TYPE:-wayland}"
if [ -z "${XAUTHORITY:-}" ]; then
  _xa="$(ls -t "${XDG_RUNTIME_DIR}"/.mutter-Xwaylandauth* 2>/dev/null | head -n1 || true)"
  [ -n "$_xa" ] && export XAUTHORITY="$_xa"
fi

notify() {  # no-op if notify-send is missing
  command -v notify-send >/dev/null 2>&1 && notify-send -a "ARIA Scan" "$1" "${2:-}" || true
}

# Send a status line + (optionally) attach a file to Telegram. No-op if the bot
# token / chat id aren't set. Plain text (no parse_mode) to avoid markdown-entity
# 400s; the full report goes as a document so the 4096-char limit doesn't truncate.
send_telegram() {
  local msg="$1" file="${2:-}"
  local tok="${TELEGRAM_BOT_TOKEN:-}" chat="${TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    echo "[$RUN_TS] Telegram not configured; skipping." >>"$ERR_FILE"; return 0
  fi
  curl -s -m 30 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${msg}" >/dev/null 2>>"$ERR_FILE" || true
  if [ -n "$file" ] && [ -f "$file" ]; then
    curl -s -m 60 "https://api.telegram.org/bot${tok}/sendDocument" \
      -F "chat_id=${chat}" -F "document=@${file}" \
      -F "caption=ARIA Bull Put Spread report — ${TODAY}" >/dev/null 2>>"$ERR_FILE" || true
  fi
}

mkdir -p "$LOG_DIR" "$STATE_DIR"

TODAY="$(date +%F)"
RUN_TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_FILE="$LOG_DIR/${TODAY}_bull-put-spread.md"
ERR_FILE="$LOG_DIR/${TODAY}_stderr.log"

on_err() { local ec=$?; notify "ARIA scan FAILED" "exit $ec — see $ERR_FILE"; exit "$ec"; }
trap on_err ERR

# Prevent overlapping runs (a hung TradingView/Claude shouldn't stack up).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$RUN_TS] Another run holds the lock — exiting." >>"$ERR_FILE"
  exit 0
fi

# ===========================================================================
# 2. Skip logic — weekends (belt & braces) + US market holidays
# ===========================================================================
DOW="$(date +%u)"  # 1=Mon … 7=Sun
if [ "${FORCE_RUN:-false}" = "true" ]; then
  echo "[$RUN_TS] FORCE_RUN=true — bypassing weekend/holiday skip (manual test)." >>"$ERR_FILE"
else
  if [ "$DOW" -ge 6 ]; then
    echo "[$RUN_TS] Weekend — US market closed. Skip." >>"$ERR_FILE"; exit 0
  fi
  if [ -f "$HOLIDAYS_FILE" ] && grep -qx "$TODAY" "$HOLIDAYS_FILE"; then
    echo "[$RUN_TS] $TODAY is a US market holiday. Skip." >>"$ERR_FILE"
    notify "ARIA scan skipped" "$TODAY — US market holiday"; exit 0
  fi
fi

# ===========================================================================
# 3. Ensure TradingView Desktop is up.
#    Readiness is delegated to the bridge's tv_health_check / tv_launch (invoked
#    by the headless Claude run below) — far more reliable than a process-name
#    guess (pgrep -f matched the MCP/bridge/claude processes here, not the GUI
#    app). Optional cold-start via bash is behind TV_BASH_LAUNCH for setups where
#    the app must exist before the bridge can attach.
# ===========================================================================
# Bring TradingView up WITH the CDP debug port, deterministically, in bash —
# then poll until the port answers, so Claude's tv_health_check just connects.
# Delegating cold-start to the headless agent proved flaky (CDP wouldn't bind).
# The KEY is launching WITH the right flags: a plain `tradingview` (no
# --remote-debugging-port) is what broke the bridge before; these flags fix it.
# --remote-allow-origins=* is required for CDP on Chromium/Electron 111+.
CDP_PORT="${CDP_PORT:-9222}"
TV_BIN="${TV_BIN:-tradingview}"
cdp_up() { curl -s -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; }
if cdp_up; then
  echo "[$RUN_TS] CDP already responding on ${CDP_PORT}." >>"$ERR_FILE"
else
  echo "[$RUN_TS] Starting TradingView with CDP on ${CDP_PORT} ('$TV_BIN')…" >>"$ERR_FILE"
  pkill -x tradingview 2>/dev/null || true
  sleep 2
  # shellcheck disable=SC2086
  # 9>&- closes the inherited lock fd so the long-lived TradingView process does
  # NOT keep holding the run lock after this script exits (that bug made the next
  # cron run exit with "Another run holds the lock").
  nohup "$TV_BIN" --remote-debugging-port="${CDP_PORT}" "--remote-allow-origins=*" >/dev/null 2>&1 9>&- &
  for _ in $(seq 1 30); do cdp_up && break; sleep 2; done
  if cdp_up; then
    echo "[$RUN_TS] CDP up after warm-up." >>"$ERR_FILE"
  else
    echo "[$RUN_TS] WARNING: CDP not up after ~60s — the run will likely fail." >>"$ERR_FILE"
    notify "ARIA scan" "TradingView CDP not ready — run may fail"
  fi
fi

# ===========================================================================
# 4. Headless Claude scan against the "Adi option swing 2.0" screener
# ===========================================================================
cd "$CLAUDE_PROJECT_DIR"
PROMPT="$(cat "$PROMPT_FILE")"

# Optional TODAY-ONLY priority overlay (e.g. instructor's daily picks). Date-gated
# so it auto-expires: applied ONLY when its first line "PRIORITY_DATE: <today>"
# matches. Never alters the permanent strategy/prompt; just appended for one day.
PRIORITY_FILE="${PRIORITY_FILE:-$ARIA_HOME/priority-today.md}"
if [ -f "$PRIORITY_FILE" ] && grep -qx "PRIORITY_DATE: ${TODAY}" "$PRIORITY_FILE"; then
  PROMPT="${PROMPT}

=== TODAY-ONLY PRIORITY OVERLAY (applies only to ${TODAY}; obey permanent rules) ===
$(cat "$PRIORITY_FILE")"
  echo "[$RUN_TS] Priority overlay applied for ${TODAY}." >>"$ERR_FILE"
else
  echo "[$RUN_TS] No priority overlay active for ${TODAY}." >>"$ERR_FILE"
fi

{
  echo "# ARIA Bull Put Spread — $TODAY"
  echo "_Run $RUN_TS · 19:00 Asia/Jerusalem ≈ 12:00 ET (mid-session: today's bar is UNSETTLED)_"
  echo
} >"$LOG_FILE"

set +e
# Prompt goes via stdin: --allowedTools is variadic and would otherwise swallow a
# trailing positional prompt as a tool name.
printf '%s' "$PROMPT" | "$CLAUDE_BIN" \
  --print \
  --model "$CLAUDE_MODEL" \
  --permission-mode default \
  --allowedTools "$CLAUDE_ALLOWED_TOOLS" \
  >>"$LOG_FILE" 2>>"$ERR_FILE"
CLAUDE_EC=${PIPESTATUS[1]}
set -e

# ===========================================================================
# 5. "Unchanged screener" guard (stale/frozen-feed detector)
#    The prompt emits a line:  SCREENER_CONSTITUENTS: SYM1,SYM2,...
# ===========================================================================
LAST_HASH_FILE="$STATE_DIR/last-constituents.sha"
SYMS_LINE="$(grep -m1 '^SCREENER_CONSTITUENTS:' "$LOG_FILE" || true)"
if [ -n "$SYMS_LINE" ]; then
  NORM="$(printf '%s' "${SYMS_LINE#SCREENER_CONSTITUENTS:}" | tr ',' '\n' | tr -d '[:space:]' | sort -u)"
  NEW_HASH="$(printf '%s' "$NORM" | sha256sum | cut -d' ' -f1)"
  if [ -f "$LAST_HASH_FILE" ] && [ "$NEW_HASH" = "$(cat "$LAST_HASH_FILE")" ]; then
    {
      echo
      echo "> ⚠️ **Screener constituents identical to the previous run.** Possible stale/frozen"
      echo "> feed or a non-trading day the holiday table missed — treat signals with caution."
    } >>"$LOG_FILE"
    if [ "$HARD_SKIP_ON_UNCHANGED" = "true" ]; then
      notify "ARIA scan skipped" "Screener unchanged (stale feed?)"
      printf '%s' "$NEW_HASH" >"$LAST_HASH_FILE"
      exit 0
    fi
  fi
  printf '%s' "$NEW_HASH" >"$LAST_HASH_FILE"
fi

# ===========================================================================
# 6. Done — desktop notification
# ===========================================================================
# Success requires BOTH a clean exit AND the completion marker the prompt must
# emit on its final line — otherwise an aborted/stub run (e.g. CDP never came up)
# would wrongly alert "ready". No marker ⇒ treat as failure.
set +e   # bulletproof the alert/exit path: never let a stray non-zero (notify-send
         # failing under cron, grep -c returning 1, etc.) trip set -e and skip the alert.
trap - ERR
if [ "${CLAUDE_EC:-1}" = "0" ] && grep -q '^SCREENER_CONSTITUENTS:' "$LOG_FILE"; then
  notify "ARIA scan ready ✓" "$TODAY — log saved"
  # Put the ACTUAL report (headline + PRIME/RADAR tables) into the message body,
  # not a generic line — and still attach the full file. Trimmed to stay under
  # Telegram's 4096-char cap; the attachment always has the complete report.
  TG_MSG="$(printf '🟢 ARIA Bull Put Spread — %s\n\n%s\n\n…(full report attached)' \
    "$TODAY" "$(sed -n '1,80p' "$LOG_FILE" | head -c 3300)")"
  send_telegram "$TG_MSG" "$LOG_FILE"
  echo "[$RUN_TS] Done → $LOG_FILE" >>"$ERR_FILE"
  exit 0
else
  notify "ARIA scan FAILED" "incomplete — see log"
  REASON="$(sed -n '3,6p' "$LOG_FILE" | head -c 800)"
  send_telegram "🔴 ARIA scan FAILED / incomplete — ${TODAY}. Reason: ${REASON:-unknown}. Full log attached." "$LOG_FILE"
  echo "[$RUN_TS] FAILURE: claude_ec=${CLAUDE_EC:-?}, marker=$(grep -c '^SCREENER_CONSTITUENTS:' "$LOG_FILE" 2>/dev/null || echo 0)" >>"$ERR_FILE"
  exit 1
fi
