You are VERIFYING the R/R-gate wiring end-to-end (NOT a full scan). Be brief.

1. Confirm the IBKR read-only option tools are reachable in this environment:
   call search_contracts for "AMZN" (security_type STK) and report the
   underlying_contract_id (US primary listing).
2. get_price_snapshot on that underlying → report AMZN last price.
3. Pull ~155 daily bars via get_price_history and compute AMZN's CURRENT
   MA-150 yourself (do not use a hardcoded/remembered value — it goes stale).
4. Pull a ~30 DTE put chain (get_option_parameters → nearest ~30 DTE →
   get_option_data around support). Read the actual strike spacing (S) in the
   chain near that MA-150 level and use IT as the spread width (do not assume
   $10 — many names list $1-2.50 apart). Get get_price_snapshot on TWO put
   strikes W apart, SHORT strike BELOW the MA-150 you just computed. Compute
   from live mids: credit = short_mid − long_mid, max loss = W − credit,
   max profit = credit, R/R = maxloss : credit.
5. Verdict: does it PASS the gate (short below support AND R/R within 1:1.5–2.5)
   or REJECT? Apply the rule strictly — do not bend "short below support".

Never call any order tool (create_order_instruction). Keep under ~200 words.
Then, on its own final line, emit exactly:
SCREENER_CONSTITUENTS: VERIFY-RR-GATE
