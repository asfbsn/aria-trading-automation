#!/usr/bin/env python3
"""Read-only, provisional cost-rule analysis of liquidity-rejected bull put spreads.

The cost thresholds below are pre-registered and provisional. This report does
not change any live gate. Expiry results are hypothetical hold-to-expiry
outcomes with no exits or trade management.
"""

import argparse
import csv
import glob
import json
import math
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path


COST_RULE_MAX_PCT_OF_CREDIT = 0.50
COST_RULE_MAX_PCT_OF_MAX_LOSS = 0.25
COST_RULE_MIN_SIZE = 10
DEFAULT_GHOST_RAW = Path("state/ghost/ghost_observations_raw.csv")
DEFAULT_REPORTS_GLOB = "logs/*_bull-put-spread.md"

REPORT_LINE_RE = re.compile(
    r"NEAR_MISS_COST:\s*(?P<ticker>\S+)\s+short\s+(?P<short>[-+\d.eE]+)"
    r"\s+long\s+(?P<long>[-+\d.eE]+)\s+exp\s+(?P<expiry>\d{4}-\d{2}-\d{2})"
    r"\s+credit_mid\s+(?P<credit>[-+\d.eE]+)\s+natural\s+(?P<natural>[-+\d.eE]+)"
    r"\s+cost_pct_credit\s+[-+\d.eE]+%\s+cost_pct_max_loss\s+[-+\d.eE]+%"
    r"\s+rr_mid\s+[-+\d.eE]+"
    r"(?:\s+min_size\s+(?P<size>\d+|na))?"
    r"\s+cost_rule_pass\s+(?P<pass>YES|NO|UNKNOWN)\b",
    re.IGNORECASE,
)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def cost_metrics(short_bid, short_ask, long_bid, long_ask, short_strike,
                 long_strike, short_bid_size=None, long_ask_size=None):
    values = [number(value) for value in
              (short_bid, short_ask, long_bid, long_ask, short_strike, long_strike)]
    if any(value is None for value in values):
        return None
    sb, sa, lb, la, ks, kl = values
    if min(sb, sa, lb, la) <= 0 or sa < sb or la < lb or ks <= kl:
        return None
    width = ks - kl
    credit_mid = (sb + sa) / 2 - (lb + la) / 2
    credit_natural = sb - la
    if credit_mid <= 0:
        return None
    cost = credit_mid - credit_natural
    max_loss_mid = width - credit_mid
    if max_loss_mid <= 0:
        return None
    cost_pct_credit = cost / credit_mid
    cost_pct_max_loss = cost / max_loss_mid
    rr_mid = max_loss_mid / credit_mid
    sb_size, la_size = number(short_bid_size), number(long_ask_size)
    min_size = min(sb_size, la_size) if sb_size is not None and la_size is not None else None
    if min_size is None:
        cost_rule_pass = None
    else:
        cost_rule_pass = (
            cost_pct_credit <= COST_RULE_MAX_PCT_OF_CREDIT
            and cost_pct_max_loss <= COST_RULE_MAX_PCT_OF_MAX_LOSS
            and min_size >= COST_RULE_MIN_SIZE
        )
    return {
        "short_strike": ks, "long_strike": kl, "width": width,
        "credit_mid": credit_mid, "credit_natural": credit_natural,
        "cost": cost, "cost_pct_credit": cost_pct_credit,
        "max_loss_mid": max_loss_mid,
        "cost_pct_max_loss": cost_pct_max_loss, "rr_mid": rr_mid,
        "min_size": min_size, "cost_rule_pass": cost_rule_pass,
    }


