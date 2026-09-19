#!/usr/bin/env python3
"""Idempotent, backed-up schema migration for ghost_observations_raw.csv contract ID persistence.

Same pattern as migrate_ghost_entries_conid.py (the ghost_entries.csv migration
from the same commit) applied to the raw observations audit log, which was
missed by that migration -- CodeRabbit caught the gap 2026-09-19, verified
against the live on-disk header (missing underlying_contract_id/
short_contract_id/long_contract_id while RAW_FIELDS already requires them),
confirmed this would crash tonight's still-active daily-scan-ghost.sh cron
(19:20 IDT) via append()'s own header-mismatch guard.

Archives the old-schema file to
ghost_observations_raw_stale_header_<date>_pre_conid.csv.bak, writes a fresh
file with RAW_FIELDS, and carries forward every existing row with the three
conid fields present but genuinely empty.

Running a second time detects the current header and cleanly no-ops without
double-archiving or row duplication.
"""

import csv
import fcntl
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ghost.ghost_fill_logger import RAW_FIELDS

CONID_FIELDS = ('underlying_contract_id', 'short_contract_id', 'long_contract_id')


def find_backup_path(raw_path: Path, as_of_date: str = None) -> Path:
    if as_of_date is None:
        as_of_date = date.today().isoformat()
    parent = raw_path.parent
    base_name = f"ghost_observations_raw_stale_header_{as_of_date}_pre_conid.csv.bak"
    bak_path = parent / base_name
    if not bak_path.exists():
        return bak_path
    counter = 1
    while True:
        bak_path = parent / f"ghost_observations_raw_stale_header_{as_of_date}_pre_conid_{counter}.csv.bak"
        if not bak_path.exists():
            return bak_path
        counter += 1


def migrate_raw(raw_path: Path, as_of_date: str = None) -> int:
    raw_path = Path(raw_path).resolve()
    if not raw_path.exists():
        print(f"[MIGRATE] {raw_path} does not exist. Nothing to migrate.")
        return 0

    parent = raw_path.parent
    lock_path = parent / '.logger.lock'
    parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        with raw_path.open('r', newline='', encoding='utf-8') as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, [])

        if existing_header == RAW_FIELDS:
            print(f"[MIGRATE] Migration already applied: {raw_path} has current schema. Clean no-op.")
            return 0

        found_conids = set(CONID_FIELDS).intersection(existing_header)
        if found_conids and existing_header != RAW_FIELDS:
            raise ValueError(
                f"[MIGRATE] Incompatible intermediate schema in {raw_path}: contains {sorted(found_conids)} "
                f"but does not match full RAW_FIELDS. Manual intervention required."
            )

        with raw_path.open('r', newline='', encoding='utf-8') as handle:
            old_reader = csv.DictReader(handle)
            old_rows = list(old_reader)

        tmp_path = raw_path.with_suffix(raw_path.suffix + f".tmp{os.getpid()}")
        bak_path = None
        archived = False
        try:
            with tmp_path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
                writer.writeheader()
                for row in old_rows:
                    migrated_row = dict(row)
                    for field in CONID_FIELDS:
                        migrated_row[field] = ''
                    writer.writerow(migrated_row)
                handle.flush()
                os.fsync(handle.fileno())

            bak_path = find_backup_path(raw_path, as_of_date=as_of_date)
            raw_path.rename(bak_path)
            archived = True
            print(f"[MIGRATE] Archived old-schema file -> {bak_path}")

            os.replace(tmp_path, raw_path)
        except Exception:
            if archived and not raw_path.exists() and bak_path is not None and bak_path.exists():
                bak_path.rename(raw_path)
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        print(f"[MIGRATE] Migrated {len(old_rows)} rows forward to new schema at {raw_path}.")
        return 0


def self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        raw_path = state_dir / 'ghost_observations_raw.csv'

        old_header = [f for f in RAW_FIELDS if f not in CONID_FIELDS]
        with raw_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=old_header)
            writer.writeheader()
            writer.writerow({k: f'val_{k}' for k in old_header})
            writer.writerow({k: f'val2_{k}' for k in old_header})

        assert migrate_raw(raw_path, as_of_date='2099-01-01') == 0
        with raw_path.open('r', newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle)
            assert reader.fieldnames == RAW_FIELDS
            rows = list(reader)
            assert len(rows) == 2
            for prefix, row in zip(('val', 'val2'), rows):
                for field in CONID_FIELDS:
                    assert row[field] == ''
                assert row['run_id'] == f'{prefix}_run_id'
        bak = state_dir / 'ghost_observations_raw_stale_header_2099-01-01_pre_conid.csv.bak'
        assert bak.exists()

        # Second run: clean no-op, no double-archive
        assert migrate_raw(raw_path, as_of_date='2099-01-01') == 0
        bak2 = state_dir / 'ghost_observations_raw_stale_header_2099-01-01_pre_conid_1.csv.bak'
        assert not bak2.exists()

        # Non-existent file: no-op
        assert migrate_raw(state_dir / 'nope.csv', as_of_date='2099-01-01') == 0

        # Incompatible partial schema: must raise
        partial_path = state_dir / 'ghost_observations_raw_partial.csv'
        with partial_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=old_header + ['underlying_contract_id'])
            writer.writeheader()
        try:
            migrate_raw(partial_path, as_of_date='2099-01-01')
            raise AssertionError("expected ValueError on incompatible partial schema")
        except ValueError:
            pass

    print("self_test: all checks passed")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--self-test':
        self_test()
        return 0

    raw_path = ROOT / 'state' / 'ghost' / 'ghost_observations_raw.csv'
    return migrate_raw(raw_path)


if __name__ == '__main__':
    sys.exit(main())
