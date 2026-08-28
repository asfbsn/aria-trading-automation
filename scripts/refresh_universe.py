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

Sources (4-source union):
1. iShares Core S&P 500 ETF (IVV) holdings CSV
2. iShares Core S&P Mid-Cap ETF (IJH) holdings CSV
3. iShares Russell 3000 ETF (IWV) holdings CSV
4. Nasdaq-100 constituent list scraped from slickcharts.com

Tickers are unioned across all 4 sources. Sector strings are assigned using
source priority: IVV > IJH > IWV > Nasdaq-100 (with yfinance assetProfile fallback).

Real market cap is fetched for all merged tickers via yfinance / Yahoo Finance,
replacing earlier ETF weight approximations. Constituents are filtered to the
$10B-$5T USD market-cap band.

Refresh cadence: run this every 1-3 months.

Usage: python3 scripts/refresh_universe.py
Writes: data/universe.csv (ticker, sector, approx_mktcap_usd)
"""
import argparse
import csv
import io
import sys
import urllib.request
from pathlib import Path

import pandas as pd
import yfinance as yf
from curl_cffi import requests

SOURCES = [
    ("IVV", "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv"),
    ("IJH", "https://www.ishares.com/us/products/239763/ishares-core-s-p-mid-cap-etf/latest-holdings.csv"),
    ("IWV", "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/latest-holdings.csv"),
]

NASDAQ100_URL = "https://www.slickcharts.com/nasdaq100"

MKTCAP_FLOOR = 10e9   # matches the live screener's "Mkt cap 10B to 5T USD" filter
MKTCAP_CEILING = 5e12

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "universe.csv"


def fetch_ishares_holdings(url: str) -> list[tuple[str, str]]:
    """Fetch and parse an iShares ETF holdings CSV returning (ticker, sector) pairs."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8", errors="replace")

    lines = content.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    reader = csv.DictReader(lines[start:])
    results = []
    for row in reader:
        ticker = (row.get("Ticker") or "").strip()
        if not ticker or ticker == "-":
            continue
        sector = (row.get("Sector") or "").strip()
        results.append((ticker, sector))
    return results


def fetch_nasdaq100_constituents() -> list[tuple[str, str]]:
    """Scrape Nasdaq-100 constituents from slickcharts.com returning (ticker, sector) pairs."""
    req = urllib.request.Request(
        NASDAQ100_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    dfs = pd.read_html(io.StringIO(html))
    if not dfs:
        raise ValueError("No tables found on slickcharts Nasdaq-100 page")

    table = dfs[0]
    results = []
    for sym in table["Symbol"]:
        ticker = str(sym).strip()
        if not ticker or ticker == "-":
            continue
        results.append((ticker, ""))
    return results


def to_yf_symbol(sym: str) -> str:
    """Map ETF constituent symbols to Yahoo Finance compatible symbols."""
    if sym == "BRKB":
        return "BRK-B"
    if sym == "BFB":
        return "BF-B"
    return sym.replace(".", "-")


def init_yahoo_session() -> tuple[requests.Session, str]:
    """Initialize a curl_cffi session and retrieve Yahoo Finance crumb."""
    session = requests.Session(impersonate="chrome")
    session.get("https://fc.yahoo.com", timeout=30)
    crumb_resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=30)
    crumb = crumb_resp.text.strip()
    return session, crumb


