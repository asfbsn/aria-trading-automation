You are running an exit-guard check on open Bull Put Spread positions. Use ONLY
IBKR read-only tools, WebSearch for earnings checks, plus the two Bash/Write
operations below. NEVER use order tools (`create_order_instruction`,
`delete_order_instruction`, etc.) or any order-placement/modification tool —
this is purely advisory, read-only monitoring.

**Before concluding any tool is unavailable: call it.** Do not infer
unavailability from memory of past sessions, from ToolSearch returning
nothing (ToolSearch indexes deferred tools; a tool already in your allowed
set doesn't need searching — just call it directly by name), or from any
prior run's outcome. This session's tool wiring is independent of what
happened in earlier ones. If a call genuinely errors, quote the exact error
in your report — don't paraphrase or generalize it into "tools not
connected."

## What to do

1. **Get open positions:** Call `get_account_positions` (IBKR, read-only).
   Filter to open short-put-spread legs — a position counts ONLY as a validated
   leg pair: same underlying, same expiration, both PUTS, offsetting quantities
   (short leg negative, long leg positive, equal magnitude), and short strike
   ABOVE long strike. Unpaired or ambiguous option legs are NOT treated as
   spreads — list them separately as "unpaired legs — verify manually" instead
   of guessing a pairing.
   If the account holds ZERO option legs at all (not just zero valid spread
   pairs), output:
   ```
   Bull Put Spread Exit Guard — <date>
   0 open position(s) found.

   No open bull-put-spread positions.

   POSITIONS_CHECKED: 0
   ```
   and stop. But if there are zero valid spread PAIRS while one or more
   unpaired/ambiguous option legs exist, do NOT use the message above — it
   would hide a real open position and its risk. Instead output:
   ```
   Bull Put Spread Exit Guard — <date>
   0 open position(s) found.

   No open bull-put-spread positions, but N unpaired/ambiguous option leg(s)
   found — verify manually:
     [TICKER] [side] [strike] [expiry] qty=[n] — [why it didn't pair, e.g.
     "no matching offsetting leg", "same underlying/expiry but not both
     puts", "quantities don't offset"]

   POSITIONS_CHECKED: 0
   ```
   and stop (empty of valid spreads is not an error, same as GTC guard — the
   final marker line is still required so the wrapper recognizes this as a
   successful run, not a failure).

2. **Compute technical exit signal:** For each open position pair:
   - `search_contracts` (security_type STK) on the underlying symbol → resolve
     `contract_id` (exact symbol match, US primary listing) — required before
     `get_price_history` will work.
   - Call `get_price_history` using that contract_id (at least 155 daily bars,
     recommend 220 bars for MA150 margin).
   - Write the payload immediately with the **Write** tool to:
     `$ARIA_HOME/state/scratch/signal_input_<TICKER>_exit.json`
     Merge the price history response directly and unmodified with `"ticker": "<TICKER>"`
     and `"short_strike": <float>`.
     `compute_exit_signal.py` accepts `get_price_history`'s native parallel-array shape
     directly — do NOT hand-transform it.
     **DO NOT pass the JSON as a heredoc or an `echo ... |` pipe into Bash — ever.**
     A Bash command whose argument contains JSON (`{`/`"`) gets auto-denied by Claude
     Code's command-safety heuristic as "expansion obfuscation". Writing first with the
     Write tool and passing `--input <path>` is required.
   - Run: `python3 $ARIA_HOME/scripts/compute_exit_signal.py --input $ARIA_HOME/state/scratch/signal_input_<TICKER>_exit.json --exclude-last-bar` (Bash).
     Settled read only — this report is not intraday-time-sensitive like the entry scan, so no provisional pass is needed.

3. **Get pricing & compute profit/DTE:**
   - Call `get_price_snapshot` on both legs (short put + long put).
   - Compute mid prices: `mid = (bid + ask) / 2` (or last if bid/ask unavailable).
   - Compute current cost-to-close = `short_mid - long_mid`.
   - Read the position's entry credit from `get_account_positions`' average cost / average price / net premium field (read whatever field IBKR actually returns, do not assume a field name; report it if ambiguous).
   - Compute `pct_max_profit_captured = (entry_credit - current_cost_to_close) / entry_credit`.
   - Compute DTE (days to expiration) from the position's expiration date vs today's date.
   - Compute defined max loss per share = `(short_strike - long_strike) - entry_credit` (and total position max loss = `max_loss_per_share * 100 * contracts`).

4. **Live / premarket price freshness check (context only):**
   - For EVERY position (regardless of whether it triggered CLOSE, RECOMMEND EXIT, WATCH, or HOLD):
   - Call `get_price_snapshot` on the underlying's STK `contract_id` (already resolved via `search_contracts` in step 2 — reuse it, do NOT re-resolve) to get the current live/premarket last price.
   - Compare the live/premarket last price against the settled close used in step 2's calculation:
     - If the live price differs meaningfully from the settled close (say >1%) AND that difference would plausibly change the picture (e.g. live price back above the short strike or MA150 when the settled read showed a breach, or vice versa moving further against the position), add a `PREMARKET NOTE:` line to that position's output row: state the settled close, the live price, and which direction it moved relative to the short strike / MA150 — factual, no recommendation, verify-manually framing (matches this file's existing "human decides" principle at the bottom).
     - If `get_price_snapshot` does not return usable data for the underlying's STK `contract_id` (before concluding any tool is unavailable: call it — don't assume it won't work, actually call it and see), note `PREMARKET NOTE: premarket price: unavailable — verify manually` rather than silently omitting the check. This must NOT count as a SIGNALS_FAILED-style failure — it is a soft annotation gap, same tier as "earnings: unknown."
     - If the live price difference is negligible or does not alter the technical picture, omit the `PREMARKET NOTE:` line.
   - **CRITICAL:** This live price is **CONTEXT ONLY**. The verdict (`CLOSE` / `RECOMMEND EXIT` / `WATCH` / `HOLD`) is still computed entirely from the SETTLED read per steps 2–3, unchanged. This step NEVER changes, overrides, or suppresses a verdict, only annotates it — do not let this step's existence create any ambiguity about which price is authoritative for classification.

