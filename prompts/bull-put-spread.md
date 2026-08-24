You are running my automated daily Bull Put Spread scan. Use ONLY IBKR read-only
tools plus the two Bash prefixes below. Apply the trading profile below exactly.

**Before concluding any tool is unavailable: call it.** Do not infer
unavailability from memory of past sessions, from ToolSearch returning
nothing (ToolSearch indexes deferred tools; a tool already in your allowed
set doesn't need searching — just call it directly by name), or from any
prior run's outcome. This session's tool wiring is independent of what
happened in earlier ones. If a call genuinely errors, quote the exact error
in your report — don't paraphrase or generalize it into "tools not
connected."

## Universe
Read `data/universe.csv` (the file itself, via Read — it's already in the repo,
no fetch needed). Columns: `ticker,sector,approx_mktcap_usd`. This replaces the
live TradingView "Adi option swing 2.0" screener DOM read for the *membership*
filters only: Region=US, Mkt cap 10B–5T USD, and an "Index" filter checking ~60
major US indices that (because Russell 3000 alone covers ~98% of US market cap)
isn't a narrow test on its own — the market-cap band is the real membership
constraint, and `data/universe.csv` is exactly that band, derived from Russell
1000 constituent weights (see `scripts/refresh_universe.py`).

The screener panel's remaining filters (confirmed 2026-08-24 from the live
filter bar) are all *daily technical* conditions, not fixed membership, which
is why the live screener typically shows ~100 names on a given day against
this file's ~650: RSI(20) < 50, close 0%–10% BELOW its 50-day SMA, and close
0%–10% ABOVE its 150-day SMA. `compute_signal.py` (SETTLED output) is the
authoritative computation of these same three per-ticker — see `checks.rsi_below_50`,
`checks.near_ma50_pullback`, `checks.near_ma150_support` below. Scan every
ticker in `data/universe.csv` — do NOT sample or pre-narrow the list yourself,
those three checks do the real narrowing (this file is refreshed every 1–3
months, not resized to fit a token budget).

## Scope
- **Daily (1D) interval only.**
- **TOKEN ECONOMY:** one efficient pass per ticker; pull option chains only for
  PRIME-eligible names (see R/R VERIFICATION). Write the report and exit
  cleanly.
- **Entry signal**: the "Premium Trading Dashboard - Adi Radmy Edition" Pine
  indicator this strategy was originally built around is a protected/invite-only
  script (source confirmed unavailable 2026-07-09) — its exact internal formulas
  were never extractable. `scripts/compute_signal.py` is the sole signal source
  now: a deterministic local computation from IBKR bars, built from the
  indicator's *declared input parameters* (RSI length 20 / threshold 50, MA50,
  MA150, Volume MA length 20). It is an approximation of Adi's original signal,
  not a replica — treat its `entry_confirmed` the way you'd treat your own
  independent technical read.
- For each ticker: call `get_price_history` (IBKR) for **at least 220 daily
  bars** (MA150 needs 150+ bars of warmup; 220 gives margin) → feed its response
  **directly, unmodified**, merged with a `"ticker"` key, to
  `python3 $ARIA_HOME/scripts/compute_signal.py` → parse the JSON result.
  `compute_signal.py` accepts `get_price_history`'s native parallel-array shape
  (`{"ticker": "...", "time": [...], "open": [...], "high": [...], "low": [...],
  "close": [...], "volume": [...]}`) directly — do NOT hand-transform it into
  `{"bars": [{"date","open",...}, ...]}` yourself, and do NOT hand-truncate the
  last bar yourself either; both reshapes are error-prone by hand (index drift
  across 150+ values) and the allowed Bash scope has no separate transform
  command to do it safely. Use the `--exclude-last-bar` flag instead (see
  TIMING below) — the script does the drop internally.
  **Bash permission is scoped to this exact script prefix — invoke it directly
  with a heredoc, not a leading pipe:**
  `python3 $ARIA_HOME/scripts/compute_signal.py [--exclude-last-bar] <<'EOF'` / JSON / `EOF`
  (a command starting with `echo ... |` or similar will NOT match the allowed
  prefix and will be blocked).
- **Earnings proximity is not automated** (no data source wired for it). If you
  already know a name's earnings date, factor it in; otherwise mark "earnings:
  unknown" and do not gate PRIME on it — flag for manual check instead.

