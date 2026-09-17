DRY-RUN SHADOW SYSTEM -- LOGS HYPOTHETICAL ENTRIES ONLY. THIS PROMPT MUST NEVER ATTEMPT TO PLACE, MODIFY, OR CANCEL A REAL ORDER, ALERT, OR WATCHLIST ENTRY. IT HAS NO SUCH CAPABILITY GRANTED AND MUST NEVER ASK FOR ONE.

You are running my automated Bull Put Spread Ghost System scan. Use ONLY the
four granted read-only IBKR tools, scoped scratch-file access, and the local
quote logger. This shadow measures displayed entry friction for v2 VRP research.

## Universe
Read `$ARIA_HOME/state/scratch/ghost_prescreen_<date>.json` using Read. The wrapper supplies
SCAN_DATE, RUN_ID, CODE_VERSION_HASH, GIT_HEAD, and GIT_DIRTY below. Use SCAN_DATE
for the filename and trade_date. Abort if the file is absent, run_id differs
from RUN_ID, or run_ts_utc is not from SCAN_DATE in America/New_York. Process
EVERY item in `candidates`, in its existing order. No signal recomputation.
Copy mode and signal_bar_date from the prescreen envelope when absent on an item.

## Scope
- **One candidate at a time. No batching or parallel IBKR calls.** Complete
  resolution, both snapshots, JSON write, and logger call before the next item.
  Prior batched history responses could not be attributed reliably to tickers.
- **Before concluding any granted tool is unavailable: call it.** A failed call
  means unavailable data for that candidate; keep unknown quote fields null.
  Never manufacture quotes, sizes, live status, or replacement legs. The one
  documented exception is the `quote_ts_is_estimated` capture-time fallback
  in step 4 below (used only because `bid_ask` carries no timestamp of its
  own) — always explicit and flagged, never silent, and never used for any
  other field.
- Exact derived contracts only. No snapping, nearby expiry, alternate width,
  strike search, liquidity rejection, sizing, or trading directives.

## Per-Candidate Processing Flow
1. Resolve the underlying with `search_contracts` if its contract id is absent.
   Confirm ticker identity. Call `get_price_snapshot` on that underlying and
   record underlying_spot from its response. If unavailable, use null.
2. Call `get_option_parameters` for that underlying's real listed expirations.
3. Call `get_option_data` bounded around derived_long_strike and
   derived_short_strike, for derived_expiry. Confirm both exact listed PUT
   strikes and the exact expiry, including each leg's underlying identity.
   Normalize date formatting only; never change the date or strike value.
   If expiry is not listed, do not query a substitute expiration. If either
   leg/expiry cannot be verified, set resolution_status=`no_exact_match` and
   ALL three resolved fields null. Still write JSON and call the logger.
4. For an exact match, set resolution_status=`exact` and copy the actual listed
   strikes/expiry into resolved fields. Call `get_price_snapshot` on the short
   PUT contract id, then separately on the long PUT contract id. Request
   `market_data_names: ["bid_ask", "option_open_interest", "top_status"]`
   and quote sizes through the tool's supported fields — `top_status` is the
   actual field that carries live/delayed/frozen status (confirmed
   empirically 2026-09-16: `bid_ask` alone never populates a usable live/
   delayed classifier, leaving `market_data_type` permanently null and every
   candidate rejected on `market_data_not_live_or_missing`). Record each
   response's OWN quote
   timestamp as timezone-aware ISO8601 UTC when the response actually carries
   one. **The `bid_ask` field does not include a per-quote timestamp** (only
   `last` does, confirmed empirically 2026-09-16) — for that case, and only
   that case, record your own current wall-clock time (timezone-aware
   ISO8601 UTC), taken immediately after receiving that leg's response, and
   set `quote_ts_is_estimated: true`. If a genuine connector timestamp ever
   is available, use it and set `quote_ts_is_estimated: false`. Never reuse a
   timestamp across legs either way. Copy bid/ask sizes, not open interest,
   into size fields. Missing sizes stay null. Preserve raw bid/ask numbers
   even if crossed, zero, or negative.
   Set `market_data_type` from each leg's own `top_status` value (REALTIME,
   DELAYED, FROZEN, FROZEN_DELAYED, REJECT) — `market_data_type` may be
   `live` only if BOTH legs' `top_status` is explicitly REALTIME (matches
   the logger's live-value allowlist case-insensitively; IBKR type 1 also
   means live if that's what the field returns instead). Delayed/frozen/
   mixed/unknown data must retain that status or null. Never infer live
   status from plausible prices, and never set `live` just because
   `top_status` was absent from the response — absent is unknown, not live.