def ghost_metrics(row):
    try:
        payload = json.loads(row.get("input_json") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    metrics = cost_metrics(
        payload.get("short_bid"), payload.get("short_ask"),
        payload.get("long_bid"), payload.get("long_ask"),
        payload.get("resolved_short_strike"), payload.get("resolved_long_strike"),
        payload.get("short_bid_size"), payload.get("long_ask_size"),
    )
    if metrics is None:
        return None
    for key in ("ticker", "trade_date"):
        if not (row.get(key) or "").strip():
            return None
    expiry = payload.get("resolved_expiry")
    try:
        date.fromisoformat(str(expiry))
    except (TypeError, ValueError):
        return None
    metrics.update({
        "ticker": row["ticker"].strip().upper(),
        "trade_date": row["trade_date"].strip(),
        "expiry": str(expiry),
        "candidate_id": row.get("candidate_id", ""),
        "source_group": ("live_accepted" if row.get("outcome") == "accepted"
                         else "live_gate_rejected"),
    })
    return metrics


def read_ghost(path, since=None):
    by_id = {}
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("outcome") == "duplicate_skipped":
                continue
            group = ("live_accepted" if row.get("outcome") == "accepted"
                     else "live_gate_rejected" if (row.get("reason") or "").startswith("live_gate_")
                     else None)
            if group is None:
                continue
            if since and (row.get("trade_date") or "") < since:
                continue
            cid = (row.get("candidate_id") or "").strip()
            if not cid or cid in by_id:
                continue
            metrics = ghost_metrics(row)
            if metrics is not None:
                metrics["source_group"] = group
                by_id[cid] = metrics
    return list(by_id.values())


def parse_report_line(line, trade_date):
    match = REPORT_LINE_RE.search(line)
    if not match:
        return None
    groups = match.groupdict()
    ks, kl = number(groups["short"]), number(groups["long"])
    credit, natural = number(groups["credit"]), number(groups["natural"])
    if None in (ks, kl, credit, natural) or ks <= kl or credit <= 0:
        return None
    width = ks - kl
    max_loss = width - credit
    if max_loss <= 0:
        return None
    size_text = groups.get("size")
    min_size = number(size_text) if size_text and size_text.lower() != "na" else None
    cost = credit - natural
    cost_pct_credit = cost / credit
    cost_pct_max_loss = cost / max_loss
    rr_mid = max_loss / credit
    computed_pass = None if min_size is None else (
        cost_pct_credit <= COST_RULE_MAX_PCT_OF_CREDIT
        and cost_pct_max_loss <= COST_RULE_MAX_PCT_OF_MAX_LOSS
        and min_size >= COST_RULE_MIN_SIZE
    )
    stated = groups["pass"].upper()
    stated_pass = {"YES": True, "NO": False, "UNKNOWN": None}[stated]
    return {
        "ticker": groups["ticker"].upper(), "trade_date": trade_date,
        "expiry": groups["expiry"], "short_strike": ks, "long_strike": kl,
        "width": width, "credit_mid": credit, "credit_natural": natural,
        "cost": cost, "cost_pct_credit": cost_pct_credit,
        "max_loss_mid": max_loss, "cost_pct_max_loss": cost_pct_max_loss,
        "rr_mid": rr_mid, "min_size": min_size,
        "cost_rule_pass": computed_pass, "stated_pass": stated_pass,
        "source_group": "near_miss",
    }


def read_reports(pattern, since=None):
    rows, seen = [], set()
    disagreements = 0
    for filename in sorted(glob.glob(pattern)):
        path = Path(filename)
        trade_date = path.name[:10]
        try:
            date.fromisoformat(trade_date)
        except ValueError:
            continue
        if since and trade_date < since:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                row = parse_report_line(line, trade_date)
                if row is None:
                    continue
                key = (trade_date, row["ticker"])
                if key in seen:
                    continue
                seen.add(key)
                disagreements += row["stated_pass"] != row["cost_rule_pass"]
                rows.append(row)
    return rows, disagreements


def fetch_close(ticker, on_or_before_date):
    """Fetch last available close on/before date; imports yfinance only on demand."""
    import yfinance as yf

    target = date.fromisoformat(str(on_or_before_date))
    start = target - timedelta(days=7)
    history = yf.Ticker(ticker).history(
        start=start.isoformat(), end=(target + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if history is None or history.empty or "Close" not in history:
        return None
    eligible = []
    for index, value in history["Close"].items():
        try:
            index_date = index.date()
        except AttributeError:
            index_date = date.fromisoformat(str(index)[:10])
        close = number(value)
        if index_date <= target and close is not None:
            eligible.append((index_date, close))
    return max(eligible, key=lambda item: item[0])[1] if eligible else None


def expiry_outcome(row, price_fn):
    close = price_fn(row["ticker"], row["expiry"])
    if close is None:
        return None
    ks, kl = row["short_strike"], row["long_strike"]
    intrinsic = max(0.0, ks - close) - max(0.0, kl - close)
    result = {}
    for label, credit in (("mid", row["credit_mid"]),
                          ("natural", row["credit_natural"])):
        pnl = credit - intrinsic
        max_loss = row["width"] - credit
        result[label] = {
            "pnl": pnl, "pnl_pct_max_loss": pnl / max_loss,
            "win": pnl > 0,
        }
    return result


def add_outcomes(rows, price_fn=fetch_close, today=None, no_prices=False):
    today = today or date.today()
    for row in rows:
        row["outcome"] = None
        if date.fromisoformat(row["expiry"]) >= today:
            row["open"] = True
        else:
            row["open"] = False
            if not no_prices:
                row["outcome"] = expiry_outcome(row, price_fn)
    return rows


def fmt(value):
    return "n/a" if value is None else f"{value:.4f}"


def summarize(rows, source_label):
    lines = [source_label]
    groups = sorted({row["source_group"] for row in rows})
    if not groups:
        groups = ["(none)"]
    for group in groups:
        items = [row for row in rows if row["source_group"] == group]
        states = {True: 0, False: 0, None: 0}
        for row in items:
            states[row["cost_rule_pass"]] += 1
        medians = []
        for key in ("credit_mid", "cost_pct_credit", "cost_pct_max_loss", "rr_mid"):
            medians.append(fmt(statistics.median(row[key] for row in items))
                           if items else "n/a")
        lines.append(
            f"  {group}: n={len(items)} pass={states[True]} fail={states[False]} "
            f"unknown={states[None]} median credit_mid/cost_pct_credit/"
            f"cost_pct_max_loss/rr_mid={'/'.join(medians)}"
        )
    return lines


def outcome_summary(rows, label):
    lines = [label]
    groups = sorted({row["source_group"] for row in rows}) or ["(none)"]
    for group in groups:
        for state in (True, False, None):
            items = [row for row in rows if row["source_group"] == group
                     and row["cost_rule_pass"] is state]
            open_count = sum(row.get("open", False) for row in items)
            expired = [row for row in items if row.get("outcome") is not None]
            state_label = {True: "pass", False: "fail", None: "unknown"}[state]
            values = [f"{group}/{state_label}: n={len(expired)} open={open_count}"]
            for fill in ("mid", "natural"):
                records = [row["outcome"][fill] for row in expired]
                win_rate = (sum(item["win"] for item in records) / len(records)
                            if records else None)
                pnls = [item["pnl_pct_max_loss"] for item in records]
                values.append(
                    f"{fill} win_rate={fmt(win_rate)} "
                    f"mean/median_pnl_pct_max_loss="
                    f"{fmt(statistics.mean(pnls) if pnls else None)}/"
                    f"{fmt(statistics.median(pnls) if pnls else None)}"
                )
            lines.append("  " + " ".join(values))
    return lines


def format_report(ghost_rows, report_rows, disagreements, price_fn=fetch_close,
                  today=None, no_prices=False):
    today = today or date.today()
    add_outcomes(ghost_rows, price_fn=price_fn, today=today, no_prices=no_prices)
    add_outcomes(report_rows, price_fn=price_fn, today=today, no_prices=no_prices)
    lines = [
        "NEAR-MISS COST REPORT (provisional pre-registered rule)",
        f"Rule: cost/credit <= {COST_RULE_MAX_PCT_OF_CREDIT:.2f}, "
        f"cost/max_loss <= {COST_RULE_MAX_PCT_OF_MAX_LOSS:.2f}, "
        f"known min_size >= {COST_RULE_MIN_SIZE}.",
        "Expiry outcomes are hypothetical hold-to-expiry, with no exits or management; "
        "small samples are indicative only.",
    ]
    lines.extend(summarize(ghost_rows, "Ghost raw ledger:"))
    lines.extend(summarize(report_rows, "Live scan reports:"))
    lines.append(f"Live report model-pass disagreements: {disagreements}")
    accepted = sum(row["source_group"] == "live_accepted" for row in ghost_rows)
    rejected = sum(row["source_group"] == "live_gate_rejected" for row in ghost_rows)
    matrix = {(gate, rule): 0 for gate in ("accepted", "rejected")
              for rule in ("pass", "fail")}
    for row in ghost_rows:
        gate = "accepted" if row["source_group"] == "live_accepted" else "rejected"
        rule = "pass" if row["cost_rule_pass"] is True else "fail" if row["cost_rule_pass"] is False else None
        if rule:
            matrix[(gate, rule)] += 1
    lines.append("Ghost 2x2 (rows live gate accepted/rejected; cols cost rule pass/fail):")
    lines.append(f"  accepted: pass={matrix['accepted', 'pass']} fail={matrix['accepted', 'fail']}")
    lines.append(f"  rejected: pass={matrix['rejected', 'pass']} fail={matrix['rejected', 'fail']}")
    lines.append(f"  unknown cost rule: accepted={sum(r['source_group']=='live_accepted' and r['cost_rule_pass'] is None for r in ghost_rows)} "
                 f"rejected={sum(r['source_group']=='live_gate_rejected' and r['cost_rule_pass'] is None for r in ghost_rows)}")
    if no_prices:
        lines.append("Expiry outcomes skipped (--no-prices).")
    else:
        lines.extend(outcome_summary(ghost_rows, "Ghost expiry outcomes by group and cost rule:"))
        lines.extend(outcome_summary(report_rows, "Live report expiry outcomes by group and cost rule:"))
    near_misses = [row for row in ghost_rows if row["source_group"] == "live_gate_rejected"
                   and row["cost_rule_pass"] is True]
    lines.append("10 lowest-cost cost-rule-pass Ghost live-gate-rejected rows:")
    for row in sorted(near_misses, key=lambda item: item["cost_pct_credit"])[:10]:
        lines.append(
            f"  {row['trade_date']} {row['ticker']} short={row['short_strike']:g} "
            f"long={row['long_strike']:g} expiry={row['expiry']} "
            f"credit_mid={row['credit_mid']:.4f} "
            f"cost_pct_credit={row['cost_pct_credit']:.4f} rr_mid={row['rr_mid']:.4f} "
            f"min_size={row['min_size']:g}"
        )
    return "\n".join(lines)


def self_test():
    expected = cost_metrics(4.85, 5.70, 3.55, 4.10, 165, 160, 1938, 253)
    assert math.isclose(expected["credit_mid"], 1.45)
    assert math.isclose(expected["credit_natural"], .75)
    assert math.isclose(expected["cost"], .70)
    assert math.isclose(expected["cost_pct_credit"], .4828, abs_tol=.0001)
    assert math.isclose(expected["max_loss_mid"], 3.55)
    assert math.isclose(expected["cost_pct_max_loss"], .1972, abs_tol=.0001)
    assert math.isclose(expected["rr_mid"], 2.448, abs_tol=.001)
    assert expected["min_size"] == 253 and expected["cost_rule_pass"] is True
    assert cost_metrics(2.4, 4.0, 1.0, 2.0, 165, 160, 100, 100)["cost_rule_pass"] is False
    unknown = cost_metrics(4.85, 5.70, 3.55, 4.10, 165, 160, None, 253)
    assert unknown["cost_rule_pass"] is None
    assert cost_metrics(1, .9, .2, .3, 165, 160) is None

    import tempfile

    with tempfile.TemporaryDirectory(prefix="near-miss-report-") as temp:
        root = Path(temp)
        raw_path = root / "raw.csv"
        fields = ["candidate_id", "trade_date", "ticker", "outcome", "reason", "input_json"]
        payload = {
            "short_bid": 4.85, "short_ask": 5.70, "long_bid": 3.55, "long_ask": 4.10,
            "resolved_short_strike": 165, "resolved_long_strike": 160,
            "resolved_expiry": "2026-10-01", "short_bid_size": 1938, "long_ask_size": 253,
        }
        failing_payload = dict(payload, short_bid_size=1, long_ask_size=1,
                               resolved_expiry="2026-10-02")
        open_payload = dict(payload, resolved_expiry=(date.today() + timedelta(days=20)).isoformat())
        with raw_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(candidate_id="a", trade_date="2026-09-01", ticker="AAA",
                                 outcome="rejected", reason="live_gate_spread_exceeds_0.30",
                                 input_json=json.dumps(payload)))
            writer.writerow(dict(candidate_id="a", outcome="duplicate_skipped"))
            writer.writerow(dict(candidate_id="b", trade_date="2026-09-01", ticker="BBB",
                                 outcome="accepted", reason="",
                                 input_json=json.dumps(failing_payload)))
            writer.writerow(dict(candidate_id="c", trade_date="2026-09-01", ticker="CCC",
                                 outcome="rejected", reason="live_gate_depth",
                                 input_json=json.dumps(open_payload)))
            writer.writerow(dict(candidate_id="d", trade_date="2026-09-01", ticker="DDD",
                                 outcome="rejected", reason="live_gate_depth",
                                 input_json=json.dumps(dict(payload, long_ask_size=None))))
            writer.writerow(dict(candidate_id="e", trade_date="2026-09-01", ticker="EEE",
                                 outcome="rejected", reason="live_gate_depth",
                                 input_json=json.dumps(payload)))
        ghost = read_ghost(raw_path)
        assert len(ghost) == 5
        assert len([row for row in ghost if row["candidate_id"] == "a"]) == 1

        report_path = root / "2026-09-03_bull-put-spread.md"
        report_line = ("NEAR_MISS_COST: AAA short 165 long 160 exp 2026-10-01 "
                       "credit_mid 1.45 natural 0.75 cost_pct_credit 1% "
                       "cost_pct_max_loss 1% rr_mid 99 min_size 253 cost_rule_pass NO")
        report_path.write_text(report_line + "\n" + report_line.replace("AAA", "AAA") + "\n",
                               encoding="utf-8")
        report_rows, disagreements = read_reports(str(root / "*_bull-put-spread.md"))
        assert len(report_rows) == 1 and disagreements == 1
        parsed = report_rows[0]
        assert math.isclose(parsed["cost_pct_credit"], .70 / 1.45)
        assert math.isclose(parsed["rr_mid"], 3.55 / 1.45)
        assert parsed["cost_rule_pass"] is True

        def fake_price(ticker, expiry):
            return {"AAA": 170, "BBB": 162, "DDD": 170, "EEE": 150}.get(ticker)

        tested = add_outcomes(ghost, price_fn=fake_price, today=date(2026, 10, 10))
        outcomes = {row["ticker"]: row["outcome"] for row in tested}
        assert math.isclose(outcomes["AAA"]["mid"]["pnl"], 1.45)
        assert math.isclose(outcomes["BBB"]["mid"]["pnl"], 1.45 - 3)
        assert math.isclose(outcomes["EEE"]["mid"]["pnl"], 1.45 - 5)
        assert tested[2]["open"] is True and tested[2]["outcome"] is None
    print("PASS: near_miss_report self-test")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ghost-raw", type=Path, default=DEFAULT_GHOST_RAW)
    parser.add_argument("--reports-glob", default=DEFAULT_REPORTS_GLOB)
    parser.add_argument("--since", help="Inclusive trade-date lower bound (YYYY-MM-DD)")
    parser.add_argument("--no-prices", action="store_true", help="Skip expiry price fetches/outcomes")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.since:
        date.fromisoformat(args.since)
    ghost = read_ghost(args.ghost_raw, args.since)
    reports, disagreements = read_reports(args.reports_glob, args.since)
    print(format_report(ghost, reports, disagreements, no_prices=args.no_prices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
