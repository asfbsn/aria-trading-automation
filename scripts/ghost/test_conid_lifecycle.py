#!/usr/bin/env python3
"""Automated end-to-end verification of all 8 required fixtures for contract ID persistence."""

import csv
import io
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ghost.ghost_fill_logger import (
    ENTRY_FIELDS, INPUT_FIELDS, log_observation, rows, append
)
from scripts.ghost.ghost_exit_logger import open_candidate_ids, is_ready_to_mark
from scripts.ghost.migrate_ghost_entries_conid import migrate_entries
from scripts.ghost.update_ghost_entry_conids import atomic_update_entries, apply_recovery_file


def fixture_1_missing_contract_id(temp_dir: Path):
    """Fixture 1: Missing contract ID is handled explicitly.

    Distinguishes:
    - 'not yet resolved': row exists in ledger with empty conid fields and resolution_status='exact'
    - 'resolution attempted and failed': row has resolution_status starting with 'unresolved_blocked:'
    - entry-logger rejection: missing contract ID rejected with f'{field}_missing'
    """
    base = dict.fromkeys(INPUT_FIELDS)
    base.update(
        candidate_id='cand_f1', run_id='run_f1', resolution_status='exact',
        derived_short_strike=100, resolved_short_strike=100,
        derived_long_strike=95, resolved_long_strike=95,
        derived_expiry='2026-10-16', resolved_expiry='2026-10-16',
        underlying_contract_id=123456, short_contract_id=None, long_contract_id=345678,
        short_bid=2.0, short_ask=2.2, long_bid=0.9, long_ask=1.1,
        short_bid_size=3, long_bid_size=2, market_data_type='live',
        short_quote_ts_utc='2026-09-15T14:00:00Z',
        long_quote_ts_utc='2026-09-15T14:00:02+00:00',
        short_quote_ts_is_estimated=False, long_quote_ts_is_estimated=False,
        z_ma150=1.8765, filter_tag='gex_positive_z_ma150_ge_1.5'
    )
    res = log_observation(json.dumps(base).encode(), temp_dir, '2026-01-01')
    assert res['outcome'] == 'rejected', f"Expected rejected, got {res['outcome']}"
    assert res['reason'] == 'short_contract_id_missing', f"Expected short_contract_id_missing, got {res['reason']}"

    # Verify distinction on ledger rows:
    entries_file = temp_dir / 'ghost_entries.csv'
    # 1. Not yet resolved row:
    not_yet_resolved = {k: '' for k in ENTRY_FIELDS}
    not_yet_resolved.update(candidate_id='not_yet', ticker='NYR', resolution_status='exact',
                            underlying_contract_id='', short_contract_id='', long_contract_id='',
                            credit_mid='1.0', resolved_expiry='2026-10-16', resolved_short_strike='100')
    append(entries_file, ENTRY_FIELDS, not_yet_resolved)

    # 2. Resolution failed row:
    failed_row = {k: '' for k in ENTRY_FIELDS}
    failed_row.update(candidate_id='failed', ticker='FAIL',
                      resolution_status='unresolved_blocked: conid_field_mismatch',
                      underlying_contract_id='', short_contract_id='', long_contract_id='',
                      credit_mid='1.0', resolved_expiry='2026-10-16', resolved_short_strike='100')
    append(entries_file, ENTRY_FIELDS, failed_row)

    read_rows = {r['candidate_id']: r for r in rows(entries_file)}
    # 'not yet resolved' has resolution_status == 'exact' with empty conid fields
    assert read_rows['not_yet']['resolution_status'] == 'exact'
    assert read_rows['not_yet']['short_contract_id'] == ''
    # 'resolution attempted and failed' has resolution_status starting with 'unresolved_blocked:'
    assert read_rows['failed']['resolution_status'].startswith('unresolved_blocked:')
    # Neither is ready to mark
    assert not is_ready_to_mark(read_rows['not_yet'])
    assert not is_ready_to_mark(read_rows['failed'])


