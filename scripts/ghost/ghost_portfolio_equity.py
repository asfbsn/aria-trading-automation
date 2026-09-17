#!/usr/bin/env python3
"""Offline ghost portfolio equity series reconstructor.

Reconstructs a daily portfolio-equity time series from Ghost System paper-trading CSVs
(ghost_entries.csv, ghost_marks.csv, ghost_exits.csv).
Pure CSV parsing and math; no network, IBKR, or production dependencies.

Conventions match scripts/backtest/friction_analyzer_v2.py:
- CONTRACT_MULTIPLIER = 100 (option quotes in per-share dollars).
- Margin reserve per spread = (resolved_short_strike - resolved_long_strike) * 100
  (simplified Reg-T style, no credit offset). Non-positive width raises ValueError.

Disclaimer requirement:
Adheres strictly to docs/self-improvement-roadmap.md ("Two separate floors").
No Sharpe ratio or annualized risk-adjusted metrics are computed or reported.
"""

import argparse
import csv
import math
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

# Support direct execution as well as repository-root imports.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ghost.ghost_fill_logger import ROOT as DEFAULT_ROOT, rows, append

try:
    from scripts.ghost.ghost_exit_logger import MARK_FIELDS, EXIT_FIELDS
except ImportError:
    MARK_FIELDS = (
        'run_id mark_date candidate_id ticker dte short_mid long_mid '
        'cost_to_close_mid cost_to_close_natural displayed_crossing_cost_per_leg_close '
        'unrealized_pnl_mid pct_max_profit_captured regime regime_data_available '
        'premium_stop_triggered profit_target_triggered time_stop_triggered '
        'code_version_hash git_head git_dirty'
    ).split()
    EXIT_FIELDS = (
        'run_id exit_date candidate_id ticker trade_date dte_at_exit '
        'exit_reason credit_mid cost_to_close_mid cost_to_close_natural '
        'displayed_crossing_cost_per_leg_close realized_pnl_mid pct_max_profit_captured '
        'regime regime_data_available code_version_hash git_head git_dirty'
    ).split()

CONTRACT_MULTIPLIER = 100

PORTFOLIO_EQUITY_FIELDS = [
    'date',
    'open_position_count',
    'fresh_mark_count',
    'stale_mark_count',
    'unrealized_pnl_dollars',
    'realized_pnl_cumulative_dollars',
    'margin_reserve_dollars',
    'peak_margin_reserve_to_date_dollars',
    'equity_dollars',
    'return_pct',
]

MANDATORY_DISCLAIMER = """\
MANDATORY ROADMAP DISCLAIMER (docs/self-improvement-roadmap.md):
Any Sharpe or return statistic computed on this equity series is descriptive only
until the ghost ledger clears its own documented coverage-and-validation floors.
Under docs/self-improvement-roadmap.md ("Two separate floors"):
  - A provisional coverage milestone (>=30 distinct tickers and >=20 trading
    sessions) unlocks preliminary descriptive friction calibration only, NOT
    strategy validation.
  - Under idealized i.i.d. daily returns near zero Sharpe, SE(SR_annual) ≈ √(252/N);
    at N=20 sessions, that is √12.6 ≈ 3.55 — an estimation standard error several
    times larger than any Sharpe this strategy could plausibly produce.
  - Thirty correlated tickers do not turn 20 market days into 600 independent observations.
In accordance with this documented constraint, no Sharpe ratio or annualized
risk-adjusted statistic is computed or reported by this script.\
"""


