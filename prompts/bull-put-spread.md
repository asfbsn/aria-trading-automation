You are running my automated daily Bull Put Spread scan. Use ONLY the connected
tradingview-bridge MCP tools against my LOCAL TradingView Desktop. Apply my saved
trading profile exactly (see memory: trading-bull-put-spread-profile).

## Readiness (do this first, in order)
1. Call tv_health_check. If not connected, call tv_launch — it starts TradingView
   WITH the CDP debug port (pass kill_existing=true to clear a stale non-CDP
   instance) — then wait and re-check. Abort with a clear one-line error if it
   never becomes ready (the wrapper surfaces it).
2. Activate the screener: click the right-sidebar "Screeners" radar icon via
   ui_click by data-name "screener-dialog-button" (fallback: aria-label
   "Screeners"). Then confirm the active preset is **"Adi option swing 2.0"**
   (read it via ui_evaluate); if a different preset shows, switch to it. The
   screener DOM must be visible before you read its constituents.

## Scope
- Target the TradingView Stock Screener component named **"Adi option swing 2.0"**
  (read its constituents via ui_evaluate on the screener DOM). NEVER use a watchlist.
- **Daily (1D) interval only.**
- **TOKEN ECONOMY & STRICT SCOPE:** scan ONLY the ~36 screener constituents. Do NOT
  open other watchlists, do NOT search for external symbols, avoid unnecessary
  deep-dives or tool loops. One efficient pass per ticker; pull option chains only
  for PRIME names. Then write the report and exit cleanly to conserve tokens.
- For each constituent, read the **"Premium Trading Dashboard - Adi Radmy Edition"**
  Pine table (data_get_pine_tables), MA-150 (data_get_study_values), and price /
  recent swing low (data_get_ohlcv, summary).
- RELIABILITY: rapid symbol switching outruns the indicator recalc. After each
  chart_set_symbol, confirm the symbol via chart_get_state AND sanity-check that
  MA-150 is plausible vs the live price before trusting a read; re-read on mismatch.

## ⚠️ Timing — settled vs provisional (lead with this)
This runs at 19:00 Israel ≈ **12:00 ET, mid US session**, so today's daily candle
is **unsettled and can still flip**. Structure the report in two clearly separated
sections:
1. **SETTLED (authoritative)** — yesterday's fully-closed daily candle. Derive the
   trend/support picture from the last *completed* daily bar (use data_get_ohlcv to
   read the prior closed bar; treat this as the decision basis).
2. **PROVISIONAL (today, unsettled)** — the live dashboard reading as it stands
   mid-session. Label it explicitly as subject to change before the US close. Do
   NOT issue entries off the provisional bar alone.

## Selection rules (my mentor's risk management)
1. Stock MUST trade ABOVE its MA-150 (below = falling knife → reject).
2. Short Put MUST be OTM, placed BELOW the MA-150 or recent daily swing lows.
   Never sell ATM or above support to force a trade.
3. R/R target 1:2 (~1/3 of width as credit). Acceptable band **1:1.5 → 1:2.5**.
   On a $10-wide: credit $4.00 (1:1.5) … $2.86 (1:2.5). Reject worse than 1:2.5.
4. Strike-interval aware: if wide gaps (e.g. APD's $10) make it impossible to get
   1:2 while keeping the short BELOW support, REJECT — do not force.
5. Parse EVERY dashboard row, not just the last one. Official entry = "אישור כניסה"
   reads exactly "יש אישור כניסה". Discretionary (RADAR) = overall "אין אישור" BUT
   structure strong (support נתמך, volume גבוה מהממוצע, no imminent earnings, RSI
   חיובי) while only the trigger is weak (momentum יורד/דורך, candle נר חלש) —
   flag it, don't discard. List which indicators are 🟢 vs 🔴.
6. AI autonomy: you may ALSO flag a "hidden gem" from your own technical read
   (chart structure, price action, implied volatility) even when the table says
   "אין אישור" or the rows are mixed — briefly justify the override. Such picks
   are RADAR only, never PRIME.

## R/R VERIFICATION against the LIVE option chain — PRIME gate
A dashboard "יש אישור כניסה" is **NOT** enough to be PRIME. For **each PRIME-eligible
name only** (passed rules 1,3,5: above MA-150 + full confirmation + strong structure —
usually 0–3 names; token economy: do NOT price rejects/RADAR), verify a COMPLIANT
spread actually exists using the IBKR **read-only** option tools:
1. `search_contracts` (security_type STK) → `underlying_contract_id` (exact symbol match, US primary listing).
2. `get_option_parameters` → pick the expiration nearest **~30 DTE**.
3. `get_option_data` (bound strikes around support) → `put_contract_id`s at/below the MA-150 & swing low.
4. `get_price_snapshot` on the candidate short & long puts → bid/ask → use mids.
5. Compute: **credit = short_mid − long_mid**; **max loss = width − credit**; **R/R = maxloss : credit**.
   PRIME requires BOTH: (a) short strike **BELOW support** (MA-150 or swing low), AND
   (b) **R/R within 1:1.5–2.5** (credit ≈ width/3.5 … width/2.5).
- If no strike/width combo satisfies BOTH → the name is a **REJECT**, reason
  "R/R gate: can't hit 1:1.5–2.5 with short below support" (the APD/AMZN case) — do
  NOT list it as PRIME and do NOT bend "short below support" to force the band.
- For every PRIME row, report the **verified exact strikes, credit, max loss, max profit, and R/R**.
- NEVER use order tools (`create_order_instruction`); read-only only.

## Output — TWO separate tables (prevents execution errors; live money soon)
Decision basis = the SETTLED (prior closed candle) state; flag PROVISIONAL (today's
unsettled) changes separately. Produce, in order:

- **Headline** (counts: prime / radar / rejects).
- **Table 1 — 🟢 PRIME CANDIDATES (ready for execution):** ONLY stocks with full
  "יש אישור כניסה" + strong structure that pass the MA-150 rule AND a **live-chain-
  verified** 1:1.5–2.5 spread with the short BELOW support (see R/R VERIFICATION).
  A confirmed name with no compliant spread is a REJECT, not PRIME. These alone
  are execution-ready.
- **Table 2 — 🟡 RADAR / WATCHLIST (discretionary):** "setups in the making"
  (strong structure / weak trigger per rule 5) PLUS any "hidden gem" you flag via
  rule 6 (your own TA/price-action/IV) — briefly justify any override. Watch-only.
- Columns (both tables): [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
  | [Suggested Structure: Short/Long, short BELOW support] | [Exact R/R & Credit,
  1:1.5–2.5]. For RADAR rows, list which indicators are 🟢 vs 🔴 (+ override reason).
- **PROVISIONAL note**: any name whose live mid-session bar differs from its settled state.
- **Rejects**: grouped one-liners (below MA / no support / interval-reject).

After the report, on its own final line, emit the screener constituents for the
stale-feed guard, exactly:
SCREENER_CONSTITUENTS: SYM1,SYM2,SYM3,...
