#!/usr/bin/env python3
"""DRAFT: arithmetic on saved VRP regime-exit artifacts; never runs a backtest.

Next week's sensitivity runs (each writes a separately named per-spread CSV):
    python3 scripts/backtest/friction_analyzer_v2.py --commission-per-leg 1.50
    python3 scripts/backtest/friction_analyzer_v2.py --commission-per-leg 1.75
    python3 scripts/backtest/friction_analyzer_v2.py --commission-per-leg 2.00

Standard library only; no network, pricing engine, or production imports.
Importing this module or requesting --help does not read CSVs or write files.
"""

import argparse
import csv
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import mean


RUN_NAME = "vrp_regime_exit"
DEFAULT_ARTIFACTS = Path(__file__).resolve().parent / (
    "run_out_exit_aware_regime/vrp_regime_exit/artifacts"
)
DEFAULT_COMMISSION_PER_LEG = 1.75  # Dollars per one-contract actual leg fill.
DEFAULT_SLIPPAGE_PER_SHARE = 0.05
CONTRACT_MULTIPLIER = 100
DEFAULT_PNL_TOLERANCE = 0.02  # Dollars per spread; allows CSV price rounding.
# Verified in exit_aware_full_universe_backtest.py, used by regime run config.
# That config also sets commission=0, so these costs are incremental, not doubled.
DEFAULT_INITIAL_CAPITAL = 1_000_000.0

# Read-only inspection of the real CSV during drafting found 6,956 DATA rows
# (6,957 lines including header): sell=1761, buy=1761, close=3378,
# exercise=34, expire=20, early_exercise=2. Entries=1761; exit records=1689.
# options_portfolio.py emits 'close' for actual buy-to-close/sell-to-close
# orders. Thus a literal buy/sell-only filter would incorrectly omit exit costs.
# Commission and slippage apply to buy/sell/close, using abs(qty), never its sign.
# Assume no trading commission or spread crossing on exercise/expire or early
# exercise. Assignment-related stock trades/fees are not modeled in these CSVs.
ORDER_SIDES = {"buy", "sell", "close"}
SETTLEMENT_SIDES = {"exercise", "expire", "early_exercise"}
TERMINAL_SIDES = {"close"} | SETTLEMENT_SIDES
# options_portfolio.py's margin-reservation gate writes one "rejected_margin"
# row (no real legs, qty=0) per signal it refuses to open -- a real side
# value, not a data error. This run's config is verified to keep the count
# at 0 (see DEFAULT_INITIAL_CAPITAL comment above), but treating it as
# "unknown" would still hard-crash the whole analysis on a future run where
# it isn't. No commission/slippage/P&L applies -- nothing ever filled.
NON_FILL_SIDES = {"rejected_margin"}


