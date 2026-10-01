#!/usr/bin/env python3
"""Ghost paper marks and exits, decided only by compute_exit_signal_v2.

Run with scripts/backtest/.venv/bin/python3, like ghost_prescreen_v2.py.
Deliberate deviation from the stdlib-only ghost_fill_logger: the exit engine
imports signal_core and live GEX resolution uses dix_fetcher_v2, requiring
pandas. Real runs omit gex_regime and let the engine fetch its own live regime.
This logger never fetches IBKR quotes/history or places orders.

Passing bars=[] selects the engine's insufficient_data=True branch. Profit
target, time stop and POSITIVE-regime premium stop need no bars. When
state/ghost/settled_bars.json (written by fetch_settled_closes.py) holds
settled daily bars for the expected last completed session, they are passed
with exclude_last_bar=True and the MA150/RSI/candle checks are persisted on
every row (informational). In NEGATIVE regime the validated settled_close
record is authoritative; only when it is missing/stale/invalid does the engine
fall back to the bars-derived short-strike check (structural_source=
bars_fallback) -- the same rule the live exit guard applies to IBKR bars.
Such a row reads structural_unknown=True with thesis_invalidated_by=
structural_breach: structural_source is what disambiguates it.

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
from scripts.compute_exit_signal_v2 import compute_exit_signal_v2, get_expected_last_completed_session

# Structural-evidence fields (Astra, 2026-09-19): without these, a `marked`
# row with no breach is indistinguishable from one where the structural check
# was never measured at all -- "verified intact" and "UNKNOWN" collapse into
# the same on-disk row. Persisted on every mark/exit row, not just computed.
STRUCTURAL_FIELDS = '''thesis_invalidated_by structural_evaluated structural_breached
structural_unknown structural_reason settled_close_used settled_close_session_date
settled_close_source settled_close_retrieved_ts_utc
structural_source bars_status bars_provided bar_close bar_rsi20 bar_ma150
check_short_strike_breached check_broke_ma150_support check_bearish_candle
check_rsi_overbought check_volume_confirmed_breakdown'''.split()
MARK_FIELDS = '''run_id mark_date candidate_id ticker dte short_mid long_mid
cost_to_close_mid cost_to_close_natural displayed_crossing_cost_per_leg_close
unrealized_pnl_mid pct_max_profit_captured regime regime_data_available
premium_stop_triggered profit_target_triggered time_stop_triggered''' \
    .split() + STRUCTURAL_FIELDS + '''code_version_hash git_head git_dirty'''.split()
EXIT_FIELDS = '''run_id exit_date candidate_id ticker trade_date dte_at_exit
exit_reason credit_mid cost_to_close_mid cost_to_close_natural
displayed_crossing_cost_per_leg_close realized_pnl_mid pct_max_profit_captured
regime regime_data_available''' \
    .split() + STRUCTURAL_FIELDS + '''code_version_hash git_head git_dirty'''.split()
# effective_settled_close_json: the settled_close dict actually used for this
# observation, post-fallback-file-substitution -- distinct from input_json,
# which stays exactly the caller's original bytes. The mark prompt never puts
# settled_close in its own JSON (it comes from state/ghost/settled_closes.json
# inside log_observation()), so without this field the raw audit trail never
# captures what evidence a verdict was actually based on (Astra, 2026-09-19).
RAW_MARK_FIELDS = '''run_id candidate_id mark_date ts_utc outcome reason
effective_settled_close_json input_json'''.split()


def ensure_header(path, fields):
    """Create empty books and fail loudly on schema drift, before any append."""
    if path.exists() and path.stat().st_size:
        with path.open(newline='', encoding='utf-8') as handle:
            if next(csv.reader(handle), []) != fields:
                raise ValueError(f'{path} header does not match current fields')
    else:
        with path.open('w', newline='', encoding='utf-8') as handle:
            csv.writer(handle).writerow(fields)


BAR_KEYS = ('date', 'open', 'high', 'low', 'close', 'volume')


def usable_bars(record, mark_date, ticker=None):
    """Settled bars for the engine, or [] with a reason: ok | missing | stale | malformed.

    stale = the bars' session is not the expected last completed session for mark_date (e.g. yesterday's fetch).
    Bars are INFORMATIONAL evidence (MA150/RSI/candle checks) -- see structural_source for how they can matter.
    Numeric validation happens HERE so a bad producer file can never raise out of log_observation before the
    raw audit row is written."""
    if record is None:
        return [], 'missing'
    if not isinstance(record, dict) or not isinstance(record.get('bars'), list) or not record['bars']:
        return [], 'malformed'
    if ticker is not None and str(record.get('ticker', '')).strip().upper() != str(ticker).strip().upper():
        return [], 'malformed'
    try:
        expected = get_expected_last_completed_session(mark_date)
    except Exception:  # noqa: BLE001
        return [], 'malformed'
    if record.get('session_date') != expected:
        return [], 'stale'
    bars = record['bars']
    for bar in bars:
        if not isinstance(bar, dict) or any(key not in bar for key in BAR_KEYS):
            return [], 'malformed'
        try:
            o, h, l, c, v = (float(bar[k]) for k in ('open', 'high', 'low', 'close', 'volume'))
        except (TypeError, ValueError):
            return [], 'malformed'
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not math.isfinite(v) or v < 0:
            return [], 'malformed'
    if str(bars[-1]['date'])[:10] != expected:
        return [], 'malformed'
    return bars, 'ok'


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
        by = signal.get('thesis_invalidated_by')
        if by in ('structural_breach', 'premium_stop'):
            return by
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
        effective_settled_close_json = ''
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

                # Distinct timestamps:
                # 1. Settled close timestamp & session date: evidence from the last
                #    completed trading session (yesterday's close, e.g. via fetch_settled_closes.py),
                #    used for the lagged structural short-strike breach check.
                # 2. Quote timestamps (short_quote_ts_utc, long_quote_ts_utc): today's
                #    live hypothetical-exit quote capture during RTH via get_price_snapshot,
                #    used for mark-to-market valuation (current_mark, pct_max_profit_captured).
                # These timestamps serve two distinct purposes and are never conflated into
                # a single 'as of' timestamp.
                settled_close = data.get('settled_close')
                if settled_close is None:
                    sc_file = state_dir / 'settled_closes.json'
                    if sc_file.exists():
                        try:
                            sc_map = json.loads(sc_file.read_text(encoding='utf-8'))
                            if isinstance(sc_map, dict):
                                settled_close = sc_map.get(entry['ticker'])
                        except Exception:
                            settled_close = None

                settled_bars_rec = data.get('settled_bars')
                if settled_bars_rec is None:
                    sb_file = state_dir / 'settled_bars.json'
                    if sb_file.exists():
                        try:
                            sb_map = json.loads(sb_file.read_text(encoding='utf-8'))
                            if isinstance(sb_map, dict):
                                settled_bars_rec = sb_map.get(entry['ticker'])
                        except Exception:
                            settled_bars_rec = None
                bars, bars_status = usable_bars(settled_bars_rec, data['mark_date'], entry['ticker'])

                signal_payload = {
                    'ticker': entry['ticker'], 'bars': bars, 'initial_credit': credit,
                    'entry_date': entry['trade_date'], 'mark_date': data['mark_date'],
                    'contracts': 1, 'current_mark': mid, 'dte': dte,
                    'short_strike': float(entry['resolved_short_strike']),
                    'settled_close': settled_close,
                }
                # exclude_last_bar=True is belt-and-braces: the engine only drops the last bar when it is dated
                # today and before 16:00 ET, which settled-only bars never are.
                signal = compute_exit_signal_v2(signal_payload, exclude_last_bar=True)
                effective_settled_close_json = json.dumps(settled_close) if settled_close is not None else ''
                # Persist the structural evidence itself, not just its effect on
                # hard_close/exit_reason -- otherwise a `marked` row with no
                # breach can't later be told apart from one where the structural
                # check was never measured at all (Astra, 2026-09-19).
                struct_eval = signal.get('structural_evaluation') or {}
                structural_row = {
                    'thesis_invalidated_by': signal.get('thesis_invalidated_by'),
                    'structural_evaluated': struct_eval.get('evaluated'),
                    'structural_breached': struct_eval.get('breached'),
                    'structural_unknown': struct_eval.get('unknown'),
                    'structural_reason': struct_eval.get('reason'),
                    'settled_close_used': struct_eval.get('close'),
                    'settled_close_session_date': struct_eval.get('session_date'),
                    'settled_close_source': settled_close.get('source') if isinstance(settled_close, dict) else None,
                    'settled_close_retrieved_ts_utc': settled_close.get('retrieved_ts_utc') if isinstance(settled_close, dict) else None,
                }
                # Where the structural verdict came from. `bars_fallback` = NEGATIVE regime, no valid settled_close,
                # decided by the bar-derived short-strike check; such an exit shows thesis_invalidated_by=
                # structural_breach together with structural_evaluated=False (settled_close not evaluated).
                insufficient = bool(signal.get('insufficient_data'))
                checks = {} if insufficient else (signal.get('checks') or {})
                if struct_eval.get('evaluated'):
                    structural_source = 'settled_close'
                elif signal.get('regime') == 'NEGATIVE' and not insufficient:
                    structural_source = 'bars_fallback'
                else:
                    structural_source = 'none'
                structural_row.update(
                    structural_source=structural_source, bars_status=bars_status,
                    bars_provided=signal.get('bars_provided', len(bars)),
                    bar_close='' if insufficient else signal.get('close'),
                    bar_rsi20='' if insufficient else signal.get('rsi20'),
                    bar_ma150='' if insufficient else signal.get('ma150'),
                    check_short_strike_breached=checks.get('short_strike_breached', ''),
                    check_broke_ma150_support=checks.get('broke_ma150_support', ''),
                    check_bearish_candle=checks.get('bearish_candle', ''),
                    check_rsi_overbought=checks.get('rsi_overbought', ''),
                    check_volume_confirmed_breakdown=checks.get('volume_confirmed_breakdown', ''))
                row = {**data, **values, **structural_row, 'ticker': entry['ticker'],
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
               'outcome': outcome, 'reason': reason,
               'effective_settled_close_json': effective_settled_close_json,
               'input_json': raw_bytes.decode('utf-8')})
        return {**result, 'outcome': outcome, 'reason': reason}


def is_ready_to_mark(position):
    """A position is ready to mark only if resolution_status is exact and contract IDs are positive ints."""
    if position.get('resolution_status') != 'exact':
        return False
    for field in ('underlying_contract_id', 'short_contract_id', 'long_contract_id'):
        val = position.get(field)
        if val is None or str(val).strip() == '':
            return False
        if isinstance(val, bool):
            return False
        try:
            int_val = int(val)
            if int_val <= 0:
                return False
        except (ValueError, TypeError):
            return False
    return True


def open_candidate_ids(state_dir, ready_to_mark_only=False):
    """Return accepted entry dicts without a terminal exit, including prior marks."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exited = {row['candidate_id'] for row in rows(state_dir / 'ghost_exits.csv')}
        candidates = [row for row in rows(state_dir / 'ghost_entries.csv')
                      if row['candidate_id'] not in exited]
        if ready_to_mark_only:
            return [c for c in candidates if is_ready_to_mark(c)]
        return candidates


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

        def record(state, data=None, bars=False):
            data = base if data is None else data
            raw_bytes = json.dumps(data, indent=2).encode()
            before = len(rows(state / 'ghost_mark_observations_raw.csv'))
            result = log_observation(raw_bytes, state, '2026-01-01')
            audit = rows(state / 'ghost_mark_observations_raw.csv')
            assert len(audit) == before + 1
            assert audit[-1]['input_json'] == raw_bytes.decode()
            assert audit[-1]['outcome'] == result['outcome']
            assert audit[-1]['reason'] == result['reason']
            if 'signal' in result and not bars:
                assert result['signal']['insufficient_data'] is True
                assert result['signal']['bars_provided'] == 0
            return result

        def write_bars(state, session='2026-09-16', last_close=100.0, n=160, ticker='TEST', last_date=None):
            from datetime import timedelta
            end = date.fromisoformat(session)
            out = []
            for i in range(n):
                d = (end - timedelta(days=n - 1 - i)).isoformat()
                c = 100.0 if i < n - 1 else last_close
                out.append({'date': d, 'open': c, 'high': c + 1, 'low': c - 1, 'close': c, 'volume': 1000})
            if last_date:
                out[-1]['date'] = last_date
            (state / 'settled_bars.json').write_text(json.dumps(
                {ticker: {'ticker': ticker, 'session_date': session, 'bars': out,
                          'source': 'yfinance', 'retrieved_ts_utc': '2026-09-17T12:00:00Z'}}))

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
            def positive(payload, **kwargs):
                assert 'gex_regime' not in payload
                return engine({**payload, 'gex_regime': {'regime': 'POSITIVE', 'data_available': True}}, **kwargs)
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

        def fixture_1_negative_structural_breach(state):
            sc = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-16',
                  'source': 'yfinance', 'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            result = record(state, {**base, 'settled_close': sc})
            assert result['outcome'] == 'exited'
            assert result['hard_close'] is True
            assert result['exit_reason'] == 'structural_breach'
            assert result['signal']['structural_evaluation']['evaluated'] is True
            assert result['signal']['structural_evaluation']['breached'] is True
            assert result['signal']['structural_evaluation']['unknown'] is False
            assert result['signal']['thesis_invalidated_by'] == 'structural_breach'
            exits = rows(state / 'ghost_exits.csv')
            assert len(exits) == 1
            assert exits[0]['exit_reason'] == 'structural_breach'
            # Persisted row, not just the in-memory signal -- this is the actual
            # gap Astra flagged: a computed-but-not-persisted verdict is
            # unrecoverable the moment the process exits (Astra, 2026-09-19).
            assert exits[0]['thesis_invalidated_by'] == 'structural_breach'
            assert exits[0]['structural_evaluated'] == 'True'
            assert exits[0]['structural_breached'] == 'True'
            assert exits[0]['structural_unknown'] == 'False'
            assert math.isclose(float(exits[0]['settled_close_used']), 95.0, abs_tol=1e-9)
            assert exits[0]['settled_close_session_date'] == '2026-09-16'
            assert exits[0]['settled_close_source'] == 'yfinance'
            assert exits[0]['settled_close_retrieved_ts_utc'] == '2026-09-17T12:00:00Z'
            raw_obs = rows(state / 'ghost_mark_observations_raw.csv')
            assert raw_obs[-1]['effective_settled_close_json'] == json.dumps(sc)

        def fixture_2_gex_unavailable_fallback(state):
            sc = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-16',
                  'source': 'yfinance', 'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            result = record(state, {**base, 'settled_close': sc})
            assert result['signal']['regime'] == 'NEGATIVE'
            assert result['signal']['regime_detail']['data_available'] is False
            assert result['outcome'] == 'exited'
            assert result['exit_reason'] == 'structural_breach'

        def fixture_3_stale_settled_close_unknown(state):
            sc = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-10',
                  'source': 'yfinance', 'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            result = record(state, {**base, 'settled_close': sc})
            assert result['outcome'] == 'marked'
            assert result['hard_close'] is False
            assert result['signal']['structural_evaluation']['evaluated'] is False
            assert result['signal']['structural_evaluation']['unknown'] is True
            assert result['signal']['structural_evaluation']['breached'] is None
            assert 'stale_settled_close' in result['signal']['structural_evaluation']['reason']

        def fixture_4_missing_settled_close_unknown(state):
            result = record(state, {**base, 'settled_close': None})
            assert result['outcome'] == 'marked'
            assert result['hard_close'] is False
            assert result['signal']['structural_evaluation']['unknown'] is True
            assert result['signal']['structural_evaluation']['breached'] is None
            assert result['signal']['structural_evaluation']['evaluated'] is False
            # Persisted, not just computed: the marked row must record UNKNOWN
            # explicitly, never leave the structural columns blank in a way
            # that could later be misread as "verified intact."
            marks = rows(state / 'ghost_marks.csv')
            assert marks[-1]['structural_unknown'] == 'True'
            assert marks[-1]['structural_evaluated'] == 'False'
            assert marks[-1]['structural_breached'] == ''
            assert marks[-1]['structural_reason'] == 'missing_settled_close'
            assert marks[-1]['thesis_invalidated_by'] == ''
            pt_res = record(state, {**base, 'mark_date': '2026-09-18',
                                    'short_quote_ts_utc': '2026-09-18T14:00:00Z',
                                    'long_quote_ts_utc': '2026-09-18T14:00:02Z',
                                    'short_bid': 1.2, 'short_ask': 1.4, 'settled_close': None})
            assert pt_res['outcome'] == 'exited'
            assert pt_res['exit_reason'] == 'profit_target'

        def fixture_5_synthetic_forward_fill_rejected(state):
            sc = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-18',
                  'source': 'yfinance', 'synthetic': True}
            result = record(state, {**base, 'settled_close': sc})
            assert result['outcome'] == 'marked'
            assert result['hard_close'] is False
            assert result['signal']['structural_evaluation']['unknown'] is True
            assert result['signal']['structural_evaluation']['breached'] is None
            assert result['signal']['structural_evaluation']['evaluated'] is False

        def fixture_6_byte_identical_exit_guard_v2(state):
            # Frozen expected output, not `git show HEAD:...` (Astra, 2026-09-19:
            # once this file is committed, HEAD becomes the NEW version, and a
            # HEAD-relative comparison silently degrades into comparing the code
            # against itself -- testing nothing while still printing PASS).
            # Regenerate this literal only via a deliberate, reviewed update, the
            # same discipline as any other frozen fixture in this codebase.
            bars = []
            for i in range(160):
                c = 100.0 if i < 159 else 95.0
                bars.append({
                    'date': f'2026-01-{(i%28)+1:02d}',
                    'open': c + 0.5, 'high': c + 1.0, 'low': c - 1.0, 'close': c, 'volume': 1000
                })
            eg_payload = {
                'ticker': 'AAPL', 'short_strike': 100.0, 'initial_credit': 1.00,
                'entry_date': '2026-05-01', 'contracts': 1, 'unrealized_pnl': -250.0,
                'current_mark': None,
                'gex_regime': {
                    'as_of_date': '2026-06-10', 'gex_date': '2026-06-09', 'gex': 5.5e9,
                    'percentile_rank': 0.45, 'regime': 'POSITIVE', 'lookback_used': 252,
                    'data_available': True, 'staleness_days': 1
                },
                'bars': bars
            }
            expected = {
                "checks": {
                    "bearish_candle": False, "broke_ma150_support": True,
                    "candle_pattern": "none", "premium_stop_triggered": True,
                    "premium_stop_unknown": False, "profit_target_triggered": False,
                    "rsi_overbought": False, "short_strike_breached": True,
                    "short_strike_unknown": False, "time_stop_triggered": False,
                    "volume_confirmed_breakdown": False,
                },
                "close": 95.0, "hard_close": True, "insufficient_data": False,
                "ma150": 99.93,
                "note": "proxy signal v2 (regime-aware) — draft for human review",
                "premium_stop": {"current_loss": 2.5, "evaluated": True,
                                  "threshold": 2.0, "triggered": True},
                "profit_target": {"evaluated": True, "pct_captured": -2.5,
                                   "threshold": 0.8, "triggered": False},
                "regime": "POSITIVE",
                "regime_detail": {
                    "as_of_date": "2026-06-10", "data_available": True,
                    "gex": 5500000000.0, "gex_date": "2026-06-09",
                    "lookback_used": 252, "percentile_rank": 0.45,
                    "regime": "POSITIVE", "staleness_days": 1,
                },
                "rsi20": 0.0, "settled": False, "short_strike": 100.0,
                "stop_mode": "PREMIUM_MULTIPLE", "thesis_invalidated": True,
                "ticker": "AAPL",
                "time_stop": {"dte": None, "evaluated": False,
                              "reason": "no_dte_data", "triggered": False},
            }
            post_res = compute_exit_signal_v2(eg_payload)
            post_json = json.dumps(post_res, sort_keys=True)
            expected_json = json.dumps(expected, sort_keys=True)
            assert post_json == expected_json, (
                f'MISMATCH against frozen baseline.\nGot: {post_json}\nExpected: {expected_json}'
            )
            # No settled_close key in eg_payload -- the additive-only keys must
            # be entirely absent, not present-with-None.
            assert 'thesis_invalidated_by' not in post_res
            assert 'structural_evaluation' not in post_res

        def fixture_7_retry_idempotency_settled_close(state):
            sc = {'ticker': 'TEST', 'close': 105.0, 'session_date': '2026-09-16', 'source': 'yfinance',
                  'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            data = {**base, 'settled_close': sc}
            res1 = record(state, data)
            assert res1['outcome'] == 'marked'
            assert len(rows(state / 'ghost_marks.csv')) == 1

            res2 = record(state, data)
            assert res2['outcome'] == 'duplicate_skipped'
            assert res2['reason'] == 'mark_already_recorded_for_date'
            assert len(rows(state / 'ghost_marks.csv')) == 1
            assert len(rows(state / 'ghost_exits.csv')) == 0

        def fixture_8_full_reconciliation(state):
            from collections import Counter
            import re
            seed(state, candidate='cand_mark', expiry='2026-10-16')
            seed(state, candidate='cand_breach', expiry='2026-10-16')
            seed(state, candidate='cand_reject', expiry='2026-10-16')

            expected_ids = {'cand_mark', 'cand_breach', 'cand_reject'}

            sc_healthy = {'ticker': 'TEST', 'close': 105.0, 'session_date': '2026-09-16', 'source': 'yfinance',
                          'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            res_m = record(state, {**base, 'candidate_id': 'cand_mark', 'settled_close': sc_healthy})
            assert res_m['outcome'] == 'marked'

            sc_breach = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-16', 'source': 'yfinance',
                         'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            res_b = record(state, {**base, 'candidate_id': 'cand_breach', 'settled_close': sc_breach})
            assert res_b['outcome'] == 'exited'
            assert res_b['exit_reason'] == 'structural_breach'

            res_r = record(state, {**base, 'candidate_id': 'cand_reject', 'short_bid': 0, 'settled_close': sc_healthy})
            assert res_r['outcome'] == 'rejected'

            raw_obs = rows(state / 'ghost_mark_observations_raw.csv')
            observed_ids = {r['candidate_id'] for r in raw_obs}
            assert observed_ids == expected_ids

            terminal = {r['candidate_id']: r for r in raw_obs}
            counts = Counter(r['outcome'] for r in terminal.values())
            assert counts['marked'] == 1
            assert counts['exited'] == 1
            assert counts['rejected'] == 1

            summary_text = f"PROCESSED: {len(terminal)} MARKED: {counts['marked']} EXITED: {counts['exited']}"
            summary_match = re.fullmatch(r'PROCESSED: (\d+) MARKED: (\d+) EXITED: (\d+)', summary_text)
            assert summary_match and tuple(map(int, summary_match.groups())) == (3, 1, 1)

        def fixture_precedence_negative_structural_over_premium(state):
            sc = {'ticker': 'TEST', 'close': 95.0, 'session_date': '2026-09-16', 'source': 'yfinance',
                  'retrieved_ts_utc': '2026-09-17T12:00:00Z'}
            data = {**base, 'short_bid': 7.9, 'short_ask': 8.1, 'settled_close': sc}
            result = record(state, data)
            assert result['outcome'] == 'exited'
            assert result['exit_reason'] == 'structural_breach'
            assert result['signal']['premium_stop']['triggered'] is True
            assert result['signal']['structural_evaluation']['breached'] is True
            assert result['signal']['thesis_invalidated_by'] == 'structural_breach'

        SC_ABOVE = {'ticker': 'TEST', 'close': 105.0, 'session_date': '2026-09-16', 'source': 'yfinance',
                    'retrieved_ts_utc': '2026-09-17T12:00:00Z'}

        def bars_positive_regime_informational(state):
            write_bars(state, last_close=105.0)
            engine = compute_exit_signal_v2
            def positive(payload, **kwargs):
                return engine({**payload, 'gex_regime': {'regime': 'POSITIVE', 'data_available': True}}, **kwargs)
            with patch.dict(log_observation.__globals__, compute_exit_signal_v2=positive):
                result = record(state, bars=True)
            assert result['outcome'] == 'marked' and result['hard_close'] is False
            assert result['signal']['insufficient_data'] is False
            row = rows(state / 'ghost_marks.csv')[0]
            assert row['bars_status'] == 'ok' and row['bars_provided'] == '160'
            assert row['structural_source'] == 'none'
            assert row['check_broke_ma150_support'] == 'False' and row['check_bearish_candle'] == 'False'
            assert math.isclose(float(row['bar_close']), 105.0) and row['bar_ma150'] != '' and row['bar_rsi20'] != ''

        def bars_negative_fallback_breach(state):
            write_bars(state, last_close=95.0)           # settled close below the 100 short strike, no settled_close record
            result = record(state, bars=True)
            assert result['outcome'] == 'exited' and result['exit_reason'] == 'structural_breach'
            row = rows(state / 'ghost_exits.csv')[0]
            assert row['thesis_invalidated_by'] == 'structural_breach'
            assert row['structural_evaluated'] == 'False' and row['structural_source'] == 'bars_fallback'
            assert row['bars_status'] == 'ok' and row['check_short_strike_breached'] == 'True'

        def bars_do_not_override_settled_close(state):
            write_bars(state, last_close=95.0)
            result = record(state, {**base, 'settled_close': SC_ABOVE}, bars=True)
            assert result['outcome'] == 'marked' and result['hard_close'] is False
            row = rows(state / 'ghost_marks.csv')[0]
            assert row['structural_source'] == 'settled_close' and row['structural_evaluated'] == 'True'
            assert row['structural_breached'] == 'False'

        def bars_stale_ignored(state):
            write_bars(state, session='2026-09-10')
            result = record(state)                       # bars=False: engine must still see NO bars
            assert result['outcome'] == 'marked' and result['signal']['insufficient_data'] is True
            row = rows(state / 'ghost_marks.csv')[0]
            assert row['bars_status'] == 'stale' and row['bars_provided'] == '0' and row['structural_source'] == 'none'
            assert row['check_broke_ma150_support'] == '' and row['bar_close'] == ''

        def bars_malformed_ignored(state):
            write_bars(state, last_date='2026-09-12')    # last bar date != claimed session
            result = record(state)
            assert result['signal']['insufficient_data'] is True
            assert rows(state / 'ghost_marks.csv')[0]['bars_status'] == 'malformed'

        def bad_bars_case(label, mutate):
            def run(state):
                write_bars(state)
                data = json.loads((state / 'settled_bars.json').read_text())
                mutate(data['TEST'])
                (state / 'settled_bars.json').write_text(json.dumps(data))
                result = record(state)
                assert result['outcome'] == 'marked', (label, result)
                assert result['signal']['insufficient_data'] is True, label
                last = rows(state / 'ghost_marks.csv')[-1]
                assert last['bars_status'] == 'malformed' and last['bars_provided'] == '0', (label, last['bars_status'])
                assert len(rows(state / 'ghost_mark_observations_raw.csv')) == 1, label   # raw audit row written
            return run

        def bars_missing_status(state):
            record(state)
            row = rows(state / 'ghost_marks.csv')[0]
            assert row['bars_status'] == 'missing' and row['bars_provided'] == '0' and row['structural_source'] == 'none'

        def bars_short_series_insufficient(state):
            write_bars(state, n=100)                     # valid session but < MIN_BARS -> engine says insufficient
            result = record(state, bars=True)
            assert result['signal']['insufficient_data'] is True and result['signal']['bars_provided'] == 100
            assert rows(state / 'ghost_marks.csv')[0]['bars_status'] == 'ok'

        def expected_session_time_aware(state):
            from datetime import datetime as _dt
            ny = ZoneInfo('America/New_York')
            fri_after = _dt(2026, 9, 18, 16, 30, tzinfo=ny)
            fri_mid = _dt(2026, 9, 18, 15, 45, tzinfo=ny)
            assert get_expected_last_completed_session('2026-09-18', now_ny=fri_after) == '2026-09-18'
            assert get_expected_last_completed_session('2026-09-18', now_ny=fri_mid) == '2026-09-17'
            assert get_expected_last_completed_session('2026-09-17', now_ny=fri_after) == '2026-09-16'   # not today: unchanged
            sat_after = _dt(2026, 9, 19, 17, 0, tzinfo=ny)
            assert get_expected_last_completed_session('2026-09-19', now_ny=sat_after) == '2026-09-18'   # weekend rolls back

        cases = [
            ('healthy mark arithmetic and offline regime fail-safe', healthy, '2026-10-16'),
            ('bars: POSITIVE regime persists informational checks, no behavior change', bars_positive_regime_informational, '2026-10-16'),
            ('bars: NEGATIVE regime without settled_close exits via bars_fallback with explicit source', bars_negative_fallback_breach, '2026-10-16'),
            ('bars: valid settled_close stays authoritative over bars', bars_do_not_override_settled_close, '2026-10-16'),
            ('bars: stale bars ignored (engine sees none)', bars_stale_ignored, '2026-10-16'),
            ('bars: malformed bars ignored', bars_malformed_ignored, '2026-10-16'),
            ('bars: non-numeric close recorded malformed, never raises', bad_bars_case('close', lambda r: r['bars'][5].__setitem__('close', 'abc')), '2026-10-16'),
            ('bars: volume None recorded malformed, never raises', bad_bars_case('volume', lambda r: r['bars'][5].__setitem__('volume', None)), '2026-10-16'),
            ('bars: negative price recorded malformed', bad_bars_case('neg', lambda r: r['bars'][5].__setitem__('low', -1.0)), '2026-10-16'),
            ('bars: inner ticker mismatch recorded malformed', bad_bars_case('ticker', lambda r: r.__setitem__('ticker', 'OTHER')), '2026-10-16'),
            ('bars: missing file recorded as missing', bars_missing_status, '2026-10-16'),
            ('bars: fewer than MIN_BARS stays insufficient_data', bars_short_series_insufficient, '2026-10-16'),
            ('engine expected session is time-aware after 16:00 ET', expected_session_time_aware, '2026-10-16'),
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
            ('fixture 1: NEGATIVE regime structural breach via settled_close',
                fixture_1_negative_structural_breach, '2026-10-16'),
            ('fixture 2: GEX-unavailable fallback defaults NEGATIVE and evaluates settled_close',
                fixture_2_gex_unavailable_fallback, '2026-10-16'),
            ('fixture 3: Stale settled_close yields explicitly UNKNOWN structural result',
                fixture_3_stale_settled_close_unknown, '2026-10-16'),
            ('fixture 4: Missing settled_close yields UNKNOWN structural while profit/time stops work',
                fixture_4_missing_settled_close_unknown, '2026-10-16'),
            ('fixture 5: Synthetic/forward-fill dummy close rejected by validation',
                fixture_5_synthetic_forward_fill_rejected, '2026-10-16'),
            ('fixture 6: Additive byte-identical output test for exit-guard_v2 payload without settled_close',
                fixture_6_byte_identical_exit_guard_v2, '2026-10-16'),
            ('fixture 7: Retry idempotency for settled-close-driven marking path',
                fixture_7_retry_idempotency_settled_close, '2026-10-16'),
            ('fixture 8: Full reconciliation covering settled-close structural path and rejections',
                fixture_8_full_reconciliation, '2026-10-16'),
            ('fixture precedence: NEGATIVE regime structural breach takes precedence over premium stop',
                fixture_precedence_negative_structural_over_premium, '2026-10-16'),
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
            ('fixture 6: entry -> list-open -> mark handoff (contract IDs flow through)',
                lambda s: test_fixture6_conids_flow_through_list_open(s), '2026-10-16'),
            ('fixture 8: unresolved row excluded from ready-to-mark set',
                lambda s: test_fixture8_unresolved_row_handling(s), '2026-10-16'),
        ])
        def test_fixture6_conids_flow_through_list_open(state):
            append(state / 'ghost_entries.csv', ENTRY_FIELDS,
                   dict(candidate_id='conid_pos', ticker='TEST', trade_date='2026-09-16',
                        credit_mid=2, resolved_expiry='2026-10-16', resolved_short_strike=100,
                        resolution_status='exact', underlying_contract_id=12345,
                        short_contract_id=67890, long_contract_id=67891))
            open_list = open_candidate_ids(state)
            pos = next((p for p in open_list if p['candidate_id'] == 'conid_pos'), None)
            assert pos is not None, 'Candidate missing from open list'
            assert pos['underlying_contract_id'] == '12345'
            assert pos['short_contract_id'] == '67890'
            assert pos['long_contract_id'] == '67891'
            assert int(pos['underlying_contract_id']) == 12345
            assert int(pos['short_contract_id']) == 67890
            assert int(pos['long_contract_id']) == 67891
            assert is_ready_to_mark(pos) is True

        def test_fixture8_unresolved_row_handling(state):
            append(state / 'ghost_entries.csv', ENTRY_FIELDS,
                   dict(candidate_id='blocked_pos', ticker='BLOCKED', trade_date='2026-09-16',
                        credit_mid=2, resolved_expiry='2026-10-16', resolved_short_strike=100,
                        resolution_status='unresolved_blocked: conid_field_mismatch',
                        underlying_contract_id='', short_contract_id='', long_contract_id=''))
            all_open = open_candidate_ids(state)
            blocked_pos = next((p for p in all_open if p['candidate_id'] == 'blocked_pos'), None)
            assert blocked_pos is not None
            assert is_ready_to_mark(blocked_pos) is False
            ready_set = open_candidate_ids(state, ready_to_mark_only=True)
            assert not any(p['candidate_id'] == 'blocked_pos' for p in ready_set)

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
    parser.add_argument('--ready-to-mark', action='store_true',
                        help='Filter --list-open to positions ready to mark (with valid contract IDs)')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.list_open:
        print(json.dumps(open_candidate_ids(ROOT / 'state/ghost', ready_to_mark_only=args.ready_to_mark)))
        return 0
    holidays = ROOT / 'us-market-holidays.txt'
    result = log_observation(args.input.read_bytes(), ROOT / 'state/ghost',
                             holidays.read_text() if holidays.exists() else '')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
