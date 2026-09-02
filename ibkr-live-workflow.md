# IBKR Live-Money Operating Procedure — Stage a Bull Put Spread Ticket

**Status:** for the upcoming transition from paper trading to live IBKR money.
**Owner submits every order. Claude only stages — never executes.**

---

## Golden rules (always apply)
1. **LIMIT orders only.** Never MARKET.
2. **Claude never submits.** `create_order_instruction` only *stages* a reviewable instruction; it is **not** a live order. You review and click submit inside IBKR. Nothing reaches the market without your manual submission.
3. **The nightly cron scan stays advisory/read-only.** Broker/order tools are never wired into the unattended job.
4. **One ticket at a time, explicit per-ticket confirmation.** No batch/auto staging.
5. **Respect the saved strategy** ([[trading-bull-put-spread-profile]]): stock above MA‑150, short put **below** support, R/R **1:1.5–2.5**, Daily-only, ~30 DTE.
6. **Size discipline (updated 2026-09-02, "High-Conviction / Velocity" model).** Spread width is dynamic — derived from each name's actual live chain spacing (S, 2S, 4S), not a fixed $10; the directive lists a PRIMARY (widest-clearing) width and any narrower FALLBACK widths. Max loss per spread = **25% of current net liquidation value** (`get_account_summary`); contracts = floor((25% × net_liq) / ((width − credit) × 100)) using the **minimum acceptable credit (width/3.5, the 1:2.5 floor) for this `credit`, not the verified mid** — same floor-credit basis as the nightly directive, so a worse fill doesn't exceed the intended 25% allocation — tried at the PRIMARY width first, then narrower fallbacks if that sizes to 0; a result of 0 at every tested width means the spread is too wide for the account — do not stage. **Max 2 concurrent spreads** (25% × 2 = same ≈50% total portfolio risk ceiling as the prior 10%/5-position split, just far more concentrated per position — a single max-loss event is now −25% of the account) and **one spread per sector** (sector from `data/universe.csv`). The nightly scan's TRADE DIRECTIVE block computes all of this — a staged ticket should match its numbers (including which width) or state why it deviates.
7. **Exits staged with the entry — manually, same as the entry ticket.** Default: right after you submit the entry fill, manually stage a GTC combo buy-to-close order at a **net debit of 20% of the received credit** (80% capture) — `create_order_instruction` stages it, you review the deep-link and submit it yourself, exactly like Step 3/4 above; nothing here places or fires an order automatically. Alert-only (not staged) downside watch: mental/alert stop on a close below the short strike or MA-150; time stop at 0 ≤ DTE ≤ 7 (closes unconditionally unless underwater with thesis intact, which escalates to a recommended exit — see `exit-guard.sh`); an already-expired position (DTE < 0) always closes unconditionally regardless of profit/loss, no exception. `exit-guard.sh` monitors these thresholds plus earnings proximity daily and tells you when to act — it never places, modifies, or cancels anything itself.

---

## Step 1 — Review the nightly advisory scan & select a setup
- Open the dated log: `~/aria-trading/logs/YYYY-MM-DD_bull-put-spread.md` (or ask me to summarize it).
- Choose **one** setup whose **SETTLED** (prior-close) signal you trust — not the provisional mid-session bar.
- Prefer a name whose **📋 Trade Directive block says EXECUTE NOW** (provisional bar confirming at support with volume) and is not BLOCKED (position cap / sector / zero-contract). A HOLD directive means wait for the next scan; a research-gate RADAR downgrade means the technicals passed but the fundamentals flagged — read the flag before overriding it.

## Step 2 — You explicitly request a ticket
- Tell me, e.g.: *"Stage a Bull Put Spread on APD, short 270 / long 260, July 17 expiry."*
- Required: **ticker, short strike, long strike, expiration.** If any is missing, I'll ask — I will **not** stage anything without an explicit request from you.

## Step 3 — I confirm size + price, then build the LIMIT instructions
1. **I confirm size + price against the directive:** first re-pull `get_account_positions` and `get_account_summary` (net_liq and open-position count may have moved since the nightly scan — a stale count could let a 3rd position slip past the 2-position cap, or a moved net_liq change the correct contract quantity), then recompute quantity from the 25% net-liq formula (golden rule 6) against those refreshed values, at the directive's actual chosen width; net-credit limit from the directive's verified mid for that width (floor: width/3.5, the 1:2.5 boundary). If you want to deviate from the directive's numbers, say so explicitly — I never silently substitute my own.
2. I re-verify the setup: above MA‑150, short strike below support, R/R within 1:1.5–2.5, sufficient buying power (`get_account_summary`), no earnings in the window.
3. Resolve contracts: `search_contracts` → `get_option_parameters` (pick the ~30 DTE expiry) → `get_option_data` (get the `put_contract_id` for **both** strikes).
4. Stage with `create_order_instruction`, **LIMIT only**:
   - **Short leg:** `side=SELL`, higher-strike `put_contract_id`, `order_type=LIMIT`, your price, `quantity`, `time_in_force=DAY`.
   - **Long leg:** `side=BUY`, lower-strike `put_contract_id`, `order_type=LIMIT`, your price, `quantity`, `time_in_force=DAY`.

> ⚠️ **Multi-leg reality:** this MCP stages **single-leg** instructions (one `contract_id` each) — it does **not** create a single net-credit combo. To control net credit and avoid leg risk, the **recommended** path is to use the staged legs as the reference and **assemble/submit the spread as one combo in IBKR** with your net-credit limit. Alternatively, submit the two legs separately, accepting that fills are independent and the net credit isn't guaranteed. I'll always state which path we're using and show the net-credit math.

## Step 4 — I hand you the deep-link(s); you review & submit
- `create_order_instruction` returns a **deep-link** to each instruction in IBKR.
- You open it, verify **legs / strikes / quantity / price / net credit**, and **submit manually**.
- Nothing goes live until you click submit. I never submit on your behalf.
- After you submit, I can confirm via `get_account_orders` / `get_order_instructions` and track the position with `get_account_positions`.
- To discard a staged-but-unsubmitted instruction: `delete_order_instruction`.

---

## Tools used
- **Read:** `get_account_summary`, `get_account_positions`, `get_account_orders`, `get_order_instructions`, `search_contracts`, `get_option_parameters`, `get_option_data`
- **Stage (LIMIT only):** `create_order_instruction` · **Cancel staged:** `delete_order_instruction`

## Before the FIRST real-money ticket (one-time checklist)
- [ ] Confirm which IBKR account is connected (paper vs live) via `get_account_summary`.
- [ ] Agree a max position size / max risk per trade.
- [ ] Dry-run this entire flow on the IBKR **paper** account end to end.
- [ ] Confirm net-credit/combo submission method in IBKR.
