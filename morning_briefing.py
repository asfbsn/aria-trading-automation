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
import fcntl
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
SENT_MARKER = LOG_DIR / f"{TODAY}_morning-briefing.sent"
LOCK_FILE = LOG_DIR / "morning-briefing.lock"


def log_err(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERR_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{RUN_TS}] {line}\n")


def load_env() -> dict:
    """Minimal .env parser (KEY=value lines, matches the bash guards'
    `source .env` -- no python-dotenv dependency for a cron script).
    Starts from os.environ so values cron already exports (e.g. a
    TELEGRAM_BOT_TOKEN set in the crontab itself, not just .env) aren't
    silently dropped; .env entries override matching keys, same precedence
    the bash guards get from `source .env` after inheriting the shell env."""
    env = dict(os.environ)
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
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
UNPAIRED_LEGS_RE = re.compile(r"^UNPAIRED_LEGS:\s*(\d+)")


def parse_exit_guard_report() -> dict:
    path = LOG_DIR / f"{TODAY}_exit-guard.md"
    if not path.exists():
        return {"available": False}
    text = path.read_text(encoding="utf-8")
    # exit-guard.sh writes the report header BEFORE Claude runs, so a file
    # existing proves nothing about completion -- a crashed/partial run would
    # otherwise look "available" with positions_checked silently missing.
    # Require the literal last non-empty line to be POSITIONS_CHECKED: N,
    # exactly what bull-put-spread-exit.md's prompt now hard-enforces as the
    # final output line -- anything else means the run didn't finish clean.
    non_empty_lines = [ln for ln in text.splitlines() if ln.strip()]
    if not non_empty_lines or not POSITIONS_CHECKED_RE.fullmatch(non_empty_lines[-1].strip()):
        return {"available": False}
    positions_checked = None
    unpaired_legs = None  # None = marker absent from an otherwise-complete report
    action_needed = []  # CLOSE or RECOMMEND EXIT
    watch_hold = []
    for line in text.splitlines():
        m = POSITIONS_CHECKED_RE.match(line.strip())
        if m:
            positions_checked = int(m.group(1))
            continue
        m = UNPAIRED_LEGS_RE.match(line.strip())
        if m:
            unpaired_legs = int(m.group(1))
            continue
        m = POSITION_LINE_RE.match(line.strip())
        if m:
            verdict = m.group("verdict").strip()
            reason = m.group("reason").strip()
            # MA150_BREACH reasons in particular carry a paragraph of backtest
            # justification (see bull-put-spread-exit.md) -- truncate here so
            # a couple of flagged positions can't blow the whole message past
            # Telegram's length cap (see format_message's own backstop too).
            if len(reason) > 200:
                reason = reason[:200] + "... [truncated, see report]"
            entry = {"ticker": m.group("ticker"), "verdict": verdict, "reason": reason}
            if verdict in ("CLOSE", "RECOMMEND EXIT"):
                action_needed.append(entry)
            else:
                watch_hold.append(entry)
    return {
        "available": True,
        "positions_checked": positions_checked,
        "unpaired_legs": unpaired_legs,
        "action_needed": action_needed,
        "watch_hold": watch_hold,
    }


ORDERS_CHECKED_RE = re.compile(r"^ORDERS_CHECKED:\s*(\d+)")
GTC_ESCALATION_RE = re.compile(r"^\U0001F6A8\s*\[ACTION REQUIRED.*$")


