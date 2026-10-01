#!/usr/bin/env python3
"""One-shot, all-or-nothing header migration for every Ghost ledger.

The loggers fail loudly on header drift (ensure_header / append), so whenever a logger's field list gains
columns the existing ledgers must be migrated before the next run. The TARGET header of each ledger is read
from the logger module's own field list at run time (nothing is hard-coded here), so a single run covers every
pending schema change at once (Item 4 bar/structural columns and the later liquidity-gate columns).

Safety: dry-run by default (--apply writes). Refuses if a Ghost scan/mark wrapper holds its lock. Existing
columns are matched by NAME and every value is preserved; new columns are filled with ''. A column present in a
ledger but absent from the target aborts the whole run (nothing is dropped). The original file is archived as
<stem>_stale_header_<date>_pre_migration.csv.bak (never overwritten) before the migrated file replaces it.
Run with scripts/backtest/.venv/bin/python3.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ghost import ghost_exit_logger, ghost_fill_logger  # noqa: E402

csv.field_size_limit(sys.maxsize)


def target_headers() -> dict[str, list[str]]:
    return {
        'ghost_entries.csv': list(ghost_fill_logger.ENTRY_FIELDS),
        'ghost_observations_raw.csv': list(ghost_fill_logger.RAW_FIELDS),
        'ghost_marks.csv': list(ghost_exit_logger.MARK_FIELDS),
        'ghost_exits.csv': list(ghost_exit_logger.EXIT_FIELDS),
        'ghost_mark_observations_raw.csv': list(ghost_exit_logger.RAW_MARK_FIELDS),
    }


def plan_one(path: Path, target: list[str]) -> dict:
    """Return {'status': 'missing'|'ok'|'migrate'|'abort', ...} without touching the file."""
    if not path.exists() or path.stat().st_size == 0:
        return {'status': 'missing'}
    with path.open(newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        data = list(reader)
    if header == target:
        return {'status': 'ok', 'rows': len(data)}
    if len(set(header)) != len(header):
        return {'status': 'abort', 'why': f'duplicate column names in header: {header}'}
    unknown = [c for c in header if c not in target]
    if unknown:
        return {'status': 'abort', 'why': f'columns not in target (would be dropped): {unknown}'}
    for number, row in enumerate(data, start=2):
        if len(row) != len(header):
            return {'status': 'abort', 'why': f'row {number} has {len(row)} cells, header has {len(header)}'}
    return {'status': 'migrate', 'rows': len(data), 'header': header, 'data': data,
            'added': [c for c in target if c not in header]}


def build_migrated(path: Path, target: list[str], plan: dict) -> Path:
    """Write the migrated copy next to the original (same dir, so os.replace is atomic) and verify it."""
    old_header, data = plan['header'], plan['data']
    idx = {name: i for i, name in enumerate(old_header)}
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.migrating.', dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(target)
            for row in data:
                writer.writerow([row[idx[c]] if c in idx else '' for c in target])
            handle.flush()
            os.fsync(handle.fileno())
        with tmp.open(newline='', encoding='utf-8') as handle:
            reader = csv.reader(handle)
            new_header = next(reader)
            new_rows = list(reader)
        assert new_header == target, 'migrated header mismatch'
        assert len(new_rows) == len(data), f'row count changed {len(data)} -> {len(new_rows)}'
        for old, new in zip(data, new_rows):
            for name, i in idx.items():
                assert new[target.index(name)] == old[i], f'value changed in column {name}'
        shutil.copymode(path, tmp)
        return tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def wrapper_lock_held(state_dir: Path) -> str | None:
    """Return the name of a Ghost wrapper lock that is currently held, else None."""
    for name in ('daily-scan-ghost.lock', 'daily-scan-ghost-mark.lock'):
        lock_path = state_dir.parent / name
        if not lock_path.exists():
            continue
        with lock_path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return name
            fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def migrate(state_dir: Path, apply: bool, today: str | None = None, check_wrapper_locks: bool = True) -> int:
    state_dir = Path(state_dir)
    today = today or date.today().isoformat()
    targets = target_headers()
    plans = {name: plan_one(state_dir / name, target) for name, target in targets.items()}
    aborts = {n: p['why'] for n, p in plans.items() if p['status'] == 'abort'}
    for name, plan in plans.items():
        tail = {'missing': 'not present (skipped)', 'ok': f"header current ({plan.get('rows')} rows)",
                'abort': 'ABORT: ' + plan.get('why', ''),
                'migrate': f"{plan.get('rows')} rows; add columns: {plan.get('added')}"}[plan['status']]
        print(f'  {name}: {tail}')
    if aborts:
        print('ABORTED before any change.')
        return 2
    todo = [n for n, p in plans.items() if p['status'] == 'migrate']
    if not todo:
        print('All ledgers up to date; nothing to do.')
        return 0
    if not apply:
        print(f'DRY RUN: {len(todo)} ledger(s) would be migrated. Re-run with --apply.')
        return 0
    if check_wrapper_locks:
        held = wrapper_lock_held(state_dir)
        if held:
            print(f'REFUSING: {held} is held (a Ghost scan/mark run is in progress). Nothing changed.')
            return 3
    with (state_dir / '.logger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        built: dict[str, Path] = {}
        try:
            for name in todo:
                built[name] = build_migrated(state_dir / name, targets[name], plans[name])
            for name in todo:
                original = state_dir / name
                backup = state_dir / f'{original.stem}_stale_header_{today}_pre_migration.csv.bak'
                if backup.exists():
                    raise FileExistsError(f'backup already exists, refusing to overwrite: {backup}')
            for name in todo:
                original = state_dir / name
                backup = state_dir / f'{original.stem}_stale_header_{today}_pre_migration.csv.bak'
                shutil.copy2(original, backup)
                os.replace(built[name], original)
                print(f'  migrated {name} (backup: {backup.name})')
        finally:
            for tmp in built.values():
                tmp.unlink(missing_ok=True)
    print(f'DONE: {len(todo)} ledger(s) migrated.')
    return 0


def _self_test() -> None:
    targets = target_headers()
    with tempfile.TemporaryDirectory(prefix='ghost-migrate-test-') as raw:
        root = Path(raw)
        state = root / 'ghost'
        state.mkdir()

        def write(name, header, rows):
            with (state / name).open('w', newline='', encoding='utf-8') as h:
                w = csv.writer(h)
                w.writerow(header)
                w.writerows(rows)

        # marks/exits/raw: old = current minus the Item-4 columns (and one with an embedded newline cell)
        new_cols = [c for c in ghost_exit_logger.STRUCTURAL_FIELDS if c in (
            'structural_source', 'bars_status', 'bars_provided', 'bar_close', 'bar_rsi20', 'bar_ma150',
            'check_short_strike_breached', 'check_broke_ma150_support', 'check_bearish_candle',
            'check_rsi_overbought', 'check_volume_confirmed_breakdown')]
        old_marks = [c for c in targets['ghost_marks.csv'] if c not in new_cols]
        row = [f'v{i}' for i in range(len(old_marks))]
        row[old_marks.index('ticker')] = 'AAA\nBBB'          # embedded newline must survive
        write('ghost_marks.csv', old_marks, [row, row])
        old_exits = [c for c in targets['ghost_exits.csv'] if c not in new_cols]
        write('ghost_exits.csv', old_exits, [])
        write('ghost_entries.csv', targets['ghost_entries.csv'], [['x'] * len(targets['ghost_entries.csv'])])
        # dry run changes nothing
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        assert migrate(state, apply=False, today='2026-10-02', check_wrapper_locks=False) == 0
        assert {p.name: p.read_bytes() for p in state.iterdir()} == before
        print('PASS: dry run changes nothing')
        # apply
        assert migrate(state, apply=True, today='2026-10-02', check_wrapper_locks=False) == 0
        with (state / 'ghost_marks.csv').open(newline='', encoding='utf-8') as h:
            rd = list(csv.reader(h))
        assert rd[0] == targets['ghost_marks.csv'] and len(rd) == 3
        assert rd[1][rd[0].index('ticker')] == 'AAA\nBBB'
        assert all(rd[1][rd[0].index(c)] == '' for c in new_cols)
        assert rd[1][rd[0].index('run_id')] == row[old_marks.index('run_id')]
        assert (state / 'ghost_marks_stale_header_2026-10-02_pre_migration.csv.bak').exists()
        assert (state / 'ghost_exits_stale_header_2026-10-02_pre_migration.csv.bak').exists()
        assert not (state / 'ghost_entries_stale_header_2026-10-02_pre_migration.csv.bak').exists()
        print('PASS: apply preserves rows/values (incl. embedded newline), adds blanks, archives originals')
        # idempotent
        assert migrate(state, apply=True, today='2026-10-02', check_wrapper_locks=False) == 0
        print('PASS: second apply is a no-op')
        # unknown column aborts everything, nothing changes
        write('ghost_exits.csv', targets['ghost_exits.csv'] + ['mystery'], [])
        write('ghost_marks.csv', old_marks, [row])
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        assert migrate(state, apply=True, today='2026-10-03', check_wrapper_locks=False) == 2
        assert {p.name: p.read_bytes() for p in state.iterdir()} == before
        print('PASS: a column that would be dropped aborts the whole run with no change')
        # held wrapper lock refuses
        write('ghost_exits.csv', old_exits, [])
        lock_path = root / 'daily-scan-ghost-mark.lock'
        with lock_path.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            before = {p.name: p.read_bytes() for p in state.iterdir()}
            assert migrate(state, apply=True, today='2026-10-03') == 3
            assert {p.name: p.read_bytes() for p in state.iterdir()} == before
        print('PASS: held wrapper lock refuses to migrate')
        # existing backup is never overwritten
        (state / 'ghost_marks_stale_header_2026-10-04_pre_migration.csv.bak').write_text('keep')
        try:
            migrate(state, apply=True, today='2026-10-04', check_wrapper_locks=False)
            raise AssertionError('expected FileExistsError')
        except FileExistsError:
            pass
        assert (state / 'ghost_marks_stale_header_2026-10-04_pre_migration.csv.bak').read_text() == 'keep'
        print('PASS: an existing backup is never overwritten')
    print('ALL TESTS PASSED')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state-dir', default=str(ghost_fill_logger.ROOT / 'state' / 'ghost'))
    parser.add_argument('--apply', action='store_true', help='actually migrate (default: dry run)')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    if args.test:
        _self_test()
        return 0
    return migrate(Path(args.state_dir), apply=args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
