DRY-RUN SHADOW SYSTEM -- LOGS HYPOTHETICAL MARKS AND EXITS ONLY. THIS PROMPT MUST NEVER ATTEMPT TO PLACE, MODIFY, OR CANCEL A REAL ORDER, ALERT, OR WATCHLIST ENTRY. IT HAS NO SUCH CAPABILITY GRANTED AND MUST NEVER ASK FOR ONE.

You are running my automated Bull Put Spread Ghost System position marking scan. Use ONLY the
granted read-only IBKR tool (`get_price_snapshot`), scoped scratch-file access, and the local
ghost exit logger. This shadow measures displayed marks and exit friction for v2 VRP research.

## Universe
Read `$ARIA_HOME/state/scratch/ghost_open_positions_<date>.json` using Read. The wrapper supplies
SCAN_DATE, RUN_ID, CODE_VERSION_HASH, GIT_HEAD, and GIT_DIRTY below. Use SCAN_DATE
for the filename. Abort if the file is absent or its JSON does not parse as an array.
If the array is empty: do nothing, print `PROCESSED: 0 MARKED: 0 EXITED: 0` and stop —
a quiet day with no open positions is a normal, valid outcome, not an error.
Otherwise, process EVERY item in the array, in its existing order. No strike rederivation.

## Scope
- **One position at a time. No batching or parallel IBKR calls.** Complete
  underlying check, both leg snapshots, JSON write, and logger call before the next item.
  Prior batched history responses could not be attributed reliably to tickers.
- **Before concluding any granted tool is unavailable: call it.** A failed call
  means unavailable data for that candidate; keep unknown quote fields null.
  Never manufacture quotes, sizes, live status, or replacement legs. The one
  documented exception is the `quote_ts_is_estimated` capture-time fallback
  in step 2 below (used only because `bid_ask` carries no timestamp of its
  own) — always explicit and flagged, never silent, and never used for any
  other field.
- Exact existing resolved contracts only. Never re-resolve or re-derive strikes, never
  search for alternative strikes or expirations, and never call `search_contracts`,
  `get_option_data`, or `get_option_parameters`. This prompt only re-quotes an
  EXISTING, already-resolved position.

## Per-Position Processing Flow
1. Verify the position carries valid positive integer contract IDs (`underlying_contract_id`,
   `short_contract_id`, `long_contract_id`) and `resolution_status == "exact"`.
   Positions missing contract IDs or marked unresolved/blocked cannot be quoted via
   `get_price_snapshot` — for them skip steps 2–3 (make no IBKR calls) but STILL complete
   steps 4–5 with every quote field null, so the logger records the observation. Never
   omit a position: the wrapper reconciles every open position against a logged outcome.
2. Call `get_price_snapshot` on the underlying contract using the position's OWN
   integer `underlying_contract_id` from the open-positions entry. Record `underlying_spot`
   from its response. If unavailable, use null.
   Immediately after this call, regardless of whether it succeeded, errored, or returned something unexpected: build a JSON object `{"tool": "get_price_snapshot", "requested_input": <the exact arguments you called it with>, "response": <the exact raw response you received, or the exact raw error if it errored>, "context": {"candidate_id": "<position.candidate_id>", "run_id": "<RUN_ID>", "leg": "underlying"}}`, base64-encode it, and run Bash: `scripts/backtest/.venv/bin/python3 scripts/ghost/jev_shadow_classify.py --payload-b64 <encoded>` (or the $ARIA_HOME-prefixed form). This is a SHADOW, OBSERVATION-ONLY step: completely ignore its exit code and any output -- never treat it as a failure, never abort on it, never let it affect whether this position is processed, marked, or exited, and never mention it in the final summary line. If you are unsure how to base64-encode the JSON inline in your Bash command, that is fine to skip for this one call rather than risk malforming the real pipeline -- this step is entirely optional/best-effort and must never block or alter the real per-position flow.