def fetch_market_caps_and_sectors(
    ticker_sectors: dict[str, str],
) -> tuple[dict[str, float], dict[str, str], int]:
    """
    Fetch real market cap and resolve missing sectors via Yahoo Finance.
    Returns (market_caps, updated_ticker_sectors, dropped_count).
    """
    session, crumb = init_yahoo_session()

    # Configure yfinance data singleton to share crumb and session
    import yfinance.data
    yfinance.data.YfData._get_cookie_and_crumb = lambda self, timeout=30: (crumb, "basic")
    yfinance.data.YfData._get_cookie_basic = lambda self, timeout=30: True
    yfinance.data.YfData._get_crumb_basic = lambda self, timeout=30: crumb

    all_tickers = list(ticker_sectors.keys())
    market_caps: dict[str, float] = {}

    # 1. Fast batch quote lookup (/v7/finance/quote) in chunks of 100
    chunk_size = 100
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        yf_chunk = [to_yf_symbol(t) for t in chunk]
        yf_to_orig = {to_yf_symbol(t): t for t in chunk}

        url = f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={','.join(yf_chunk)}&crumb={crumb}"
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("quoteResponse", {}).get("result", [])
                for q in results:
                    sym = q.get("symbol")
                    orig_t = yf_to_orig.get(sym, sym)
                    mcap = q.get("marketCap")
                    if mcap is not None:
                        market_caps[orig_t] = float(mcap)
        except Exception as e:
            print(f"WARNING: Batch quote chunk {i} failed: {e}", file=sys.stderr)

    # 2. Fallback for tickers missing marketCap from batch quotes
    missing_mktcap = [t for t in all_tickers if t not in market_caps]
    for t in missing_mktcap:
        yf_sym = to_yf_symbol(t)
        try:
            ticker_obj = yf.Ticker(yf_sym, session=session)
            mc = ticker_obj.fast_info.market_cap
            if mc is not None and mc > 0:
                market_caps[t] = float(mc)
        except Exception:
            pass

    # 3. Resolve missing sectors for tickers found only in sources without sector
    missing_sector = [t for t, s in ticker_sectors.items() if not s and t in market_caps]
    for t in missing_sector:
        yf_sym = to_yf_symbol(t)
        try:
            url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yf_sym}?modules=assetProfile&crumb={crumb}"
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                res = resp.json().get("quoteSummary", {}).get("result")
                if res:
                    sec = res[0].get("assetProfile", {}).get("sector", "")
                    if sec:
                        ticker_sectors[t] = sec
        except Exception:
            pass

    # 4. Identify dropped tickers
    dropped_count = 0
    for t in all_tickers:
        if t not in market_caps:
            dropped_count += 1
            print(f"WARNING: Market cap lookup failed for {t} -- dropping ticker.", file=sys.stderr)

    return market_caps, ticker_sectors, dropped_count


def build_universe() -> tuple[list[dict], int]:
    """Fetch 4 sources, union tickers with sector priority, fetch market caps, and filter."""
    ticker_sectors: dict[str, str] = {}

    # Sources 1-3: iShares ETFs (IVV, IJH, IWV in order)
    for name, url in SOURCES:
        try:
            pairs = fetch_ishares_holdings(url)
            for t, sec in pairs:
                if t not in ticker_sectors:
                    ticker_sectors[t] = sec
                elif not ticker_sectors[t] and sec:
                    ticker_sectors[t] = sec
        except Exception as e:
            print(f"ERROR: Failed to fetch {name} holdings from {url}: {e}", file=sys.stderr)
            raise

    # Source 4: Nasdaq-100
    try:
        ndx_pairs = fetch_nasdaq100_constituents()
        for t, sec in ndx_pairs:
            if t not in ticker_sectors:
                ticker_sectors[t] = sec
    except Exception as e:
        print(f"ERROR: Failed to fetch Nasdaq-100 constituents: {e}", file=sys.stderr)
        raise

    print(f"Union of 4 sources contains {len(ticker_sectors)} unique raw tickers.")

    market_caps, updated_ticker_sectors, dropped_count = fetch_market_caps_and_sectors(ticker_sectors)

    # Filter to $10B-$5T market-cap band
    rows = []
    for t, sec in updated_ticker_sectors.items():
        if t not in market_caps:
            continue
        mc = market_caps[t]
        if MKTCAP_FLOOR <= mc <= MKTCAP_CEILING:
            rows.append({
                "ticker": t,
                "sector": sec,
                "approx_mktcap_usd": round(mc),
            })

    rows.sort(key=lambda r: -r["approx_mktcap_usd"])
    return rows, dropped_count


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild data/universe.csv from 4-source union with real market caps"
    )
    parser.parse_args()

    rows, dropped_count = build_universe()

    if not rows:
        print("ERROR: parsed 0 constituents -- check source URLs and market cap fetcher.", file=sys.stderr)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "sector", "approx_mktcap_usd"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} tickers to {OUTPUT_PATH}")
    print(f"Total dropped due to failed market cap lookup: {dropped_count}")
    print(f"Top 5 by market cap: {', '.join(r['ticker'] for r in rows[:5])}")


if __name__ == "__main__":
    main()

