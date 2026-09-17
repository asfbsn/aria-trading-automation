#!/usr/bin/env python3
"""Ghost paper marks and exits, decided only by compute_exit_signal_v2.

Run with scripts/backtest/.venv/bin/python3, like ghost_prescreen_v2.py.
Deliberate deviation from the stdlib-only ghost_fill_logger: the exit engine
imports signal_core and live GEX resolution uses dix_fetcher_v2, requiring
pandas. Real runs omit gex_regime and let the engine fetch its own live regime.
This logger never fetches IBKR quotes/history or places orders.

Passing bars=[] ALWAYS selects the engine's insufficient_data=True branch.
Profit target, time stop and POSITIVE-regime premium stop need no bars and
remain evaluated. The NEGATIVE-regime structural short-strike-breach stop is
NOT evaluated: it needs settled OHLCV bars, which this logger never fetches,
avoiding a new get_price_history IBKR grant. This is a real scope limitation;
a mark does not establish that the structural thesis is intact.

All prices/P&L are option quote dollars per share, including realized_pnl_mid
(a paper mid-price estimate, not an execution). Both quote timestamps must be
in RTH and on mark_date in New York. Estimated timestamps remain permitted;
RTH/skew then describe capture times, not confirmed exchange quote times.
No wall-clock freshness threshold is imposed; callers supply fresh captures.
Every normal attempt retains its original JSON in the raw audit CSV. Writes
reuse the entry logger's append/header checks under its shared .logger.lock;
as in that logger, multiple CSV appends are not a crash-atomic transaction.
"""

import argparse
import csv
import fcntl
import json
import math
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Support direct script invocation as well as imports from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ghost.ghost_fill_logger import (
    ROOT, rows, append, in_regular_hours, quote_time, QUOTE_SKEW_MAX_SECONDS,
)
from scripts.compute_exit_signal_v2 import compute_exit_signal_v2

MARK_FIELDS = '''run_id mark_date candidate_id ticker dte short_mid long_mid
cost_to_close_mid cost_to_close_natural displayed_crossing_cost_per_leg_close
unrealized_pnl_mid pct_max_profit_captured regime regime_data_available
premium_stop_triggered profit_target_triggered time_stop_triggered
code_version_hash git_head git_dirty'''.split()
EXIT_FIELDS = '''run_id exit_date candidate_id ticker trade_date dte_at_exit
exit_reason credit_mid cost_to_close_mid cost_to_close_natural
displayed_crossing_cost_per_leg_close realized_pnl_mid pct_max_profit_captured
regime regime_data_available code_version_hash git_head git_dirty'''.split()
RAW_MARK_FIELDS = '''run_id candidate_id mark_date ts_utc outcome reason input_json'''.split()


def ensure_header(path, fields):
    """Create empty books and fail loudly on schema drift, before any append."""
    if path.exists() and path.stat().st_size:
        with path.open(newline='', encoding='utf-8') as handle:
            if next(csv.reader(handle), []) != fields:
                raise ValueError(f'{path} header does not match current fields')
    else:
        with path.open('w', newline='', encoding='utf-8') as handle:
            csv.writer(handle).writerow(fields)


def validate(data, holiday_text):
    values = {}
    try:
        mark_day = date.fromisoformat(data.get('mark_date'))
        if mark_day.isoformat() != data['mark_date']:
            raise ValueError('mark_date must be YYYY-MM-DD')
    except (ValueError, TypeError):
        return values, 'missing_or_invalid_mark_date'
    try:
        timestamps = [quote_time(data.get(f'{leg}_quote_ts_utc'))
                      for leg in ('short', 'long')]
    except (ValueError, TypeError, AttributeError, OverflowError):
        return values, 'missing_or_invalid_quote_timestamp'
    if abs((timestamps[0] - timestamps[1]).total_seconds()) > QUOTE_SKEW_MAX_SECONDS:
        return values, f'quote_skew_exceeds_{QUOTE_SKEW_MAX_SECONDS}_seconds'
    if not all(in_regular_hours(ts, holiday_text) for ts in timestamps):
        return values, 'quote_outside_regular_hours'
    if any(ts.astimezone(ZoneInfo('America/New_York')).date() != mark_day for ts in timestamps):
        return values, 'quote_date_does_not_match_mark_date'
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
    return values, ''