def validate_initial_capital(value: float) -> float:
    """Ensure initial capital is a positive, finite number; fail loud otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"initial_capital must be a positive finite number, got {value!r}")
    return float(value)


def parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def compute_max_drawdown(equity_series: list[float]) -> float:
    """Calculate peak-to-trough max drawdown as a non-negative fraction."""
    if not equity_series:
        return 0.0
    peak = equity_series[0]
    max_dd = 0.0
    for eq in equity_series:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def reconstruct_portfolio_equity(
    state_dir: Path,
    initial_capital: float = 100000.0,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> tuple[list[dict], dict]:
    """Reconstruct daily equity series from ghost CSVs.

    Args:
        state_dir: Directory containing ghost CSVs (ghost_entries.csv, ghost_marks.csv, ghost_exits.csv).
        initial_capital: Starting capital in dollars (must be positive).
        start_date: Optional start date override (inclusive).
        end_date: Optional end date override (inclusive).

    Returns:
        tuple of (daily_rows, summary_metrics).
    """
    capital = validate_initial_capital(initial_capital)
    state_dir = Path(state_dir)

    entries_path = state_dir / 'ghost_entries.csv'
    marks_path = state_dir / 'ghost_marks.csv'
    exits_path = state_dir / 'ghost_exits.csv'

    raw_entries = rows(entries_path)
    raw_marks = rows(marks_path)
    raw_exits = rows(exits_path)

    # 1. Parse entries
    entries_by_id: dict[str, dict] = {}
    for entry in raw_entries:
        cid = entry.get('candidate_id', '').strip()
        if not cid:
            raise ValueError(f"{entries_path}: row with missing candidate_id")
        trade_date_str = entry.get('trade_date', '').strip()
        if not trade_date_str:
            raise ValueError(f"{entries_path}: candidate {cid} missing trade_date")
        trade_date = date.fromisoformat(trade_date_str)

        short_strike = float(entry['resolved_short_strike'])
        long_strike = float(entry['resolved_long_strike'])
        if not (math.isfinite(short_strike) and math.isfinite(long_strike)):
            raise ValueError(f"{entries_path}: candidate {cid} has non-finite strikes: short={short_strike}, long={long_strike}")
        width = short_strike - long_strike
        if width <= 0:
            raise ValueError(f"{entries_path}: candidate {cid} has non-positive spread width: {width}")

        credit_mid = float(entry['credit_mid'])
        if not math.isfinite(credit_mid) or credit_mid <= 0:
            raise ValueError(f"{entries_path}: candidate {cid} has invalid credit_mid: {credit_mid}")

        entries_by_id[cid] = {
            'candidate_id': cid,
            'ticker': entry.get('ticker', ''),
            'trade_date': trade_date,
            'resolved_short_strike': short_strike,
            'resolved_long_strike': long_strike,
            'width': width,
            'credit_mid': credit_mid,
        }

    # 2. Parse exits
    exits_by_id: dict[str, dict] = {}
    for exit_row in raw_exits:
        cid = exit_row.get('candidate_id', '').strip()
        if not cid:
            raise ValueError(f"{exits_path}: row with missing candidate_id")
        exit_date_str = exit_row.get('exit_date', '').strip()
        if not exit_date_str:
            raise ValueError(f"{exits_path}: candidate {cid} missing exit_date")
        exit_date = date.fromisoformat(exit_date_str)
        cost_to_close_mid = float(exit_row['cost_to_close_mid'])
        realized_pnl_mid = float(exit_row['realized_pnl_mid'])
        if not (math.isfinite(cost_to_close_mid) and math.isfinite(realized_pnl_mid)):
            raise ValueError(f"{exits_path}: candidate {cid} has non-finite exit values")

        exits_by_id[cid] = {
            'candidate_id': cid,
            'exit_date': exit_date,
            'cost_to_close_mid': cost_to_close_mid,
            'realized_pnl_mid': realized_pnl_mid,
        }

    # Integrity cross-check
    unknown_exits = set(exits_by_id) - set(entries_by_id)
    if unknown_exits:
        raise ValueError(f"{exits_path}: contains candidates not in entries: {sorted(unknown_exits)}")

    # 3. Parse marks
    marks_by_candidate_date: dict[tuple[str, date], float] = {}
    for mark_row in raw_marks:
        cid = mark_row.get('candidate_id', '').strip()
        if not cid:
            raise ValueError(f"{marks_path}: row with missing candidate_id")
        mark_date_str = mark_row.get('mark_date', '').strip()
        if not mark_date_str:
            raise ValueError(f"{marks_path}: candidate {cid} missing mark_date")
        mark_date = date.fromisoformat(mark_date_str)
        cost_to_close_mid = float(mark_row['cost_to_close_mid'])
        if not math.isfinite(cost_to_close_mid):
            raise ValueError(f"{marks_path}: candidate {cid} on {mark_date} has non-finite cost_to_close_mid")
        marks_by_candidate_date[(cid, mark_date)] = cost_to_close_mid

    unknown_marks = {cid for (cid, _) in marks_by_candidate_date} - set(entries_by_id)
    if unknown_marks:
        raise ValueError(f"{marks_path}: contains candidates not in entries: {sorted(unknown_marks)}")

    # 4. Determine date range
    parsed_start = parse_date(start_date)
    parsed_end = parse_date(end_date)

    entry_dates = [e['trade_date'] for e in entries_by_id.values()]
    mark_dates = [k[1] for k in marks_by_candidate_date.keys()]
    exit_dates = [e['exit_date'] for e in exits_by_id.values()]

    if parsed_start is not None:
        cal_start = parsed_start
    elif entry_dates:
        cal_start = min(entry_dates)
    else:
        raise ValueError("Cannot determine start date: no entries present and no --start-date provided")

    if parsed_end is not None:
        cal_end = parsed_end
    else:
        all_dates = entry_dates + mark_dates + exit_dates
        if all_dates:
            cal_end = max(all_dates)
        else:
            cal_end = cal_start

    if cal_start > cal_end:
        raise ValueError(f"Start date {cal_start} is after end date {cal_end}")

    # 5. Daily series reconstruction
    daily_rows: list[dict] = []
    peak_margin = 0.0
    last_known_mark: dict[str, float] = {}

    current_day = cal_start
    one_day = timedelta(days=1)

    while current_day <= cal_end:
        open_count = 0
        fresh_count = 0
        stale_count = 0
        day_unrealized = 0.0
        day_margin = 0.0

        for cid, entry in entries_by_id.items():
            if entry['trade_date'] > current_day:
                continue

            exit_info = exits_by_id.get(cid)
            if exit_info and exit_info['exit_date'] <= current_day:
                # Position exited on or before today
                if exit_info['exit_date'] == current_day:
                    # Fresh observation today for the closing position
                    fresh_count += 1
                    last_known_mark[cid] = exit_info['cost_to_close_mid']
                # Closed position: open_count=0, margin=0, unrealized=0
                continue

            # Position is open today
            open_count += 1
            day_margin += entry['width'] * CONTRACT_MULTIPLIER

            if (cid, current_day) in marks_by_candidate_date:
                mark_price = marks_by_candidate_date[(cid, current_day)]
                last_known_mark[cid] = mark_price
                fresh_count += 1
            else:
                # Stale mark carried forward from prior mark or entry credit_mid
                mark_price = last_known_mark.get(cid, entry['credit_mid'])
                stale_count += 1

            pos_unrealized = (entry['credit_mid'] - mark_price) * CONTRACT_MULTIPLIER
            day_unrealized += pos_unrealized

        # Cumulative realized P&L across all positions exited on or before today
        cum_realized = 0.0
        for cid, exit_info in exits_by_id.items():
            if exit_info['exit_date'] <= current_day:
                cum_realized += exit_info['realized_pnl_mid'] * CONTRACT_MULTIPLIER

        peak_margin = max(peak_margin, day_margin)
        equity = capital + cum_realized + day_unrealized
        ret_pct = (equity / capital) - 1.0

        daily_rows.append({
            'date': current_day.isoformat(),
            'open_position_count': open_count,
            'fresh_mark_count': fresh_count,
            'stale_mark_count': stale_count,
            'unrealized_pnl_dollars': day_unrealized,
            'realized_pnl_cumulative_dollars': cum_realized,
            'margin_reserve_dollars': day_margin,
            'peak_margin_reserve_to_date_dollars': peak_margin,
            'equity_dollars': equity,
            'return_pct': ret_pct,
        })

        current_day += one_day

    # 6. Portfolio metrics
    distinct_accepted = len(entries_by_id)
    distinct_exited = len(exits_by_id)
    distinct_open = distinct_accepted - distinct_exited

    metrics = compute_portfolio_metrics(
        daily_rows=daily_rows,
        initial_capital=capital,
        distinct_accepted=distinct_accepted,
        distinct_open=distinct_open,
        distinct_exited=distinct_exited,
    )

    return daily_rows, metrics


def compute_portfolio_metrics(
    daily_rows: list[dict],
    initial_capital: float,
    distinct_accepted: int,
    distinct_open: int,
    distinct_exited: int,
) -> dict:
    if not daily_rows:
        return {
            'start_date': None,
            'end_date': None,
            'total_calendar_days': 0,
            'initial_capital': initial_capital,
            'final_equity': initial_capital,
            'total_return_pct': 0.0,
            'max_drawdown': 0.0,
            'distinct_accepted': distinct_accepted,
            'distinct_open': distinct_open,
            'distinct_exited': distinct_exited,
            'total_stale_marks': 0,
        }

    equity_series = [r['equity_dollars'] for r in daily_rows]
    final_equity = equity_series[-1]
    total_return_pct = (final_equity / initial_capital) - 1.0
    max_dd = compute_max_drawdown(equity_series)
    total_stale = sum(r['stale_mark_count'] for r in daily_rows)

    return {
        'start_date': daily_rows[0]['date'],
        'end_date': daily_rows[-1]['date'],
        'total_calendar_days': len(daily_rows),
        'initial_capital': initial_capital,
        'final_equity': final_equity,
        'total_return_pct': total_return_pct,
        'max_drawdown': max_dd,
        'distinct_accepted': distinct_accepted,
        'distinct_open': distinct_open,
        'distinct_exited': distinct_exited,
        'total_stale_marks': total_stale,
    }


def format_summary(metrics: dict) -> str:
    """Format short text summary with mandatory roadmap disclaimer."""
    lines = [
        "=" * 72,
        "GHOST PORTFOLIO EQUITY REPORT",
        "=" * 72,
        f"Date Range:          {metrics['start_date']} to {metrics['end_date']} ({metrics['total_calendar_days']} calendar days)",
        f"Initial Capital:     ${metrics['initial_capital']:,.2f}",
        f"Final Equity:        ${metrics['final_equity']:,.2f}",
        f"Total Return:        {metrics['total_return_pct']:.4%}",
        f"Max Drawdown:        {metrics['max_drawdown']:.4%} ({metrics['max_drawdown']:.6f})",
        f"Positions Seen:      {metrics['distinct_accepted']} accepted, {metrics['distinct_open']} open, {metrics['distinct_exited']} exited",
        f"Total Stale Marks:   {metrics['total_stale_marks']} (across entire daily series)",
        "=" * 72,
        MANDATORY_DISCLAIMER,
        "=" * 72,
    ]
    return "\n".join(lines)


def write_portfolio_equity_csv(daily_rows: list[dict], out_path: Path) -> None:
    """Write the full daily series to CSV."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=PORTFOLIO_EQUITY_FIELDS)
        writer.writeheader()
        for row in daily_rows:
            writer.writerow(row)


