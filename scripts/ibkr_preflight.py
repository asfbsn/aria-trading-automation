#!/usr/bin/env python3
"""IBKR connector preflight for the headless cron wrappers.

Every ARIA job that needs IBKR runs a headless `claude --print` session through the claude.ai
"Interactive Brokers (IBKR)" connector. When that connector's OAuth login expires (it did on
2026-10-16), the session starts without any IBKR tools and each job fails in its own vague way.
This probe reads the session's MCP status at start-up (the stream-json `system/init` event, which
is emitted before any model work, so the probe is killed immediately and costs almost nothing)
and reports ONE clear problem.

Exit codes (the wrappers must treat anything but 2 as "proceed"):
  0  connector connected, OR the probe was inconclusive (pending, timeout, unparseable, ...)
  2  DEFINITE failure: the connector needs re-authorization -> the wrapper skips its session

The probe fails OPEN: it can only ever block a run on a definite `needs-auth`, never because the
probe itself broke.

Stale-cache handling: Claude Code remembers servers that failed auth for ~15 minutes
(~/.claude/mcp-needs-auth-cache.json) and headless sessions trust that cache without retrying.
After the owner re-authorizes, `claude mcp list` (a live check) says Connected while headless
sessions still see `needs-auth`. When that exact disagreement is observed the stale IBKR entry is
removed (backup kept) and the probe repeated once.

Alerts: one Telegram message per outage (re-sent after ALERT_REPEAT_SECONDS), cleared on success.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_MATCH = 'Interactive Brokers'
PROBE_TOOL = 'mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts'
NEEDS_AUTH_CACHE = Path.home() / '.claude' / 'mcp-needs-auth-cache.json'
ALERT_STAMP = ROOT / 'state' / 'ibkr_preflight_alert.stamp'
ALERT_REPEAT_SECONDS = 4 * 3600
LIVE_JOBS = {'daily-scan', 'exit-guard', 'gtc-guard'}


def log(message):
    print(f'[ibkr_preflight {time.strftime("%Y-%m-%d %H:%M:%S")}] {message}', file=sys.stderr, flush=True)


def init_status(line):
    """IBKR MCP status from one stream-json line: a status string, or None if not the init event / no IBKR server."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get('type') != 'system' or event.get('subtype') != 'init':
        return None
    for server in event.get('mcp_servers') or []:
        if isinstance(server, dict) and SERVER_MATCH in str(server.get('name', '')):
            return str(server.get('status', '')).strip().lower() or None
    return None


def probe_once(claude_bin='claude', timeout=75):
    """Start a headless session, read its init event, kill it. Returns the IBKR status string or None."""
    command = [claude_bin, '--print', '--output-format', 'stream-json', '--verbose',
               '--model', 'claude-haiku-4-5-20251001', '--permission-mode', 'default',
               '--allowedTools', PROBE_TOOL,
               '--settings', '{"enabledPlugins":{"claude-mem@thedotmack":false}}']
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, cwd=str(ROOT))
    except OSError as error:
        log(f'cannot start probe session: {error}')
        return None
    deadline = time.monotonic() + timeout
    try:
        try:
            process.stdin.write('ok')
            process.stdin.close()
        except OSError:
            pass
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            status = init_status(line)
            if status is not None:
                return status
            if '"subtype":"init"' in line.replace(' ', ''):
                return None   # init event without an IBKR server entry: inconclusive
    finally:
        process.kill()
        process.wait()
    return None