def exit_reason(signal):
    if signal['time_stop']['is_expired']:
        return 'expired'
    if signal['time_stop']['triggered']:
        return 'time_stop'
    if signal['profit_target']['triggered']:
        return 'profit_target'
    if signal['thesis_invalidated']:
        return 'premium_stop'
    raise RuntimeError('hard_close without a recognized exit trigger')


def log_observation(raw_bytes, state_dir, holiday_text):
    data = json.loads(raw_bytes)
    if not isinstance(data, dict):
        raise ValueError('Input must be one candidate JSON object')
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marks = state_dir / 'ghost_marks.csv'
        exits = state_dir / 'ghost_exits.csv'
        raw = state_dir / 'ghost_mark_observations_raw.csv'
        for path, fields in ((marks, MARK_FIELDS), (exits, EXIT_FIELDS), (raw, RAW_MARK_FIELDS)):
            ensure_header(path, fields)
        candidate = data.get('candidate_id', '')
        entry = next((row for row in rows(state_dir / 'ghost_entries.csv')
                      if row['candidate_id'] == candidate), None)
        outcome, reason = 'rejected', ''
        result = {'candidate_id': candidate}
        if entry is None:
            reason = 'candidate_not_found_in_entries'
        elif any(row['candidate_id'] == candidate for row in rows(exits)):
            outcome, reason = 'duplicate_skipped', 'candidate_already_exited'
        elif any(row['candidate_id'] == candidate and row['mark_date'] == data.get('mark_date')
                 for row in rows(marks)):
            outcome, reason = 'duplicate_skipped', 'mark_already_recorded_for_date'
        else:
            values, reason = validate(data, holiday_text)
            if not reason:
                credit = float(entry['credit_mid'])
                if not math.isfinite(credit) or credit <= 0:
                    raise ValueError('Accepted entry has invalid credit_mid')
                dte = (date.fromisoformat(entry['resolved_expiry']) - date.fromisoformat(data['mark_date'])).days
                mid = values['short_mid'] - values['long_mid']
                natural = data['short_ask'] - data['long_bid']
                pnl = credit - mid
                # Bound on DISPLAYED-quote crossing cost, not an observed fill.
                values.update(cost_to_close_mid=mid, cost_to_close_natural=natural,
                              displayed_crossing_cost_per_leg_close=(natural - mid) / 2,
                              credit_mid=credit, unrealized_pnl_mid=pnl,
                              pct_max_profit_captured=pnl / credit, dte=dte)
                # bars=[] always takes insufficient_data: profit/time stops and
                # POSITIVE premium stop work; NEGATIVE structural stop needs
                # settled OHLCV and is unmeasured. No get_price_history grant.
                # Never copy caller overrides; real runs resolve GEX live.
                signal = compute_exit_signal_v2({
                    'ticker': entry['ticker'], 'bars': [], 'initial_credit': credit,
                    'entry_date': entry['trade_date'], 'contracts': 1,
                    'current_mark': mid, 'dte': dte,
                    'short_strike': float(entry['resolved_short_strike']),
                })
                row = {**data, **values, 'ticker': entry['ticker'],
                       'regime': signal['regime'],
                       'regime_data_available': signal['regime_detail']['data_available']}
                result.update(hard_close=signal['hard_close'], signal=signal)
                if signal['hard_close']:
                    why = exit_reason(signal)
                    row.update(exit_date=data['mark_date'], trade_date=entry['trade_date'],
                               dte_at_exit=dte, exit_reason=why, realized_pnl_mid=pnl)
                    append(exits, EXIT_FIELDS, row)
                    outcome = 'exited'
                    result['exit_reason'] = why
                else:
                    row.update(premium_stop_triggered=signal['premium_stop']['triggered'],
                               profit_target_triggered=signal['profit_target']['triggered'],
                               time_stop_triggered=signal['time_stop']['triggered'])
                    append(marks, MARK_FIELDS, row)
                    outcome = 'marked'
        append(raw, RAW_MARK_FIELDS, {**data, 'ts_utc': datetime.now(timezone.utc).isoformat(),
               'outcome': outcome, 'reason': reason, 'input_json': raw_bytes.decode('utf-8')})
        return {**result, 'outcome': outcome, 'reason': reason}