def fixture_2_mismatched_invalid_contract_id(temp_dir: Path):
    """Fixture 2: 4 distinct rejections (boolean, float, nonpositive, exact-verification mismatch)."""
    base = dict.fromkeys(INPUT_FIELDS)
    base.update(
        candidate_id='cand_f2', run_id='run_f2', resolution_status='exact',
        derived_short_strike=100, resolved_short_strike=100,
        derived_long_strike=95, resolved_long_strike=95,
        derived_expiry='2026-10-16', resolved_expiry='2026-10-16',
        underlying_contract_id=123456, short_contract_id=234567, long_contract_id=345678,
        short_bid=2.0, short_ask=2.2, long_bid=0.9, long_ask=1.1,
        short_bid_size=3, long_bid_size=2, market_data_type='live',
        short_quote_ts_utc='2026-09-15T14:00:00Z',
        long_quote_ts_utc='2026-09-15T14:00:02+00:00',
        short_quote_ts_is_estimated=False, long_quote_ts_is_estimated=False,
        z_ma150=1.8765, filter_tag='gex_positive_z_ma150_ge_1.5'
    )
    def test_case(override, expected_reason):
        payload = {**base, **override}
        res = log_observation(json.dumps(payload).encode(), temp_dir, '2026-01-01')
        assert res['outcome'] == 'rejected', f"Expected rejected for {override}, got {res['outcome']}"
        assert res['reason'] == expected_reason, f"Expected reason {expected_reason}, got {res['reason']}"

    # 1. boolean rejected (Python isinstance(True, bool))
    test_case({'candidate_id': 'f2_bool', 'short_contract_id': True}, 'short_contract_id_is_boolean')
    test_case({'candidate_id': 'f2_bool_u', 'underlying_contract_id': False}, 'underlying_contract_id_is_boolean')

    # 2. float / fractional rejected
    test_case({'candidate_id': 'f2_float', 'short_contract_id': 12345.67}, 'short_contract_id_float_or_fractional')

    # 3. non-positive (<=0) rejected
    test_case({'candidate_id': 'f2_zero', 'short_contract_id': 0}, 'short_contract_id_nonpositive')
    test_case({'candidate_id': 'f2_neg', 'short_contract_id': -100}, 'short_contract_id_nonpositive')

    # 4. exact-verification rule mismatch rejected
    test_case({'candidate_id': 'f2_mismatch', 'resolution_status': 'conid_field_mismatch'}, 'conid_field_mismatch')
    test_case({'candidate_id': 'f2_mismatch_flag', 'conid_field_mismatch': True}, 'conid_field_mismatch')


def fixture_3_csv_integer_round_trip(temp_dir: Path):
    """Fixture 3: CSV integer round-trip preserves true positive integer."""
    base = dict.fromkeys(INPUT_FIELDS)
    base.update(
        candidate_id='cand_f3', run_id='run_f3', resolution_status='exact',
        derived_short_strike=100, resolved_short_strike=100,
        derived_long_strike=95, resolved_long_strike=95,
        derived_expiry='2026-10-16', resolved_expiry='2026-10-16',
        underlying_contract_id=987654321, short_contract_id=876543210, long_contract_id=765432109,
        short_bid=2.0, short_ask=2.2, long_bid=0.9, long_ask=1.1,
        short_bid_size=3, long_bid_size=2, market_data_type='live',
        short_quote_ts_utc='2026-09-15T14:00:00Z',
        long_quote_ts_utc='2026-09-15T14:00:02+00:00',
        short_quote_ts_is_estimated=False, long_quote_ts_is_estimated=False,
        z_ma150=1.8765, filter_tag='gex_positive_z_ma150_ge_1.5'
    )
    res = log_observation(json.dumps(base).encode(), temp_dir, '2026-01-01')
    assert res['outcome'] == 'accepted', f"Failed to accept valid payload: {res}"

    entry_rows = rows(temp_dir / 'ghost_entries.csv')
    matched = [r for r in entry_rows if r['candidate_id'] == 'cand_f3']
    assert len(matched) == 1
    row = matched[0]

    # Verify on-disk string formatting is an exact integer string
    assert row['underlying_contract_id'] == '987654321'
    assert row['short_contract_id'] == '876543210'
    assert row['long_contract_id'] == '765432109'

    # Verify not corrupted into float string ("987654321.0") or boolean ("True")
    assert not row['underlying_contract_id'].endswith('.0')
    assert row['underlying_contract_id'] != 'True'

    # Verify round-trips to exact Python int
    assert int(row['underlying_contract_id']) == 987654321
    assert int(row['short_contract_id']) == 876543210
    assert int(row['long_contract_id']) == 765432109


