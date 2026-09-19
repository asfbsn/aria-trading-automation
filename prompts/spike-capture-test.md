SPIKE ARTIFACT — NOT PIPELINE. THROWAWAY, ONE-TIME CAPTURE-FEASIBILITY TEST ONLY.
THIS PROMPT MUST NEVER PLACE, MODIFY, OR CANCEL A REAL ORDER, ALERT, OR WATCHLIST
ENTRY. IT HAS NO SUCH CAPABILITY GRANTED AND MUST NEVER ASK FOR ONE. IT ALSO HAS
NO WRITE/EDIT/BASH CAPABILITY OF ANY KIND — it cannot touch state/ghost/ or any
production file. Its only purpose is to exercise IBKR read tools so the
`--output-format stream-json` capture around this run can be inspected afterward.

## Input
Read `state/scratch/spike_capture_input.json`. It carries one already-resolved
real position's identifiers (ticker, underlying/short/long contract IDs, expiry)
copied from the live ledger for this test only — do not resolve, re-derive, or
look up anything yourself. No search_contracts is granted; none is needed.

## Steps, in this exact order
1. Call `get_price_history` on `underlying_contract_id`, a small bar count
   (e.g. 5 daily bars) — this is a capture-shape test, not a real analysis.
2. Call `get_option_parameters` for the underlying to get the expiration_id
   matching `resolved_expiry` (required input for step 3's tool — resolve it
   here, not before).
3. Call `get_option_data` for the underlying, using that expiration_id,
   bounded loosely around the known short/long strikes (exact strikes not
   required — any valid bounded request exercising this tool is sufficient).
4. Call `get_price_snapshot` on `short_contract_id`.
5. Immediately call `get_price_snapshot` on `short_contract_id` AGAIN — same
   contract ID, deliberate repeat, back to back. This is intentional, not an
   error — the test needs two independent tool_use events against the same
   input to check they get distinct call IDs.
6. Call `get_price_snapshot` on `long_contract_id`.

Record nothing to any file. Do not call Bash. Do not call Edit or Write.

## Output
After all six calls (real API responses or real error responses either way —
market is closed, rejections/delayed data are an expected, fine outcome for
this test), end your response with exactly this one line, verbatim, nothing
before or after it:

PROCESSED: 999 MARKED: 0 EXITED: 0

The 999 is a deliberate, unmistakable fixture value — never a real count. Do
not add commentary, analysis, or recommendations before or after that line.
