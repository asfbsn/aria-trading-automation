#!/usr/bin/env python3
"""Offline shadow quote logger. No broker imports, network, or execution capability.

All prices are option quote dollars per share. displayed_crossing_cost_per_leg
is the cost of fully crossing DISPLAYED quotes, not an observed real fill (no
order ever placed). Above 0.025/0.05 means that assumption is already implausible
against displayed markets; below does NOT prove a real combo fill is achievable.
Like friction_analyzer_v2.py, multiply this per-leg/per-share/per-side cost by
2 * 100 for one two-leg entry, not by four (which would include an exit).

Accepted and rejected observations are terminal; duplicates remain auditable.
Raw CSV preserves input values; input_json and archived bytes preserve JSON types,
nulls, extra fields, and original formatting. RTH uses the repository's local
holiday table (unknown years fail closed), standard 09:30–16:00 New York hours,
and 13:00 closes on July 3, Christmas Eve, and the Friday after Thanksgiving.
Update the holiday table for exceptional closures and future years.
"""

import argparse
import csv
import fcntl
import hashlib
import json
import math
import re
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
INPUT_FIELDS = '''candidate_id run_id trade_date signal_bar_date mode ticker
signal_close ma150 vrp_ratio iv_current hv_current iv_as_of_date gex_regime
gex_percentile gex_data_available gex_as_of derived_short_strike derived_long_strike
derived_expiry resolved_short_strike resolved_long_strike resolved_expiry
resolution_status underlying_spot short_bid short_ask short_bid_size short_ask_size
short_quote_ts_utc long_bid long_ask long_bid_size long_ask_size long_quote_ts_utc
market_data_type in_rth_claimed code_version_hash git_head git_dirty'''.split()
ENTRY_FIELDS = '''run_id quote_ts_utc trade_date signal_bar_date mode ticker candidate_id
in_rth market_data_type signal_close ma150 vrp_ratio iv_current hv_current
iv_as_of_date gex_regime gex_percentile gex_data_available gex_as_of
resolved_short_strike resolved_long_strike resolved_expiry resolution_status
underlying_spot short_bid short_ask short_mid short_bid_size short_ask_size long_bid
long_ask long_mid long_bid_size long_ask_size short_quote_ts_utc long_quote_ts_utc
quote_skew_seconds short_spread_abs long_spread_abs credit_mid credit_natural
displayed_crossing_cost_per_leg spread_width_pct_of_credit liquidity_gate_pass
code_version_hash git_head git_dirty'''.split()
RAW_FIELDS = ['run_id', 'candidate_id', 'attempt_n', 'ts_utc', 'outcome', 'reason'] + [
    key for key in INPUT_FIELDS if key not in ('run_id', 'candidate_id')
] + ['in_rth', 'in_rth_claim_agrees', 'quote_skew_seconds', 'input_json']


