#!/usr/bin/env python3
"""ARIA Morning Briefing -- one consolidated Telegram message aggregating
what the human (manual execution layer for stops, per the 2026-09-05 audit)
needs before the 9:30 ET open: a plain macro readout, how many positions are
open, and which ones exit-guard.sh flagged CLOSE / RECOMMEND EXIT today.

Does NOT touch signal_core.py, bull-put-spread-exit.md, or any live prompt.
Does NOT call IBKR itself -- IBKR access in this project only exists inside
a `claude -p` run with the MCP connector attached (see daily-scan.sh /
exit-guard.sh / gtc-guard.sh); a standalone script has no path to it. This
script instead reads the markdown reports exit-guard.sh and gtc-guard.sh
already write to disk each morning, plus fetches public SPY/VIX data itself.

SCHEDULING NOTE (deliberate change from the requested 08:30 ET): exit-guard.sh
only runs in its own 09:00-09:14 ET window. A briefing at 08:30 ET would have
nothing to aggregate -- either stale (yesterday's) data or nothing at all.
This script self-gates to 09:16-09:29 ET instead: comfortably after
exit-guard/gtc-guard finish, comfortably before the 9:30 open. Override via
BRIEFING_WINDOW_START_HHMM / BRIEFING_WINDOW_END_HHMM if you disagree.

MACRO SECTION -- important: this is NOT research/timesfm_macro_radar.py.
That script was backtested tonight (research/timesfm_macro_radar_backtest.py)
and killed: 51.0% accuracy on 49 real windows vs 85.7% for doing nothing at
all, HALT fired on 55% of windows at 18.5% precision. Wiring a proven-worse-
than-random signal into a daily decision-support tool -- even just labeled
"macro radar" -- would undo tonight's whole point. This reports plain facts
(SPY close, SPY vs its own 150-day MA, VIX level) with no verdict, no HALT/
SAFE call, no predictive claim. If a real macro gate is ever built (see the
audit's IWM/SPY, HYG/TLT relative-strength idea, still unbuilt), swap in a
FACTS_ONLY=False path here explicitly -- don't let this comment rot.

Usage (cron): the venv has yfinance; call its python directly, no activation
needed --
  16,19,22,25,28 14-18 * * 1-5 /home/assaf/Projects/aria-trading/scripts/backtest/.venv/bin/python3 /home/assaf/Projects/aria-trading/morning_briefing.py >>/home/assaf/Projects/aria-trading/logs/morning-briefing-cron.log 2>&1
(Israel-local cron; CRON_TZ doesn't work on this box, same as the other
guards -- fires every 3 min across the Israel-local range the true 09:16-
09:29 ET window can fall in across DST, self-gates internally below.)
"""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

ARIA_HOME = Path(__file__).resolve().parent
LOG_DIR = ARIA_HOME / "logs"
HOLIDAYS_FILE = ARIA_HOME / "us-market-holidays.txt"
ENV_FILE = ARIA_HOME / ".env"

WINDOW_START_HHMM = int(os.environ.get("BRIEFING_WINDOW_START_HHMM", "916"))
WINDOW_END_HHMM = int(os.environ.get("BRIEFING_WINDOW_END_HHMM", "929"))

TODAY = date.today().isoformat()
RUN_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
LOG_FILE = LOG_DIR / f"{TODAY}_morning-briefing.md"
ERR_FILE = LOG_DIR / f"{TODAY}_morning-briefing-stderr.log"


def log_err(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERR_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{RUN_TS}] {line}\n")


def load_env() -> dict:
    """Minimal .env parser (KEY=value lines, matches the bash guards'
    `source .env` -- no python-dotenv dependency for a cron script)."""
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def check_schedule_gate(force: bool) -> bool:
    if force:
        return True
    weekday = datetime.now().isoweekday()
    if weekday >= 6:
        log_err("Weekend -- US market closed. Skip.")
        return False
    if HOLIDAYS_FILE.exists() and TODAY in HOLIDAYS_FILE.read_text().splitlines():
        log_err(f"{TODAY} is a US market holiday. Skip.")
        return False
    ny_now = datetime.now(ZoneInfo("America/New_York"))
    ny_hhmm = ny_now.hour * 100 + ny_now.minute
    if not (WINDOW_START_HHMM <= ny_hhmm <= WINDOW_END_HHMM):
        log_err(f"Outside {WINDOW_START_HHMM}-{WINDOW_END_HHMM} America/New_York "
                f"(NY time now: {ny_hhmm}) -- skip.")
        return False
    return True


POSITION_LINE_RE = re.compile(
    r"^(?P<ticker>\S+).*\|\s*verdict=(?P<verdict>[A-Z ]+?)\s*\|\s*reason=(?P<reason>.*)$"
)
POSITIONS_CHECKED_RE = re.compile(r"^POSITIONS_CHECKED:\s*(\d+)")


