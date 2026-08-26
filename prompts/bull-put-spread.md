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
Read `$ARIA_HOME/state/scratch/prescreen_<today>.json` (via the Read tool; the
wrapper guarantees it exists and is from today — if it is missing or its
`"date"` field is not today, ABORT with an explicit error, do not fall back to
scanning the full universe). Process ONLY the tickers in its `"shortlist"`
array through the existing per-ticker fetch→write→compute flow below. IBKR bars
+ `compute_signal.py` remain the authoritative signal source — the prescreen is
an over-inclusive yfinance-based pre-filter, and its `"per_ticker"` values are
reference only, never a substitute for `compute_signal.py`.

Also read `data/universe.csv` (the file itself, via Read — columns:
`ticker,sector,approx_mktcap_usd`), but now only for sector lookups (portfolio
guards in TRADE DIRECTIVE) and the filtered-count context. This file represents
the membership filters (Region=US, Mkt cap 10B–5T USD, derived from Russell 1000
constituent weights — see `scripts/refresh_universe.py`). The local prescreen
stage filters this universe down to the shortlist using widened technical bands,
and you must scan every ticker in the shortlist without sampling or further
pre-filtering.

## Scope
- **Daily (1D) interval only.**
- **TOKEN ECONOMY:** one efficient pass per ticker; pull option chains only for
  PRIME-eligible names (see R/R VERIFICATION). Write the report and exit
  cleanly.
- **Macro context (once at scan start):** at scan start, ONCE (not per ticker),
  run one WebSearch for current macro regime — VIX level, SPY vs its own MA150
  trend, and any major scheduled macro event (Fed decision, CPI, jobs report)
  inside the next ~30 days. Output a 3–5 line "Market Context" header before
  Table 1. If a major macro event falls inside the ~30 DTE window, note it in
  every directive block (context only — NOT a gate; the research gate handles
  name-specific catalysts).
- **Entry signal**: the "Premium Trading Dashboard - Adi Radmy Edition" Pine
  indicator this strategy was originally built around is a protected/invite-only
  script (source confirmed unavailable 2026-07-09) — its exact internal formulas
  were never extractable. `scripts/compute_signal.py` is the sole signal source
  now: a deterministic local computation from IBKR bars, built from the
  indicator's *declared input parameters* (RSI length 20 / threshold 50, MA50,
  MA150, Volume MA length 20). It is an approximation of Adi's original signal,
  not a replica — treat its `entry_confirmed` the way you'd treat your own
  independent technical read.
