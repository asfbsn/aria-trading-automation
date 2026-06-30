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
6. **Size discipline.** Verify buying power/margin (`get_account_summary`) and a pre-agreed max risk per trade before staging.

---

## Step 1 — Review the nightly advisory scan & select a setup
- Open the dated log: `~/aria-trading/logs/YYYY-MM-DD_bull-put-spread.md` (or ask me to summarize it).
- Choose **one** setup whose **SETTLED** (prior-close) signal you trust — not the provisional mid-session bar.

## Step 2 — You explicitly request a ticket
- Tell me, e.g.: *"Stage a Bull Put Spread on APD, short 270 / long 260, July 17 expiry."*
- Required: **ticker, short strike, long strike, expiration.** If any is missing, I'll ask — I will **not** stage anything without an explicit request from you.

## Step 3 — I confirm size + price, then build the LIMIT instructions
1. **I ask you first** for: **quantity** (number of spreads) and your **net-credit limit** (target ≥ 1:2 R/R per the profile). I never assume size.
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
