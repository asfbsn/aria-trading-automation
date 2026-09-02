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
array through the two-phase flow below.

Phase A classifies the bulk of the shortlist (~125–130 names) locally from
prescreen's data alone with zero tool calls. Phase B runs the full IBKR bars +
`compute_signal.py` verification ONLY for the finalist set (names with local
`entry_confirmed: true` OR prescreen data-failures; typically <=15 names).
Prescreen data is authoritative for non-finalist REJECT/RADAR decisions;
IBKR bars + `compute_signal.py` remain the sole authoritative signal source for
all finalists and every name that can reach a trade directive.

Also read `data/universe.csv` (the file itself, via Read — columns:
`ticker,sector,approx_mktcap_usd`), but now only for sector lookups (portfolio
guards in TRADE DIRECTIVE) and the filtered-count context. This file represents
the membership filters (Region=US, Mkt cap 10B–5T USD, derived from Russell 1000
constituent weights — see `scripts/refresh_universe.py`). The local prescreen
stage filters this universe down to the shortlist using widened technical bands,
and you must account for every ticker in the shortlist without sampling or further
pre-filtering.

## Scope
- **Daily (1D) interval only.**
- **TOKEN ECONOMY:** two-phase processing. Classify all ~140 shortlisted tickers
  locally in Phase A (zero tool calls, near-zero tokens); run IBKR bars +
  `compute_signal.py` in Phase B only for the finalist set (typically <=15 names).
  Pull option chains only for PRIME-eligible names (see R/R VERIFICATION). Write
  the report and exit cleanly.
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
  were never extractable. `scripts/compute_signal.py` (and prescreen's
  `signal_core.entry_checks()`) is the signal source: a deterministic local
  computation built from the indicator's *declared input parameters* (RSI
  length 20 / threshold 50, MA50, MA150, Volume MA length 20). It is an
  approximation of Adi's original signal, not a replica — treat its
  `entry_confirmed` the way you'd treat your own independent technical read.

## Two-Phase Processing Flow

### Phase A — local classification (all ~140 shortlisted names):
Read the prescreen JSON and `data/universe.csv` FIRST (already covered above).
Phase A itself makes ZERO further tool calls — no `search_contracts`, no
`get_price_history`, no WebSearch, nothing per-ticker. It only reads fields
already sitting in the prescreen JSON you already loaded. For every ticker in
the shortlist:
- If it's in prescreen's `"failures"` list → it joins the finalist set (Phase B),
  do not classify it locally (no data to classify from). Data-failure tickers
  get a fair independent shot via real IBKR data, preserving the rule that
  prescreen never silently drops a data failure.
- Else read its `per_ticker[ticker]` entry:
  - If `entry_confirmed: true` → it joins the finalist set (Phase B).
  - If `entry_confirmed: false` → classify NOW, no IBKR needed:
    - **RADAR** if structure is strong per the rule 5 threshold (mirror it
      exactly): `above_ma150`, `near_ma150_support`, `volume_above_avg` all
      true, momentum/candle checks (`rsi_below_50`, `rsi_rising`,
      `bullish_candle`) weak. List which checks are 🟢/🔴 from prescreen's
      `checks` dict directly — same format as today, just sourced from
      prescreen instead of a fresh IBKR compute.
    - **REJECT** otherwise — grouped one-liner reason from which prescreen
      check(s) failed (below MA-150 / no support / etc.), same style as today.
    - **Rule 6 "hidden gem" discretion** still applies here, but is now based
      on what prescreen's local data shows (RSI, MA%, volume, candle_pattern) —
      state explicitly in any hidden-gem row that the read is from local
      prescreen data, not a fresh IBKR chart pull (this is a real capability
      narrowing versus before — state this plainly).