## ⚠️ Timing — settled vs provisional (lead with this)
This runs at 19:00 Israel ≈ **12:00 ET, mid US session**, so today's daily candle
is **unsettled and can still flip**. `get_price_history`'s last bar may be
today's live/in-progress session. Structure the report in two clearly separated
sections, and run `compute_signal.py` **twice** per ticker on the SAME
`get_price_history` response (don't re-fetch):
1. **SETTLED (authoritative)** — `python3 $ARIA_HOME/scripts/compute_signal.py
   --exclude-last-bar` (drops today's in-progress bar internally; output has
   `"settled": true`). This drives every PRIME/RADAR/REJECT decision below.
2. **PROVISIONAL (today, unsettled)** — `python3
   $ARIA_HOME/scripts/compute_signal.py` (no flag, full bars including today;
   output has `"settled": false`). Label it explicitly as subject to change
   before the US close. Do NOT issue entries off the provisional read alone.

## Selection rules (my mentor's risk management)
"MA-150", "RSI", "entry confirmation" below all mean `compute_signal.py`'s
**SETTLED** output — that's the sole, authoritative signal source now.
1. Stock MUST trade ABOVE its MA-150 (`checks.above_ma150` — below = falling
   knife → reject).
2. Short Put MUST be OTM, placed BELOW the MA-150 or recent daily swing lows.
   Never sell ATM or above support to force a trade.
3. R/R target 1:2 (~1/3 of width as credit). Acceptable band **1:1.5 → 1:2.5**.
   On a $10-wide: credit $4.00 (1:1.5) … $2.86 (1:2.5). Reject worse than 1:2.5.
4. Strike-interval aware: if wide gaps (e.g. APD's $10) make it impossible to
   get 1:2 while keeping the short BELOW support, REJECT — do not force.
5. Read every check in `compute_signal.py`'s `checks` object, not just
   `entry_confirmed`: `above_ma150`, `rsi_below_50`, `rsi_rising`,
   `near_ma50_pullback`, `near_ma150_support`, `volume_above_avg`,
   `bullish_candle` (+ `candle_pattern` for context — doesn't gate). Official
   entry = `entry_confirmed: true` (all seven gating checks pass). Discretionary
   (RADAR) = `entry_confirmed: false` BUT structure strong (`above_ma150`,
   `near_ma150_support`, `volume_above_avg`, no imminent earnings) while only
   momentum/candle checks are weak — flag it, don't discard. List which
   checks are 🟢 vs 🔴.
6. AI autonomy: you may ALSO flag a "hidden gem" from your own technical read
   (chart structure, price action, implied volatility) even when
   `entry_confirmed` is false or checks are mixed — briefly justify the
   override. Such picks are RADAR only, never PRIME.

## R/R VERIFICATION against the LIVE option chain — PRIME gate
`entry_confirmed: true` is **NOT** enough to be PRIME. For **each PRIME-eligible
name only** (passed rules 1,3,5: above MA-150 + full confirmation + strong
structure — usually 0–3 names; token economy: do NOT price rejects/RADAR),
verify a COMPLIANT spread actually exists using the IBKR **read-only** option
tools:
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

- **Headline** (counts: prime / radar / rejects / how many tickers scanned from `data/universe.csv`).
- **Table 1 — 🟢 PRIME CANDIDATES (ready for execution):** ONLY stocks with
  `entry_confirmed: true` + strong structure that pass the MA-150 rule AND a
  **live-chain-verified** 1:1.5–2.5 spread with the short BELOW support (see
  R/R VERIFICATION). A confirmed name with no compliant spread is a REJECT,
  not PRIME. These alone are execution-ready.
- **Table 2 — 🟡 RADAR / WATCHLIST (discretionary):** "setups in the making"
  (strong structure / weak trigger per rule 5) PLUS any "hidden gem" you flag via
  rule 6 (your own TA/price-action/IV) — briefly justify any override. Watch-only.
- Columns (both tables): [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
  | [Suggested Structure: Short/Long, short BELOW support] | [Exact R/R & Credit,
  1:1.5–2.5]. For RADAR rows, list which checks are 🟢 vs 🔴 (+ override reason).
- **PROVISIONAL note**: any name whose live mid-session bar differs from its settled state.
- **Rejects**: grouped one-liners (below MA / no support / interval-reject).

After the report, emit these THREE lines, each on its own line, in this exact
order, as the literal last thing you output:

SCREENER_CONSTITUENTS: SYM1,SYM2,SYM3,...
SIGNALS_COMPLETED: <count of tickers where get_price_history + compute_signal.py
both ran successfully and produced a checks object — regardless of whether the
result was PRIME, RADAR, or REJECT>
SIGNALS_FAILED: <count of tickers you could NOT get a checks object for — a
tool error, an unavailable connector, a bad/empty price-history response, etc.
This is NOT the same as "REJECT": a ticker you technically rejected (below
MA-150, no compliant R/R, ...) still counts as COMPLETED, not FAILED>

SIGNALS_COMPLETED + SIGNALS_FAILED MUST equal the number of tickers in
`data/universe.csv`. If any ticker failed, list which ones and why in the
Rejects section — do NOT silently drop it from the count. SIGNALS_FAILED > 0
means the run is treated as a failure by the wrapper script even if the report
body looks complete — this is intentional: a real tool/data failure must never
be reported as a clean run.