def parse_gtc_guard_report() -> dict:
    """gtc-guard.sh's report -- see prompts/gtc-order-guard.md for the exact
    contract. No per-order verdict= line like exit-guard's; just a live-order
    count (ORDERS_CHECKED: N, same completeness-gate discipline as
    parse_exit_guard_report()) plus optional stale-order escalation lines
    (literal '\U0001F6A8 [ACTION REQUIRED...') this briefing surfaces
    verbatim -- those are exactly what needs eyes before the open."""
    path = LOG_DIR / f"{TODAY}_gtc-guard.md"
    if not path.exists():
        return {"available": False}
    text = path.read_text(encoding="utf-8")
    non_empty_lines = [ln for ln in text.splitlines() if ln.strip()]
    if not non_empty_lines or not ORDERS_CHECKED_RE.fullmatch(non_empty_lines[-1].strip()):
        return {"available": False}
    orders_checked = int(ORDERS_CHECKED_RE.fullmatch(non_empty_lines[-1].strip()).group(1))
    escalations = [ln.strip() for ln in text.splitlines() if GTC_ESCALATION_RE.match(ln.strip())]
    return {
        "available": True,
        "orders_checked": orders_checked,
        "escalations": escalations,
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


def format_message(macro: str, exit_report: dict, gtc_report: dict) -> str:
    lines = [f"\U0001F305 ARIA Morning Briefing -- {TODAY}", "", "MACRO (informational only):", macro, ""]

    if not exit_report["available"]:
        lines.append("EXIT-GUARD: no report for today yet -- either it hasn't run, or "
                      "the 09:00-09:14 ET window failed. Check positions manually.")
    else:
        n = exit_report["positions_checked"]
        lines.append(f"OPEN POSITIONS: {n if n is not None else 'unknown'}")
        lines.append("")
        unpaired = exit_report["unpaired_legs"]
        action_lines = list(exit_report["action_needed"])
        if unpaired:
            action_lines.append({
                "ticker": "UNPAIRED",
                "verdict": "VERIFY MANUALLY",
                "reason": f"{unpaired} unpaired/ambiguous option leg(s) found -- see "
                          f"{LOG_DIR / f'{TODAY}_exit-guard.md'} for detail",
            })
        elif unpaired is None:
            action_lines.append({
                "ticker": "UNPAIRED",
                "verdict": "VERIFY MANUALLY",
                "reason": "unpaired-leg count unavailable (marker missing from report) -- verify manually",
            })
        if action_lines:
            lines.append("\U0001F534 ACTION NEEDED AT THE OPEN:")
            for p in action_lines:
                lines.append(f"  {p['ticker']}: {p['verdict']} -- {p['reason']}")
        else:
            lines.append("✅ No CLOSE / RECOMMEND EXIT flags today.")
        if exit_report["watch_hold"]:
            lines.append("")
            lines.append("Other open positions (WATCH/HOLD, no action needed):")
            for p in exit_report["watch_hold"]:
                lines.append(f"  {p['ticker']}: {p['verdict']}")

    lines.append("")
    if not gtc_report["available"]:
        lines.append("GTC-GUARD: no report for today yet -- either it hasn't run, or "
                      "failed. Check live orders manually before the open.")
    else:
        lines.append(f"LIVE ORDERS: {gtc_report['orders_checked']}")
        if gtc_report["escalations"]:
            lines.append("")
            for line in gtc_report["escalations"]:
                lines.append(f"  {line}")

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

    # Cron fires every 3 min across 09:16-09:29 ET to survive DST drift (see
    # module docstring) -- that's up to 5 invocations landing inside the
    # window on an ordinary day. A bare check-then-write on SENT_MARKER has a
    # race if two invocations ever overlap (e.g. a slow Telegram call still
    # in flight when the next 3-min tick fires) -- both could pass the check
    # before either writes the marker. Hold an exclusive, non-blocking lock
    # for the whole check-through-marker-write span; a run that can't get it
    # just means another one is already actively handling today, so skip.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "a")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_err("Another briefing run holds the lock -- skip.")
        return 0

    try:
        # --force / FORCE_RUN bypasses this too, same as the schedule gate,
        # so manual re-runs still work.
        if not force and SENT_MARKER.exists():
            log_err("Already sent today (marker present) -- skip.")
            return 0

        env = load_env()
        exit_report = parse_exit_guard_report()
        gtc_report = parse_gtc_guard_report()
        macro = fetch_macro_facts()
        message = format_message(macro, exit_report, gtc_report)

        LOG_FILE.write_text(message + "\n", encoding="utf-8")

        # Telegram caps messages at 4096 chars; per-reason truncation above
        # handles the common case, this is the hard backstop so a send never
        # silently fails on length -- mirrors exit-guard.sh's own truncation
        # guard for the same limit.
        if len(message) > 3900:
            message = message[:3900] + f"\n\n⚠️ TRUNCATED -- full report: {LOG_FILE}"

        if send_telegram(message, env):
            SENT_MARKER.write_text(RUN_TS + "\n", encoding="utf-8")
            log_err("Done -- sent.")
            return 0
        log_err(f"Done -- Telegram send failed, report is in {LOG_FILE}.")
        return 1
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