5. Using Write (covered by the scoped Edit grant), write the full object below
   to `$ARIA_HOME/state/scratch/ghost_quote_<TICKER>_<candidate_id>.json`. Preserve candidate
   values; use envelope/wrapper metadata as specified. No omitted keys. Unknown
   quote data stays null, including all leg quotes on resolution failure.
   in_rth_claimed is your claim or null; the logger independently computes RTH.
6. Immediately run Bash:
   `python3 $ARIA_HOME/scripts/ghost/ghost_fill_logger.py --input $ARIA_HOME/state/scratch/ghost_quote_<TICKER>_<candidate_id>.json`
   Use its returned outcome as authoritative. Do not filter before logging.
   On a logger failure, abort; never claim that candidate was processed.

## Quote JSON Schema
```json
{
  "candidate_id": "<candidate.candidate_id>",
  "run_id": "<RUN_ID>",
  "trade_date": "<SCAN_DATE>",
  "signal_bar_date": "<prescreen.signal_bar_date>",
  "mode": "<prescreen.mode>",
  "ticker": "<candidate.ticker>",
  "signal_close": 0.0,
  "ma150": 0.0,
  "vrp_ratio": 0.0,
  "iv_current": 0.0,
  "hv_current": 0.0,
  "iv_as_of_date": "<candidate.iv_as_of_date>",
  "gex_regime": "<candidate.gex_regime>",
  "gex_percentile": 0.0,
  "gex_data_available": false,
  "gex_as_of": "<candidate.gex_as_of>",
  "derived_short_strike": 0.0,
  "derived_long_strike": 0.0,
  "derived_expiry": "<candidate.derived_expiry>",
  "resolved_short_strike": null,
  "resolved_long_strike": null,
  "resolved_expiry": null,
  "resolution_status": "no_exact_match",
  "underlying_spot": null,
  "short_bid": null,
  "short_ask": null,
  "short_bid_size": null,
  "short_ask_size": null,
  "short_quote_ts_utc": null,
  "long_bid": null,
  "long_ask": null,
  "long_bid_size": null,
  "long_ask_size": null,
  "long_quote_ts_utc": null,
  "quote_ts_is_estimated": false,
  "market_data_type": null,
  "in_rth_claimed": null,
  "code_version_hash": "<CODE_VERSION_HASH>",
  "git_head": "<GIT_HEAD>",
  "git_dirty": false
}
```
Numeric/boolean placeholders above are not defaults: copy signal/GEX/derived
values from the candidate and git_dirty from GIT_DIRTY. Replace resolved/quote
nulls only with verified observations. Preserve unavailable prescreen values.

## Output
End with exactly one summary line, nothing else:
`PROCESSED: <count> ACCEPTED: <count> REJECTED: <count>`
Processed counts each DISTINCT candidate from the prescreen output that received at
least one logger outcome this run — count a candidate once even if a retry needed more
than one logger call for it, never the raw call count. Accepted and rejected count only
candidates whose FINAL outcome this run was accepted/rejected respectively; a candidate
whose last call was duplicate_skipped does not count toward either. No recommendations
or additional commentary after the summary.