def open_candidate_ids(state_dir):
    """Return accepted entry dicts without a terminal exit, including prior marks."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exited = {row['candidate_id'] for row in rows(state_dir / 'ghost_exits.csv')}
        return [row for row in rows(state_dir / 'ghost_entries.csv')
                if row['candidate_id'] not in exited]


def self_test():
    from unittest.mock import patch
    from scripts.ghost.ghost_fill_logger import ENTRY_FIELDS

    base = dict(candidate_id='first', run_id='synthetic', mark_date='2026-09-17',
                short_bid=1.9, short_ask=2.1, long_bid=.9, long_ask=1.1,
                short_bid_size=3, short_ask_size=3, long_bid_size=2, long_ask_size=2,
                short_quote_ts_utc='2026-09-17T14:00:00Z',
                long_quote_ts_utc='2026-09-17T14:00:02Z',
                market_data_type='live', quote_ts_is_estimated=False,
                in_rth_claimed=None, underlying_spot=105, code_version_hash='test',
                git_head='test', git_dirty=False)
    failures = 0
    with tempfile.TemporaryDirectory(prefix='ghost-exit-self-test-') as temp:
        def seed(state, candidate='first', expiry='2026-10-16'):
            state.mkdir(exist_ok=True)
            append(state / 'ghost_entries.csv', ENTRY_FIELDS,
                   dict(candidate_id=candidate, ticker='TEST', trade_date='2026-09-16',
                        credit_mid=2, resolved_expiry=expiry, resolved_short_strike=100))

        def record(state, data=None):
            data = base if data is None else data
            raw_bytes = json.dumps(data, indent=2).encode()
            before = len(rows(state / 'ghost_mark_observations_raw.csv'))
            result = log_observation(raw_bytes, state, '2026-01-01')
            audit = rows(state / 'ghost_mark_observations_raw.csv')
            assert len(audit) == before + 1
            assert audit[-1]['input_json'] == raw_bytes.decode()
            assert audit[-1]['outcome'] == result['outcome']
            assert audit[-1]['reason'] == result['reason']
            if 'signal' in result:
                assert result['signal']['insufficient_data'] is True
                assert result['signal']['bars_provided'] == 0
            return result

        def healthy(state):
            result = record(state)
            assert result['outcome'] == 'marked'
            assert result['hard_close'] is False
            assert result['signal']['regime'] == 'NEGATIVE'
            assert result['signal']['regime_detail']['data_available'] is False
            assert result['signal']['regime_detail']['reason'].startswith('dix_fetcher_error:')
            assert rows(state / 'ghost_exits.csv') == []
            assert len(rows(state / 'ghost_marks.csv')) == 1
            row = rows(state / 'ghost_marks.csv')[0]
            for key, expected in dict(short_mid=2, long_mid=1, cost_to_close_mid=1,
                    cost_to_close_natural=1.2, displayed_crossing_cost_per_leg_close=.1,
                    unrealized_pnl_mid=1, pct_max_profit_captured=.5, dte=29).items():
                assert math.isclose(float(row[key]), expected, abs_tol=1e-12), key
            for key in ('premium_stop_triggered', 'profit_target_triggered', 'time_stop_triggered'):
                assert row[key] == 'False', key

        def close_case(state, expected, data=None):
            result = record(state, data)
            assert result['outcome'] == 'exited'
            assert result['hard_close'] is True
            assert result['exit_reason'] == expected
            assert rows(state / 'ghost_marks.csv') == []
            exits = rows(state / 'ghost_exits.csv')
            assert len(exits) == 1
            assert exits[0]['exit_reason'] == expected
            assert exits[0]['trade_date'] == '2026-09-16'
            assert exits[0]['exit_date'] == base['mark_date']
            mid = result['signal']['profit_target']['pct_captured']
            assert math.isclose(float(exits[0]['pct_max_profit_captured']), mid, abs_tol=1e-12)
            assert math.isclose(float(exits[0]['realized_pnl_mid']), 2 * mid, abs_tol=1e-12)
            return result

        def profit(state):
            close_case(state, 'profit_target', {**base, 'short_bid': 1.2, 'short_ask': 1.4})

        def premium(state):
            engine = compute_exit_signal_v2
            def positive(payload):
                assert 'gex_regime' not in payload
                return engine({**payload, 'gex_regime': {'regime': 'POSITIVE', 'data_available': True}})
            # Only this test injects gex_regime, directly into the engine payload.
            with patch.dict(log_observation.__globals__, compute_exit_signal_v2=positive):
                result = close_case(state, 'premium_stop', {**base, 'short_bid': 7.9, 'short_ask': 8.1})
            assert result['signal']['regime'] == 'POSITIVE'
            assert result['signal']['regime_detail']['data_available'] is True
            assert math.isclose(result['signal']['premium_stop']['current_loss'], 5, abs_tol=1e-12)
            assert math.isclose(result['signal']['premium_stop']['threshold'], 4, abs_tol=1e-12)

        def duplicate_mark(state):
            record(state)
            result = record(state)
            assert result['outcome'] == 'duplicate_skipped'
            assert result['reason'] == 'mark_already_recorded_for_date'
            assert len(rows(state / 'ghost_marks.csv')) == 1
            assert rows(state / 'ghost_exits.csv') == []

        def duplicate_exit(state):
            profit(state)
            result = record(state)
            assert result['outcome'] == 'duplicate_skipped'
            assert result['reason'] == 'candidate_already_exited'
            assert len(rows(state / 'ghost_exits.csv')) == 1
            assert rows(state / 'ghost_marks.csv') == []

        def reject(state, changes, reason):
            result = record(state, {**base, **changes})
            assert result['outcome'] == 'rejected'
            assert result['reason'] == reason
            assert rows(state / 'ghost_marks.csv') == []
            assert rows(state / 'ghost_exits.csv') == []

        def open_entries(state):
            seed(state, 'second')
            profit(state)
            remaining = open_candidate_ids(state)
            assert [row['candidate_id'] for row in remaining] == ['second']
            assert remaining[0] == rows(state / 'ghost_entries.csv')[1]

        def next_date(state):
            record(state)
            result = record(state, {**base, 'mark_date': '2026-09-18',
                            'short_quote_ts_utc': '2026-09-18T14:00:00Z',
                            'long_quote_ts_utc': '2026-09-18T14:00:02Z'})
            assert result['outcome'] == 'marked'
            assert len(rows(state / 'ghost_marks.csv')) == 2

        def mark_then_exit(state):
            record(state)
            result = record(state, {**base, 'mark_date': '2026-09-18',
                            'short_quote_ts_utc': '2026-09-18T14:00:00Z',
                            'long_quote_ts_utc': '2026-09-18T14:00:02Z',
                            'short_bid': 1.2, 'short_ask': 1.4})
            assert result['outcome'] == 'exited'
            assert len(rows(state / 'ghost_marks.csv')) == 1
            assert len(rows(state / 'ghost_exits.csv')) == 1
            assert open_candidate_ids(state) == []

        def rejected_retry(state):
            reject(state, {'short_bid': 0}, 'short_bid_missing_or_nonpositive_or_nonfinite')
            result = record(state)
            assert result['outcome'] == 'marked'
            assert len(rows(state / 'ghost_marks.csv')) == 1

        def estimated_boundary(state):
            result = record(state, {**base, 'quote_ts_is_estimated': True,
                            'market_data_type': ' REAL-TIME ',
                            'long_quote_ts_utc': f'2026-09-17T14:00:{QUOTE_SKEW_MAX_SECONDS:02d}Z'})
            assert result['outcome'] == 'marked'
            values, reason = validate(base, '2026-01-01')
            assert reason == ''
            assert math.isclose(values['short_spread_abs'], .2, abs_tol=1e-12)
            assert math.isclose(values['long_spread_abs'], .2, abs_tol=1e-12)

        def negative_loss(state):
            result = record(state, {**base, 'short_bid': 7.9, 'short_ask': 8.1,
                            'hard_close': True, 'gex_regime': {'regime': 'POSITIVE'}})
            assert result['outcome'] == 'marked'
            assert result['signal']['premium_stop']['triggered'] is True
            assert result['signal']['thesis_invalidated'] is False
            assert result['hard_close'] is False

        def header_mismatch(state):
            (state / 'ghost_marks.csv').write_text('wrong,header\n')
            try:
                record(state)
            except ValueError as error:
                assert 'header does not match' in str(error)
            else:
                raise AssertionError('schema mismatch did not raise')
            assert (state / 'ghost_marks.csv').read_text() == 'wrong,header\n'

        cases = [
            ('healthy mark arithmetic and offline regime fail-safe', healthy, '2026-10-16'),
            ('profit-target exit', profit, '2026-10-16'),
            ('time-stop exit at DTE 7', lambda s: close_case(s, 'time_stop'), '2026-09-24'),
            ('time-stop exit at DTE 0', lambda s: close_case(s, 'time_stop'), '2026-09-17'),
            ('expired underwater exit', lambda s: close_case(s, 'expired',
                {**base, 'short_bid': 3.9, 'short_ask': 4.1}), '2026-09-16'),
            ('premium-stop exit in POSITIVE regime', premium, '2026-10-16'),
            ('duplicate mark audited', duplicate_mark, '2026-10-16'),
            ('duplicate exit audited', duplicate_exit, '2026-10-16'),
            ('unknown candidate rejected', lambda s: reject(s, {'candidate_id': 'unknown'},
                'candidate_not_found_in_entries'), '2026-10-16'),
            ('missing market_data_type rejected', lambda s: reject(s, {'market_data_type': None},
                'market_data_not_live_or_missing'), '2026-10-16'),
            ('quote skew rejected', lambda s: reject(s,
                {'long_quote_ts_utc': '2026-09-17T14:00:20Z'},
                f'quote_skew_exceeds_{QUOTE_SKEW_MAX_SECONDS}_seconds'), '2026-10-16'),
            ('open candidates subtract terminal exits', open_entries, '2026-10-16'),
            ('next date permits another mark', next_date, '2026-10-16'),
            ('exit after prior mark preserves mark history', mark_then_exit, '2026-10-16'),
            ('rejected quote permits corrected retry', rejected_retry, '2026-10-16'),
            ('estimated quote at skew limit and live alias accepted', estimated_boundary, '2026-10-16'),
            ('NEGATIVE premium loss stays mark; caller overrides ignored', negative_loss, '2026-10-16'),
            ('schema mismatch fails loudly', header_mismatch, '2026-10-16'),
            ('time stop takes priority over profit target',
                lambda s: close_case(s, 'time_stop', {**base, 'short_bid': 1.2, 'short_ask': 1.4}), '2026-09-24'),
        ]
        for leg in ('short', 'long'):
            for label, changes, reason in [
                ('zero', {f'{leg}_bid': 0}, f'{leg}_bid_missing_or_nonpositive_or_nonfinite'),
                ('nonfinite', {f'{leg}_ask': float('inf')}, f'{leg}_ask_missing_or_nonpositive_or_nonfinite'),
                ('crossed', {f'{leg}_bid': 10}, f'{leg}_crossed_quotes'),
            ]:
                cases.append((f'{leg} {label} quote rejected',
                              lambda s, c=changes, r=reason: reject(s, c, r), '2026-10-16'))
        cases.extend([
            ('invalid timestamp rejected', lambda s: reject(s, {'short_quote_ts_utc': None},
                'missing_or_invalid_quote_timestamp'), '2026-10-16'),
            ('outside RTH rejected despite caller claim', lambda s: reject(s,
                {'short_quote_ts_utc': '2026-09-17T21:00:00Z',
                 'long_quote_ts_utc': '2026-09-17T21:00:02Z', 'in_rth_claimed': True},
                'quote_outside_regular_hours'), '2026-10-16'),
            ('quote date mismatch rejected', lambda s: reject(s, {'mark_date': '2026-09-18'},
                'quote_date_does_not_match_mark_date'), '2026-10-16'),
            ('invalid mark date rejected', lambda s: reject(s, {'mark_date': 'bad'},
                'missing_or_invalid_mark_date'), '2026-10-16'),
        ])
        # Exercise the REAL resolver's exception fail-safe without a network
        # attempt. A socket guard also makes accidental network use fail tests.
        with patch('scripts.compute_exit_signal_v2.dix_latest', side_effect=RuntimeError('offline self-test')), \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')) as network:
            for index, (label, check, expiry) in enumerate(cases):
                state = Path(temp) / str(index)
                seed(state, expiry=expiry)
                try:
                    check(state)
                    assert network.call_count == 0
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
    group.add_argument('--list-open', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.list_open:
        print(json.dumps(open_candidate_ids(ROOT / 'state/ghost')))
        return 0
    holidays = ROOT / 'us-market-holidays.txt'
    result = log_observation(args.input.read_bytes(), ROOT / 'state/ghost',
                             holidays.read_text() if holidays.exists() else '')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