def fixture_4_migration_idempotency(temp_dir: Path):
    """Fixture 4: Run migration script twice; second run is clean no-op."""
    old_fields = [f for f in ENTRY_FIELDS if f not in ('underlying_contract_id', 'short_contract_id', 'long_contract_id')]
    entries_path = temp_dir / 'ghost_entries.csv'

    rows_data = [
        {'candidate_id': f'idempotent_{i}', 'ticker': sym, 'resolution_status': 'exact', 'credit_mid': '0.75'}
        for i, sym in enumerate(['XOM', 'JPM', 'WMT'])
    ]
    for r in rows_data:
        for f in old_fields:
            if f not in r:
                r[f] = 'x'

    with entries_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=old_fields)
        writer.writeheader()
        for r in rows_data:
            writer.writerow(r)

    # First run: migrates
    ret1 = migrate_entries(entries_path, as_of_date='2026-09-19')
    assert ret1 == 0
    bak_files_1 = list(temp_dir.glob('ghost_entries_stale_header_*_pre_conid*.csv.bak'))
    assert len(bak_files_1) == 1
    bak_file = bak_files_1[0]
    bak_mtime = bak_file.stat().st_mtime_ns
    entries_bytes_post_1 = entries_path.read_bytes()

    # Second run: clean no-op
    ret2 = migrate_entries(entries_path, as_of_date='2026-09-19')
    assert ret2 == 0
    bak_files_2 = list(temp_dir.glob('ghost_entries_stale_header_*_pre_conid*.csv.bak'))
    assert len(bak_files_2) == 1, "Double-archiving occurred on second migration run"
    assert bak_files_2[0].stat().st_mtime_ns == bak_mtime, "Backup modified on second run"
    assert entries_path.read_bytes() == entries_bytes_post_1, "File modified on second run"
    assert len(rows(entries_path)) == 3, "Rows duplicated or lost"