def number(value):
    """Reject corrupt or nonfinite financial inputs instead of propagating NaN."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite numeric input: {value!r}")
    return result


def nonnegative(value):
    result = number(value)
    if result < 0:
        raise argparse.ArgumentTypeError("Must be nonnegative")
    return result


def read_csv(path, required):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = set(required) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        return list(reader)


def key(row):
    return row["code"], row["entry_date"]


def unique_index(rows, label):
    indexed = {}
    for row in rows:
        k = key(row)
        if k in indexed:
            raise ValueError(f"Duplicate {label} key: {k}")
        indexed[k] = row
    return indexed


def parsed_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def annualized(roc, days):
    """Calendar-day equivalent; undefined domains/overflow become flagged blanks."""
    if days is None or days <= 0:
        return None, "missing_or_nonpositive_holding_days"
    if roc < -1:
        return None, "roc_below_minus_one"
    if roc == -1:
        return -1.0, ""
    try:
        value = math.expm1(math.log1p(roc) * 365.0 / days)
    except OverflowError:
        return None, "annualization_overflow"
    if not math.isfinite(value):
        return None, "annualization_nonfinite"
    return value, ""


def analyze(args):
    artifacts = args.artifacts
    entries = unique_index(read_csv(artifacts / "entries.csv", [
        "code", "entry_date", "short_strike", "long_strike", "credit_est",
    ]), "entry")
    exits = unique_index(read_csv(artifacts / "exits.csv", [
        "code", "entry_date", "exit_fill_date", "captured_at_trigger", "reason",
    ]), "exit")
    trades = read_csv(artifacts / "trades.csv", [
        "code", "entry_date", "side", "qty", "pnl", "timestamp", "strike", "expiry",
    ])
    metrics_rows = read_csv(artifacts / "metrics.csv", [
        "final_value", "total_return", "trade_count",
    ])
    summary_path = args.summary or artifacts.parents[1] / "summary.csv"
    summaries = [r for r in read_csv(summary_path, [
        "run", "peak_reserve_usd", "full_total_return",
    ]) if r["run"] == RUN_NAME]
    if not entries or len(metrics_rows) != 1 or len(summaries) != 1:
        raise ValueError("Need entries, exactly one metrics row and one matching summary row")
    metrics, summary = metrics_rows[0], summaries[0]
    if set(exits) - set(entries):
        raise ValueError("Exit records without matching entries")
    sides = Counter(r["side"] for r in trades)
    unknown = set(sides) - ORDER_SIDES - SETTLEMENT_SIDES - NON_FILL_SIDES
    if unknown:
        raise ValueError(f"Unknown sides; review cost classification before proceeding: {unknown}")
    grouped = defaultdict(list)
    for trade in trades:
        if key(trade) not in entries:
            raise ValueError(f"Trade without entry: {key(trade)}")
        grouped[key(trade)].append(trade)

    results = []
    skipped_margin_rejected = []
    for k, entry in entries.items():
        legs = grouped[k]
        opening = [r for r in legs if r["side"] in {"buy", "sell"}]
        terminal = [r for r in legs if r["side"] in TERMINAL_SIDES]
        if legs and all(r["side"] in NON_FILL_SIDES for r in legs):
            # Margin-rejected: no real legs ever filled for this entry. Exclude
            # from the two-leg assertion and every P&L/commission/slippage
            # aggregate below -- there's nothing to charge friction against.
            skipped_margin_rejected.append(k)
            continue
        # This draft covers the verified one-contract vertical dataset. Reject
        # changed sizing rather than silently mixing per-contract and total P&L.
        if (len(opening) != 2 or {r["side"] for r in opening} != {"buy", "sell"}
                or any(abs(number(r["qty"])) != 1 for r in legs)):
            raise ValueError(f"Expected one-contract, two-leg opening: {k}")
        identity = lambda r: (number(r["strike"]), r["expiry"], number(r["qty"]))
        opened_legs = Counter(identity(r) for r in opening)
        ended_legs = Counter(identity(r) for r in terminal)
        if ended_legs - opened_legs:
            raise ValueError(f"Unmatched or duplicate terminal legs: {k}")
        complete = ended_legs == opened_legs
        status = "closed" if complete else ("partial" if terminal else "open")
        exit_row = exits.get(k)
        # Recorded leg P&L is already dollars (100 multiplier already applied).
        # It captures actual fills and settlements, including losses. Never clip
        # captured_at_trigger to [0,1], and never re-price any option here.
        gross = math.fsum(number(r["pnl"]) for r in legs)
        trigger_pnl = (number(entry["credit_est"]) * CONTRACT_MULTIPLIER
                       * number(exit_row["captured_at_trigger"])) if exit_row else None
        difference = gross - trigger_pnl if trigger_pnl is not None else None
        # Trigger estimates can differ from next-day-open fills. Preserve BOTH
        # derivations and their signed difference; report mismatch counts below.
        check = ("no_exit_record" if difference is None else
                 "incomplete_position" if not complete else
                 "mismatch" if abs(difference) > args.pnl_tolerance else "match")
        order_units = sum(abs(number(r["qty"])) for r in legs if r["side"] in ORDER_SIDES)
        commission = order_units * args.commission_per_leg
        # Interpret the requested $0.05 as OPTION QUOTE dollars per share:
        # 0.05 * 100 = $5 per leg, $10 per two-leg entry, $10 per explicit exit.
        # It is not $0.05 total cash per contract. Settlements have zero slippage.
        slippage = order_units * args.slippage_per_share * CONTRACT_MULTIPLIER
        net = gross - commission - slippage
        width = number(entry["short_strike"]) - number(entry["long_strike"])
        if width <= 0:
            raise ValueError(f"Nonpositive spread width: {k}")
        # Requested simplified Reg-T/CBOE-style reserve: width * 100, with NO
        # credit offset. Broker/account rules can differ; this is not a claim
        # that every account uses this margin. No entries are resized/replayed.
        margin = width * CONTRACT_MULTIPLIER
        roc = net / margin
        # Explicit exits use the requested exit_fill_date. Settled spreads with
        # no exit record use the last recorded terminal timestamp, not expiry.
        # Missing exit_fill_date is flagged, not silently replaced by expiry.
        end = exit_row["exit_fill_date"] if exit_row else ""
        end_source = "exits.exit_fill_date" if exit_row else ""
        if not exit_row and complete:
            dates = [parsed_date(r["timestamp"]) for r in terminal]
            if all(dates):
                end = max(dates).isoformat()
                end_source = "trades.last_terminal_timestamp"
        start_date, end_date = parsed_date(k[1]), parsed_date(end)
        days = (end_date - start_date).days if start_date and end_date else None
        ann, ann_status = (annualized(roc, days) if complete else
                           (None, "incomplete_position"))
        results.append(dict(
            code=k[0], entry_date=k[1], exit_fill_date=end, exit_date_source=end_source,
            reason=exit_row["reason"] if exit_row else "", position_status=status,
            gross_pnl=gross, trigger_estimated_pnl=trigger_pnl,
            pnl_difference=difference, pnl_crosscheck=check,
            actual_order_leg_units=order_units, commission=commission,
            slippage=slippage, net_pnl=net, margin=margin, roc=roc,
            holding_days=days, annualized_roc=ann, annualization_status=ann_status,
        ))

    # Requested observed period: min/max ENTRY dates, not fixed IS/OOS dates.
    # This omits pre-entry warmup and post-last-entry holding time; label it.
    entry_dates = [parsed_date(k[1]) for k in entries]
    valid_dates = [d for d in entry_dates if d is not None]
    days = (max(valid_dates) - min(valid_dates)).days if valid_dates else None
    peak = number(summary["peak_reserve_usd"])
    if peak <= 0 or args.initial_capital <= 0:
        raise ValueError("Peak reserve and initial capital must be positive")
    gross = math.fsum(r["gross_pnl"] for r in results)
    commission = math.fsum(r["commission"] for r in results)
    slippage = math.fsum(r["slippage"] for r in results)
    net = math.fsum(r["net_pnl"] for r in results)
    gross_return = number(summary["full_total_return"])
    # Full return includes the backtest's marked equity, not just closed P&L.
    # Subtract ALL observed costs on the SAME initial-capital denominator;
    # report the mark/rounding residual instead of pretending realized P&L
    # necessarily reconciles to final equity. No new valuation is performed.
    net_return = gross_return - (commission + slippage) / args.initial_capital
    gross_ann, gross_ann_status = annualized(gross / peak, days)
    net_ann, net_ann_status = annualized(net / peak, days)
    closed = [r for r in results if r["position_status"] == "closed"]
    valid_ann = [r for r in closed if r["annualized_roc"] is not None]
    gross_trade_ann = [annualized(r["gross_pnl"] / r["margin"], r["holding_days"])[0]
                       for r in closed]
    gross_trade_ann = [v for v in gross_trade_ann if v is not None]

    def average(values):
        return mean(values) if values else None

    def fmt(value, percent=False):
        return "N/A" if value is None else (f"{value:.4%}" if percent else f"{value:,.2f}")

    print(f"DRAFT friction arithmetic | commission=${args.commission_per_leg:.2f}/leg "
          f"| slippage=${args.slippage_per_share:.4f}/share/leg")
    print(f"Observed sides: {dict(sides)}; metrics.trade_count={metrics['trade_count']} (LEGS)")
    print(f"Position status: {dict(Counter(r['position_status'] for r in results))}")
    if skipped_margin_rejected:
        print(f"Skipped (margin-rejected, no fill, excluded from all aggregates below): "
              f"{len(skipped_margin_rejected)} of {len(entries)} entries")
    print(f"{'Measure':<53} {'Gross':>19} {'After friction':>19}")
    comparison = [
        ("Total opened spreads", len(results), len(results), False),
        ("Total incremental commission ($)", 0, commission, False),
        ("Total incremental slippage ($)", 0, slippage, False),
        ("Total recorded realized P&L ($), less all costs", gross, net, False),
        ("Full total return (summary equity basis)", gross_return, net_return, True),
        ("Mean completed-spread ROC", average([r['gross_pnl'] / r['margin'] for r in closed]),
         average([r['roc'] for r in closed]), True),
        ("Mean trade annualized ROC (not portfolio return)", average(gross_trade_ann),
         average([r['annualized_roc'] for r in valid_ann]), True),
        ("Peak-reserve-based realized return approximation", gross / peak, net / peak, True),
        ("Peak-reserve annualized approximation (NOT CAGR)", gross_ann, net_ann, True),
    ]
    for label, before, after, percent in comparison:
        print(f"{label:<53} {fmt(before, percent):>19} {fmt(after, percent):>19}")
    print(f"Initial capital: ${args.initial_capital:,.2f}; saved peak reserve: ${peak:,.2f}")
    print(f"Entry-date period: {min(valid_dates) if valid_dates else 'N/A'} to "
          f"{max(valid_dates) if valid_dates else 'N/A'} ({days} days); "
          f"invalid entry dates: {len(entry_dates) - len(valid_dates)}")
    print("Peak-reserve approximation uses saved uncapped reserve, not a daily margin curve; "
          "saved reserve may offset credit unlike the width-only per-spread margin here.")
    print("Open/partial rows retain recorded realized P&L and incurred costs; their ROC is "
          "provisional and excluded from completed-trade averages/annualization.")
    print(f"P&L cross-check, tolerance ${args.pnl_tolerance}: "
          f"{dict(Counter(r['pnl_crosscheck'] for r in results))}")
    differences = [abs(r['pnl_difference']) for r in results if r['pnl_difference'] is not None]
    print(f"Max absolute trigger-vs-recorded P&L difference: ${max(differences, default=0):,.4f}; "
          "recorded leg P&L used; individual discrepancies retained in CSV.")
    print(f"Skipped net trade annualizations: {len(results) - len(valid_ann)}; reasons: "
          f"{dict(Counter(r['annualization_status'] for r in results if r['annualization_status']))}")
    print(f"Skipped gross completed-trade annualizations: {len(closed) - len(gross_trade_ann)}")
    print(f"Portfolio annualization flags: gross={gross_ann_status or 'OK'}, "
          f"net={net_ann_status or 'OK'}")
    residual = number(metrics["final_value"]) - args.initial_capital - gross
    print(f"Final equity minus initial capital minus recorded P&L: ${residual:,.4f} "
          "(unrealized marks/rounding/reconciliation residual, not realized profit).")
    print(f"Metrics total_return minus summary full_total_return: "
          f"{number(metrics['total_return']) - gross_return:.8f}; "
          f"actual leg rows minus metrics.trade_count: {len(trades) - number(metrics['trade_count']):g}")
    # Existing IS/OOS/full risk statistics remain reference-only. A constant
    # cost subtraction cannot reconstruct adjusted daily Sharpe or drawdown.
    print("Unadjusted summary references (not recomputed):")
    for name, value in summary.items():
        if name.startswith(("IS_", "OOS_", "full_")):
            print(f"  {name}: {value}")

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--summary", type=Path, help="Default: artifacts/../../summary.csv")
    parser.add_argument("--commission-per-leg", type=nonnegative,
                        default=DEFAULT_COMMISSION_PER_LEG, help="Dollars; try 1.50, 1.75, 2.00")
    parser.add_argument("--slippage-per-share", type=nonnegative,
                        default=DEFAULT_SLIPPAGE_PER_SHARE, help="Quote dollars; 0.05 = $5/leg")
    parser.add_argument("--pnl-tolerance", type=nonnegative, default=DEFAULT_PNL_TOLERANCE)
    parser.add_argument("--initial-capital", type=nonnegative, default=DEFAULT_INITIAL_CAPITAL,
                        help="Original equity denominator; verified run default $1,000,000")
    args = parser.parse_args()
    # Only a normal explicit invocation reaches analysis and the single output
    # write. Exclusive creation prevents overwriting prior sensitivity results.
    output = args.artifacts.parent / (
        f"friction_adjusted_trades_commission_{args.commission_per_leg:g}"
        f"_slippage_{args.slippage_per_share:g}.csv"
    )
    if output.exists():
        parser.error(f"Output already exists; preserve or move it before rerunning: {output}")
    try:
        results = analyze(args)
        with output.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
