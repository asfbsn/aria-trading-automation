#!/usr/bin/env python3
"""Idempotent, backed-up schema migration for ghost_entries.csv contract ID persistence.

Archives the old-schema file to ghost_entries_stale_header_<date>_pre_conid.csv.bak,
writes a fresh file with ENTRY_FIELDS, and carries forward every existing row with
underlying_contract_id, short_contract_id, and long_contract_id present but genuinely
empty, awaiting one-time audited broker resolution.

Running a second time detects the current header and cleanly no-ops without double-
archiving or row duplication.
"""

import argparse
import csv
import fcntl
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ghost.ghost_fill_logger import ENTRY_FIELDS


def find_backup_path(entries_path: Path, as_of_date: str = None) -> Path:
    if as_of_date is None:
        as_of_date = date.today().isoformat()
    parent = entries_path.parent
    base_name = f"ghost_entries_stale_header_{as_of_date}_pre_conid.csv.bak"
    bak_path = parent / base_name
    if not bak_path.exists():
        return bak_path
    counter = 1
    while True:
        bak_path = parent / f"ghost_entries_stale_header_{as_of_date}_pre_conid_{counter}.csv.bak"
        if not bak_path.exists():
            return bak_path
        counter += 1


def migrate_entries(entries_path: Path, as_of_date: str = None) -> int:
    entries_path = Path(entries_path).resolve()
    if not entries_path.exists():
        print(f"[MIGRATE] {entries_path} does not exist. Nothing to migrate.")
        return 0

    parent = entries_path.parent
    lock_path = parent / '.logger.lock'
    parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        with entries_path.open('r', newline='', encoding='utf-8') as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, [])

        if existing_header == ENTRY_FIELDS:
            print(f"[MIGRATE] Migration already applied: {entries_path} has current schema. Clean no-op.")
            return 0

        # Safety: check if an incompatible partial header is present
        conid_fields = {'underlying_contract_id', 'short_contract_id', 'long_contract_id'}
        found_conids = conid_fields.intersection(existing_header)
        if found_conids and existing_header != ENTRY_FIELDS:
            raise ValueError(
                f"[MIGRATE] Incompatible intermediate schema in {entries_path}: contains {sorted(found_conids)} "
                f"but does not match full ENTRY_FIELDS. Manual intervention required."
            )

        # Archive old file
        bak_path = find_backup_path(entries_path, as_of_date=as_of_date)
        entries_path.rename(bak_path)
        print(f"[MIGRATE] Archived old-schema file -> {bak_path}")

        # Read old rows
        with bak_path.open('r', newline='', encoding='utf-8') as handle:
            old_reader = csv.DictReader(handle)
            old_rows = list(old_reader)

        # Write fresh file to temp file
        tmp_path = entries_path.with_suffix(entries_path.suffix + f".tmp{os.getpid()}")
        with tmp_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=ENTRY_FIELDS)
            writer.writeheader()
            for row in old_rows:
                migrated_row = dict(row)
                migrated_row['underlying_contract_id'] = ''
                migrated_row['short_contract_id'] = ''
                migrated_row['long_contract_id'] = ''
                writer.writerow(migrated_row)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, entries_path)
        print(f"[MIGRATE] Migrated {len(old_rows)} rows forward to new schema at {entries_path}.")
        return 0


