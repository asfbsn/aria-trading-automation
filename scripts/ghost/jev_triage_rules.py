"""
Shared IBKR tool-response triage rule -- single source of truth for the
classification instructions/criteria used by the Jev shadow classifier.
Ported into the repo from the 2026-09-19 spike (session scratchpad
typesafe-smoke/triage_rules.py) at revision 3 -- do not hand-edit both
copies independently; this repo copy is now the authoritative one.

Revision 2 (2026-09-19): fixes the blind spot found in compare_real.py's
invalid_contract_id_bare_empty case. A bare {} in response to a single,
specific-entity lookup (a numeric contract_id, an order id) is a silent
failure/invalid-input signal, not "no data" -- NO_DATA is reserved for
requests that structurally CAN legitimately return zero rows (a strike
range, a symbol set). Distinguishing the two requires the request shape,
not just the response -- classify using both.

Revision 3 (2026-09-19): fixes the blind spot found in the deliberate
harvest run (state/jev_training_raw/) -- a closed-market/FROZEN
get_price_snapshot with `last` present but `bid_ask` entirely absent, though
explicitly requested, was reading as VALID because SOME requested data came
back. User's architectural ruling: automated options mark-to-market uses the
bid/ask midpoint, not `last` (which can be stale); a response missing an
explicitly-requested, load-bearing field must not be waved through as VALID
just because other fields succeeded. Generalized: ANY explicitly-requested
field that is entirely absent (not null, not an empty placeholder -- just
missing) with no error explaining why makes the whole response
MALFORMED_DATA, even when other requested fields are present and valid.
Partial success is not VALID.

Validation basis: 17/18 (94.4%) on state/jev_training_raw/ (18 real harvested
IBKR edge cases, hand-labeled). NOTE (2026-09-19, advisor review): that
accuracy is IN-SAMPLE -- both rule revisions were written in direct response
to misses on this exact fixture set, then scored on it. Effective n is ~14
(4 exact duplicates), one symbol/day/closed-market, one labeler. Treat as
"the taxonomy and harvest pattern are sound," not as a validated production
accuracy figure. Any confidence-threshold/fallback design needs real
out-of-sample data (e.g. this shadow log, accumulated over live runs and
independently labeled) before it can be trusted.
"""

LABELS = ["VALID", "TOOL_ERROR", "NO_DATA", "MALFORMED_DATA"]

INSTRUCTIONS = (
    "Classify the integrity of this IBKR tool response, using BOTH the response "
    "and what was requested. Apply this exact precedence, top to bottom, "
    "stopping at the first that matches: "
    "(1) TOOL_ERROR -- the response carries an explicit error/failure field or "
    "message from the tool itself; OR the request targeted a single, specific, "
    "presumably-existing entity (e.g. a numeric contract_id, a single order id) "
    "and the response came back empty or near-empty (e.g. {}) with no error "
    "field -- an unexplained empty response to a targeted single-entity lookup "
    "means the entity was invalid or the call silently failed, not an absence "
    "of data. "
    "(2) NO_DATA -- no error field, and the response is a structurally valid "
    "but empty result for a request that can legitimately return zero matches "
    "(e.g. a strike-range or symbol-set query with nothing in range). Empty is "
    "a valid, expected answer shape for this kind of request -- it is not a "
    "single-entity lookup. "
    "(3) MALFORMED_DATA -- no error, non-empty, but the data is structurally "
    "invalid, missing required fields relative to what was requested, or "
    "otherwise not usable as-is. This INCLUDES the case where the response is "
    "partially populated: if ANY field the caller explicitly requested is "
    "entirely absent from the response (not present as null or an empty "
    "placeholder -- just missing) and no error field explains why, classify "
    "as MALFORMED_DATA even if other requested fields came back present and "
    "individually valid. A partially-successful response is not VALID -- a "
    "silently dropped field (e.g. bid_ask missing from a price snapshot used "
    "for mark-to-market, while `last` is present) can be more dangerous than "
    "an explicit error, because it looks superficially usable. Also apply "
    "this when a response field is present but self-flagged invalid by the "
    "tool itself (e.g. an isValid: false sub-field with a nonsensical value). "
    "(4) VALID -- none of the above apply; the response is a well-formed, "
    "usable, non-empty result with no defects, and nothing explicitly "
    "requested is missing."
)

CRITERIA = {
    "VALID": "Well-formed, non-empty, usable data with no error, no structural defects, and nothing explicitly requested is missing.",
    "TOOL_ERROR": "An explicit error/failure message, OR a bare/near-empty response to a single-specific-entity lookup (invalid input or silent failure).",
    "NO_DATA": "No error, structurally valid, legitimately empty result for a range/set-style query (not a single-entity lookup).",
    "MALFORMED_DATA": "No error reported, non-empty, but structurally invalid, missing a field that was explicitly requested (even if other requested fields succeeded), or containing a self-flagged-invalid sub-field.",
}
