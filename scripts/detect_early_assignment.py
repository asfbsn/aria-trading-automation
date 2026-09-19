#!/usr/bin/env python3
"""Detect early assignment on a short put leg by diffing IBKR account
positions against the last known snapshot.

get_account_positions has no dedicated assignment field (confirmed
empirically 2026-09-17 against a real live account) -- an assignment must be
inferred: a short PUT option position present in the LAST snapshot but
absent (or zeroed) now, together with a new or increased STK position in
the SAME underlying (assignment on a short put forces buying shares at the
strike, i.e. a new/larger long stock position).

Deliberately narrow: a short put vanishing WITHOUT a matching stock-position
increase is NOT flagged here (that's an ordinary manual close, or a
transient data timing issue) -- it stays covered by
prompts/bull-put-spread-exit.md's existing "unpaired legs -- verify
manually" fallback, which has no cross-run memory of its own. This script
adds exactly the missing cross-run memory for the one pattern that actually
indicates assignment, not a catch-all for every way a leg can disappear.
False positives here would dilute trust in an urgent alert; that's a worse
failure mode than a silent gap already covered by the existing fallback.

Symbol is parsed from `contract_description`'s leading token (the schema has
no separate `symbol` field on this account) -- e.g. "KLAC Oct09'26 165 PUT
@AMEX" -> "KLAC". This is a real, uncontrolled convention observed on one
live account only; if IBKR ever returns a different description shape, this
heuristic could misparse -- it fails toward NOT alerting (a missing/odd
description sorts oddly and simply won't match), never toward inventing a
false assignment.

Exit code is always 0 (advisory, matches this repo's exit-guard philosophy).
The result is communicated via a stdout marker line whose presence the
calling prompt copies verbatim, never via exit code.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / 'state' / 'exit_guard_last_positions.json'


def symbol_of(position):
    description = position.get('contract_description') or ''
    return description.split()[0] if description.strip() else ''


def is_short_put(position):
    description = (position.get('contract_description') or '').upper()
    quantity = position.get('position')
    return (
        position.get('asset_class') == 'OPT'
        and 'PUT' in description
        and isinstance(quantity, (int, float))
        and quantity < 0
    )


def stock_quantity(positions, symbol):
    total = 0.0
    for position in positions:
        if position.get('asset_class') == 'STK' and symbol_of(position) == symbol:
            quantity = position.get('position')
            if isinstance(quantity, (int, float)):
                total += quantity
    return total


def _magnitude(position):
    quantity = position.get('position') if position is not None else None
    return abs(quantity) if isinstance(quantity, (int, float)) else 0.0


def detect(fresh_positions, baseline_positions):
    """Returns a list of EARLY_ASSIGNMENT_DETECTED marker strings (possibly empty).

    Aggregated PER SYMBOL, not per contract_id: multiple short puts on the
    same underlying share one stock-position delta. Evaluating each
    contract_id independently against the FULL stock increase let one real
    assignment justify an alert for every OTHER short put that merely closed
    manually on the same symbol -- double/triple-counted from a single
    100-share move (CodeRabbit finding, 2026-09-17). Also now catches
    PARTIAL assignment (e.g. -2 contracts -> -1): comparing only "did the
    contract vanish entirely" missed a still-present, partially-reduced
    position completely (same finding) -- this compares baseline vs fresh
    QUANTITY per contract, not just presence/absence.

    Exact per-contract attribution (which specific short put, at which
    strike, was the one actually assigned) is NOT determinable from account
    position deltas alone when multiple legs changed in the same window --
    this reports how many contracts on the symbol are LIKELY assigned
    (bounded by both the quantity that disappeared and the shares that
    appeared) and lists every contract that changed, for a human to
    reconcile against IBKR's actual assignment notice.
    """
    fresh_by_id = {p['contract_id']: p for p in fresh_positions if 'contract_id' in p}
    baseline_shorts_by_symbol = {}
    for position in baseline_positions:
        if not is_short_put(position):
            continue
        symbol = symbol_of(position)
        if not symbol:
            continue
        baseline_shorts_by_symbol.setdefault(symbol, []).append(position)

    alerts = []
    for symbol, shorts in baseline_shorts_by_symbol.items():
        removed_qty = 0.0
        changed = []
        for position in shorts:
            contract_id = position.get('contract_id')
            baseline_qty = abs(position.get('position', 0))
            fresh_match = fresh_by_id.get(contract_id)
            fresh_qty = _magnitude(fresh_match)
            this_removed = max(0.0, baseline_qty - fresh_qty)
            if this_removed > 0:
                removed_qty += this_removed
                changed.append((contract_id, position.get('position'), fresh_qty))
        if removed_qty <= 0:
            continue
        old_stock_qty = stock_quantity(baseline_positions, symbol)
        new_stock_qty = stock_quantity(fresh_positions, symbol)
        stock_increase = max(0.0, new_stock_qty - old_stock_qty)
        if stock_increase <= 0:
            continue
        likely_assigned = min(removed_qty, stock_increase / 100.0)
        if likely_assigned <= 0:
            continue
        detail = '; '.join(
            f"contract_id={cid} was position={was_pos}, now magnitude={now_qty:g}"
            for cid, was_pos, now_qty in changed
        )
        alerts.append(
            f"EARLY_ASSIGNMENT_DETECTED: {symbol} -- ~{likely_assigned:g} contract(s) likely "
            f"assigned (of {removed_qty:g} total short-put quantity removed across "
            f"{len(changed)} contract(s): {detail}); STK position changed from "
            f"{old_stock_qty:g} to {new_stock_qty:g} shares. Exact per-contract attribution "
            f"is not determinable from position deltas alone when multiple legs changed -- "
            f"verify manually."
        )
    return alerts


def load_positions(path):
    with Path(path).open('r', encoding='utf-8') as handle:
        data = json.load(handle)
    positions = data.get('positions') if isinstance(data, dict) else data
    if not isinstance(positions, list):
        raise ValueError(f'{path}: expected a positions list (or {{"positions": [...]}})')
    return positions


def load_baseline(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    return load_positions(path)


def save_baseline(path, positions):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump({'positions': positions}, handle, indent=2)
    tmp.replace(path)


def run(positions_path, baseline_path):
    fresh = load_positions(positions_path)
    baseline = load_baseline(baseline_path)
    alerts = detect(fresh, baseline)
    # An empty fresh payload (IBKR outage, auth hiccup, a malformed capture)
    # would otherwise overwrite the baseline with [] unconditionally --
    # detect() correctly finds nothing to alert on (no short put vanished
    # relative to an empty comparison), but a REAL assignment happening
    # between this run and the next would then be compared against that
    # now-empty baseline and never caught either, since there'd be no prior
    # short put on record to notice vanishing. Defeats the script's one job
    # (CodeRabbit finding, 2026-09-17). Keep the previous baseline whenever
    # fresh is empty and there was a real baseline to lose; an empty fresh
    # payload against an already-empty baseline is a genuine no-op, not a
    # loss.
    if fresh or not baseline:
        save_baseline(baseline_path, fresh)
    else:
        print('WARN: empty positions payload -- keeping previous baseline, not overwriting it',
              file=sys.stderr)
    return alerts


def self_test():
    import tempfile

    failures = 0

    def short_put(contract_id, qty, symbol='KLAC', strike=170):
        return {'contract_id': contract_id, 'position': qty, 'asset_class': 'OPT',
                'contract_description': f"{symbol} Oct09'26 {strike} PUT @AMEX"}

    def long_put(contract_id, qty, symbol='KLAC', strike=165):
        return {'contract_id': contract_id, 'position': qty, 'asset_class': 'OPT',
                'contract_description': f"{symbol} Oct09'26 {strike} PUT @AMEX"}

    def stock(qty, symbol='KLAC'):
        return {'contract_id': 999, 'position': qty, 'asset_class': 'STK',
                'contract_description': f'{symbol}'}

    def check(label, fn):
        nonlocal failures
        try:
            fn()
            print(f'PASS: {label}')
        except Exception as error:
            failures += 1
            print(f'FAIL: {label}: {error}')

    def first_run_no_baseline():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            fresh = [short_put(1, -1), long_put(2, 1)]
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert alerts == [], alerts
            assert json.loads(base.read_text())['positions'] == fresh

    def assignment_detected():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            baseline = [short_put(1, -1), long_put(2, 1)]
            base.write_text(json.dumps({'positions': baseline}))
            fresh = [long_put(2, 1), stock(100)]
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert len(alerts) == 1, alerts
            assert 'KLAC' in alerts[0] and 'contract_id=1' in alerts[0], alerts[0]
            assert json.loads(base.read_text())['positions'] == fresh

    def manual_close_not_flagged():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            baseline = [short_put(1, -1), long_put(2, 1)]
            base.write_text(json.dumps({'positions': baseline}))
            fresh = [long_put(2, 1)]  # short put closed manually, no new stock
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert alerts == [], alerts

    def no_change_no_alert():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            positions = [short_put(1, -1), long_put(2, 1)]
            base.write_text(json.dumps({'positions': positions}))
            pos_path.write_text(json.dumps({'positions': positions}))
            alerts = run(pos_path, base)
            assert alerts == []

    def zeroed_not_absent_still_detected():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            baseline = [short_put(1, -1)]
            base.write_text(json.dumps({'positions': baseline}))
            fresh = [short_put(1, 0), stock(100)]  # contract still listed, qty zeroed
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert len(alerts) == 1, alerts

    def two_short_puts_same_symbol_one_stock_increase_not_double_counted():
        # contract_id 1 (strike 170) vanishes AND contract_id 3 (strike 160)
        # vanishes too, but stock only increased by 100 shares -- ONE of them
        # was actually assigned, the other closed manually (no matching
        # shares for it). Must emit exactly ONE alert for the symbol, not two
        # -- independently checking each contract_id against the full 100-
        # share increase would previously have fired for both.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            baseline = [short_put(1, -1, strike=170), short_put(3, -1, strike=160)]
            base.write_text(json.dumps({'positions': baseline}))
            fresh = [stock(100)]  # both option legs gone, only one contract's worth of shares
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert len(alerts) == 1, alerts
            assert 'contract_id=1' in alerts[0] and 'contract_id=3' in alerts[0], alerts[0]
            assert '~1' in alerts[0], alerts[0]  # likely_assigned capped at 1, not 2

    def partial_assignment_detected():
        # 2 contracts at baseline, 1 remains (still nonzero -> "still open"
        # under the old vanished-vs-present check, which silently missed
        # this entirely). 100 shares appeared -- exactly one contract's
        # worth -- consistent with a partial assignment of 1 of 2.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            baseline = [short_put(1, -2)]
            base.write_text(json.dumps({'positions': baseline}))
            fresh = [short_put(1, -1), stock(100)]
            pos_path.write_text(json.dumps({'positions': fresh}))
            alerts = run(pos_path, base)
            assert len(alerts) == 1, alerts
            assert '~1' in alerts[0], alerts[0]

    def baseline_updates_every_run():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            first = [short_put(1, -1)]
            pos_path.write_text(json.dumps({'positions': first}))
            run(pos_path, base)
            second = [short_put(1, -1), long_put(2, 1)]
            pos_path.write_text(json.dumps({'positions': second}))
            run(pos_path, base)
            assert json.loads(base.read_text())['positions'] == second

    def empty_fresh_payload_preserves_baseline():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            real_baseline = [short_put(1, -1), long_put(2, 1)]
            base.write_text(json.dumps({'positions': real_baseline}))
            pos_path.write_text(json.dumps({'positions': []}))  # e.g. an IBKR outage
            alerts = run(pos_path, base)
            assert alerts == [], alerts  # nothing to compare against an empty fetch
            assert json.loads(base.read_text())['positions'] == real_baseline, (
                'baseline must survive an empty fresh payload -- otherwise a real '
                'assignment before the NEXT run would be invisible (no prior short '
                'put on record to notice vanishing)'
            )

    def empty_fresh_against_empty_baseline_is_a_real_noop():
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'baseline.json'
            pos_path = Path(temp) / 'positions.json'
            pos_path.write_text(json.dumps({'positions': []}))
            alerts = run(pos_path, base)
            assert alerts == []
            assert json.loads(base.read_text())['positions'] == []

    check('first run with no baseline seeds it, no alert', first_run_no_baseline)
    check('vanished short put + new stock triggers assignment alert', assignment_detected)
    check('vanished short put with no stock change is not flagged', manual_close_not_flagged)
    check('unchanged positions produce no alert', no_change_no_alert)
    check('zeroed (not absent) short put still detected', zeroed_not_absent_still_detected)
    check('two short puts, one stock increase -- not double-counted',
          two_short_puts_same_symbol_one_stock_increase_not_double_counted)
    check('partial assignment (quantity reduced, not vanished) detected', partial_assignment_detected)
    check('baseline file updates on every run', baseline_updates_every_run)
    check('empty fresh payload preserves the previous baseline', empty_fresh_payload_preserves_baseline)
    check('empty fresh against an already-empty baseline is a real no-op', empty_fresh_against_empty_baseline_is_a_real_noop)

    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--positions', type=Path)
    group.add_argument('--self-test', action='store_true')
    parser.add_argument('--baseline', type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    if args.self_test:
        failures = self_test()
        return 1 if failures else 0
    for alert in run(args.positions, args.baseline):
        print(alert)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