- **Process tickers with minimal narration, not one at a time with commentary.**
  Batching all of one step (e.g. every `get_price_history` call) before moving
  to the next step was tried and rejected 2026-08-25: it forced reconstructing
  many large bar arrays from earlier context when writing them out, which is a
  real transcription-error risk — and when that risk was flagged mid-run, the
  fallback was to reimplement the RSI/MA math from memory instead of using
  `compute_signal.py`, which is unacceptable (unverified, ad-hoc math on a
  system whose whole point is a deterministic signal source). NEVER improvise
  a replacement calculation — if a step is failing, stop and report it, don't
  route around `compute_signal.py`.
  Instead, for EACH ticker, in tight sequence with **no narration in between**
  (silence is fine and expected for routine tickers):
  1. `search_contracts` → resolve contract_id.
  2. `get_price_history` (**at least 220 daily bars** — MA150 needs 150+ bars
     warmup, 220 gives margin).
  3. Immediately — same turn, right after seeing that response, while it's
     still fresh — merge it **directly, unmodified**, with a `"ticker"` key,
     and write it with the **Write** tool to that ticker's OWN file:
     `$ARIA_HOME/state/scratch/signal_input_<TICKER>.json` (e.g.
     `signal_input_AAPL.json`). Do not defer this to a later batch pass —
     writing right after the fetch, one ticker at a time, is what keeps the
     transcription accurate.
  4. Run `python3 $ARIA_HOME/scripts/compute_signal.py --input
     $ARIA_HOME/state/scratch/signal_input_<TICKER>.json [--exclude-last-bar]`
     (Bash), both SETTLED and PROVISIONAL passes.
  5. Classify (REJECT/RADAR/PRIME-eligible) and move to the next ticker.
     **Do not narrate each ticker individually** — most are REJECTs; those
     just go straight into the grouped one-liner Rejects section at the end,
     no per-ticker explanation needed. Save actual reasoning for RADAR/PRIME
     names and the R/R verification stage, where it's earned. The speed win
     is dropping the narration for routine tickers, not restructuring the
     fetch→write→compute order — keep that order tight and immediate.
  `compute_signal.py` accepts `get_price_history`'s native parallel-array shape
  (`{"ticker": "...", "time": [...], "open": [...], "high": [...], "low": [...],
  "close": [...], "volume": [...]}`) directly — do NOT hand-transform it into
  `{"bars": [{"date","open",...}, ...]}` yourself, and do NOT hand-truncate the
  last bar yourself either; both reshapes are error-prone by hand (index drift
  across 150+ values). Use the `--exclude-last-bar` flag instead (see TIMING
  below) — the script does the drop internally.
  **DO NOT pass the JSON as a heredoc or an `echo ... |` pipe into Bash — ever.**
  A Bash command whose argument literally contains JSON (any `{`/`"` together)
  gets silently auto-denied by Claude Code's own command-safety heuristic as
  "expansion obfuscation" — not the allowedTools gate, no quoting fixes it —
  and the run will sit with zero output until the wrapper's timeout kills it
  (confirmed 2026-08-25, this was the actual cause of every prior scan hang).
  The Write-file-then-`--input`-flag path above is the only safe route.
  **Why this matters (confirmed 2026-08-25):** with heavy narration between
  every tool call, measured ~2min/ticker — 654 tickers that way is ~24h, not
  viable. The slow part was never the computation (compute_signal.py runs in
  milliseconds); it's each full agent reasoning turn costing real wall-clock
  seconds. Cutting narration for routine tickers alone (same tight
  fetch→write→compute order, just silent) measured ~1.9 sec/call on a
  20-ticker sample — dropping narration is the actual fix, not restructuring
  the order of operations or scanning fewer tickers.
- **Earnings proximity is not automated** (no data source wired for it). If you
  already know a name's earnings date, factor it in; otherwise mark "earnings:
  unknown" and do not gate PRIME on it — flag for manual check instead.

