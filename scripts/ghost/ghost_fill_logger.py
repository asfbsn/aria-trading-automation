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

Quote timestamps: get_price_snapshot's bid_ask field carries no per-quote
timestamp of its own (confirmed empirically 2026-09-16 -- only `last` does).
short_quote_ts_utc/long_quote_ts_utc may therefore be the CALLER'S OWN capture
time (when it received that leg's response), not an exchange-confirmed quote
timestamp -- short_quote_ts_is_estimated / long_quote_ts_is_estimated mark
this case explicitly, PER LEG (one leg can carry a genuine connector
timestamp while the other falls back to capture time -- a single shared flag
couldn't represent that, CodeRabbit finding 2026-09-17). Treat
quote_skew_seconds and RTH classification accordingly when either flag is
true: they measure time between our two tool calls for that leg, not
genuine exchange-side quote skew.
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
# 5s was calibrated for genuine simultaneous exchange-side quote timestamps.
# Capture-time timestamps (see quote_ts_is_estimated) reflect real tool-call
# latency between the two legs instead -- empirically 8-11s on 2026-09-16's
# live smoke test -- so 5s rejected almost every real candidate. Widened
# uniformly (not conditioned on quote_ts_is_estimated) per explicit user
# decision that date; still bounds inter-leg staleness, just to a looser
# number reflecting what capture-time timestamps can actually measure.
QUOTE_SKEW_MAX_SECONDS = 15
INPUT_FIELDS = '''candidate_id run_id trade_date signal_bar_date mode ticker
signal_close ma150 vrp_ratio iv_current hv_current iv_as_of_date gex_regime
gex_percentile gex_data_available gex_as_of z_ma150 filter_tag derived_short_strike derived_long_strike
derived_expiry resolved_short_strike resolved_long_strike resolved_expiry
resolution_status underlying_contract_id short_contract_id long_contract_id
underlying_spot short_bid short_ask short_bid_size short_ask_size
short_quote_ts_utc long_bid long_ask long_bid_size long_ask_size long_quote_ts_utc
short_quote_ts_is_estimated long_quote_ts_is_estimated market_data_type in_rth_claimed
code_version_hash git_head git_dirty'''.split()
ENTRY_FIELDS = '''run_id quote_ts_utc trade_date signal_bar_date mode ticker candidate_id
in_rth market_data_type signal_close ma150 vrp_ratio iv_current hv_current
iv_as_of_date gex_regime gex_percentile gex_data_available gex_as_of z_ma150 filter_tag
resolved_short_strike resolved_long_strike resolved_expiry resolution_status
underlying_contract_id short_contract_id long_contract_id
underlying_spot short_bid short_ask short_mid short_bid_size short_ask_size long_bid
long_ask long_mid long_bid_size long_ask_size short_quote_ts_utc long_quote_ts_utc
short_quote_ts_is_estimated long_quote_ts_is_estimated quote_skew_seconds
short_spread_abs long_spread_abs credit_mid
credit_natural displayed_crossing_cost_per_leg spread_width_pct_of_credit
liquidity_gate_pass code_version_hash git_head git_dirty'''.split()
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
    if data.get('resolution_status') == 'conid_field_mismatch' or data.get('conid_field_mismatch') is True:
        return values, 'conid_field_mismatch'
    if data.get('resolution_status') != 'exact':
        return values, 'resolution_not_exact'
    for leg in ('short', 'long'):
        if data.get(f'resolved_{leg}_strike') != data.get(f'derived_{leg}_strike') or data.get(f'resolved_{leg}_strike') is None:
            return values, f'{leg}_strike_not_exact'
    if not data.get('resolved_expiry') or data['resolved_expiry'] != data.get('derived_expiry'):
        return values, 'expiry_not_exact'
    for conid_field in ('underlying_contract_id', 'short_contract_id', 'long_contract_id'):
        conid = data.get(conid_field)
        if conid is None:
            return values, f'{conid_field}_missing'
        if isinstance(conid, bool):
            return values, f'{conid_field}_is_boolean'
        if not isinstance(conid, int):
            return values, f'{conid_field}_float_or_fractional'
        if conid <= 0:
            return values, f'{conid_field}_nonpositive'
    if timestamp_error:
        return values, 'missing_or_invalid_quote_timestamp'
    if values['quote_skew_seconds'] > QUOTE_SKEW_MAX_SECONDS:
        return values, f'quote_skew_exceeds_{QUOTE_SKEW_MAX_SECONDS}_seconds'
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
    if not header:
        # A field list change (this file changed INPUT_FIELDS/ENTRY_FIELDS
        # twice in one night, 2026-09-16) silently corrupts every row
        # appended after the change: csv.DictWriter writes by the CURRENT
        # fields order, but the file's on-disk header is still the OLD
        # order, so every reader positionally misreads columns from that
        # point on with no error anywhere -- caught only by eyeballing a
        # garbled value (quote_skew_seconds read back as "True"). Fail
        # loudly instead of writing a row that will read back wrong.
        with path.open('r', newline='', encoding='utf-8') as existing:
            existing_header = next(csv.reader(existing), [])
        if existing_header != fields:
            raise ValueError(
                f'{path} header does not match current fields -- would silently '
                f'misalign every column from here on. Existing: {existing_header}. '
                f'Current: {fields}. Migrate or archive the old file before writing.'
            )
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
    # CSV round-trips every value as a string (csv.DictReader never restores
    # the original JSON type), so comparing a non-string candidate_id (int,
    # None, ...) against already-written rows never matches -- attempt
    # always resets to 1 and, for a repeated identical malformed payload,
    # collides on the SAME archive filename (safe_id_1.json) and crashes on
    # the exclusive-create open() instead of degrading to a clean rejection.
    # Compare on a stable string form either way (a no-op for the normal
    # case where candidate already is one).
    candidate_key = candidate if isinstance(candidate, str) else str(candidate)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_path = state_dir / 'ghost_observations_raw.csv'
        entry_path = state_dir / 'ghost_entries.csv'
        previous = [row for row in rows(raw_path) if row['candidate_id'] == candidate_key]
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
                row['candidate_id'] == candidate_key for row in rows(entry_path)):
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
                underlying_contract_id=123456, short_contract_id=234567, long_contract_id=345678,
                short_bid=2.0, short_ask=2.2, long_bid=0.9, long_ask=1.1,
                short_bid_size=3, long_bid_size=2, market_data_type='live',
                short_quote_ts_utc='2026-09-15T14:00:00Z',
                long_quote_ts_utc='2026-09-15T14:00:02+00:00',
                short_quote_ts_is_estimated=False, long_quote_ts_is_estimated=False,
                z_ma150=1.8765, filter_tag='gex_positive_z_ma150_ge_1.5')
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
            assert math.isclose(float(row['z_ma150']), 1.8765, abs_tol=1e-6), 'z_ma150'
            assert row['filter_tag'] == 'gex_positive_z_ma150_ge_1.5', 'filter_tag'
            assert row['underlying_contract_id'] == '123456'
            assert row['short_contract_id'] == '234567'
            assert row['long_contract_id'] == '345678'
            assert int(row['underlying_contract_id']) == 123456
            assert int(row['short_contract_id']) == 234567
            assert int(row['long_contract_id']) == 345678
        def skew():
            # Must exceed QUOTE_SKEW_MAX_SECONDS (15s), not the old 5s bar.
            result = record({**base, 'candidate_id': 'skew', 'long_quote_ts_utc': '2026-09-15T14:00:20Z'})
            assert result['outcome'] == 'rejected' and 'skew' in result['reason']
        def skew_within_widened_threshold():
            # 10s exceeds the OLD 5s bar but must pass under the current 15s
            # one -- proves the widened threshold actually took effect, not
            # just that some skew value still rejects.
            result = record({**base, 'candidate_id': 'skew_ok', 'long_quote_ts_utc': '2026-09-15T14:00:10Z'})
            assert result['outcome'] == 'accepted'
        def resolution():
            assert record({**base, 'candidate_id': 'resolution', 'resolution_status': 'no_exact_match'})['reason'] == 'resolution_not_exact'
        def duplicate():
            assert record(base)['outcome'] == 'duplicate_skipped'
            valid_entries = [r for r in rows(state / 'ghost_entries.csv') if r['candidate_id'] == 'valid']
            assert len(valid_entries) == 1
            assert (state / 'raw_responses/valid_2.json').read_bytes() == json.dumps(base, indent=2).encode()
        def missing_live():
            payload = {**base, 'candidate_id': 'missing_live'}
            del payload['market_data_type']
            assert record(payload)['reason'] == 'market_data_not_live_or_missing'
        def estimated_timestamp():
            # short/long_quote_ts_is_estimated=True (capture-time fallback,
            # no genuine connector timestamp) must still accept normally --
            # the flags are provenance metadata, not a validation gate. Per-
            # leg, not a single shared flag: one leg can have a genuine
            # connector timestamp while the other falls back to capture time
            # (CodeRabbit finding, 2026-09-17) -- test asymmetric legs, not
            # just both-True, to prove they're actually independent.
            result = record({**base, 'candidate_id': 'estimated',
                              'short_quote_ts_is_estimated': True,
                              'long_quote_ts_is_estimated': False})
            assert result['outcome'] == 'accepted'
            row = [r for r in rows(state / 'ghost_entries.csv') if r['candidate_id'] == 'estimated'][0]
            assert row['short_quote_ts_is_estimated'] == 'True'
            assert row['long_quote_ts_is_estimated'] == 'False'
        def repeated_malformed_candidate_id():
            # A non-string candidate_id (e.g. an upstream bug sending an
            # int) resubmitted with byte-identical payload must not crash:
            # safe_id (sha256 fallback) is identical both times, so attempt
            # must correctly increment to 2 via a type-stable comparison, or
            # the archive write's exclusive-create collides on the same
            # filename and raises FileExistsError instead of a clean second
            # rejection.
            payload = {**base, 'candidate_id': 12345}
            first = record(payload)
            second = record(payload)  # must reach clean duplicate detection, not crash
            assert first['reason'] == 'missing_or_invalid_candidate_id'
            assert second['outcome'] == 'duplicate_skipped'
            assert second['attempt_n'] == first['attempt_n'] + 1
        def fixture1_missing_contract_id():
            # Fixture 1: Missing contract ID (None/absent) is explicitly rejected.
            payload = {**base, 'candidate_id': 'missing_conid', 'short_contract_id': None}
            res = record(payload)
            assert res['outcome'] == 'rejected'
            assert res['reason'] == 'short_contract_id_missing'
        def fixture2_mismatched_invalid_contract_ids():
            # Fixture 2: 4 distinct rejections:
            # 1. boolean rejected
            res_bool = record({**base, 'candidate_id': 'bool_conid', 'short_contract_id': True})
            assert res_bool['outcome'] == 'rejected' and res_bool['reason'] == 'short_contract_id_is_boolean'
            # 2. float/fractional rejected
            res_float = record({**base, 'candidate_id': 'float_conid', 'short_contract_id': 12345.67})
            assert res_float['outcome'] == 'rejected' and res_float['reason'] == 'short_contract_id_float_or_fractional'
            # 3. non-positive (<=0) rejected
            res_zero = record({**base, 'candidate_id': 'zero_conid', 'short_contract_id': 0})
            assert res_zero['outcome'] == 'rejected' and res_zero['reason'] == 'short_contract_id_nonpositive'
            res_neg = record({**base, 'candidate_id': 'neg_conid', 'short_contract_id': -99})
            assert res_neg['outcome'] == 'rejected' and res_neg['reason'] == 'short_contract_id_nonpositive'
            # 4. Item 2 exact-verification rule mismatch rejected
            res_mismatch = record({**base, 'candidate_id': 'mismatch_conid', 'resolution_status': 'conid_field_mismatch'})
            assert res_mismatch['outcome'] == 'rejected' and res_mismatch['reason'] == 'conid_field_mismatch'
        def fixture3_csv_integer_round_trip():
            # Fixture 3: Real positive integer contract IDs round-trip accurately through CSV.
            payload = {**base, 'candidate_id': 'roundtrip', 'underlying_contract_id': 987654321,
                       'short_contract_id': 876543210, 'long_contract_id': 765432109}
            assert record(payload)['outcome'] == 'accepted'
            row = [r for r in rows(state / 'ghost_entries.csv') if r['candidate_id'] == 'roundtrip'][0]
            # Ensure on-disk string representation is plain integer string
            assert row['underlying_contract_id'] == '987654321'
            assert row['short_contract_id'] == '876543210'
            assert row['long_contract_id'] == '765432109'
            # Must NOT roundtrip as boolean string, float string, or None
            assert not row['underlying_contract_id'].endswith('.0')
            assert row['underlying_contract_id'] != 'True'
            # Type restores cleanly to exact int
            assert int(row['underlying_contract_id']) == 987654321
            assert int(row['short_contract_id']) == 876543210
            assert int(row['long_contract_id']) == 765432109
        for label, check in [('valid arithmetic', valid), ('20-second skew rejected', skew),
                             ('10-second skew accepted under widened threshold', skew_within_widened_threshold),
                             ('no exact match', resolution), ('idempotency', duplicate),
                             ('missing market_data_type', missing_live),
                             ('estimated timestamp still accepts', estimated_timestamp),
                             ('repeated malformed candidate_id does not crash', repeated_malformed_candidate_id),
                             ('fixture 1: missing contract ID rejected', fixture1_missing_contract_id),
                             ('fixture 2: mismatched/invalid contract ID rejected (4 distinct cases)', fixture2_mismatched_invalid_contract_ids),
                             ('fixture 3: CSV integer round-trip', fixture3_csv_integer_round_trip)]:
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
