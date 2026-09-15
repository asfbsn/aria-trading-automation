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
6. **Size discipline (updated 2026-09-02, "Global Heap" dynamic allocator model).** Spread width is dynamic — derived from each name's actual live chain spacing (S, 2S, 4S), not a fixed $10; the directive lists a PRIMARY (widest-clearing) width and any narrower FALLBACK widths. Sizing is a shared **50%-of-net-liq risk pool** ("the Heap"), not a fixed per-trade %: the nightly scan ranks its PRIME survivors by R/R (richest credit-per-risk first) and allocates top-down, `contracts = floor(min(0.25×net_liq, heap_remaining) / max_loss_per_contract)` using the **minimum acceptable credit (width/3.5, the 1:2.5 floor) for this `credit`, not the verified mid** — same floor-credit basis as before, so a worse fill doesn't exceed the intended allocation. The 25%-of-net_liq figure is a per-trade ceiling (not a fixed allocation) so one name can't swallow the whole heap. Tried at the PRIMARY width first, then narrower fallbacks if that doesn't fit `heap_remaining`; if nothing fits at any width → `BLOCKED — heap exhausted` — do not stage. **Total exposure across all open spreads is hard-capped at 50% of net_liq** (position count now floats by geometry, typically 2–5, instead of a fixed cap) and **one spread per sector** (sector from `data/universe.csv`). The nightly scan's TRADE DIRECTIVE block computes all of this — a staged ticket should match its numbers (including which width and the heap arithmetic) or state why it deviates.
7. **Exits staged with the entry — manually, same as the entry ticket.** Default: right after you submit the entry fill, manually stage a buy-to-close GTC exit at a **net debit of 20% of the received credit** (80% capture) — target: a single combo order **assembled and submitted directly in IBKR** (see the Multi-leg reality note in Step 3; `create_order_instruction` only stages single legs, and separate closing legs can fill independently, leaving naked exposure — never present single-leg staging as a combo). If you instead accept submitting the two legs separately, that's a real risk decision to make explicitly, not a default. Alert-only (not staged) downside watch: mental/alert stop on a close below the short strike or MA-150; time stop at 0 ≤ DTE ≤ 7 (closes unconditionally unless underwater with thesis intact, which escalates to a recommended exit — see `exit-guard.sh`); an already-expired position (DTE < 0) always closes unconditionally regardless of profit/loss, no exception. `exit-guard.sh` monitors these thresholds plus earnings proximity daily and tells you when to act — it never places, modifies, or cancels anything itself.

---

## Step 1 — Review the nightly advisory scan & select a setup
- Open the dated log: `~/aria-trading/logs/YYYY-MM-DD_bull-put-spread.md` (or ask me to summarize it).
- Choose **one** setup whose **SETTLED** (prior-close) signal you trust — not the provisional mid-session bar.
- Prefer a name whose **📋 Trade Directive block says EXECUTE NOW** (provisional bar confirming at support with volume) and is not BLOCKED (heap exhausted / sector / zero-contract). A HOLD directive means wait for the next scan; a research-gate RADAR downgrade means the technicals passed but the fundamentals flagged — read the flag before overriding it.

## Step 2 — You explicitly request a ticket
- Tell me, e.g.: *"Stage a Bull Put Spread on APD, short 270 / long 260, July 17 expiry."*
- Required: **ticker, short strike, long strike, expiration.** If any is missing, I'll ask — I will **not** stage anything without an explicit request from you.

## Step 3 — I confirm size + price, then build the LIMIT instructions
1. **I confirm size + price against the directive:** first re-pull `get_account_positions`, `get_account_summary`, AND `get_account_orders` (net_liq and open positions may have moved since the nightly scan — a stale read could let total exposure slip past the 50% heap). `get_account_positions` only shows FILLED positions -- a spread staged and submitted but not yet filled carries real heap-consuming risk that wouldn't appear there at all. **If `get_account_orders` shows any of our spread legs still pending (not filled, not cancelled), I stop and say so — the next ticket doesn't get staged until that order fills or you cancel it.** This is stricter than "reserve the pending risk and net it against `heap_remaining`" on purpose: reconstructing a pending order's max-loss from its raw legs is one more place to get the arithmetic wrong on a live account, and golden rule 4 (one ticket at a time) already implies this — this just makes the check for it explicit rather than assumed. Once confirmed clear, **re-run the full allocator** against the refreshed values — ranking, sector check, width selection (PRIMARY then fallbacks), and quantity (golden rule 6) — not just the quantity at the directive's already-chosen width; `heap_remaining` shifting since the scan can change which width even fits, not only how many contracts. If you're staging a lower-ranked name while skipping a higher-ranked one from the same directive, say so — the skipped name never actually consumed heap capacity (nothing is allocated until staged), so re-running the allocator from current state is correct, not a double-count. Net-credit limit from the directive's verified mid for the width actually selected (floor: width/3.5, the 1:2.5 boundary). If you want to deviate from the directive's numbers, say so explicitly — I never silently substitute my own.
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
