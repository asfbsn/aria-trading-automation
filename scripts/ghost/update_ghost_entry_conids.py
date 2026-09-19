#!/usr/bin/env python3
"""Atomic in-place updater for ghost_entries.csv contract IDs.

Supports one-time recovery resolution updates. Under .logger.lock, reads full CSV,
updates only the targeted candidate_id row(s), writes to a PID-tagged temp file,
fsyncs, and atomically replaces the destination.

Never performs partial/interrupted rewrites. If an update fails or is interrupted,
the existing ghost_entries.csv is preserved completely intact.
"""

import argparse
import csv
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ghost.ghost_fill_logger import ENTRY_FIELDS
from scripts.ghost.ghost_exit_logger import is_ready_to_mark, open_candidate_ids


def coerce_conid(x):
    """Return positive integer contract ID or None."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x if x > 0 else None
    if isinstance(x, str):
        s = x.strip()
        if s.isdigit():
            val = int(s)
            return val if val > 0 else None
    return None


def atomic_update_entries(entries_path: Path, update_map: dict) -> dict:
    """Atomically update matching candidate rows in entries_path.

    update_map: dict mapping candidate_id -> dict of field updates.
    Returns: dict with counts {'attempted': N, 'resolved': M, 'unresolved_blocked': K}
    """
    entries_path = Path(entries_path).resolve()
    if not entries_path.exists():
        raise FileNotFoundError(f"{entries_path} does not exist")

    parent = entries_path.parent
    lock_path = parent / '.logger.lock'
    parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        with entries_path.open('r', newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            if fields != ENTRY_FIELDS:
                raise ValueError(
                    f"{entries_path} schema does not match ENTRY_FIELDS. "
                    f"Run migration first. Existing: {fields}"
                )
            all_rows = list(reader)

        candidate_indices = {row['candidate_id']: idx for idx, row in enumerate(all_rows)}

        attempted = len(update_map)
        resolved = 0
        unresolved_blocked = 0

        for cand_id, updates in update_map.items():
            if cand_id not in candidate_indices:
                raise KeyError(f"Candidate {cand_id} not found in {entries_path}")

            idx = candidate_indices[cand_id]
            target_row = dict(all_rows[idx])

            # Check if resolved vs unresolved-blocked
            status = updates.get('resolution_status')
            raw_underlying = updates.get('underlying_contract_id')
            raw_short = updates.get('short_contract_id')
            raw_long = updates.get('long_contract_id')

            underlying = coerce_conid(raw_underlying)
            short = coerce_conid(raw_short)
            long = coerce_conid(raw_long)

            if status == 'exact':
                if underlying is None or short is None or long is None:
                    malformed = []
                    if underlying is None:
                        malformed.append(f"underlying_contract_id={raw_underlying!r}")
                    if short is None:
                        malformed.append(f"short_contract_id={raw_short!r}")
                    if long is None:
                        malformed.append(f"long_contract_id={raw_long!r}")
                    raise ValueError(
                        f"Candidate {cand_id} asserted resolution_status='exact' but had malformed conid field(s): {', '.join(malformed)}"
                    )

            if underlying is not None and short is not None and long is not None and (status is None or status == 'exact'):
                target_row['underlying_contract_id'] = str(underlying)
                target_row['short_contract_id'] = str(short)
                target_row['long_contract_id'] = str(long)
                target_row['resolution_status'] = 'exact'
                resolved += 1
            else:
                reason = updates.get('reason') or updates.get('unresolved_reason') or status or 'resolution_failed'
                target_row['underlying_contract_id'] = ''
                target_row['short_contract_id'] = ''
                target_row['long_contract_id'] = ''
                target_row['resolution_status'] = f"unresolved_blocked: {reason}"
                unresolved_blocked += 1

            all_rows[idx] = target_row

        # Write to temp file then atomic replace
        tmp_path = entries_path.with_suffix(entries_path.suffix + f".tmp{os.getpid()}")
        try:
            with tmp_path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=ENTRY_FIELDS)
                writer.writeheader()
                for row in all_rows:
                    writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, entries_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        return {'attempted': attempted, 'resolved': resolved, 'unresolved_blocked': unresolved_blocked}


def apply_recovery_file(entries_path: Path, recovery_file: Path) -> dict:
    recovery_file = Path(recovery_file).resolve()
    data = json.loads(recovery_file.read_text(encoding='utf-8'))
    if isinstance(data, list):
        update_map = {item['candidate_id']: item for item in data}
    elif isinstance(data, dict):
        if 'candidates' in data and isinstance(data['candidates'], list):
            update_map = {item['candidate_id']: item for item in data['candidates']}
        else:
            update_map = data
    else:
        raise ValueError("Recovery file must be a JSON array or dict mapping candidate_id to updates")

    result = atomic_update_entries(entries_path, update_map)
    summary_line = f"ATTEMPTED: {result['attempted']} RESOLVED: {result['resolved']} UNRESOLVED-BLOCKED: {result['unresolved_blocked']}"
    print(summary_line)
    return result


def self_test():
    failures = 0
    with tempfile.TemporaryDirectory(prefix='ghost-update-test-') as temp:
        temp_dir = Path(temp)
        entries_file = temp_dir / 'ghost_entries.csv'

        # Seed 3 rows with ENTRY_FIELDS
        base_rows = []
        for i, sym in enumerate(['AAPL', 'MSFT', 'GOOG']):
            r = {k: '' for k in ENTRY_FIELDS}
            r.update({
                'candidate_id': f'cand_{i}',
                'run_id': f'run_{i}',
                'trade_date': '2026-09-17',
                'signal_bar_date': '2026-09-16',
                'mode': 'vrp_only',
                'ticker': sym,
                'resolution_status': 'exact',
                'credit_mid': '1.00',
                'resolved_short_strike': '150.0',
                'resolved_long_strike': '145.0',
                'resolved_expiry': '2026-10-16',
                'underlying_contract_id': '',
                'short_contract_id': '',
                'long_contract_id': '',
            })
            base_rows.append(r)

        def write_base_file():
            with entries_file.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=ENTRY_FIELDS)
                writer.writeheader()
                for r in base_rows:
                    writer.writerow(r)

        write_base_file()

        # Fixture 7: Atomic and targeted update
        try:
            # Record row 0 and row 2 initial representations
            with entries_file.open('r', newline='', encoding='utf-8') as f:
                initial_rows = list(csv.DictReader(f))
            row0_initial = dict(initial_rows[0])
            row2_initial = dict(initial_rows[2])

            # Update ONLY row 1 (cand_1)
            update_spec = {
                'cand_1': {
                    'underlying_contract_id': 11111,
                    'short_contract_id': 22222,
                    'long_contract_id': 33333,
                    'resolution_status': 'exact'
                }
            }
            res = atomic_update_entries(entries_file, update_spec)
            assert res['attempted'] == 1
            assert res['resolved'] == 1
            assert res['unresolved_blocked'] == 0

            with entries_file.open('r', newline='', encoding='utf-8') as f:
                after_rows = list(csv.DictReader(f))

            # Row 1 is updated
            assert after_rows[1]['underlying_contract_id'] == '11111'
            assert after_rows[1]['short_contract_id'] == '22222'
            assert after_rows[1]['long_contract_id'] == '33333'
            assert after_rows[1]['resolution_status'] == 'exact'

            # Rows 0 and 2 are completely untouched (byte-identical dict values)
            for k in ENTRY_FIELDS:
                assert after_rows[0][k] == row0_initial[k], f"Row 0 field {k} mutated!"
                assert after_rows[2][k] == row2_initial[k], f"Row 2 field {k} mutated!"

            # Simulated interruption test: assert temp-file-then-replace pattern protects the file
            content_before_failure = entries_file.read_bytes()

            with patch('os.replace', side_effect=IOError("Simulated disk error during atomic replace")):
                try:
                    atomic_update_entries(entries_file, {
                        'cand_0': {
                            'underlying_contract_id': 99999,
                            'short_contract_id': 88888,
                            'long_contract_id': 77777,
                            'resolution_status': 'exact'
                        }
                    })
                    assert False, "Should have raised IOError"
                except IOError:
                    pass

            # File is completely intact after interrupted update
            assert entries_file.read_bytes() == content_before_failure, "File corrupted by interrupted update!"
            # Temp file was cleaned up
            leftover_tmps = list(temp_dir.glob('*.tmp*'))
            assert len(leftover_tmps) == 0, f"Leftover temp files found: {leftover_tmps}"

            print("PASS: fixture 7: atomic and targeted update with simulated interruption protection")

        except Exception as error:
            failures += 1
            print(f"FAIL: fixture 7 self-test: {error}")

        # Fixture 8: Unresolved-row handling
        try:
            # Update row 0 with resolution failure (conid_field_mismatch)
            recovery_scratch = temp_dir / 'recovery_scratch.json'
            recovery_scratch.write_text(json.dumps([
                {
                    'candidate_id': 'cand_0',
                    'resolution_status': 'conid_field_mismatch',
                    'reason': 'conid_field_mismatch'
                }
            ]), encoding='utf-8')

            summary = apply_recovery_file(entries_file, recovery_scratch)
            assert summary['attempted'] == 1
            assert summary['resolved'] == 0
            assert summary['unresolved_blocked'] == 1

            with entries_file.open('r', newline='', encoding='utf-8') as f:
                rows_now = list(csv.DictReader(f))

            row0 = rows_now[0]
            assert row0['resolution_status'] == 'unresolved_blocked: conid_field_mismatch'
            assert row0['underlying_contract_id'] == ''
            assert row0['short_contract_id'] == ''
            assert row0['long_contract_id'] == ''

            # Assert excluded from ready-to-mark set
            assert is_ready_to_mark(row0) is False
            ready_candidates = open_candidate_ids(temp_dir, ready_to_mark_only=True)
            assert not any(c['candidate_id'] == 'cand_0' for c in ready_candidates)
            # Row 1 (resolved earlier) IS in ready candidates
            assert any(c['candidate_id'] == 'cand_1' for c in ready_candidates)

            print("PASS: fixture 8: unresolved-row handling (marked blocked, excluded from ready-to-mark, counted in summary)")

        except Exception as error:
            failures += 1
            print(f"FAIL: fixture 8 self-test: {error}")

    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries-path', type=Path, default=ROOT / 'state/ghost/ghost_entries.csv')
    parser.add_argument('--recovery-file', type=Path, help='Path to recovery scratch JSON file')
    parser.add_argument('--self-test', action='store_true', help='Run self tests for Fixtures 7 and 8')
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.recovery_file:
        parser.error("--recovery-file is required unless --self-test is specified")

    apply_recovery_file(args.entries_path, args.recovery_file)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
