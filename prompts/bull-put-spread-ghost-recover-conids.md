DRY-RUN SHADOW SYSTEM -- RECOVERS OPTION CONTRACT IDS ONLY. THIS PROMPT MUST NEVER ATTEMPT TO PLACE, MODIFY, OR CANCEL A REAL ORDER, ALERT, OR WATCHLIST ENTRY. IT HAS NO SUCH CAPABILITY GRANTED AND MUST NEVER ASK FOR ONE.

You are running my automated Bull Put Spread Ghost System one-time contract ID recovery scan.
Use ONLY the three granted read-only broker tools (`search_contracts`, `get_option_parameters`, `get_option_data`),
scoped read of the current entries CSV, and scoped write of your scratch recovery JSON.
This shadow scan resolves option contract IDs for existing ghost entries that are missing them.

## Input
Read `$ARIA_HOME/state/ghost/ghost_entries.csv` using Read.
The wrapper supplies SCAN_DATE, RUN_ID, CODE_VERSION_HASH, GIT_HEAD, and GIT_DIRTY below.
Identify all rows where contract IDs are missing (`underlying_contract_id`, `short_contract_id`, or `long_contract_id` is empty or absent).
If no rows require recovery: do nothing, write an empty array `[]` to the scratch file, print:
`ATTEMPTED: 0 RESOLVED: 0 UNRESOLVED-BLOCKED: 0`
and stop.
Otherwise, process EVERY such row, in its existing order. One candidate at a time. No batching or parallel broker calls.

## Per-Row Resolution Flow
For each candidate row requiring recovery:
1. Resolve the underlying with `search_contracts` if `underlying_contract_id` is missing.
   Confirm ticker identity against the row's `ticker`. Capture `underlying_contract_id` as the
   positive integer contract ID from the response (never a ticker string).
2. Call `get_option_parameters` for that underlying's real listed expirations if needed.
3. Call `get_option_data` bounded around the row's `resolved_short_strike` and `resolved_long_strike`,
   for `resolved_expiry`. Confirm both listed PUT strikes and the exact expiry.
4. Cross-check verification rule (never infer from ticker strings, never silently accept):
   Cross-check each contract's own returned fields from `get_option_data`:
   - right == "PUT"
   - strike matches `resolved_short_strike` / `resolved_long_strike` exactly
   - expiry matches `resolved_expiry` exactly
   - currency == "USD"
   - multiplier == 100
   If all fields match:
     - Set `resolution_status: "exact"`
     - Capture `short_contract_id` and `long_contract_id` as positive integers.
   If any field does NOT match (e.g. strike mismatch, expiry mismatch, right!=PUT, currency!=USD, multiplier!=100):
     - Set `resolution_status: "unresolved_blocked: conid_field_mismatch"`
     - Leave `underlying_contract_id`, `short_contract_id`, `long_contract_id` as null.
     - Document the specific mismatch in `reason`.
   If contracts cannot be found or tool call fails:
     - Set `resolution_status: "unresolved_blocked: contract_not_found"` (or specific failure reason)
     - Leave all contract IDs as null.
     - Never guess, never silently drop a row.

## Output Scratch File
Using Write (covered by the scoped Edit grant), write the full JSON array of recovery objects to:
`$ARIA_HOME/state/scratch/ghost_recovery_<SCAN_DATE>_<RUN_ID>.json`

Schema per item:
```json
{
  "candidate_id": "<row.candidate_id>",
  "ticker": "<row.ticker>",
  "underlying_contract_id": 123456,
  "short_contract_id": 234567,
  "long_contract_id": 345678,
  "resolution_status": "exact",
  "reason": ""
}
```
For unresolved rows:
```json
{
  "candidate_id": "<row.candidate_id>",
  "ticker": "<row.ticker>",
  "underlying_contract_id": null,
  "short_contract_id": null,
  "long_contract_id": null,
  "resolution_status": "unresolved_blocked: conid_field_mismatch",
  "reason": "short strike mismatch"
}
```

## Summary
End with exactly one summary line, nothing else:
`ATTEMPTED: <count> RESOLVED: <count> UNRESOLVED-BLOCKED: <count>`
Attempted counts all rows from ghost_entries.csv that needed recovery this run.
Resolved counts rows successfully verified with positive integer contract IDs.
Unresolved-blocked counts rows where resolution failed or contract fields mismatched.
No recommendations or additional commentary after the summary.
