#!/usr/bin/env python3
"""Read-only summary of Ghost resolution and displayed quote costs."""

import argparse
import csv
import json
import math
import statistics
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

DEFAULT_RAW = Path('state/ghost/ghost_observations_raw.csv')


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def monthly(value):
    try:
        day = date.fromisoformat(value)
        return day.weekday() == 4 and 15 <= day.day <= 21
    except (TypeError, ValueError):
        return None


def quote_metrics(row):
    try:
        payload = json.loads(row.get('input_json') or '{}')
        if not isinstance(payload, dict):
            return None
    except (TypeError, ValueError):
        return None
    fields = {key: number(payload.get(key)) for key in (
        'short_bid', 'short_ask', 'long_bid', 'long_ask',
        'resolved_short_strike', 'resolved_long_strike')}
    if any(value is None for value in fields.values()):
        return None
    sb, sa, lb, la = (fields[key] for key in ('short_bid', 'short_ask', 'long_bid', 'long_ask'))
    width = fields['resolved_short_strike'] - fields['resolved_long_strike']
    credit = (sb + sa) / 2 - (lb + la) / 2
    if min(sb, sa, lb, la) <= 0 or sa < sb or la < lb or width <= 0 or credit <= 0:
        return None
    cost = credit - (sb - la)
    max_loss = width - credit
    return dict(credit=credit, width=width, cost=cost, cost_credit=cost / credit,
                cost_loss=cost / max_loss if max_loss > 0 else None,
                rr=max_loss / credit, short_oi=number(payload.get('short_open_interest')),
                long_oi=number(payload.get('long_open_interest')))


def read_raw(path, since=None):
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if since and (row.get('trade_date') or '') < since:
                continue
            yield row


def report(path, since=None):
    rows = list(read_raw(path, since))
    first = {}
    for row in rows:
        cid = row.get('candidate_id')
        if cid and cid not in first and row.get('outcome') != 'duplicate_skipped':
            first[cid] = row
    lines = [f'Rows: {len(rows)}  Distinct candidates: {len(first)}', 'Resolution outcomes (first attempt):']
    resolution = defaultdict(lambda: [0, 0])
    for row in first.values():
        kind = monthly(row.get('derived_expiry'))
        group = (row.get('filter_tag') or '(blank)',
                 'monthly' if kind is True else 'weekly' if kind is False else 'unknown')
        resolution[group][row.get('resolution_status') == 'exact'] += 1
    for (tag, kind), counts in sorted(resolution.items()):
        lines.append(f'  {tag} / {kind}: exact={counts[1]} not_exact={counts[0]}')

    grouped = defaultdict(list)
    low_cost = []
    for row in rows:
        if row.get('outcome') == 'duplicate_skipped':
            continue
        metrics = quote_metrics(row)
        if metrics is None:
            continue
        reason = row.get('reason') or ''
        group = 'accepted' if row.get('outcome') == 'accepted' else reason if reason.startswith('live_gate_') else 'others'
        grouped[group].append(metrics)
        if reason.startswith('live_gate_') and metrics['cost_credit'] <= .25:
            low_cost.append((metrics, row))
    lines.append('Quote costs (usable quotes, positive mid credit):')
    for group, items in sorted(grouped.items()):
        def med(key):
            values = [item[key] for item in items if item[key] is not None]
            return f'{statistics.median(values):.4f}' if values else 'n/a'
        band = sum(1 for item in items if 1.5 <= item['rr'] <= 2.5)
        lines.append(f'  {group}: n={len(items)} median credit={med("credit")} width={med("width")} '
                     f'crossing_cost={med("cost")} cost/credit={med("cost_credit")} '
                     f'cost/max_loss={med("cost_loss")} R/R={med("rr")} R/R_band={band}')
    band = sum(1 for item, _ in low_cost if 1.5 <= item['rr'] <= 2.5)
    lines.append(f'Liquidity rejects with cost/credit <= 25%: {len(low_cost)}  R/R_band={band}')
    for item, row in sorted(low_cost, key=lambda pair: pair[0]['cost_credit'])[:8]:
        oi = f'{item["short_oi"]:g}/{item["long_oi"]:g}' if item['short_oi'] is not None and item['long_oi'] is not None else 'n/a'
        lines.append(f'  {row.get("trade_date") or "?"} {row.get("ticker") or "?"} '
                     f'credit/width={item["credit"]:.4f}/{item["width"]:.4f} '
                     f'cost/credit={item["cost_credit"]:.4f} R/R={item["rr"]:.4f} '
                     f'OI={oi} reason={row.get("reason") or "?"}')
    paired = [row for row in first.values() if (row.get('old_rule_would_match') or '').lower() in ('true', 'false')]
    old = sum(row['old_rule_would_match'].lower() == 'true' for row in paired)
    new = sum(row.get('resolution_status') == 'exact' for row in paired)
    lines.append(f'Paired new cohort: n={len(paired)} old_rule_match={old} new_rule_match={new}')
    listed = [row.get('derived_expiry_listed', '').lower() for row in paired]
    lines.append(f'  derived expiry listed: yes={listed.count("true")} no={listed.count("false")} unknown={len(listed) - listed.count("true") - listed.count("false")}'
                 ' (listed=no => the old exact-Friday rule certainly could not resolve; old_rule_match=False alone only means a different date was chosen)')
    return '\n'.join(lines)


def self_test():
    with tempfile.TemporaryDirectory(prefix='ghost-cost-report-') as temp:
        path = Path(temp) / 'raw.csv'
        fields = ['candidate_id', 'trade_date', 'ticker', 'filter_tag', 'derived_expiry',
                  'resolution_status', 'outcome', 'reason', 'old_rule_would_match', 'input_json']
        payload = dict(short_bid=2.0, short_ask=2.2, long_bid=.9, long_ask=1.1,
                       resolved_short_strike=100, resolved_long_strike=95,
                       short_open_interest=50, long_open_interest=500)
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(candidate_id='a', trade_date='2026-09-15', ticker='TEST',
                                 filter_tag='listedexp', derived_expiry='2026-10-16',
                                 resolution_status='exact', outcome='rejected',
                                 reason='live_gate_open_interest_below_100',
                                 old_rule_would_match='False', input_json=json.dumps(payload)))
            writer.writerow(dict(candidate_id='a', outcome='duplicate_skipped'))
            writer.writerow(dict(candidate_id='b', trade_date='2026-09-15', ticker='TEST',
                                 filter_tag='listedexp', derived_expiry='2026-10-09',
                                 resolution_status='no_exact_match', outcome='rejected',
                                 reason='resolution_not_exact', input_json='{}'))
        item = quote_metrics({'input_json': json.dumps(payload)})
        assert math.isclose(item['credit'], 1.1)
        assert math.isclose(item['cost'], .2)
        assert math.isclose(item['cost_credit'], .2 / 1.1)
        assert math.isclose(item['cost_loss'], .2 / 3.9)
        assert math.isclose(item['rr'], 3.9 / 1.1)
        output = report(path)
        assert 'Distinct candidates: 2' in output
        assert 'monthly: exact=1 not_exact=0' in output
        assert 'weekly: exact=0 not_exact=1' in output
        assert 'Liquidity rejects with cost/credit <= 25%: 1' in output
        assert 'old_rule_match=0 new_rule_match=1' in output
        assert 'Distinct candidates: 0' in report(path, '2026-09-16')
    print('PASS: ghost_cost_report self-test')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, default=DEFAULT_RAW)
    parser.add_argument('--since')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if args.since:
            date.fromisoformat(args.since)
        print(report(args.raw, args.since))


if __name__ == '__main__':
    main()
