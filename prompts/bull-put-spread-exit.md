You are running an exit-guard check on open Bull Put Spread positions. Use ONLY
IBKR read-only tools plus the two Bash/Write operations below. NEVER use order tools
(`create_order_instruction`, `delete_order_instruction`, etc.) or any order-placement/
modification tool — this is purely advisory, read-only monitoring.

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
   If no open bull-put-spread positions exist, output:
   ```
   Bull Put Spread Exit Guard — <date>
   0 open position(s) found.

   No open bull-put-spread positions.
   ```
   and stop (empty is not an error, same as GTC guard).

2. **Compute technical exit signal:** For each open position pair:
   - Call `get_price_history` on the underlying symbol (at least 155 daily bars,
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

4. **Classify position verdict:**
   - 🔴 **CLOSE** — `thesis_invalidated` is true (short strike breached: `close < short_strike`, OR broke MA150 support: `close < ma150`), OR `pct_max_profit_captured >= 0.80` (80% profit target reached — buy back at 20% of credit; this 80%-capture target is the default baseline GTC target and exits may still be managed dynamically), OR `DTE <= 7` (time stop).
   - 🟡 **WATCH** — `thesis_invalidated` is false, but `rsi_overbought` is true (RSI > 70) OR `bearish_candle` is true (`shooting_star` or `bearish_engulfing`) — discretionary reversal signal, does not force a close.
   - 🟢 **HOLD** — none of the above (thesis intact, profit < 80%, DTE > 7).

## Output
Keep it short and scannable — this goes straight to Telegram, not a report file.
Format:

```
Bull Put Spread Exit Guard — <date>
<N> open position(s) found.

[one line per position]
```

Line format for each position:
`[Ticker] [short_strike]/[long_strike] exp=[date] DTE=[n] | captured=[pct]% | verdict=[CLOSE/WATCH/HOLD] | reason=[why]`

Then, on its own final line, emit exactly:
POSITIONS_CHECKED: <N>

Do not add commentary, recommendations, or risk assessment beyond the verdict and one-line reason — this is a factual listing only. The human decides what action to take.