def in_regular_hours(now, holiday_text):
    local = now.astimezone(ZoneInfo('America/New_York'))
    day = local.date()
    holidays = {line.strip() for line in holiday_text.splitlines()
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', line.strip())}
    if not any(value.startswith(str(day.year) + '-') for value in holidays):
        return False
    if local.weekday() >= 5 or day.isoformat() in holidays:
        return False
    yesterday = day - timedelta(days=1)
    early = ((day.month, day.day) in ((7, 3), (12, 24)) or
             (day.month == 11 and day.weekday() == 4 and 22 <= yesterday.day <= 28))
    return time(9, 30) <= local.time() < time(13 if early else 16)


def quote_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def validate(data):
    values = {'quote_skew_seconds': None}
    try:
        short_ts = quote_time(data.get('short_quote_ts_utc'))
        long_ts = quote_time(data.get('long_quote_ts_utc'))
        values['quote_skew_seconds'] = abs((short_ts - long_ts).total_seconds())
        # Available even if a later check rejects the candidate for an
        # unrelated reason (strike/expiry/liquidity) -- RTH classification
        # should reflect when the quotes were actually taken, not whenever
        # this logger process happened to run afterward.
        values['_rth_ts'] = max(short_ts, long_ts)
    except (ValueError, TypeError, AttributeError, OverflowError):
        timestamp_error = True
    else:
        timestamp_error = False
    if data.get('resolution_status') != 'exact':
        return values, 'resolution_not_exact'
    for leg in ('short', 'long'):
        if data.get(f'resolved_{leg}_strike') != data.get(f'derived_{leg}_strike') or data.get(f'resolved_{leg}_strike') is None:
            return values, f'{leg}_strike_not_exact'
    if not data.get('resolved_expiry') or data['resolved_expiry'] != data.get('derived_expiry'):
        return values, 'expiry_not_exact'
    if timestamp_error:
        return values, 'missing_or_invalid_quote_timestamp'
    if values['quote_skew_seconds'] > 5:
        return values, 'quote_skew_exceeds_5_seconds'
    # Explicit allowlist, never substring matching ("not live" must fail).
    if str(data.get('market_data_type', '')).strip().lower() not in {'live', 'real-time', 'realtime', 'real_time', '1'}:
        return values, 'market_data_not_live_or_missing'
    for leg in ('short', 'long'):
        for side in ('bid', 'ask'):
            value = data.get(f'{leg}_{side}')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                return values, f'{leg}_{side}_missing_or_nonpositive_or_nonfinite'
        bid, ask = data[f'{leg}_bid'], data[f'{leg}_ask']
        mid = bid / 2 + ask / 2
        if not 0 < bid <= mid <= ask:
            return values, f'{leg}_crossed_quotes'
        values[f'{leg}_mid'] = mid
        values[f'{leg}_spread_abs'] = ask - bid
    if values['short_mid'] <= values['long_mid']:
        return values, 'nonpositive_mid_credit'
    credit = values['short_mid'] - values['long_mid']
    natural = data['short_bid'] - data['long_ask']
    values.update(credit_mid=credit, credit_natural=natural,
                  displayed_crossing_cost_per_leg=(credit - natural) / 2,
                  spread_width_pct_of_credit=(values['short_spread_abs'] + values['long_spread_abs']) / credit,
                  liquidity_gate_pass=bool(values['short_spread_abs'] <= 0.30 and
                                           values['long_spread_abs'] <= 0.30 and
                                           data.get('short_bid_size') and data.get('long_ask_size')),
                  quote_ts_utc=max(short_ts, long_ts).isoformat())
    return values, ''


def rows(path):
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def append(path, fields, row):
    header = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        if header:
            writer.writeheader()
        writer.writerow(row)


def log_observation(raw_bytes, state_dir, holiday_text):
    data = json.loads(raw_bytes)
    if not isinstance(data, dict):
        raise ValueError('Input must be one candidate JSON object')
    candidate = data.get('candidate_id', '')
    safe_id = candidate if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_-]+', candidate) else hashlib.sha256(raw_bytes).hexdigest()
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_path = state_dir / 'ghost_observations_raw.csv'
        entry_path = state_dir / 'ghost_entries.csv'
        previous = [row for row in rows(raw_path) if row['candidate_id'] == candidate]
        attempt = max((int(row['attempt_n']) for row in previous), default=0) + 1
        archive = state_dir / 'raw_responses'
        archive.mkdir(exist_ok=True)
        with (archive / f'{safe_id}_{attempt}.json').open('xb') as handle:
            handle.write(raw_bytes)
        now = datetime.now(timezone.utc)
        values, reason = validate(data)
        # Classify RTH against the actual quote timestamp when available
        # (see the _rth_ts comment in validate()); fall back to wall-clock
        # time only when timestamps themselves are missing/invalid.
        rth = in_regular_hours(values.pop('_rth_ts', now), holiday_text)
        if safe_id != candidate:
            reason = 'missing_or_invalid_candidate_id'
        outcome = 'rejected' if reason else 'accepted'
        if any(row['outcome'] in {'accepted', 'rejected', 'terminal'} for row in previous) or any(
                row['candidate_id'] == candidate for row in rows(entry_path)):
            outcome, reason = 'duplicate_skipped', 'candidate_already_terminal'
        if outcome == 'accepted':
            append(entry_path, ENTRY_FIELDS, {**data, **values, 'in_rth': rth})
        claim = data.get('in_rth_claimed')
        append(raw_path, RAW_FIELDS, {**data, 'attempt_n': attempt,
               'ts_utc': now.isoformat(), 'outcome': outcome, 'reason': reason,
               'in_rth': rth, 'in_rth_claim_agrees': rth == claim if isinstance(claim, bool) else None,
               'quote_skew_seconds': values['quote_skew_seconds'],
               'input_json': raw_bytes.decode('utf-8')})
        return {'candidate_id': candidate, 'attempt_n': attempt, 'outcome': outcome, 'reason': reason}