### Phase B — full IBKR verification (finalist set only, typically <=15 names):
For each name in the finalist set (local `entry_confirmed: true` OR a prescreen
data-failure), run EXACTLY the per-ticker pipeline:
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
5. Classify (REJECT/RADAR/PRIME) from the real IBKR-derived `entry_confirmed`
   and checks (IBKR result is authoritative here, it can overturn prescreen's
   local read in either direction — that's expected and fine).
   - PRIME-eligible names from this phase proceed into R/R VERIFICATION →
     QUALITATIVE RESEARCH GATE → TRADE DIRECTIVE exactly as today (do not
     change those three sections' internal logic at all).
   - Non-PRIME names drop to RADAR or REJECT with their IBKR-derived checks.

**Phase B execution rules:**
- Process finalist tickers with minimal narration, in tight sequence.
- `compute_signal.py` accepts `get_price_history`'s native parallel-array shape
  (`{"ticker": "...", "time": [...], "open": [...], "high": [...], "low": [...],
  "close": [...], "volume": [...]}`) directly — do NOT hand-transform it into
  `{"bars": [{"date","open",...}, ...]}` yourself, and do NOT hand-truncate the
  last bar yourself either. Use the `--exclude-last-bar` flag instead (see
  TIMING below) — the script does the drop internally.
- **DO NOT pass the JSON as a heredoc or an `echo ... |` pipe into Bash — ever.**
  A Bash command whose argument literally contains JSON (any `{`/`"` together)
  gets silently auto-denied by Claude Code's own command-safety heuristic as
  "expansion obfuscation" — not the allowedTools gate, no quoting fixes it —
  and the run will sit with zero output until the wrapper's timeout kills it.
  The Write-file-then-`--input`-flag path above is the only safe route.
- **Earnings proximity is not automated** (no data source wired for it). If you
  already know a name's earnings date, factor it in; otherwise mark "earnings:
  unknown" and do not gate PRIME on it — flag for manual check instead.

## ⚠️ Timing — settled vs provisional (applies within Phase B)
This runs at 19:00 Israel ≈ **12:00 ET, mid US session**, so today's daily candle
is **unsettled and can still flip**. `get_price_history`'s last bar may be
today's live/in-progress session. In Phase B, run `compute_signal.py` **twice**
per finalist ticker on the SAME `get_price_history` response written once to that
ticker's `signal_input_<TICKER>.json` (don't re-fetch or rewrite the file):
1. **SETTLED (authoritative)** — add `--exclude-last-bar` (drops today's
   in-progress bar internally; output has `"settled": true`). This drives
   every PRIME/RADAR/REJECT decision.
2. **PROVISIONAL (today, unsettled)** — no flag, full bars including today;
   output has `"settled": false`. Label it explicitly as subject to change
   before the US close. Do NOT issue entries off the provisional read alone.
   (PROVISIONAL drives only the Trade Directive's EXECUTE-NOW-vs-HOLD trigger,
   which only finalists reach).

## Selection rules (my mentor's risk management)
In Phase A, rules are evaluated against prescreen's local data. In Phase B,
"MA-150", "RSI", "entry confirmation" mean `compute_signal.py`'s **SETTLED**
output from IBKR bars — that's the authoritative signal source for finalists.
1. Stock MUST trade ABOVE its MA-150 (`checks.above_ma150` — below = falling
   knife → reject).
2. Short Put MUST be OTM, chosen via tiered search (deepest-safest first):
   primary target is below MA-150 or swing low with clearance; if R/R 1:1.5–2.5
   cannot be met, short strike may flex up to sit AT MA-150 or basing support,
   but NEVER above MA-150. Stop at first (deepest) passing strike. (See
   R/R VERIFICATION for full tiered mechanics).
3. R/R target 1:2 (~1/3 of width as credit). Acceptable band **1:1.5 → 1:2.5**.
   Same ratio at any width: $10-wide needs credit $4.00 (1:1.5) … $2.86 (1:2.5);
   $2.50-wide needs $1.00 … $0.71; $1-wide needs $0.40 … $0.29. Reject worse than 1:2.5.
4. Strike-interval AND width aware (see R/R VERIFICATION's width search): if wide
   gaps (e.g. JBHT/APD only list $10 strikes near the money) prevent hitting
   1:1.5–2.5 at primary depth even after testing every listed width, step up
   through the flexibility tier (up to MA-150). REJECT only if no width/depth
   combo at any tier clears the 1:1.5–2.5 band.
5. Read every check in `checks` object, not just `entry_confirmed`:
   `above_ma150`, `rsi_below_50`, `rsi_rising`, `near_ma50_pullback`,
   `near_ma150_support`, `volume_above_avg`, `bullish_candle` (+ `candle_pattern`
   for context — doesn't gate). Official entry = `entry_confirmed: true` (all
   seven gating checks pass). Discretionary (RADAR) = `entry_confirmed: false`
   BUT structure strong (`above_ma150`, `near_ma150_support`, `volume_above_avg`,
   no imminent earnings) while only momentum/candle checks are weak — flag it,
   don't discard. List which checks are 🟢 vs 🔴.
6. AI autonomy: you may ALSO flag a "hidden gem" from your own technical read
   (chart structure, price action, implied volatility) even when
   `entry_confirmed` is false or checks are mixed — briefly justify the
   override. In Phase A, base this on prescreen's local indicators; state
   explicitly that the read is from local prescreen data. Such picks are RADAR
   only, never PRIME.

## R/R VERIFICATION against the LIVE option chain — PRIME gate
`entry_confirmed: true` is **NOT** enough to be PRIME. For **each PRIME-eligible
name only** (passed rules 1,3,5: above MA-150 + full confirmation + strong
structure — usually 0–3 names; token economy: do NOT price rejects/RADAR),
verify a COMPLIANT spread actually exists using the IBKR **read-only** option
tools:
1. `search_contracts` (security_type STK) → `underlying_contract_id` (exact symbol match, US primary listing).
2. **IV percentile check (underlying, once per candidate):** call `get_price_snapshot`
   on `underlying_contract_id` (the STK contract from step 1) with
   `market_data_names: ["implied_volatility_percentile"]`.
   - **Field name / response key convention:** Request arguments use UNDERSCORES
     (`implied_volatility_percentile`), but IBKR response keys use HYPHENS
     (`implied-volatility-percentile`). Read `high_13w`, `high_26w`, `high_52w`
     sub-fields — these are FRACTIONS (0.0 to 1.0; e.g. 0.294 = 29.4th percentile;
     multiply by 100 for display/check).
   - **Terminology note:** This uses **IV PERCENTILE** (how current IV compares
     to the trailing 13/26/52-week range of IV highs), NOT "IV Rank"
     ((current IV − 52wk low) / (52wk high − 52wk low)). IBKR MCP exposes IV
     percentile only. Do NOT use `option_midpoint_iv` (per-option IV is unreliable
     on IBKR MCP and returns `isValid: false`).
   - **Verdict rule (26-week window `high_26w * 100` as primary threshold; 13w/52w for context):**
     - If `high_26w * 100 < 30` → **Downgrade to RADAR** (Table 2), reason
       `IV_LOW: 26-week IV percentile <NN>% < 30 -- insufficient premium for the risk taken`
       (still proceed to steps 3–7 to price the spread for Table 2).
     - If `high_26w * 100 > 80` → add informational note ONLY (NOT a downgrade; does
       not change classification): `IV_HIGH: 26-week IV percentile <NN>% > 80 -- elevated premium may reflect a priced-in event; verify no unexplained catalyst`.
     - If field is missing / invalid / tool error → note `IV: unavailable — verify manually`
       (same tier as `earnings: unknown — verify manually`; do NOT downgrade or reject
       on that basis alone).
3. `get_option_parameters` → pick the expiration nearest **~30 DTE**.
4. `get_option_data` (bound strikes around support) → `put_contract_id`s at/below the MA-150 & swing low.
5. `get_price_snapshot` on candidate short & long puts with
   `market_data_names: ["bid_ask", "option_open_interest"]` (request uses
   underscores; response keys are `bid_ask` and hyphenated `option-open-interest`
   with `callInterest` and `putInterest` integer sub-fields — read `putInterest`
   for put legs). Compute mids: `mid = (bid + ask) / 2`.
6. **Option liquidity gate (hard REJECT, per-leg) — evaluated PER CANDIDATE
   STRIKE PAIR, inside the tiered strike search in step 7, not as a one-shot
   pre-check.** For EACH candidate leg (short put AND long put) of whichever
   strike pair the tiered search is currently testing, verify:
   - Bid-ask spread: `(ask − bid) ≤ $0.30` on BOTH legs.
   - Open interest: `putInterest ≥ 100` on BOTH legs.
   - If `(ask − bid) > $0.30` OR `putInterest < 100` on EITHER leg → this
     candidate strike pair fails, exactly like an R/R-band miss — do NOT
     compute R/R for it, move to the next strike the tiered search would
     try (same tier, then Tier 2). Do not reject the whole name over one
     failed pair while other untested strikes remain.
   - Only once EVERY candidate pair across BOTH tiers has been tried and
     none clears BOTH the liquidity gate AND the R/R band → the name is a
     **REJECT**, reason `Liquidity gate: no strike pair cleared bid-ask
     ≤$0.30 and open interest ≥100 across tested strikes` (or, if some pairs
     were liquid but none cleared R/R, use the existing R/R-gate reject
     reason below instead — name the actual blocking condition, don't
     default to blaming liquidity if R/R was the real blocker).
7. **Width search (dynamic, chain-driven — never assume a fixed width).** At
   whichever short-strike depth is currently being tested (Tier 1 or Tier 2
   below), read the actual strike spacing (S) from step 4's `get_option_data`
   result around that strike — this is a real per-name, per-expiry market fact,
   not a script setting: a $17 stock may list S=$1, an $85 stock S=$2.50, and a
   name like JBHT/APD only $10 apart even close to the money (confirmed live
   2026-09-02: JBHT Oct'26 chain near $263 spot lists strikes at 230/240/250/260
   — no $1/$2.50/$5 increments exist there at all; that's the chain, not a
   choice). Test every long-put strike below the short strike that's actually
   listed, which typically yields candidate widths **S, 2S, and 4S** (skip any
   not actually present on the chain). For each width, compute credit/max-loss/
   R-R (step 8) and the liquidity gate (step 6) on both legs. State which S you
   identified and which widths you tested.
   - **Width selection rule:** among the widths that clear BOTH the liquidity
     gate and the R/R band at this depth, the PRIMARY pick is the WIDEST one —
     it carries a larger credit cushion against the fixed $0.30 bid-ask gate and
     the ~$2–3 round-trip commission per spread (both are a much bigger bite out
     of a $1-wide's credit than a $10-wide's). Record every narrower width that
     also cleared as a FALLBACK, ordered narrowest-first — TRADE DIRECTIVE sizing
     may need one of these if the primary width doesn't fit the account's
     per-trade budget (see HOW MUCH). A width that clears the gates but is
     dropped only because a wider one also cleared is not a rejection — keep its
     numbers on hand for the fallback.
8. Compute: **credit = short_mid − long_mid**; **max loss = width − credit**; **R/R = maxloss : credit**.
   Apply the **tiered strike search (deepest-safest first)**, testing the
   liquidity gate (step 6) and width search (step 7) together on each candidate
   depth — at least one width at that depth must clear BOTH to be eligible:
   - **Tier 1 (Primary target):** Short strike placed a few percentage points below MA-150, OR below the recent local swing low (lowest wicks of recent daily candles) — whichever gives more room. Test if any tested width hits R/R **1:1.5–2.5** (credit ≈ width/3.5 … width/2.5).
   - **Tier 2 (Flexibility tier):** If no width at Tier 1 can hit R/R 1.5–2.5, walk the short strike up — as far as sitting AT the MA-150 line itself, or the lower edge of a genuine multi-day consolidation/basing zone — but **NEVER above MA-150** (absolute hard ceiling).
   - **Selection rule:** Walk the strike up from the primary target only as far as strictly necessary to clear 1.5–2.5 AND the liquidity gate (at any tested width), and stop at the first (deepest/safest) strike that clears both. Do not pick a shallower strike if a deeper one works.
- If no strike/width combo across either tier satisfies 1:1.5–2.5 with sufficient liquidity (even with short at MA-150) → the name is a **REJECT**, reason
  "R/R gate: cannot hit 1:1.5–2.5 even at MA-150 flexibility tier" (e.g. the APD/AMZN case where wide strike intervals prevent compliant credit even at the MA-150 ceiling) — do
  NOT list it as PRIME and NEVER place short strike above MA-150.
- **REJECT always wins over RADAR:** a name with no compliant liquid spread (this step) never becomes RADAR via an IV_LOW or research-gate flag below — those downgrades only ever apply to a name that already has a valid, priced, liquid, R/R-compliant spread. A name with no compliant spread has nothing to price for Table 2 and stays REJECT, full stop.
- For every PRIME row, report the **verified exact strikes, credit, max loss, max profit, and R/R for the PRIMARY width**; list any FALLBACK widths compactly (one line each: strikes, credit, max loss, R/R).
- NEVER use order tools (`create_order_instruction`); read-only only.

## 🔎 QUALITATIVE RESEARCH GATE — PRIME-eligible only
Runs ONLY on names that survive R/R VERIFICATION (passed technical rules 1,3,5 AND
verified a compliant 1:1.5–2.5 spread via tiered strike search at/below MA-150 — typically 0–3
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

- **WHAT:** ticker, exact short/long put strikes, expiration date (the ~30 DTE expiry already chosen in R/R VERIFICATION — do not re-pick), and which width is being used (PRIMARY, or a FALLBACK — see HOW MUCH).
- **WHEN (execution-window rule):** this scan runs inside the 12:00–13:00 ET execution window (19:00–20:00 Israel). Directive = `EXECUTE NOW` **only if** the PROVISIONAL pass (unsettled bar, no `--exclude-last-bar`) shows ALL THREE of: `above_ma150`, `near_ma150_support`, `volume_above_avg` — i.e. today's live bar is confirming the settled signal at support with real volume, not fighting it. If any of the three is false on the provisional read → directive = `HOLD — provisional bar not confirming; re-evaluate next scan` (name which check failed). This gates EXECUTION TIMING only — PRIME classification itself stays based on the SETTLED read, per the existing Timing section.
- **AT WHAT PRICE:** entry limit credit = the verified mid credit from R/R VERIFICATION **for the width actually selected for sizing** (state $ — re-state it if a FALLBACK width ended up used instead of PRIMARY); minimum acceptable credit = that width/3.5 (the 1:2.5 floor, state $) — if fills would require accepting less, do not chase, skip the trade.
- **R/R restated:** print max loss ÷ credit; hard rule: if outside 1.5–2.5, NO directive is emitted for the name (even if it somehow reached this section) — it reverts to REJECT with reason "R/R outside 1.5–2.5 at directive stage".
- **Portfolio guards (check BEFORE sizing):** call `get_account_positions` (read-only). Count currently-open bull-put-spread positions — a position counts ONLY as a validated leg pair: same underlying, same expiration, both PUTS, offsetting quantities (short leg negative, long leg positive, equal magnitude), and short strike ABOVE long strike; unpaired or ambiguous option legs are NOT counted as spreads but must be flagged in the report as "unpaired legs — verify manually". If validated pairs ≥ 2 → `BLOCKED — 2-position cap reached`. If any open position's underlying is in the SAME sector as this candidate (sector column from `data/universe.csv`) → `BLOCKED — sector correlation with <existing ticker>`. Blocked names remain listed as PRIME in Table 1 (signal is real) but their directive block states the BLOCKED status instead of an executable order.
- **HOW MUCH (position sizing, updated 2026-09-02 — "High-Conviction / Velocity" model):** call `get_account_summary` (read-only) for current net liquidation value. `contracts = floor( (0.25 × net_liq) / (max_loss_per_contract × 100) )` where `max_loss_per_contract = width − credit` (per share, for the width being tried) — use the **minimum acceptable credit (width/3.5, the same floor stated in AT WHAT PRICE)** for this `credit`, not the verified mid. The entry order may fill anywhere from mid down to that floor; sizing off mid would understate max_loss (and oversize the position) if the actual fill lands at the floor. 25% per trade × the 2-position cap above = the same **50% total portfolio risk ceiling** as before — only the per-trade/position-count split changed, not the total. **This is a materially more concentrated model than the prior 10%/5-position split** — a single max-loss event is now −25% of the account, and two correlated breaches (a broad-market drawdown hitting both open names at once, the exact 2022-style scenario the bear-track kill was grounded in) is −50% in one stroke. Sizing here still uses the floor credit (worst-case fill), not an assumption that active management/GTC exits will realize less than max loss — that assumption is unbacktested and a single-day gap through the MA150 stop can realize near-max loss before `exit-guard.sh`'s once-daily pre-open check ever runs.
  - Try the PRIMARY (widest-clearing) width first — at 25%/$750-on-$3k this comfortably reaches $5-wide and often $10-wide on this universe, unlike the prior 10%/$300 budget which was narrow-width-only. Show the arithmetic inline (net_liq, allocation $ = 25% of it, per-contract max loss $, resulting integer).
  - **If the result is 0, retry with each FALLBACK width from R/R VERIFICATION, narrowest first** (smaller max_loss_per_contract fits a small allocation more easily) — re-show the arithmetic for the width that actually sizes to ≥1 contract, and update WHAT/AT WHAT PRICE above to match that width, not the primary one.
  - **Only if EVERY tested width (primary + all fallbacks) still sizes to 0 → directive = `BLOCKED — spread too wide for current account equity even at narrowest available width`; never emit a 0-contract order.**
- **Macro context note:** if a major scheduled macro event (from the scan-start Market Context check) falls inside the ~30 DTE window, note it here for context (does not gate execution).
- **EXITS (mandatory in every executable block):**
  - Profit-take: place GTC buy-to-close at **20% of received credit** (captures 80% of max profit; state the $ price). Note: this 80%-capture target is the default baseline GTC order; exits remain dynamically manageable by the exit guard / discretion on momentum and market conditions.
  - Stop: close if the underlying CLOSES below the short strike OR below its MA150 (identical thresholds to `prompts/bull-put-spread-exit.md`'s exit guard — the systems are deliberately symmetric; note: if short strike sits at MA150 from flexibility tier, stop triggers on MA150 breach close to entry, which is expected).
  - Time stop: close at DTE ≤ 7 unconditionally UNLESS the position is underwater (`pct_max_profit_captured < 0`) with its thesis still technically intact, in which case it becomes a recommended (not forced) exit (`TIME_RISK`) with max loss stated and framed as a deliberate reversal bet.
  - Earnings risk: a position can separately trigger on imminent earnings date falling before expiry with price within 5% of the short strike (`EARNINGS_RISK`, recommended not forced), evaluated daily post-entry by `exit-guard.sh` (the entry scan's own research gate already covers earnings before entry).

Reminder: this is an advisory directive — the human places every order; NEVER use order tools.

## Output — TWO separate tables (prevents execution errors; live money soon)
Decision basis = the SETTLED (prior closed candle) state; flag PROVISIONAL (today's
unsettled) changes separately. Produce, in order:

- **Headline** (counts: prime / radar / rejects / finalists verified / how many tickers in prescreen shortlist).
  These three counts (prime/radar/reject) MUST equal the actual number of rows
  you write in Table 1, the actual number of rows in Table 2, and the actual
  count of tickers named across the Rejects section, respectively — compute
  the headline numbers AFTER the tables/rejects are written, by counting what
  you just wrote, never from a running tally kept during classification. A
  running tally drifts silently over ~150+ sequential classifications; a
  finished table's row count does not.
- **Market Context** (3–5 lines: VIX level, SPY vs its own MA150 trend, upcoming major macro events inside ~30 days).
- **Table 1 — 🟢 PRIME CANDIDATES (ready for execution):** ONLY stocks with
  `entry_confirmed: true` + strong structure that pass the MA-150 rule, have a
  **live-chain-verified** 1:1.5–2.5 spread via tiered strike search at/below MA-150
  (see R/R VERIFICATION), AND clear the qualitative research gate (all checks clear or
  unknown/unavailable without red flags). Evaluated in this order: a confirmed
  name with no compliant liquid spread (fails R/R and/or the liquidity gate
  across every tested strike) is a REJECT — full stop, it never reaches the
  IV/research checks below. Only a name that HAS a compliant liquid spread
  proceeds to IV/research: check 4 (SEC) downgrades that name to REJECT; any
  🔴 research flag on checks 1–3 or IV percentile below 30 (`IV_LOW`)
  downgrades it to RADAR instead. Table 1 rows are the ones that cleared every
  stage — these alone are execution-ready.
- **Table 2 — 🟡 RADAR / WATCHLIST (discretionary):** "setups in the making"
  (strong structure / weak trigger per rule 5), any "hidden gem" you flag via
  rule 6 (your own TA/price-action/IV), PLUS any research-gate downgrade from
  PRIME (checks 1–3 🔴 flag) or IV-percentile downgrade (`IV_LOW`) — briefly
  justify any override or flag. Watch-only.
- **📋 Trade Directives:** per-PRIME execution plan blocks (see TRADE DIRECTIVE section above).
- Columns:
  - **Table 1 (PRIME):** [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
    | [Suggested Structure: Short/Long (short at/below MA-150)] | [Exact R/R & Credit,
    1:1.5–2.5] | [🔎 Research: status + one-line note] (e.g. "clear" or "earnings:
    unknown — verify manually").
  - **Table 2 (RADAR):** [Ticker] | [Daily Confirmation / why] | [MA-150 / Swing Low]
    | [Suggested Structure: Short/Long (short at/below MA-150)] | [Exact R/R & Credit,
    1:1.5–2.5]. List which checks are 🟢 vs 🔴 (+ override reason). Rows arriving
    via research-gate or IV-percentile downgrade must state it explicitly (e.g.
    "Research-gate downgrade: ANALYST_RED_FLAG — Morgan Stanley downgrade to Underweight, 2026-08-20"
    or "IV downgrade: IV_LOW — 26-week IV percentile 22% < 30 -- insufficient premium for the risk taken").
- **PROVISIONAL note**: any name whose live mid-session bar differs from its settled state.
- **Rejects**: grouped one-liners (below MA / no support / interval-reject). One
  grouped line: `prescreen-filtered: <prescreen_filtered_count> names (failed loose MA bands locally, never sent through IBKR)`
  plus prescreen "failures" listed with reasons. A `SEC_RED_FLAG` hard-reject
  or liquidity-gate REJECT gets its own clearly labeled one-liner naming the specific
  finding (e.g. "Liquidity gate: short put bid-ask $0.45 > $0.30" or "Liquidity gate: long put open interest 42 < 100",
  not lumped anonymously with technical rejects).

After the report, emit these FIVE lines, each on its own line, in this exact
order, as the literal last thing you output:

SCREENER_CONSTITUENTS: SYM1,SYM2,SYM3,...
SIGNALS_COMPLETED: <MUST be computed as (the exact number of tickers you just
  listed in SCREENER_CONSTITUENTS above) minus SIGNALS_FAILED below — count the
  comma-separated symbols you just wrote, do not use a separately-remembered
  running tally from classifying tickers one by one. This is the same number
  that must equal prime-rows + radar-rows + reject-tickers-named from the
  Headline check above; if your Headline counts and this number disagree,
  recount both from what you actually wrote before emitting either.>
SIGNALS_FAILED: <count of tickers you could NOT reach ANY definitive answer for — a genuine tool exception, timeout, or empty/malformed response where you got nothing usable. A shortlisted ticker missing from prescreen's JSON entirely also counts here>
FINALISTS_VERIFIED: <count of finalist tickers where Phase B reached a definitive, final answer — REJECT, RADAR, or PRIME. This INCLUDES `insufficient_data` (compute_signal.py ran and told you there weren't enough bars — that's a real, final REJECT reason, not a failure) and "no contract found" (search_contracts genuinely returned nothing — also a real, final REJECT reason). The litmus test: if you can write a REJECT reason for the ticker, it counts here, NOT in SIGNALS_FAILED>
FINALISTS_VERIFIED_TICKERS: <comma-separated list of the exact tickers counted in FINALISTS_VERIFIED above — the wrapper compares this set (not just the count) against the prescreen-derived expected finalist set, so it must name every one, no substitutions>

**Litmus test for SIGNALS_FAILED vs a REJECT (applies in both Phase A and Phase B):**
if you have ANY definitive answer to report — including "insufficient data",
"no contract found", "below MA-150", or any other concrete reason — that ticker
is COMPLETED/VERIFIED with a REJECT classification, never FAILED. SIGNALS_FAILED
is reserved ONLY for a ticker where a tool call itself broke (exception, timeout,
garbage response) and you have nothing to write down at all.

SCREENER_CONSTITUENTS = the shortlist tickers. SIGNALS_COMPLETED +
SIGNALS_FAILED MUST equal the length of the prescreen shortlist (not the full
universe). Prescreen failures are accounted in the wrapper, not in these
counters. If any shortlisted ticker failed, list which ones and why in the
Rejects section — do NOT silently drop it from the count. SIGNALS_FAILED > 0
means the run is treated as a failure by the wrapper script even if the report
body looks complete — this is intentional: a genuine tool-call breakage must
never be reported as a clean run. `insufficient_data` and "no contract found"
are NOT tool-call breakages — see the litmus test above.

FINALISTS_VERIFIED must equal the count of (prescreen entry_confirmed==true tickers)
+ (prescreen failures count) — the wrapper validates this independently from the
prescreen JSON, so don't try to game it by routing fewer names to Phase B than
prescreen's data implies. FINALISTS_VERIFIED_TICKERS must be exactly that same
set of tickers (order doesn't matter, the wrapper sorts both sides) — matching
the count alone is not enough; the wrapper checks membership too, so silently
swapping one expected finalist for an unexpected one fails the run even if the
count still lines up.