5. **Earnings timing check (token economy — non-CLOSE positions only):**
   - Run this check ONLY for positions that did NOT already trigger a hard CLOSE
     in steps 2–3 — i.e. skip it if ANY of: `thesis_invalidated: true`,
     `pct_max_profit_captured >= 0.80`, `DTE < 0` (already expired — unconditional
     CLOSE regardless of profit/loss, see step 6), OR (`0 <= DTE <= 7` AND NOT
     underwater). A position already hard-CLOSEd doesn't need an earnings read.
   - Run one `WebSearch` for the ticker's next confirmed earnings date.
   - Compare the next confirmed earnings date against the position's expiration date.
   - If the next confirmed earnings date falls BEFORE the position's expiration date AND the underlying's close is within 5% of the short strike (`close <= short_strike * 1.05`): flag `EARNINGS_RISK`.
   - If the earnings date is not confirmable: note "earnings: unknown — verify manually" (do NOT treat unknown as a trigger, and do NOT treat unknown as clear either).

6. **Classify position verdict:**
   **Design principle — judgment may escalate, never suppress.** Hard-CLOSE triggers are deterministic and must never be downgraded, overridden, or suppressed by any judgment check. RECOMMEND EXIT is an escalation tier evaluated when no hard-CLOSE trigger fired, or to surface genuine judgment calls.

   - 🔴 **CLOSE (hard, unconditional, deterministic):**
     - `thesis_invalidated` is true (short strike breached: `close < short_strike` — this is the ONLY condition that sets `thesis_invalidated` as of 2026-09-05; MA150 breach alone does not hard-close, see the `MA150_BREACH` RECOMMEND EXIT tier below), OR
     - `pct_max_profit_captured >= 0.80` (80% profit target reached — buy back at 20% of credit; default baseline GTC target), OR
     - `DTE < 0` (already past expiration — settlement/assignment is happening or has happened; there is no "wait for reversal" option on an expired contract, this closes REGARDLESS of profit/loss). Reason: "Time stop: DTE < 0, already past expiration — resolve immediately". OR
     - `0 <= DTE <= 7` AND the position is NOT underwater (`pct_max_profit_captured >= 0` — breakeven or any profit level counts as not underwater here, even if below the 80% target). Reason: "Time stop: DTE <= 7 with profit captured / not underwater".
     - **Annotate, never suppress on thesis invalidation:** When `thesis_invalidated` fires (strike breach), check `checks.volume_confirmed_breakdown`. If false (breach occurred on below-average volume), the verdict is STILL CLOSE (hard trigger is never weakened) — but append `(light volume — possible whipsaw, verify manually)` to the reason. If volume confirmed the breakdown, no annotation needed, or optionally note `(volume-confirmed breakdown)`.

   - 🟠 **RECOMMEND EXIT (judgment/escalation layer — always with named reason code and one-line justification; NEVER forces CLOSE label; always evaluated even when no CLOSE trigger fired):**
     - `STRIKE_UNKNOWN` (fail-safe, checked first): `checks.short_strike_unknown` is true (the `short_strike` input was missing/null, so `thesis_invalidated` could not be evaluated — a `false` there means "not measured," not "confirmed intact"). Reason format: `STRIKE_UNKNOWN: short_strike was not provided — thesis could not be evaluated, risk is unmeasured. Verify manually.` This overrides WATCH/HOLD for this position regardless of any other check's result; it does NOT override a CLOSE trigger above, since those don't depend on `short_strike` being known (time-stop, profit-target) except the strike-breach CLOSE itself, which is also unmeasured in this state — treat as RECOMMEND EXIT, not CLOSE, since a breach can't be confirmed either.
     - `MA150_BREACH` (locked advisory-only 2026-09-05, final config): `checks.broke_ma150_support` is true AND `thesis_invalidated` is false (short strike not yet breached — if it also breached, that's already the hard CLOSE above, this tier doesn't double-fire). Reason format: `MA150_BREACH: settled close ($<close>) below 150-day MA ($<ma150>), short strike ($<short_strike>) not yet breached. Advisory, not a hard stop — a $3k Global-Heap-Allocator backtest paired MA150-as-a-hard-stop against the final loosened-candle entry rule (any green close, not strict hammer/engulfing) and found it catastrophic ($116.21/mo strike-only vs -$19.57/mo with MA150 hard stop): looser entries land closer to MA150 at signal time, so a hard MA150 stop whipsaws out of positions that still have room to work. Human judgment call.`
     - `EARNINGS_RISK`: The position's next confirmed earnings date falls before its expiration date AND the underlying's close is within 5% of the short strike (`close <= short_strike * 1.05`). Reason format: `EARNINGS_RISK: earnings on <date> before expiry <exp_date> with close ($<close>) within 5% of short strike ($<short_strike>)`. If earnings date is unknown, do not trigger `EARNINGS_RISK`; note "earnings: unknown — verify manually".
     - `TIME_RISK`: `0 <= DTE <= 7` (not negative — an expired position is already a hard CLOSE above, this tier never applies to it) AND the position IS underwater (`pct_max_profit_captured < 0`) AND `thesis_invalidated` is false (thesis still intact). Reason format: `TIME_RISK: DTE=<n> <= 7 and underwater (captured=<pct>%), but thesis intact. Defined max loss is $<max_loss_per_share> ($<total_max_loss> total, capped). Holding into final week is a deliberate bet on reversal, not blind hope.`

   - 🟡 **WATCH (unchanged):**
     - `thesis_invalidated` is false, no RECOMMEND EXIT trigger, but `rsi_overbought` is true (RSI > 70) OR `bearish_candle` is true (`shooting_star` or `bearish_engulfing`) — discretionary reversal signal, does not force a close.

   - 🟢 **HOLD:**
     - None of the above (thesis intact, `short_strike_unknown` is false, profit < 80%, DTE > 7, no earnings risk, momentum/candles intact).

## Output
Keep it short and scannable — this goes straight to Telegram, not a report file.
Format:

```
Bull Put Spread Exit Guard — <date>
<N> open position(s) found.

[one line per position, followed by optional PREMARKET NOTE line]
```

Line format for each position:
`[Ticker] [short_strike]/[long_strike] exp=[date] DTE=[n] | captured=[pct]% | verdict=[CLOSE/RECOMMEND EXIT/WATCH/HOLD] | reason=[why, including any reason code and the light-volume annotation when applicable]`
(`[pct]` = `pct_max_profit_captured * 100`, e.g. a ratio of 0.35 displays as "35", not "0.35" — the underlying ratio is still what all threshold comparisons above use.)

If a premarket note applies (meaningful move vs strike/MA150 or data unavailable), place it on its own line directly after that position's main verdict line, before moving to the next position:
`  PREMARKET NOTE: [settled close, live price, and movement vs strike/MA150, or "premarket price: unavailable — verify manually"]`

Then, on its own final line, emit exactly:
POSITIONS_CHECKED: <N>

Do not add commentary, recommendations, or risk assessment beyond the verdict, one-line reason, and factual premarket note — this is a factual listing only. The human decides what action to take.