def self_test():
    base = dict.fromkeys(INPUT_FIELDS)
    base.update(candidate_id='valid', run_id='synthetic', resolution_status='exact',
                derived_short_strike=100, resolved_short_strike=100,
                derived_long_strike=95, resolved_long_strike=95,
                derived_expiry='2026-10-16', resolved_expiry='2026-10-16',
                short_bid=2.0, short_ask=2.2, long_bid=0.9, long_ask=1.1,
                short_bid_size=3, long_bid_size=2, market_data_type='live',
                short_quote_ts_utc='2026-09-15T14:00:00Z',
                long_quote_ts_utc='2026-09-15T14:00:02+00:00')
    failures = 0
    with tempfile.TemporaryDirectory(prefix='ghost-self-test-') as temp:
        state = Path(temp)
        def record(data):
            return log_observation(json.dumps(data, indent=2).encode(), state, '2026-01-01')
        def valid():
            assert record(base)['outcome'] == 'accepted'
            row = rows(state / 'ghost_entries.csv')[0]
            for key, expected in dict(short_mid=2.1, long_mid=1, short_spread_abs=.2,
                    long_spread_abs=.2, credit_mid=1.1, credit_natural=.9,
                    displayed_crossing_cost_per_leg=.1, spread_width_pct_of_credit=4/11,
                    quote_skew_seconds=2).items():
                assert math.isclose(float(row[key]), expected, abs_tol=1e-12), key
        def skew():
            result = record({**base, 'candidate_id': 'skew', 'long_quote_ts_utc': '2026-09-15T14:00:10Z'})
            assert result['outcome'] == 'rejected' and 'skew' in result['reason']
        def resolution():
            assert record({**base, 'candidate_id': 'resolution', 'resolution_status': 'no_exact_match'})['reason'] == 'resolution_not_exact'
        def duplicate():
            assert record(base)['outcome'] == 'duplicate_skipped'
            assert len(rows(state / 'ghost_entries.csv')) == 1
            assert (state / 'raw_responses/valid_2.json').read_bytes() == json.dumps(base, indent=2).encode()
        def missing_live():
            payload = {**base, 'candidate_id': 'missing_live'}
            del payload['market_data_type']
            assert record(payload)['reason'] == 'market_data_not_live_or_missing'
        for label, check in [('valid arithmetic', valid), ('10-second skew', skew),
                             ('no exact match', resolution), ('idempotency', duplicate),
                             ('missing market_data_type', missing_live)]:
            try:
                check()
                print(f'PASS: {label}')
            except Exception as error:
                failures += 1
                print(f'FAIL: {label}: {error}')
    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--input', type=Path)
    group.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    holidays = ROOT / 'us-market-holidays.txt'
    result = log_observation(args.input.read_bytes(), ROOT / 'state/ghost',
                             holidays.read_text() if holidays.exists() else '')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