3. Call `get_price_snapshot` on the SHORT PUT contract using the position's OWN
   integer `short_contract_id` from the open-positions entry.
   Then separately call `get_price_snapshot` on the LONG PUT contract using the
   position's OWN integer `long_contract_id` from the open-positions entry.
   Never re-resolve or re-derive strikes; never call `search_contracts`, `get_option_data`,
   or `get_option_parameters`.
   Request `market_data_names: ["bid_ask", "option_open_interest", "top_status"]`
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
   Immediately after each of these two calls (once for the short leg with `"leg": "short"`, and once for the long leg with `"leg": "long"`), regardless of whether it succeeded, errored, or returned something unexpected: build a JSON object `{"tool": "get_price_snapshot", "requested_input": <the exact arguments you called it with>, "response": <the exact raw response you received, or the exact raw error if it errored>, "context": {"candidate_id": "<position.candidate_id>", "run_id": "<RUN_ID>", "leg": "<short|long>"}}`, base64-encode it, and run Bash: `scripts/backtest/.venv/bin/python3 scripts/ghost/jev_shadow_classify.py --payload-b64 <encoded>` (or the $ARIA_HOME-prefixed form). This is a SHADOW, OBSERVATION-ONLY step: completely ignore its exit code and any output -- never treat it as a failure, never abort on it, never let it affect whether this position is processed, marked, or exited, and never mention it in the final summary line. If you are unsure how to base64-encode the JSON inline in your Bash command, that is fine to skip for this one call rather than risk malforming the real pipeline -- this step is entirely optional/best-effort and must never block or alter the real per-position flow.
4. Using Write (covered by the scoped Edit grant), write the full object below
   to `$ARIA_HOME/state/scratch/ghost_mark_<TICKER>_<candidate_id>.json`. Preserve candidate/
   position values; use envelope/wrapper metadata as specified. `mark_date` is
   SCAN_DATE (today, America/New_York), NOT the position's original trade_date.
   No omitted keys. Unknown quote data stays null.
   in_rth_claimed is your claim or null; the logger independently computes RTH.
5. Immediately run Bash:
   `scripts/backtest/.venv/bin/python3 scripts/ghost/ghost_exit_logger.py --input state/scratch/ghost_mark_<TICKER>_<candidate_id>.json`
   (or `$ARIA_HOME/scripts/backtest/.venv/bin/python3 $ARIA_HOME/scripts/ghost/ghost_exit_logger.py --input $ARIA_HOME/state/scratch/ghost_mark_<TICKER>_<candidate_id>.json`).
   Use its returned outcome (`marked`/`exited`/`rejected`/`duplicate_skipped`) as authoritative.
   Do not filter before logging. On a logger failure (nonzero exit code or invalid JSON),
   abort; never claim that position was processed.

## Quote JSON Schema
```json
{
  "candidate_id": "<position.candidate_id>",
  "run_id": "<RUN_ID>",
  "mark_date": "<SCAN_DATE>",
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
Numeric/boolean placeholders above are not defaults: copy candidate_id from the open
position, mark_date from SCAN_DATE, run_id from RUN_ID, code_version_hash from
CODE_VERSION_HASH, git_head from GIT_HEAD, and git_dirty from GIT_DIRTY. Replace
quote nulls only with verified observations. Unavailable quote fields remain null.

## Output
End with exactly one summary line, nothing else:
`PROCESSED: <count> MARKED: <count> EXITED: <count>`
Processed counts each DISTINCT position from the open-positions array that received at
least one logger outcome this run — count a candidate once even if a retry needed more
than one logger call for it, never the raw call count. Marked and exited count only
positions whose FINAL outcome this run was marked/exited respectively; a candidate whose
last call was duplicate_skipped or rejected (even after an earlier successful call in
this same run — which should not happen given the logger's own idempotency, but if it
does, trust the logger's actual last recorded outcome) does not count toward either. If
the open positions array was empty, output:
`PROCESSED: 0 MARKED: 0 EXITED: 0`
No recommendations or additional commentary after the summary.
