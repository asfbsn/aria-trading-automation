#!/usr/bin/env python3
"""
Rebuilds data/universe.csv -- the static candidate universe that replaces the
live TradingView "Adi option swing 2.0" screener DOM read for the daily scan.

Why this exists: the screener's own filters are Region=US, Mkt cap 10B-5T USD,
and an "Index" filter checking ~60 major US indices (S&P sectors, Russell
1000/2000/3000, NASDAQ 100/Composite, Dow, PHLX sectors, KBW, etc.) -- which,
because Russell 3000 alone covers ~98% of US market cap, is not a narrow
membership test. The real constraint is the market-cap band. The remaining
conditions (RSI(20)<50, price near MA150 support) are signal/technical checks
already computed independently per-ticker by compute_signal.py from IBKR
bars -- this script only replaces the universe/membership step.

Source: iShares Russell 1000 ETF (IWB) holdings CSV -- free, current, updated
daily by BlackRock. Russell 1000 = the ~1000 largest US companies by
float-adjusted market cap, so it's a reasonable proxy for "large+mid cap US
stock membership" without needing a paid fundamentals API.

Market cap isn't a column in the holdings file (only the fund's own dollar
position per holding is). Since Russell 1000 is float-cap-weighted, each
holding's Weight (%) is proportional to its share of the index's total
market cap -- so approx_mktcap = weight% / 100 * RUSSELL_1000_TOTAL_MKTCAP.
RUSSELL_1000_TOTAL_MKTCAP must be updated by hand at each refresh (FTSE
Russell publishes it at each semi-annual reconstitution, see
https://www.lseg.com/en/media-centre/press-releases/ftse-russell/ -- search
"Russell US Indexes Reconstitution"). This is an approximation, not exact
market cap -- fine for a wide $10B-$5T band, not precise at the boundary.

Refresh cadence: run this every 1-3 months. Russell 1000 itself only
reconstitutes semi-annually (main in June, review in November), and the
wide market-cap band tolerates weight/price drift between refreshes.

Usage: python3 scripts/refresh_universe.py [--mktcap-total <dollars>]
Writes: data/universe.csv (ticker, sector, approx_mktcap_usd)
"""
import argparse
import csv
import io
import sys
import urllib.request
from pathlib import Path

IWB_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239707/"
    "ishares-russell-1000-etf/latest-holdings.csv"
)

# FTSE Russell's published total market cap for the Russell 1000 index at
# its most recent reconstitution. Update this each time you re-run the
# refresh with fresher data (search "Russell US Indexes Reconstitution").
DEFAULT_RUSSELL_1000_TOTAL_MKTCAP = 72.1e12  # June 2026 reconstitution, as of Apr 30 2026

MKTCAP_FLOOR = 10e9   # matches the live screener's "Mkt cap 10B to 5T USD" filter
MKTCAP_CEILING = 5e12

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "universe.csv"


def fetch_holdings_csv() -> str:
    req = urllib.request.Request(IWB_HOLDINGS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_holdings(raw_csv: str, total_mktcap: float):
    lines = raw_csv.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    reader = csv.DictReader(lines[start:])
    rows = []
    for row in reader:
        ticker = (row.get("Ticker") or "").strip()
        if not ticker or ticker == "-":
            continue
        try:
            weight = float(row["Weight (%)"])
        except (KeyError, ValueError):
            continue
        approx_mktcap = weight / 100.0 * total_mktcap
        if MKTCAP_FLOOR <= approx_mktcap <= MKTCAP_CEILING:
            rows.append({
                "ticker": ticker,
                "sector": (row.get("Sector") or "").strip(),
                "approx_mktcap_usd": round(approx_mktcap),
            })
    rows.sort(key=lambda r: -r["approx_mktcap_usd"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mktcap-total", type=float, default=DEFAULT_RUSSELL_1000_TOTAL_MKTCAP,
                     help="Russell 1000 total market cap in USD (update from FTSE Russell's latest reconstitution)")
    args = ap.parse_args()

    raw = fetch_holdings_csv()
    rows = parse_holdings(raw, args.mktcap_total)

    if not rows:
        print("ERROR: parsed 0 constituents -- IWB CSV format may have changed.", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "sector", "approx_mktcap_usd"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} tickers to {OUTPUT_PATH}")
    print(f"Top 5: {', '.join(r['ticker'] for r in rows[:5])}")


if __name__ == "__main__":
    main()
