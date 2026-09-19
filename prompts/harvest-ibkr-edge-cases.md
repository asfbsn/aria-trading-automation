HARVEST SCRIPT — NOT PIPELINE. Deliberately triggers real IBKR connector
responses across VALID / TOOL_ERROR / NO_DATA / MALFORMED_DATA shapes, for
building a balanced fixture set to calibrate the Jev tool-response-triage
classifier. Read-only market data only. You have NO order/alert/watchlist/
account-mutation capability granted, and must never attempt one. You have
NO Write/Edit/Bash capability of any kind -- your only job is to make the
calls below, in order, and let each real response appear in your output.
Do not retry a failed/errored call. Do not work around a denial or an error
by trying alternate arguments beyond what's specified below -- an error
response IS the desired outcome for the TOOL_ERROR-targeted calls.

## Input
Read `state/scratch/spike_capture_input.json` for a known-good baseline:
`underlying_contract_id` (AAPL), `short_contract_id`, `long_contract_id`,
`resolved_expiry`. Do not resolve, re-derive, or look up anything else
yourself -- no search_contracts is granted; none is needed.

## Calls, in this exact order (18 total)

Targeting VALID (baseline, for comparison -- we already have several of these):
1. `get_price_snapshot` on `underlying_contract_id`, market_data_names=["last","bid_ask"].
2. `get_option_parameters` on `underlying_contract_id`.

Targeting TOOL_ERROR (deliberately malformed/invalid calls -- an explicit
error response is success for this section, not a problem to fix):
3. `get_price_history` with `contract_id` = `underlying_contract_id`, `period="1d"`,
   `bar_count="5"` (deliberately wrong parameter names/enum -- omits the required
   `security_type` and `step` fields entirely).
4. `get_option_parameters` with `contract_id` = `underlying_contract_id` (deliberately
   wrong field name -- should be `underlying_contract_id`).
5. `get_price_snapshot` on contract_id `-1` (negative, structurally invalid).
6. `get_price_snapshot` on contract_id `999999999999` (a huge, almost certainly
   nonexistent numeric id -- distinct from `-1` and from the small-invalid-id
   case already captured earlier today).

Targeting NO_DATA (structurally valid range/set queries with legitimately zero
matches -- not single-entity lookups):
7. `get_option_data` on `expiration_id` = the value from `get_option_parameters`'s
   `current_expiration` (call #2's result), `min_strike=5000`, `max_strike=6000`.
8. `get_option_data` on the same `expiration_id`, `min_strike=0.01`, `max_strike=0.05`
   (a different out-of-range direction -- absurdly low strikes).

Targeting MALFORMED_DATA (requests likely to come back partial, inconsistent,
or with fields that don't match what was asked, on real illiquid/edge contracts
-- not guaranteed, real market behavior decides the actual shape):
9. `get_price_snapshot` on `underlying_contract_id`, market_data_names=["last",
   "bid_ask","top_status","option_open_interest","bond_yield","future_open_interest"]
   (a deliberately mismatched field list -- option/futures/bond fields requested
   on a plain equity).
10. `get_price_snapshot` on `short_contract_id`, market_data_names=["last","bid_ask",
    "option_open_interest","option_midpoint_iv","top_status"] (a real, possibly
    illiquid single-leg option contract -- thin/frozen/partial quotes are a real
    possible outcome, not guaranteed).
11. `get_price_snapshot` on `long_contract_id`, same market_data_names as call 10.
12. `get_price_history` on `underlying_contract_id`, `security_type="STK"`,
    `step="ONE_DAY"`, `step_count=1`, `outside_rth=true` (edge case: single-bar
    request outside regular trading hours).

Repeat calls 7 and 9 through 11 once more each (calls 13-16), to see whether the
same edge-case request produces a consistent shape on a second attempt a few
seconds later, or something different (quotes move, frozen/live status can flip).

17. `get_price_snapshot` on `underlying_contract_id`, market_data_names=[] (empty
    list -- tests the tool's own documented default-fields fallback behavior
    against an explicit empty request, a real edge of the parameter contract
    itself, not the market).
18. `get_option_data` on `expiration_id` = the value from call 2's
    `current_expiration`, with NO min_strike/max_strike bounds at all (the
    tool's own docs warn this can return hundreds of rows on a liquid name --
    real behavior at that other edge, full unbounded chain).

## Output
After all 18 calls (real responses or real error responses either way are the
correct outcome -- do not treat an error as something to fix), end your
response with exactly this one line, verbatim, nothing before or after it:

PROCESSED: 999 MARKED: 0 EXITED: 0

The 999 is a deliberate, unmistakable fixture value -- never a real count. Do
not add commentary, analysis, or recommendations before or after that line.