def parse_exit_guard_report() -> dict:
    path = LOG_DIR / f"{TODAY}_exit-guard.md"
    if not path.exists():
        return {"available": False}
    text = path.read_text(encoding="utf-8")
    positions_checked = None
    action_needed = []  # CLOSE or RECOMMEND EXIT
    watch_hold = []
    for line in text.splitlines():
        m = POSITIONS_CHECKED_RE.match(line.strip())
        if m:
            positions_checked = int(m.group(1))
            continue
        m = POSITION_LINE_RE.match(line.strip())
        if m:
            verdict = m.group("verdict").strip()
            entry = {"ticker": m.group("ticker"), "verdict": verdict, "reason": m.group("reason").strip()}
            if verdict in ("CLOSE", "RECOMMEND EXIT"):
                action_needed.append(entry)
            else:
                watch_hold.append(entry)
    return {
        "available": True,
        "positions_checked": positions_checked,
        "action_needed": action_needed,
        "watch_hold": watch_hold,
    }


def fetch_macro_facts() -> str:
    """Plain facts, no forecast, no verdict -- see module docstring for why
    this is deliberately not research/timesfm_macro_radar.py."""
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="9mo", progress=False, auto_adjust=True)["Close"].dropna().squeeze()
        vix = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)["Close"].dropna().squeeze()
        if spy.empty or vix.empty:
            return "Macro: unavailable (empty data) -- verify manually."
        spy_last = float(spy.iloc[-1])
        ma150 = float(spy.tail(150).mean()) if len(spy) >= 150 else None
        vix_last = float(vix.iloc[-1])
        if ma150:
            pct = (spy_last - ma150) / ma150 * 100
            spy_line = f"SPY ${spy_last:.2f} ({pct:+.1f}% vs its own 150d MA ${ma150:.2f})"
        else:
            spy_line = f"SPY ${spy_last:.2f} (150d MA unavailable -- insufficient history)"
        return f"{spy_line} | VIX {vix_last:.1f}\n(facts only -- no forecast, no gate, no verdict)"
    except Exception as e:  # noqa: BLE001 -- best-effort context, never block the briefing on this
        log_err(f"Macro fetch failed: {e!r}")
        return "Macro: fetch failed -- verify manually."


def format_message(macro: str, exit_report: dict) -> str:
    lines = [f"\U0001F305 ARIA Morning Briefing -- {TODAY}", "", "MACRO (informational only):", macro, ""]

    if not exit_report["available"]:
        lines.append("EXIT-GUARD: no report for today yet -- either it hasn't run, or "
                      "the 09:00-09:14 ET window failed. Check positions manually.")
    else:
        n = exit_report["positions_checked"]
        lines.append(f"OPEN POSITIONS: {n if n is not None else 'unknown'}")
        lines.append("")
        if exit_report["action_needed"]:
            lines.append("\U0001F534 ACTION NEEDED AT THE OPEN:")
            for p in exit_report["action_needed"]:
                lines.append(f"  {p['ticker']}: {p['verdict']} -- {p['reason']}")
        else:
            lines.append("✅ No CLOSE / RECOMMEND EXIT flags today.")
        if exit_report["watch_hold"]:
            lines.append("")
            lines.append("Other open positions (WATCH/HOLD, no action needed):")
            for p in exit_report["watch_hold"]:
                lines.append(f"  {p['ticker']}: {p['verdict']}")

    return "\n".join(lines)


def send_telegram(msg: str, env: dict) -> bool:
    tok = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        log_err("Telegram not configured; skipping.")
        return False
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as resp:
            if 200 <= resp.status < 300:
                return True
            body = resp.read().decode("utf-8", errors="replace")
            log_err(f"TELEGRAM SEND FAILED -- HTTP {resp.status}. Alerts are NOT reaching Telegram. Response: {body}")
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log_err(f"TELEGRAM SEND FAILED -- HTTP {e.code}. Alerts are NOT reaching Telegram. Response: {body}")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        log_err(f"TELEGRAM SEND FAILED -- transport error ({e!r}). Alerts are NOT reaching Telegram.")
        return False


def main() -> int:
    force = "--force" in sys.argv or os.environ.get("FORCE_RUN") == "true"
    if not check_schedule_gate(force):
        return 0

    env = load_env()
    exit_report = parse_exit_guard_report()
    macro = fetch_macro_facts()
    message = format_message(macro, exit_report)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(message + "\n", encoding="utf-8")

    if send_telegram(message, env):
        log_err("Done -- sent.")
        return 0
    log_err(f"Done -- Telegram send failed, report is in {LOG_FILE}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