## ⚠️ Timing — settled vs provisional (lead with this)
This runs at 19:00 Israel ≈ **12:00 ET, mid US session**, so today's daily candle
is **unsettled and can still flip**. `get_price_history`'s last bar may be
today's live/in-progress session. Structure the report in two clearly separated
sections, and run `compute_signal.py` **twice** per ticker on the SAME
`get_price_history` response written once to that ticker's
`signal_input_<TICKER>.json` (don't re-fetch or rewrite the file):
1. **SETTLED (authoritative)** — add `--exclude-last-bar` (drops today's
   in-progress bar internally; output has `"settled": true`). This drives
   every PRIME/RADAR/REJECT decision below.
2. **PROVISIONAL (today, unsettled)** — no flag, full bars including today;
   output has `"settled": false`. Label it explicitly as subject to change
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

## 🔎 QUALITATIVE RESEARCH GATE — PRIME-eligible only
Runs ONLY on names that survive R/R VERIFICATION (passed technical rules 1,3,5 AND
verified a compliant 1:1.5–2.5 spread with short below support — typically 0–3
names; token economy: do NOT search rejects/RADAR). For each such candidate, use
`WebSearch` (and `WebFetch` for specific URLs worth opening, e.g. SEC filing index
pages) to perform four checks:

1. **Earnings timing** — search next confirmed earnings date. If it falls BEFORE
   the option expiration chosen in R/R VERIFICATION (~30 DTE expiry) → 🔴 flag
   `EARNINGS_BEFORE_EXPIRY: <date>` (gap risk inside short premium). If no date
   is confirmable, flag `earnings: unknown — verify manually` (do not treat
   unknown as clear, but do not block/red-flag on unknown).
2. **Analyst sentiment** — search recent (~30 days) rating changes and consensus
   price target. 🔴 flag `ANALYST_RED_FLAG: <what was found>` if there was a
   recent downgrade OR consensus price target sits meaningfully (>10%) below
   current price. Cite specifics (firm name, rating change, target number) — not
   vague "sentiment negative."
3. **News / sector catalysts** — search recent headlines (last 1–2 weeks) for
   material negative catalysts: guidance cuts, regulatory action, product recalls,
   litigation, or sector-wide selloff specific to this name. 🔴 flag
   `NEWS_RED_FLAG: <one-line summary + finding>` if material. Routine market
   noise/commentary is not a flag.
4. **SEC filings** — search most recent 8-K/10-Q filings plus recent Form 4s (e.g.
   `site:sec.gov <ticker> 8-K` or EDGAR search via WebFetch). 🔴 flag
   `SEC_RED_FLAG: <what was found>` ONLY for material adverse disclosures:
   restatements, guidance withdrawal, going-concern warnings, or an
   insider-selling cluster — defined as **3+ distinct insiders filing Form 4
   open-market sales within any 30-day window over the last 90 days** (a single
   seller, or scheduled 10b5-1 plan sales, is NOT a cluster). Routine periodic
   filings are not flags.

**Verdict rules (hard gate):**
- **Checks 1–3 🔴 flag (earnings / analyst / news)** → **Downgrade to RADAR**
  (Table 2). Retain full technical details; state the research flag(s) as the
  justification.
- **Check 4 🔴 flag (SEC filings)** → **Downgrade to REJECT** (hard stop, no
  discretionary override). Group in Rejects with specific finding named.
- **Tool error / empty results on any check** → Do NOT treat as clear and do NOT
  fail the scan. Mark `<category>: unavailable — verify manually` and continue.
  This does NOT increment `SIGNALS_FAILED` (technical pipeline counter only).
- **All four checks clear (or unknown/unavailable without red flag)** → **PRIME**
  (Table 1). Every Table 1 row must display its research status inline.

## 📋 TRADE DIRECTIVE — final PRIME names only
For each name that survived ALL gates (technical rules + R/R verification + research gate), emit one EXECUTION PLAN block containing, in order:

- **WHAT:** ticker, exact short/long put strikes, expiration date (the ~30 DTE expiry already chosen in R/R VERIFICATION — do not re-pick).
- **WHEN (execution-window rule):** this scan runs inside the 12:00–13:00 ET execution window (19:00–20:00 Israel). Directive = `EXECUTE NOW` **only if** the PROVISIONAL pass (unsettled bar, no `--exclude-last-bar`) shows ALL THREE of: `above_ma150`, `near_ma150_support`, `volume_above_avg` — i.e. today's live bar is confirming the settled signal at support with real volume, not fighting it. If any of the three is false on the provisional read → directive = `HOLD — provisional bar not confirming; re-evaluate next scan` (name which check failed). This gates EXECUTION TIMING only — PRIME classification itself stays based on the SETTLED read, per the existing Timing section.
- **AT WHAT PRICE:** entry limit credit = the verified mid credit from R/R VERIFICATION (state $); minimum acceptable credit = width/3.5 (the 1:2.5 floor, state $) — if fills would require accepting less, do not chase, skip the trade.
- **R/R restated:** print max loss ÷ credit; hard rule: if outside 1.5–2.5, NO directive is emitted for the name (even if it somehow reached this section) — it reverts to REJECT with reason "R/R outside 1.5–2.5 at directive stage".
- **Portfolio guards (check BEFORE sizing):** call `get_account_positions` (read-only). Count currently-open bull-put-spread positions — a position counts ONLY as a validated leg pair: same underlying, same expiration, both PUTS, offsetting quantities (short leg negative, long leg positive, equal magnitude), and short strike ABOVE long strike; unpaired or ambiguous option legs are NOT counted as spreads but must be flagged in the report as "unpaired legs — verify manually". If validated pairs ≥ 8 → `BLOCKED — 8-position cap reached`. If any open position's underlying is in the SAME sector as this candidate (sector column from `data/universe.csv`) → `BLOCKED — sector correlation with <existing ticker>`. Blocked names remain listed as PRIME in Table 1 (signal is real) but their directive block states the BLOCKED status instead of an executable order.
- **HOW MUCH (position sizing):** call `get_account_summary` (read-only) for current net liquidation value. `contracts = floor( (0.0625 × net_liq) / (max_loss_per_contract × 100) )` where `max_loss_per_contract = width − credit` (per share). Show the arithmetic inline (net_liq, allocation $ = 6.25% of it, per-contract max loss $, resulting integer). **If the result is 0 → directive = `BLOCKED — spread too wide for current account equity`; never emit a 0-contract order.**
- **Macro context note:** if a major scheduled macro event (from the scan-start Market Context check) falls inside the ~30 DTE window, note it here for context (does not gate execution).
- **EXITS (both mandatory in every executable block):**
  - Profit-take: place GTC buy-to-close at **20% of received credit** (captures 80% of max profit; state the $ price). Note: this 80%-capture target is the default baseline GTC order; exits remain dynamically manageable by the exit guard / discretion on momentum and market conditions.
  - Stop: close if the underlying CLOSES below the short strike OR below its MA150 (identical thresholds to `prompts/bull-put-spread-exit.md`'s exit guard — the systems are deliberately symmetric).
  - Time stop: close at DTE ≤ 7 regardless of P/L.

Reminder: this is an advisory directive — the human places every order; NEVER use order tools.

## Output — TWO separate tables (prevents execution errors; live money soon)
Decision basis = the SETTLED (prior closed candle) state; flag PROVISIONAL (today's
unsettled) changes separately. Produce, in order:

- **Headline** (counts: prime / radar / rejects / how many tickers scanned from prescreen shortlist).
- **Market Context** (3–5 lines: VIX level, SPY vs its own MA150 trend, upcoming major macro events inside ~30 days).
- **Table 1 — 🟢 PRIME CANDIDATES (ready for execution):** ONLY stocks with
  `entry_confirmed: true` + strong structure that pass the MA-150 rule, have a
  **live-chain-verified** 1:1.5–2.5 spread with the short BELOW support (see
  R/R VERIFICATION), AND clear the qualitative research gate (all checks clear or
  unknown/unavailable without red flags). A confirmed name with no compliant
  spread is a REJECT; any 🔴 research flag on checks 1–3 downgrades to RADAR, and
  check 4 (SEC) downgrades to REJECT. These alone are execution-ready.
- **Table 2 — 🟡 RADAR / WATCHLIST (discretionary):** "setups in the making"
  (strong structure / weak trigger per rule 5), any "hidden gem" you flag via
  rule 6 (your own TA/price-action/IV), PLUS any research-gate downgrade from
  PRIME (checks 1–3 🔴 flag) — briefly justify any override or flag. Watch-only.
- **📋 Trade Directives:** per-PRIME execution plan blocks (see TRADE DIRECTIVE section above).
- Columns:
  - **Table 1 (PRIME):** [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
    | [Suggested Structure: Short/Long, short BELOW support] | [Exact R/R & Credit,
    1:1.5–2.5] | [🔎 Research: status + one-line note] (e.g. "clear" or "earnings:
    unknown — verify manually").
  - **Table 2 (RADAR):** [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
    | [Suggested Structure: Short/Long, short BELOW support] | [Exact R/R & Credit,
    1:1.5–2.5]. List which checks are 🟢 vs 🔴 (+ override reason). Rows arriving
    via research-gate downgrade must state it explicitly (e.g. "Research-gate
    downgrade: ANALYST_RED_FLAG — Morgan Stanley downgrade to Underweight, 2026-08-20").
- **PROVISIONAL note**: any name whose live mid-session bar differs from its settled state.
- **Rejects**: grouped one-liners (below MA / no support / interval-reject). One
  grouped line: `prescreen-filtered: <prescreen_filtered_count> names (failed loose MA bands locally, never sent through IBKR)`
  plus prescreen "failures" listed with reasons. A `SEC_RED_FLAG` hard-reject
  gets its own clearly labeled one-liner naming the specific finding (not
  lumped anonymously with technical rejects).

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

SCREENER_CONSTITUENTS = the shortlist tickers. SIGNALS_COMPLETED +
SIGNALS_FAILED MUST equal the length of the prescreen shortlist (not the full
universe). Prescreen failures are accounted in the wrapper, not in these
counters. If any shortlisted ticker failed, list which ones and why in the
Rejects section — do NOT silently drop it from the count. SIGNALS_FAILED > 0
means the run is treated as a failure by the wrapper script even if the report
body looks complete — this is intentional: a real tool/data failure must never
be reported as a clean run.