def fixture_5_migration_preserves_every_existing_field(temp_dir: Path):
    """Fixture 5: Synthetic pre-migration row: every original field is byte-identical."""
    old_fields = [f for f in ENTRY_FIELDS if f not in ('underlying_contract_id', 'short_contract_id', 'long_contract_id')]
    entries_path = temp_dir / 'ghost_entries.csv'

    original_row = {
        'candidate_id': 'cand_preserve_test',
        'run_id': 'run_alpha_123',
        'trade_date': '2026-09-16',
        'signal_bar_date': '2026-09-15',
        'mode': 'vrp_only',
        'ticker': 'CVX',
        'resolution_status': 'exact',
        'filter_tag': 'legacy_pre_z_ma150_migration',
        'z_ma150': '1.87654321',
        'credit_mid': '0.7150000000000001',
        'resolved_short_strike': '190.0',
        'resolved_long_strike': '180.0',
        'resolved_expiry': '2026-10-16',
        'underlying_spot': '212.22',
        'short_bid': '0.66',
        'short_ask': '0.77',
        'short_quote_ts_utc': '2026-09-16T18:50:53Z',
        'long_quote_ts_utc': '2026-09-16T18:50:57Z',
        'quote_skew_seconds': '4.0',
        'code_version_hash': 'aa17bd0e38f232f358e5020a148cddd82039f1d49d7c61e5b16a1ffce23c737e',
        'git_head': '971f6084b1ffec68e41166c75c5e498cd0929521',
        'git_dirty': 'True',
    }
    for f in old_fields:
        if f not in original_row:
            original_row[f] = f'dummy_{f}'

    with entries_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow(original_row)

    migrate_entries(entries_path, as_of_date='2026-09-19')

    with entries_path.open('r', newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        migrated_rows = list(reader)

    assert len(migrated_rows) == 1
    post_row = migrated_rows[0]

    # Every single pre-existing field is byte-identical
    for f in old_fields:
        assert post_row[f] == original_row[f], f"Mismatch on field {f}: {post_row[f]} != {original_row[f]}"

    # Three new fields are present and empty
    assert post_row['underlying_contract_id'] == ''
    assert post_row['short_contract_id'] == ''
    assert post_row['long_contract_id'] == ''


def fixture_6_entry_to_list_open_to_mark_handoff(temp_dir: Path):
    """Fixture 6: entry -> list-open -> mark handoff: contract IDs flow through without extra calls."""
    entries_path = temp_dir / 'ghost_entries.csv'
    entry_row = {f: '' for f in ENTRY_FIELDS}
    entry_row.update(
        candidate_id='handoff_cand_1',
        ticker='NVDA',
        trade_date='2026-09-18',
        resolution_status='exact',
        underlying_contract_id='481516',
        short_contract_id='234210',
        long_contract_id='234211',
        resolved_short_strike='120.0',
        resolved_long_strike='115.0',
        resolved_expiry='2026-10-16',
        credit_mid='1.50'
    )
    append(entries_path, ENTRY_FIELDS, entry_row)

    open_positions = open_candidate_ids(temp_dir)
    pos = next((p for p in open_positions if p['candidate_id'] == 'handoff_cand_1'), None)
    assert pos is not None, "Position not returned by open_candidate_ids"

    # Verify ID fields exist and are positive ints
    for k in ('underlying_contract_id', 'short_contract_id', 'long_contract_id'):
        assert k in pos, f"Missing key {k} in open position dict"
        assert int(pos[k]) > 0, f"Field {k} not positive integer"

    # Mark prompt logic can consume directly:
    u_id = int(pos['underlying_contract_id'])
    s_id = int(pos['short_contract_id'])
    l_id = int(pos['long_contract_id'])
    assert u_id == 481516
    assert s_id == 234210
    assert l_id == 234211
    assert is_ready_to_mark(pos) is True


def fixture_7_atomic_and_targeted_row_update(temp_dir: Path):
    """Fixture 7: Update row 2 of 3 rows: rows 1 and 3 are byte-identical; interruption survives."""
    entries_path = temp_dir / 'ghost_entries.csv'
    rows_3 = []
    for i in range(3):
        r = {f: f'val_{f}_{i}' for f in ENTRY_FIELDS}
        r.update(candidate_id=f'cand_{i}', resolution_status='exact',
                 underlying_contract_id='', short_contract_id='', long_contract_id='')
        rows_3.append(r)

    with entries_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=ENTRY_FIELDS)
        writer.writeheader()
        for r in rows_3:
            writer.writerow(r)

    # Initial snapshot of rows 0 and 2
    with entries_path.open('r', newline='', encoding='utf-8') as handle:
        orig = list(csv.DictReader(handle))
    orig_0 = dict(orig[0])
    orig_2 = dict(orig[2])

    # Update row 1
    atomic_update_entries(entries_path, {
        'cand_1': {
            'underlying_contract_id': 101,
            'short_contract_id': 202,
            'long_contract_id': 303,
            'resolution_status': 'exact'
        }
    })

    with entries_path.open('r', newline='', encoding='utf-8') as handle:
        updated = list(csv.DictReader(handle))

    assert updated[1]['underlying_contract_id'] == '101'
    assert updated[1]['short_contract_id'] == '202'
    assert updated[1]['long_contract_id'] == '303'

    # Rows 0 and 2 are untouched (byte-identical)
    for f in ENTRY_FIELDS:
        assert updated[0][f] == orig_0[f], f"Row 0 field {f} altered"
        assert updated[2][f] == orig_2[f], f"Row 2 field {f} altered"

    # Interruption test: simulated crash during atomic replace
    content_before_crash = entries_path.read_bytes()
    with patch('os.replace', side_effect=OSError("Simulated system crash during atomic rename")):
        try:
            atomic_update_entries(entries_path, {
                'cand_0': {
                    'underlying_contract_id': 999,
                    'short_contract_id': 999,
                    'long_contract_id': 999,
                    'resolution_status': 'exact'
                }
            })
            assert False, "Should have raised OSError"
        except OSError:
            pass

    assert entries_path.read_bytes() == content_before_crash, "File modified despite interruption"
    assert len(list(temp_dir.glob('*.tmp*'))) == 0, "Temporary file not cleaned up"