def self_test():
    old_fields = [f for f in ENTRY_FIELDS if f not in ('underlying_contract_id', 'short_contract_id', 'long_contract_id')]
    failures = 0

    with tempfile.TemporaryDirectory(prefix='ghost-migrate-test-') as temp:
        temp_dir = Path(temp)
        entries_file = temp_dir / 'ghost_entries.csv'

        # Seed synthetic old-schema file with 3 rows
        synthetic_rows = [
            {
                'candidate_id': f'cand_{i}',
                'run_id': f'run_{i}',
                'trade_date': '2026-09-16',
                'signal_bar_date': '2026-09-15',
                'mode': 'vrp_only',
                'ticker': sym,
                'resolution_status': 'exact',
                'filter_tag': 'legacy_pre_z_ma150_migration',
                'z_ma150': '1.5',
                'credit_mid': '0.50',
                'resolved_short_strike': '100.0',
                'resolved_long_strike': '95.0',
                'resolved_expiry': '2026-10-16',
            }
            for i, sym in enumerate(['CVX', 'KO', 'MS'])
        ]
        # Pad all old_fields
        for r in synthetic_rows:
            for k in old_fields:
                if k not in r:
                    r[k] = f'val_{k}'

        def write_pre_migration_file():
            with entries_file.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=old_fields)
                writer.writeheader()
                for r in synthetic_rows:
                    writer.writerow(r)

        write_pre_migration_file()

        # Fixture 4 & 5
        try:
            # 1. Run migration first time
            res1 = migrate_entries(entries_file, as_of_date='2026-09-19')
            assert res1 == 0, 'Migration 1 returned non-zero'

            # Assert backup exists
            bak_files = list(temp_dir.glob('ghost_entries_stale_header_*_pre_conid*.csv.bak'))
            assert len(bak_files) == 1, f'Expected 1 backup file, got {len(bak_files)}'
            bak_file = bak_files[0]

            # Assert new file has ENTRY_FIELDS header and 3 rows
            with entries_file.open('r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)
                assert header == ENTRY_FIELDS, 'New file header does not match ENTRY_FIELDS'
                rows_after = list(csv.DictReader(f, fieldnames=header))
                # Note: first line was header so DictReader with fieldnames read remaining 3 rows
                assert len(rows_after) == 3, f'Expected 3 rows, got {len(rows_after)}'

            # Fixture 5: Assert every original field is byte-identical post-migration
            for orig, migrated in zip(synthetic_rows, rows_after):
                for key in old_fields:
                    assert migrated[key] == orig[key], f'Field {key} mismatch: {migrated[key]!r} != {orig[key]!r}'
                assert migrated['underlying_contract_id'] == '', 'underlying_contract_id not empty'
                assert migrated['short_contract_id'] == '', 'short_contract_id not empty'
                assert migrated['long_contract_id'] == '', 'long_contract_id not empty'

            print("PASS: fixture 5: migration preserves every existing field byte-identical with empty conids")

            # Fixture 4: Run migration second time (idempotency test)
            entries_bytes_before = entries_file.read_bytes()
            bak_mtime_before = bak_file.stat().st_mtime_ns

            res2 = migrate_entries(entries_file, as_of_date='2026-09-19')
            assert res2 == 0, 'Migration 2 returned non-zero'

            # No double-archiving: still exactly 1 backup file
            bak_files_2 = list(temp_dir.glob('ghost_entries_stale_header_*_pre_conid*.csv.bak'))
            assert len(bak_files_2) == 1, f'Double-archiving occurred: {bak_files_2}'
            assert bak_files_2[0].stat().st_mtime_ns == bak_mtime_before, 'Backup file modified on second run'

            # No duplicated rows, byte-identical to post-migration 1
            assert entries_file.read_bytes() == entries_bytes_before, 'File content modified on second run'
            with entries_file.open('r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                assert len(list(reader)) == 3, 'Rows duplicated or lost'

            print("PASS: fixture 4: migration idempotency (second run is clean no-op)")

        except Exception as error:
            failures += 1
            print(f"FAIL: migration self-test: {error}")

    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries-path', type=Path, default=ROOT / 'state/ghost/ghost_entries.csv',
                        help='Path to ghost_entries.csv to migrate (default: state/ghost/ghost_entries.csv)')
    parser.add_argument('--as-of-date', type=str, default=None,
                        help='Archive date stamp YYYY-MM-DD (default: today)')
    parser.add_argument('--self-test', action='store_true', help='Run synthetic migration self-tests')
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    return migrate_entries(args.entries_path, as_of_date=args.as_of_date)


if __name__ == '__main__':
    raise SystemExit(main())
