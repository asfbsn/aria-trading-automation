You are VERIFYING the R/R-gate wiring end-to-end (NOT a full scan). Be brief.

1. Confirm the IBKR read-only option tools are reachable in this environment:
   call search_contracts for "AMZN" (security_type STK) and report the
   underlying_contract_id (US primary listing).
2. get_price_snapshot on that underlying → report AMZN last price.
3. Pull a ~30 DTE put chain (get_option_parameters → nearest ~30 DTE →
   get_option_data around support) and get_price_snapshot on TWO put strikes that
   form a $10-wide Bull Put Spread with the SHORT strike BELOW ~234.65 (AMZN's
   MA-150). Compute from live mids: credit = short_mid − long_mid, max loss =
   10 − credit, max profit = credit, R/R = maxloss : credit.
4. Verdict: does it PASS the gate (short below support AND R/R within 1:1.5–2.5)
   or REJECT? Apply the rule strictly — do not bend "short below support".

Never call any order tool (create_order_instruction). Keep under ~200 words.
Then, on its own final line, emit exactly:
SCREENER_CONSTITUENTS: VERIFY-RR-GATE