def live_check_connected(claude_bin='claude'):
    """True/False from `claude mcp list` (a LIVE connection check); None if unknown."""
    try:
        result = subprocess.run([claude_bin, 'mcp', 'list'], capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if SERVER_MATCH in line:
            if 'Connected' in line and '✔' in line:
                return True
            if 'Needs authentication' in line or 'needs auth' in line.lower():
                return False
            return None
    return None


def clear_stale_cache_entry(path=NEEDS_AUTH_CACHE):
    """Remove the IBKR entry from the needs-auth cache (backup kept). Returns True if an entry was removed."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    keys = [key for key in data if SERVER_MATCH in key]
    if not keys:
        return False
    try:
        shutil.copy(path, str(path) + '.bak-preflight')
        for key in keys:
            data.pop(key)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.needs-auth-', suffix='.tmp')
        with os.fdopen(handle, 'w') as out:
            json.dump(data, out)
        os.replace(tmp, path)
    except OSError as error:
        log(f'could not update needs-auth cache: {error}')
        return False
    return True


def check(probe=probe_once, live_check=live_check_connected, clear_cache=clear_stale_cache_entry,
          attempts=3, wait=20, sleep=time.sleep):
    """Returns (verdict, detail) with verdict in {'ok', 'blocked', 'inconclusive'}."""
    cache_cleared = False
    last = None
    for attempt in range(1, attempts + 1):
        status = probe()
        last = status
        if status == 'connected':
            return 'ok', f'connected (attempt {attempt})'
        if status == 'needs-auth':
            if not cache_cleared and live_check() is True and clear_cache():
                cache_cleared = True
                log('connector is live-connected but a stale needs-auth cache entry hid it; entry removed, re-probing')
                continue
            return 'blocked', 'IBKR connector needs re-authorization'
        if attempt < attempts:
            log(f'connector status {status!r} (attempt {attempt}/{attempts}); retrying in {wait}s')
            sleep(wait)
    return 'inconclusive', f'last status {last!r}; proceeding'


def telegram_credentials():
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN', ''), os.environ.get('TELEGRAM_CHAT_ID', '')
    env_file = ROOT / '.env'
    if (not token or not chat) and env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[len('export '):].lstrip()
            if '=' not in line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() == 'TELEGRAM_BOT_TOKEN' and not token:
                token = value
            elif key.strip() == 'TELEGRAM_CHAT_ID' and not chat:
                chat = value
    return token, chat


def alert_due(stamp=ALERT_STAMP, now=None):
    now = time.time() if now is None else now
    try:
        return now - float(stamp.read_text().strip()) >= ALERT_REPEAT_SECONDS
    except (OSError, ValueError):
        return True


def send_alert(job, send=True, stamp=ALERT_STAMP):
    message = (f'🔴 ARIA: the IBKR connector needs re-authorization. Job "{job}" was skipped at '
               f'{time.strftime("%H:%M %Z")}. Fix: claude.ai → Settings → Connectors → Interactive Brokers → reconnect.')
    if job in LIVE_JOBS:
        message += ' Live-account checks (exit guard / GTC guard / scan) are NOT running until then: verify positions and orders manually in IBKR.'
    if not alert_due(stamp):
        log('alert already sent recently; not repeating')
        return False
    if send:
        token, chat = telegram_credentials()
        if not token or not chat:
            log('Telegram not configured; cannot alert')
            return False
        data = urllib.parse.urlencode({'chat_id': chat, 'text': message}).encode()
        try:
            with urllib.request.urlopen(f'https://api.telegram.org/bot{token}/sendMessage', data=data, timeout=30):
                pass
        except Exception as error:  # noqa: BLE001 - alert failure must never break the job
            log(f'Telegram send failed: {type(error).__name__}')
            return False
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
    except OSError:
        pass
    return True


def clear_alert(stamp=ALERT_STAMP):
    try:
        stamp.unlink()
    except OSError:
        pass


def self_test():
    failures = 0

    def expect(label, condition):
        nonlocal failures
        print(('PASS: ' if condition else 'FAIL: ') + label)
        failures += 0 if condition else 1

    init = lambda status: json.dumps({'type': 'system', 'subtype': 'init', 'mcp_servers': [
        {'name': 'claude.ai Canva', 'status': 'needs-auth'},
        {'name': 'claude.ai Interactive Brokers (IBKR)', 'status': status}]})
    expect('init status parsed', init_status(init('connected')) == 'connected')
    expect('needs-auth parsed', init_status(init('needs-auth')) == 'needs-auth')
    expect('non-init line ignored', init_status('{"type":"assistant"}') is None)
    expect('garbage ignored', init_status('not json') is None)
    expect('init without IBKR is None', init_status('{"type":"system","subtype":"init","mcp_servers":[]}') is None)

    sleeps = []
    run = lambda statuses, live=None, cleared=False: check(
        probe=iter(statuses).__next__, live_check=lambda: live, clear_cache=lambda: cleared,
        attempts=3, wait=0, sleep=sleeps.append)
    expect('connected -> ok', run(['connected'])[0] == 'ok')
    expect('pending then connected -> ok', run(['pending', 'connected'])[0] == 'ok')
    expect('needs-auth with live needs-auth -> blocked', run(['needs-auth'], live=False)[0] == 'blocked')
    expect('needs-auth with unknown live check -> blocked', run(['needs-auth'], live=None)[0] == 'blocked')
    expect('stale cache cleared then connected -> ok', run(['needs-auth', 'connected'], live=True, cleared=True)[0] == 'ok')
    expect('stale cache cleared but still needs-auth -> blocked',
           run(['needs-auth', 'needs-auth'], live=True, cleared=True)[0] == 'blocked')
    expect('live connected but nothing to clear -> blocked', run(['needs-auth'], live=True, cleared=False)[0] == 'blocked')
    expect('all pending -> inconclusive (fail open)', run(['pending', 'pending', 'pending'])[0] == 'inconclusive')
    expect('unparseable probes -> inconclusive (fail open)', run([None, None, None])[0] == 'inconclusive')

    with tempfile.TemporaryDirectory() as temp:
        cache = Path(temp) / 'mcp-needs-auth-cache.json'
        cache.write_text(json.dumps({'claude.ai Canva': {'timestamp': 1},
                                     'claude.ai Interactive Brokers (IBKR)': {'timestamp': 2}}))
        expect('stale entry removed', clear_stale_cache_entry(cache) is True)
        remaining = json.loads(cache.read_text())
        expect('other servers untouched', list(remaining) == ['claude.ai Canva'])
        expect('backup written', Path(str(cache) + '.bak-preflight').exists())
        expect('nothing left to remove', clear_stale_cache_entry(cache) is False)
        stamp = Path(temp) / 'stamp'
        expect('alert due with no stamp', alert_due(stamp, now=1000.0))
        stamp.write_text('1000')
        expect('alert suppressed within window', not alert_due(stamp, now=1000.0 + 60))
        expect('alert due after window', alert_due(stamp, now=1000.0 + ALERT_REPEAT_SECONDS + 1))
        expect('send_alert dedupes (no network in test)', send_alert('x', send=False, stamp=Path(temp) / 's2') is True
               and send_alert('x', send=False, stamp=Path(temp) / 's2') is False)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--job', default='unknown', help='calling job name (used in the alert)')
    parser.add_argument('--attempts', type=int, default=4)
    parser.add_argument('--wait', type=int, default=15)
    parser.add_argument('--no-alert', action='store_true', help='do not send Telegram')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        verdict, detail = check(attempts=max(1, args.attempts), wait=max(0, args.wait))
    except Exception as error:  # noqa: BLE001 - the probe must never block a run by crashing
        log(f'probe error ({type(error).__name__}: {error}); proceeding')
        return 0
    log(f'{verdict}: {detail}')
    if verdict == 'ok':
        clear_alert()
        return 0
    if verdict == 'blocked':
        if not args.no_alert:
            send_alert(args.job)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
