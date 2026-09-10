#!/usr/bin/env python3
"""Tier-1 screener parity audit -- standalone, on-demand, manual-trigger only.

NEVER wired into daily-scan.sh or the cron schedule (by design -- this is a
periodic sanity check the user runs by hand, not a production dependency).

WHAT THIS DOES: diffs our prescreen.py shortlist against a captured snapshot
of Adi's real "Filters 5" TradingView screener population, bucketed by root
cause, so a mismatch says *why*, not just *that*.

WHAT THIS DOES NOT DO: fetch that ground truth itself. TradingView's filter
values live behind custom (non-<input>) widgets and the screener's results
table is virtualized (only ~30-40 rows exist in the DOM at once) -- neither
is capturable from a plain Python process. Only an interactive agent session
driving TradingView Desktop via the tradingview-bridge MCP (CDP) can reliably
extract it. See CAPTURE.md in this directory for that manual procedure.
This script's job starts *after* that capture: consume its output, diff,
report. Point --ground-truth at whatever that step produced.

Adi's real 5 filters (confirmed live via the filter panel, 2026-09-10 --
scripts/audit/fixtures/filters5_readout.txt has the raw capture):
  1. Mkt cap: 10B-5T USD                         (universe eligibility)
  2. RSI, 20: 50                                 (paired with the dashboard's
                                                   own "RSI below 50" label ->
                                                   RSI(20) < 50)
  3. SMA, 50: price 0%-10% BELOW SMA50           (our near_ma50_pullback)
  4. SMA, 150: price 0%-10% ABOVE SMA150         (our near_ma150_support)
  5. Vol: above average, boolean toggle          (our volume_above_avg)

Our tier-1 boolean is the AND of exactly the 4 checks that map onto filters
2-5 (market cap isn't a per-ticker check -- it's enforced by universe.csv
membership, checked separately below):

    rsi_below_50 AND near_ma50_pullback AND near_ma150_support AND volume_above_avg

Deliberately excludes rsi_rising, bullish_candle, above_ma150 -- confirmed
2026-09-09 (live Pine-table reads) as tier-2-only (Adi's on-chart dashboard),
never part of the screener. Diffing against entry_confirmed (the tier-2 gate)
would flood the report with false "in Adi's, missing from ours" mismatches.

Usage:
    python3 audit_tier1.py --ground-truth fixtures/adi_screener_2026-09-10.json
    python3 audit_tier1.py --ground-truth <file> --prescreen state/scratch/prescreen_2026-09-09.json
    python3 audit_tier1.py --ground-truth <file> --refresh
        --refresh re-runs prescreen.py fresh into logs/audit/ (NEVER into
        state/scratch/, which daily-scan.sh reads) -- costs ~750 yfinance
        downloads, avoid running this right before/during the 19:00 scan.

Ground-truth fixture format (JSON):
    {
      "captured_at": "2026-09-10T21:15:00+03:00",
      "method": "dom_scroll_scrape" | "network_capture" | "other -- describe it",
      "market_open": false,
      "symbols": ["SYY", "BBIO", "NASDAQ:AAPL", "NYSE:BRK.B", ...]
    }
A bare JSON list of symbols also works; captured_at/method/market_open just
won't appear in the report header.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

TIER1_KEYS = ("rsi_below_50", "near_ma50_pullback", "near_ma150_support", "volume_above_avg")
TIER2_ONLY_KEYS = ("rsi_rising", "bullish_candle", "above_ma150")


def normalize_symbol(sym: str) -> str:
    """'NASDAQ:AAPL' / 'NYSE:BRK.B' / 'BRK-B' -> 'AAPL' / 'BRKB' / 'BRKB'.

    Matches universe.csv's no-punctuation ticker style so both sides diff
    on the same key. Does NOT fix the separate, pre-existing universe.csv
    issue where yfinance rejects 'BRKB'/'BFB' outright (that's a data-source
    bug, out of scope here -- it will surface below as a real
    no_prescreen_data bucket entry, which is correct: it genuinely has no
    data, for a reason worth knowing, not a bug in this script).
    """
    if ":" in sym:
        sym = sym.split(":", 1)[1]
    return sym.replace(".", "").replace("-", "").upper()


def load_ground_truth(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        symbols, meta = data, {}
    else:
        symbols, meta = data["symbols"], data
    normalized = {}
    for s in symbols:
        n = normalize_symbol(s)
        normalized.setdefault(n, s)  # keep first original spelling seen, for display
    return normalized, meta


def load_universe(path):
    universe = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            universe[normalize_symbol(row["ticker"])] = row["ticker"]
    return universe


def run_prescreen_refresh(out_path):
    script = os.path.join(REPO_ROOT, "scripts", "prescreen.py")
    print(f"[audit_tier1] --refresh: running prescreen.py fresh -> {out_path}")
    print("[audit_tier1] (NOT touching state/scratch/ -- daily-scan.sh never sees this file)")
    subprocess.run([sys.executable, script, "--output", out_path], check=True, cwd=REPO_ROOT)


def latest_prescreen_file():
    scratch_dir = os.path.join(REPO_ROOT, "state", "scratch")
    if not os.path.isdir(scratch_dir):
        candidates = []
    else:
        candidates = sorted(
            f for f in os.listdir(scratch_dir)
            if f.startswith("prescreen_") and f.endswith(".json")
        )
    if not candidates:
        sys.exit(
            "No prescreen_*.json found in state/scratch/. "
            "Run with --refresh, or point --prescreen at an existing file."
        )
    return os.path.join(scratch_dir, candidates[-1])


def tier1_pass(checks: dict) -> bool:
    return all(checks.get(k, False) for k in TIER1_KEYS)


def market_open_now_utc() -> bool:
    """Rough US-equity-hours check (9:30-16:00 ET, weekdays). Not holiday-aware --
    informational context for the report, not a gate."""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--ground-truth", required=True,
        help="Path to a captured Adi-screener symbol-list JSON fixture (see CAPTURE.md)",
    )
    ap.add_argument(
        "--prescreen", default=None,
        help="Path to a prescreen_*.json (default: latest in state/scratch/, READ-ONLY)",
    )
    ap.add_argument(
        "--refresh", action="store_true",
        help="Re-run prescreen.py fresh into logs/audit/ instead of reading an existing file "
             "(~750 yfinance downloads -- avoid near the 19:00 production scan)",
    )
    ap.add_argument(
        "--universe", default=os.path.join(REPO_ROOT, "data", "universe.csv"),
    )
    ap.add_argument(
        "--out-dir", default=os.path.join(REPO_ROOT, "logs", "audit"),
    )
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    run_ts = datetime.now(timezone.utc)

    gt_symbols, gt_meta = load_ground_truth(args.ground_truth)
    universe = load_universe(args.universe)

    if args.refresh:
        refresh_path = os.path.join(
            args.out_dir, f"prescreen_refresh_{run_ts.strftime('%Y%m%d_%H%M%S')}.json"
        )
        run_prescreen_refresh(refresh_path)
        prescreen_path = refresh_path
    elif args.prescreen:
        prescreen_path = args.prescreen
    else:
        prescreen_path = latest_prescreen_file()
        print(f"[audit_tier1] Reading latest existing prescreen output (READ-ONLY): {prescreen_path}")

    with open(prescreen_path) as f:
        prescreen = json.load(f)
    per_ticker = prescreen.get("per_ticker", {})

    per_ticker_norm = {}
    for ticker, rec in per_ticker.items():
        per_ticker_norm[normalize_symbol(ticker)] = (ticker, rec)

    our_tier1 = set()
    for norm, (ticker, rec) in per_ticker_norm.items():
        if tier1_pass(rec.get("checks", {})):
            our_tier1.add(norm)

    gt_set = set(gt_symbols.keys())
    matches = gt_set & our_tier1
    missing_from_ours = gt_set - our_tier1   # Adi has it, we don't
    extra_in_ours = our_tier1 - gt_set       # we have it, Adi doesn't

    buckets = {"not_in_universe": [], "no_prescreen_data": [], "checks_disagree": []}
    for norm in sorted(missing_from_ours):
        display = gt_symbols[norm]
        if norm not in universe:
            buckets["not_in_universe"].append(display)
        elif norm not in per_ticker_norm:
            buckets["no_prescreen_data"].append(display)
        else:
            ticker, rec = per_ticker_norm[norm]
            checks = rec.get("checks", {})
            failing = [k for k in TIER1_KEYS if not checks.get(k, False)]
            buckets["checks_disagree"].append(
                {"ticker": ticker, "failing_tier1_checks": failing,
                 "checks": {k: checks.get(k) for k in TIER1_KEYS}}
            )

    extra_detail = []
    for norm in sorted(extra_in_ours):
        ticker, rec = per_ticker_norm[norm]
        in_gt_universe_but_not_matched = norm not in gt_set
        extra_detail.append(ticker)

    report_lines = []
    report_lines.append("# Tier-1 Screener Parity Audit")
    report_lines.append(f"Run (UTC): {run_ts.isoformat()}")
    report_lines.append(f"Ground truth file: {args.ground_truth}")
    if gt_meta.get("captured_at"):
        report_lines.append(f"Ground truth captured at: {gt_meta['captured_at']}")
    if gt_meta.get("method"):
        report_lines.append(f"Ground truth capture method: {gt_meta['method']}")
    if "market_open" in gt_meta:
        report_lines.append(f"Market open at capture time: {gt_meta['market_open']}")
    try:
        report_lines.append(f"Market open now: {market_open_now_utc()}")
    except Exception:
        pass
    report_lines.append(f"Prescreen source: {prescreen_path} (date: {prescreen.get('date', '?')})")
    report_lines.append("")
    report_lines.append(
        "Note: comparing against the 4-check tier-1 boolean "
        "(rsi_below_50, near_ma50_pullback, near_ma150_support, volume_above_avg), "
        "NOT entry_confirmed (tier-2, adds rsi_rising/above_ma150/bullish_candle -- "
        "not part of the real screener)."
    )
    report_lines.append("")
    report_lines.append(f"Adi's screener (ground truth): {len(gt_set)} symbols")
    report_lines.append(f"Our tier-1 pass set: {len(our_tier1)} symbols")
    report_lines.append(f"Matches: {len(matches)}")
    report_lines.append(f"In Adi's, missing from ours: {len(missing_from_ours)}")
    report_lines.append(f"In ours, missing from Adi's: {len(extra_in_ours)}")
    report_lines.append("")

    report_lines.append("## In Adi's screener, missing from ours -- by cause")
    report_lines.append(f"### Not in our universe.csv at all ({len(buckets['not_in_universe'])})")
    report_lines.append(", ".join(buckets["not_in_universe"]) or "(none)")
    report_lines.append("")
    report_lines.append(
        f"### In universe.csv but prescreen had no data for it ({len(buckets['no_prescreen_data'])})"
    )
    report_lines.append(", ".join(buckets["no_prescreen_data"]) or "(none)")
    report_lines.append("")
    report_lines.append(
        f"### Data present, our checks disagree ({len(buckets['checks_disagree'])})"
    )
    for entry in buckets["checks_disagree"]:
        report_lines.append(
            f"- {entry['ticker']}: fails {entry['failing_tier1_checks']} -> {entry['checks']}"
        )
    if not buckets["checks_disagree"]:
        report_lines.append("(none)")
    report_lines.append("")

    report_lines.append(f"## In ours, missing from Adi's ({len(extra_in_ours)})")
    report_lines.append(", ".join(extra_detail) or "(none)")
    report_lines.append("")

    report = "\n".join(report_lines)
    print(report)

    out_path = os.path.join(args.out_dir, f"tier1_audit_{run_ts.strftime('%Y%m%d_%H%M%S')}.md")
    with open(out_path, "w") as f:
        f.write(report + "\n")
    print(f"\n[audit_tier1] Report written to {out_path}")


if __name__ == "__main__":
    main()