def self_test() -> int:
    """Self-test suite covering exact numeric calculations, edge cases, and schema constraints."""
    from scripts.ghost.ghost_fill_logger import ENTRY_FIELDS

    failures = 0

    def run_case(label: str, fn):
        nonlocal failures
        try:
            fn()
            print(f"PASS: {label}")
        except Exception as error:
            failures += 1
            print(f"FAIL: {label}: {error}")

    with tempfile.TemporaryDirectory(prefix='ghost-equity-self-test-') as temp:
        temp_dir = Path(temp)

        # Helper to seed synthetic entries
        def seed_entry(state: Path, candidate_id: str, trade_date: str, credit_mid: float,
                       short_strike: float = 100.0, long_strike: float = 95.0, expiry: str = '2026-10-16'):
            append(state / 'ghost_entries.csv', ENTRY_FIELDS, {
                'run_id': 'synthetic_entry',
                'candidate_id': candidate_id,
                'ticker': 'TEST',
                'trade_date': trade_date,
                'resolved_short_strike': short_strike,
                'resolved_long_strike': long_strike,
                'resolved_expiry': expiry,
                'credit_mid': credit_mid,
            })

        # Helper to seed synthetic marks
        def seed_mark(state: Path, candidate_id: str, mark_date: str, cost_to_close_mid: float):
            append(state / 'ghost_marks.csv', MARK_FIELDS, {
                'run_id': 'synthetic_mark',
                'candidate_id': candidate_id,
                'ticker': 'TEST',
                'mark_date': mark_date,
                'cost_to_close_mid': cost_to_close_mid,
                'unrealized_pnl_mid': 0.0,
            })

        # Helper to seed synthetic exits
        def seed_exit(state: Path, candidate_id: str, exit_date: str, cost_to_close_mid: float, realized_pnl_mid: float):
            append(state / 'ghost_exits.csv', EXIT_FIELDS, {
                'run_id': 'synthetic_exit',
                'candidate_id': candidate_id,
                'ticker': 'TEST',
                'trade_date': '2026-09-16',
                'exit_date': exit_date,
                'cost_to_close_mid': cost_to_close_mid,
                'realized_pnl_mid': realized_pnl_mid,
            })

        # Case 1: Single position lifecycle exact numbers
        def test_single_position():
            state = temp_dir / 'case1'
            state.mkdir(exist_ok=True)
            seed_entry(state, 'pos1', '2026-09-16', credit_mid=1.50, short_strike=100.0, long_strike=95.0)
            seed_mark(state, 'pos1', '2026-09-17', cost_to_close_mid=1.00)
            seed_exit(state, 'pos1', '2026-09-18', cost_to_close_mid=0.60, realized_pnl_mid=0.90)

            # Date range: 2026-09-16 through 2026-09-19 (4 days)
            daily, metrics = reconstruct_portfolio_equity(state, initial_capital=100000.0, end_date='2026-09-19')
            by_date = {r['date']: r for r in daily}

            # Day 1: Entry date (2026-09-16) - carried forward credit_mid, unrealized = 0, stale=1
            d1 = by_date['2026-09-16']
            assert d1['open_position_count'] == 1
            assert d1['fresh_mark_count'] == 0
            assert d1['stale_mark_count'] == 1
            assert math.isclose(d1['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d1['realized_pnl_cumulative_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d1['margin_reserve_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d1['peak_margin_reserve_to_date_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d1['equity_dollars'], 100000.0, abs_tol=1e-9)
            assert math.isclose(d1['return_pct'], 0.0, abs_tol=1e-9)

            # Day 2: Fresh mark date (2026-09-17) - mark 1.00, unrealized = (1.50 - 1.00)*100 = 50.0
            d2 = by_date['2026-09-17']
            assert d2['open_position_count'] == 1
            assert d2['fresh_mark_count'] == 1
            assert d2['stale_mark_count'] == 0
            assert math.isclose(d2['unrealized_pnl_dollars'], 50.0, abs_tol=1e-9)
            assert math.isclose(d2['realized_pnl_cumulative_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d2['margin_reserve_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d2['peak_margin_reserve_to_date_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d2['equity_dollars'], 100050.0, abs_tol=1e-9)
            assert math.isclose(d2['return_pct'], 50.0 / 100000.0, abs_tol=1e-9)

            # Day 3: Exit date (2026-09-18) - position exited today, realized = 0.90*100 = 90.0, unrealized = 0
            d3 = by_date['2026-09-18']
            assert d3['open_position_count'] == 0
            assert d3['fresh_mark_count'] == 1  # Exit row is fresh observation today
            assert d3['stale_mark_count'] == 0
            assert math.isclose(d3['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d3['realized_pnl_cumulative_dollars'], 90.0, abs_tol=1e-9)
            assert math.isclose(d3['margin_reserve_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d3['peak_margin_reserve_to_date_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d3['equity_dollars'], 100090.0, abs_tol=1e-9)
            assert math.isclose(d3['return_pct'], 90.0 / 100000.0, abs_tol=1e-9)

            # Day 4: Date after exit (2026-09-19) - realized P&L is permanent, open_position_count is 0
            d4 = by_date['2026-09-19']
            assert d4['open_position_count'] == 0
            assert d4['fresh_mark_count'] == 0
            assert d4['stale_mark_count'] == 0
            assert math.isclose(d4['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d4['realized_pnl_cumulative_dollars'], 90.0, abs_tol=1e-9)
            assert math.isclose(d4['margin_reserve_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d4['peak_margin_reserve_to_date_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d4['equity_dollars'], 100090.0, abs_tol=1e-9)
            assert math.isclose(d4['return_pct'], 90.0 / 100000.0, abs_tol=1e-9)

        run_case("single position lifecycle exact arithmetic", test_single_position)

        # Case 2: Gap date carries forward prior mark as stale
        def test_gap_date():
            state = temp_dir / 'case2'
            state.mkdir(exist_ok=True)
            seed_entry(state, 'gap_pos', '2026-09-16', credit_mid=2.00)
            seed_mark(state, 'gap_pos', '2026-09-17', cost_to_close_mid=1.20)
            # 2026-09-18 has NO mark
            seed_mark(state, 'gap_pos', '2026-09-19', cost_to_close_mid=0.80)

            daily, _ = reconstruct_portfolio_equity(state, initial_capital=100000.0)
            by_date = {r['date']: r for r in daily}

            gap_row = by_date['2026-09-18']
            assert gap_row['open_position_count'] == 1
            assert gap_row['stale_mark_count'] == 1
            assert gap_row['fresh_mark_count'] == 0
            # Carried forward 1.20 mark -> (2.00 - 1.20) * 100 = 80.0
            assert math.isclose(gap_row['unrealized_pnl_dollars'], 80.0, abs_tol=1e-9)

        run_case("gap date carries forward prior mark and increments stale_mark_count", test_gap_date)

        # Case 3: Two overlapping open positions and running peak margin
        def test_overlapping_positions():
            state = temp_dir / 'case3'
            state.mkdir(exist_ok=True)
            # Pos A: width 5 -> margin 500
            seed_entry(state, 'posA', '2026-09-16', credit_mid=1.50, short_strike=100.0, long_strike=95.0)
            # Pos B: width 10 -> margin 1000
            seed_entry(state, 'posB', '2026-09-16', credit_mid=2.00, short_strike=200.0, long_strike=190.0)

            # Day 2 (2026-09-17): Both open and marked
            seed_mark(state, 'posA', '2026-09-17', cost_to_close_mid=1.00)  # unrealized = +50
            seed_mark(state, 'posB', '2026-09-17', cost_to_close_mid=2.50)  # unrealized = -50

            # Day 3 (2026-09-18): Pos B exits; Pos A remains open
            seed_exit(state, 'posB', '2026-09-18', cost_to_close_mid=2.20, realized_pnl_mid=-0.20)  # -20
            seed_mark(state, 'posA', '2026-09-18', cost_to_close_mid=0.80)  # unrealized = +70

            daily, _ = reconstruct_portfolio_equity(state, initial_capital=100000.0)
            by_date = {r['date']: r for r in daily}

            d2 = by_date['2026-09-17']
            assert d2['open_position_count'] == 2
            assert math.isclose(d2['margin_reserve_dollars'], 1500.0, abs_tol=1e-9)
            assert math.isclose(d2['peak_margin_reserve_to_date_dollars'], 1500.0, abs_tol=1e-9)
            assert math.isclose(d2['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d2['equity_dollars'], 100000.0, abs_tol=1e-9)

            d3 = by_date['2026-09-18']
            assert d3['open_position_count'] == 1
            assert math.isclose(d3['margin_reserve_dollars'], 500.0, abs_tol=1e-9)
            # Peak margin reserve must retain running max of 1500.0 even after reserve drops
            assert math.isclose(d3['peak_margin_reserve_to_date_dollars'], 1500.0, abs_tol=1e-9)
            assert math.isclose(d3['unrealized_pnl_dollars'], 70.0, abs_tol=1e-9)
            assert math.isclose(d3['realized_pnl_cumulative_dollars'], -20.0, abs_tol=1e-9)
            assert math.isclose(d3['equity_dollars'], 100050.0, abs_tol=1e-9)

        run_case("two overlapping positions and peak margin reserve retention", test_overlapping_positions)

        # Case 4: Date range with zero open positions (before entry and after exit)
        def test_zero_position_dates():
            state = temp_dir / 'case4'
            state.mkdir(exist_ok=True)
            seed_entry(state, 'pos1', '2026-09-16', credit_mid=1.50)
            seed_exit(state, 'pos1', '2026-09-17', cost_to_close_mid=1.00, realized_pnl_mid=0.50)

            # Range spans before entry (2026-09-14) and after exit (2026-09-19)
            daily, _ = reconstruct_portfolio_equity(state, initial_capital=100000.0,
                                                   start_date='2026-09-14', end_date='2026-09-19')
            assert len(daily) == 6
            by_date = {r['date']: r for r in daily}

            # Date before entry (2026-09-14)
            d_before = by_date['2026-09-14']
            assert d_before['open_position_count'] == 0
            assert d_before['fresh_mark_count'] == 0
            assert d_before['stale_mark_count'] == 0
            assert math.isclose(d_before['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_before['realized_pnl_cumulative_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_before['margin_reserve_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_before['peak_margin_reserve_to_date_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_before['equity_dollars'], 100000.0, abs_tol=1e-9)
            assert math.isclose(d_before['return_pct'], 0.0, abs_tol=1e-9)

            # Date after all positions exited (2026-09-19)
            d_after = by_date['2026-09-19']
            assert d_after['open_position_count'] == 0
            assert d_after['fresh_mark_count'] == 0
            assert d_after['stale_mark_count'] == 0
            assert math.isclose(d_after['unrealized_pnl_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_after['realized_pnl_cumulative_dollars'], 50.0, abs_tol=1e-9)
            assert math.isclose(d_after['margin_reserve_dollars'], 0.0, abs_tol=1e-9)
            assert math.isclose(d_after['peak_margin_reserve_to_date_dollars'], 500.0, abs_tol=1e-9)
            assert math.isclose(d_after['equity_dollars'], 100050.0, abs_tol=1e-9)
            assert math.isclose(d_after['return_pct'], 50.0 / 100000.0, abs_tol=1e-9)

        run_case("date range with zero open positions produces documented zero shape without crash", test_zero_position_dates)

        # Case 5: Non-positive initial capital fails loudly
        def test_nonpositive_initial_capital():
            state = temp_dir / 'case5'
            state.mkdir(exist_ok=True)
            seed_entry(state, 'p', '2026-09-16', credit_mid=1.0)
            for bad_cap in (0, -100.0, float('nan'), float('inf'), -0.01):
                try:
                    reconstruct_portfolio_equity(state, initial_capital=bad_cap)
                except ValueError as err:
                    assert 'positive finite' in str(err)
                else:
                    raise AssertionError(f"Non-positive initial_capital {bad_cap} did not raise ValueError")

        run_case("non-positive initial capital fails loudly", test_nonpositive_initial_capital)

        # Case 6: Non-positive spread width fails loudly
        def test_nonpositive_spread_width():
            state = temp_dir / 'case6'
            state.mkdir(exist_ok=True)
            # short strike 95 <= long strike 100
            seed_entry(state, 'bad_width', '2026-09-16', credit_mid=1.0, short_strike=95.0, long_strike=100.0)
            try:
                reconstruct_portfolio_equity(state)
            except ValueError as err:
                assert 'non-positive spread width' in str(err)
            else:
                raise AssertionError("Non-positive spread width did not raise ValueError")

        run_case("non-positive spread width fails loudly", test_nonpositive_spread_width)

        # Case 7: Explicitly assert absence of Sharpe/annualized fields
        def test_no_sharpe_or_annualized_fields():
            state = temp_dir / 'case7'
            state.mkdir(exist_ok=True)
            seed_entry(state, 'p1', '2026-09-16', credit_mid=1.0)
            daily, metrics = reconstruct_portfolio_equity(state)

            # Check CSV header
            for field in PORTFOLIO_EQUITY_FIELDS:
                lower = field.lower()
                assert 'sharpe' not in lower, f"Sharpe found in CSV field: {field}"
                assert 'annual' not in lower, f"Annualized found in CSV field: {field}"
                assert 'sortino' not in lower, f"Sortino found in CSV field: {field}"
                assert 'cagr' not in lower, f"CAGR found in CSV field: {field}"

            # Check formatted summary (excluding the disclaimer text which cites the roadmap rule)
            summary_text = format_summary(metrics)
            lines_before_disclaimer = []
            for line in summary_text.splitlines():
                if "MANDATORY ROADMAP DISCLAIMER" in line:
                    break
                lines_before_disclaimer.append(line)
            header_and_body = "\n".join(lines_before_disclaimer)

            assert 'sharpe' not in header_and_body.lower(), "Sharpe field found in summary output"
            assert 'annual' not in header_and_body.lower(), "Annualized field found in summary output"
            assert 'sortino' not in header_and_body.lower(), "Sortino field found in summary output"

            # Check metrics dictionary keys
            for key in metrics:
                lower = key.lower()
                assert 'sharpe' not in lower, f"Sharpe found in metric key: {key}"
                assert 'annual' not in lower, f"Annualized found in metric key: {key}"

        run_case("explicit absence of Sharpe and annualized risk fields", test_no_sharpe_or_annualized_fields)

        # Case 8: Data quality visibility and max drawdown calculation
        def test_data_quality_and_drawdown():
            state = temp_dir / 'case8'
            state.mkdir(exist_ok=True)
            # Pos 1 enters on 2026-09-15 (credit 2.00), no marks on 16/17 (stale=2), exits on 18 at 4.00 (loss of 2.00 -> -$200)
            seed_entry(state, 'loss_pos', '2026-09-15', credit_mid=2.00)
            seed_exit(state, 'loss_pos', '2026-09-18', cost_to_close_mid=4.00, realized_pnl_mid=-2.00)

            daily, metrics = reconstruct_portfolio_equity(state, initial_capital=10000.0)
            # Peak equity was 10000.0; drops on exit to 9800.0. Drawdown = 200 / 10000 = 0.02
            assert math.isclose(metrics['max_drawdown'], 0.02, abs_tol=1e-6)
            # Days: 15 (stale: entry credit), 16 (stale), 17 (stale), 18 (exit today -> fresh mark)
            # Total stale marks = 3
            assert metrics['total_stale_marks'] == 3
            assert metrics['distinct_accepted'] == 1
            assert metrics['distinct_open'] == 0
            assert metrics['distinct_exited'] == 1

        run_case("data quality visibility and peak-to-trough drawdown calculation", test_data_quality_and_drawdown)

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initial-capital', type=float, default=100000.0,
                        help='Initial capital in dollars (positive finite float, default: 100000.0)')
    parser.add_argument('--out', type=Path, default=DEFAULT_ROOT / 'state/ghost/ghost_portfolio_equity.csv',
                        help='Output path for reconstructed daily equity CSV (default: state/ghost/ghost_portfolio_equity.csv)')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_ROOT / 'state/ghost',
                        help='State directory containing ghost CSVs (default: state/ghost)')
    parser.add_argument('--start-date', type=str, default=None,
                        help='Optional start date (YYYY-MM-DD); defaults to earliest trade_date')
    parser.add_argument('--end-date', type=str, default=None,
                        help='Optional end date (YYYY-MM-DD); defaults to latest date across ghost files')
    parser.add_argument('--self-test', action='store_true',
                        help='Run self-test suite and report PASS/FAIL per case')

    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        daily_rows, metrics = reconstruct_portfolio_equity(
            state_dir=args.state_dir,
            initial_capital=args.initial_capital,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    write_portfolio_equity_csv(daily_rows, args.out)
    print(format_summary(metrics))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