def fixture_8_unresolved_row_handling(temp_dir: Path):
    """Fixture 8: Unresolved row marked blocked, excluded from ready-to-mark, counted in summary."""
    entries_path = temp_dir / 'ghost_entries.csv'
    row = {f: '' for f in ENTRY_FIELDS}
    row.update(candidate_id='unresolved_cand', ticker='AMZN', resolution_status='exact',
               underlying_contract_id='', short_contract_id='', long_contract_id='')
    append(entries_path, ENTRY_FIELDS, row)

    # Resolution fails (e.g. conid_field_mismatch)
    recovery_data = [
        {
            'candidate_id': 'unresolved_cand',
            'resolution_status': 'conid_field_mismatch',
            'reason': 'conid_field_mismatch'
        }
    ]
    scratch_file = temp_dir / 'recovery.json'
    scratch_file.write_text(json.dumps(recovery_data), encoding='utf-8')

    res = apply_recovery_file(entries_path, scratch_file)
    assert res['attempted'] == 1
    assert res['resolved'] == 0
    assert res['unresolved_blocked'] == 1

    post_rows = rows(entries_path)
    cand_row = next(r for r in post_rows if r['candidate_id'] == 'unresolved_cand')
    assert cand_row['resolution_status'] == 'unresolved_blocked: conid_field_mismatch'
    assert cand_row['underlying_contract_id'] == ''
    assert cand_row['short_contract_id'] == ''
    assert cand_row['long_contract_id'] == ''

    # Excluded from ready-to-mark set
    assert is_ready_to_mark(cand_row) is False
    ready_candidates = open_candidate_ids(temp_dir, ready_to_mark_only=True)
    assert not any(c['candidate_id'] == 'unresolved_cand' for c in ready_candidates)


def run_all():
    fixtures = [
        ('Fixture 1: Missing contract ID handled explicitly (distinguishes unresolved states)', fixture_1_missing_contract_id),
        ('Fixture 2: Mismatched/invalid contract ID rejected (4 distinct assertions)', fixture_2_mismatched_invalid_contract_id),
        ('Fixture 3: CSV integer round-trip preserves true positive integer', fixture_3_csv_integer_round_trip),
        ('Fixture 4: Migration idempotency (clean no-op on repeat)', fixture_4_migration_idempotency),
        ('Fixture 5: Migration preserves every existing field byte-identical', fixture_5_migration_preserves_every_existing_field),
        ('Fixture 6: entry -> list-open -> mark handoff flows without resolution calls', fixture_6_entry_to_list_open_to_mark_handoff),
        ('Fixture 7: Recovery script row-update is atomic and targeted with interruption protection', fixture_7_atomic_and_targeted_row_update),
        ('Fixture 8: Unresolved-row handling (marked blocked, excluded from ready-to-mark, counted in summary)', fixture_8_unresolved_row_handling),
    ]
    failures = 0
    for name, func in fixtures:
        with tempfile.TemporaryDirectory(prefix='ghost-conid-fixture-') as temp:
            temp_dir = Path(temp)
            try:
                func(temp_dir)
                print(f"PASS: {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL: {name}: {e}")
                import traceback
                traceback.print_exc()

    if failures:
        print(f"\nTotal Failures: {failures}")
        return 1
    print("\nALL 8 FIXTURES PASSED VERBATIM.")
    return 0


if __name__ == '__main__':
    raise SystemExit(run_all())
