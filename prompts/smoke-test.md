You are validating the ARIA daily-scan automation PLUMBING (smoke test — NOT the full scan).

Do this briefly, in order. Use only the read-only IBKR tools and the local signal script.
1. Call `search_contracts` for AAPL (security_type STK) and note the US primary-listing `contract_id`.
   If the call fails or the IBKR tools are unavailable, say so and stop.
2. Call `get_price_snapshot` on that contract_id and report the last price.
3. Call `get_price_history` on that contract_id for at least 155 daily bars, pipe the bars through
   `python3 scripts/compute_signal.py` as the scan prompt describes, and report the returned close and
   `above_ma150` value.
4. Print a 4-line OK summary: auth ok? IBKR connector attached? price snapshot readable? signal script ran?

Then, on its own final line, emit exactly the following ONLY if every check above succeeded
(otherwise omit it so the watchdog sees an incomplete run):
SCREENER_CONSTITUENTS: AAPL

Keep under ~180 words. Do NOT run the full per-ticker scan.
