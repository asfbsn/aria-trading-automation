# ARIA Self-Improvement Roadmap

_as of 2026-09-17_

## Verdict

Automated tuning is adopted, gated by controls, not banned. Fable's core claim held up under a deeper review from a second quant reviewer (Astra): scheduling compute to run overnight does not fix selection bias. Only registration, protected confirmation data, and an honest denominator (every attempt logged, not just winners) do that.

Both reviewers agree on one blocking constraint regardless of protocol sophistication: the ghost ledger has 3 rows and no outcome data yet. No *confirmation* — a decision that promotes a tuned parameter to live use — is possible until that changes. Bounded *development* work against already-explored history is a separate, unblocked track — see Sequenced roadmap below.

## The core constraint

`state/ghost/ghost_entries.csv` has 3 rows (CVX, KO, MS, 2026-09-16). Its schema (`ENTRY_FIELDS` in `ghost_fill_logger.py`) has no exit, mark, or outcome column. Sharpe, drawdown, and win rate are undefined on it today — there is nothing to score yet.

The entire 2023–2026 backtest history has already been explored this month (structural-2of3, cushion sweeps, Bounce Exit, VRP/GEX). No untouched historical holdout remains for *confirmation*. Explored history remains legitimate for mechanism checks, cost sensitivity, and bounded parameter-sensitivity development (e.g. checking whether VRP=1.1 is a spiky optimum) — it just can never again serve as an independent confirmation test, and any such check that influences the eventual parameter choice must be logged as development search, not treated as a free look.

**Two separate floors, do not conflate them (second-reviewer correction):** a **provisional coverage milestone** — valid entry-quote observations spanning at least 30 distinct tickers and 20 trading sessions — is enough to start calibrating displayed-quote crossing costs. It is nowhere near enough to validate a strategy statistically. Note these are different units measuring different things: 30 tickers is breadth of coverage, 20 sessions is the length of the time series that actually drives statistical power in the formula below — don't conflate them into one "N". Under idealized i.i.d. daily returns near zero Sharpe, SE(SR_annual) ≈ √(252/N) where N is sessions; at N=20, that's √12.6 ≈ 3.55 — an annualized Sharpe estimate with a standard error several times larger than any Sharpe this strategy could plausibly produce. 30 correlated tickers don't turn 20 market days into 600 independent observations either. So: the coverage milestone unlocks preliminary descriptive friction calibration only, not guaranteed estimation precision. The bar for validating a parameter choice is a separate, effect-size-and-dependence-dependent power calculation, calculated from development assumptions *before* freeze — not by repeatedly checking observed significance or power against arriving data — and is not assumed to be cleared just because the coverage milestone is.

**Implication:** build fidelity and ledger infrastructure now. Do not promote any tuned parameter to live use, and do not treat forward ghost data as confirmation, until (1) a ghost exit/mark logger exists, (2) the candidate, its costs, and the confirmation protocol are frozen in advance, and (3) forward outcomes collected *after* that freeze clear a real validation-power floor — not the lower cost-calibration floor.

## Architecture: 5 roles, 3 existing mechanisms + 1 new file

Permissions and file boundaries do the separating — not separate AI personas. Every role below already maps to something in this repo except the ledger.

| Role | What it already is | Authority |
| --- | --- | --- |
| Production executor | `ibkr-live-workflow.md`: human stages and submits every order | Broker read access only; no autonomous order placement |
| Data & fidelity service | Ghost System (`daily-scan-ghost.sh`, `ghost_fill_logger.py`) | Append-only observations; zero order capability |
| Research proposer | Backtest venv (`scripts/backtest/`), no broker MCP attached | Development/historical data only |
| Experiment & evaluation | **New:** a registration runner + `research/runs.csv`, an immutable append-only experiment ledger | On a single-operator box there is no technical wall between "development" and "confirmation" data — procedural isolation is the correct scope: freeze the candidate, cost model, endpoint, and evaluation date before future observations arrive, no performance-driven changes or optional stopping during confirmation, enforced by a runner that registers every attempt before it runs plus honest adherence, not by infrastructure. `daily-scan-ghost.sh`'s `allowedTools` grant list is a genuine executor-level restriction, but only as strong as the assumption that permitted shell/file tools can't be used to route around it — it isn't a substitute for the procedural discipline above. |
| Promotion controller | Git review (already this session's practice) | Human merge is the sole promotion authority |

## Experiment protocol (development now, confirmation later)

1. **Register before running.** Hypothesis, parameter family, search budget, objective tradeoff (not just "highest Sharpe"), cost model, split boundaries, and stopping rule, written to `research/proposals/<date>-<knob>.md` before the run starts. This applies to bounded historical development too, not just confirmation runs — a VRP=1.1 sensitivity check that later influences the frozen candidate counts as search and must be logged as such.
2. **Chronological walk-forward for development; frozen candidate for confirmation.** Split by date, not randomly, while developing on explored history. Once a candidate, its cost model, and the confirmation protocol are frozen, confirmation runs only once against forward data collected *after* that freeze.
3. **Evaluate the exact object you'll deploy.** A single frozen parameter value, not "the search procedure" — production won't re-run the search live.
4. **Confirmation data must be collected after freeze, not merely "new."** Data that was inspected, used to calibrate costs, or used to pick a candidate belongs to development, however new it is. On a solo box there's no technical partition to enforce this — it's a discipline: don't look at, or fit anything to, forward ghost outcomes collected after the freeze date until the confirmation test itself runs.
5. **Multiplicity applies to every comparison behind a claim, not just the initial search.** A single candidate's single confirmation run, evaluated independently on frozen protocol, doesn't need a per-candidate penalty. But multiple confirmation attempts, repeated promotion tries, or successive "let's test again" cycles do need a multiplicity or sequential correction — registering each attempt documents it, it doesn't neutralize it.
6. **"Insufficient evidence" is a normal, allowed outcome.** No nightly-winner requirement, no forced weekly strategy change.

Log per experiment where estimable: Deflated Sharpe Ratio (needs a defensible selection-history assumption), Probability of Backtest Overfitting (an experiment-level diagnostic requiring a pool of competing configurations, not a per-run number), and a block bootstrap (never per-trade resampling — spread trades are dependent and overlapping). Mark "not estimable" rather than fabricating a number when the inputs aren't there.

## Sequenced roadmap

1. **Now** — registration runner + `research/runs.csv`: immutable, append-only experiment ledger, covering development runs too. Append-only *events* (`registered`, `completed`, `failed`, `aborted`), not one mutable row per attempt — a registration event written before the run starts, a terminal event written after, so interrupted or unsuccessful attempts stay visible without rewriting the registration row.
2. **Now** — complete the ghost lifecycle: exit/mark logger, plus a portfolio-equity view (open positions, cash, costs, sizing, overlapping exposures) — Sharpe/drawdown need a consistent equity series, not just closed-trade P&L. This is the actual blocking gap for any performance measurement.
3. **Now, bounded** — historical development on already-explored data: mechanism checks, cost-sensitivity checks, and checking whether VRP=1.1 is a spiky/overfit optimum. Allowed because it's logged as development (step 1) and never treated as confirmation — not gated behind the ghost floor.
4. **Ongoing** — nightly ledger-integrity report: data-availability rate, estimated-timestamp rate, liquidity-gate-pass rate, N vs. floor. Read-only, no mutation.
5. **At the coverage milestone** (30+ tickers / 20+ sessions of real ghost *entry-quote* observations — no exits needed here, a much lower bar than validation, see above) — use ghost entry-quote crossing costs as a preliminary sanity check on `friction_analyzer_v2.py`'s $0.05/share entry-side assumption (`$0.05 × 100 = $5/leg`, verified at `friction_analyzer_v2.py:188-190`). This is entry-friction calibration only and needs no exit/mark data. It does not touch Sharpe or drawdown: the analyzer explicitly treats `IS_`/`OOS_`/`full_` risk stats as unadjusted references and does not recompute them with costs folded into a daily equity path (`friction_analyzer_v2.py:304-309`) — that requires the portfolio-equity view from step 2. Exit friction under stressed liquidation is a separate, harder question entry quotes cannot answer; keep entry calibration, exit-cost assumptions, and actual fill slippage labeled separately, never conflated.
6. **Freeze, then confirm** — once step 3's development work and step 5's cost calibration produce a candidate worth testing, freeze the candidate, its cost model, and the confirmation protocol before collecting any more forward ghost data. Only ghost outcomes collected *after* that freeze count as confirmation data.
7. **Later** — evaluate once, under the frozen protocol, when forward outcomes clear the (higher, effect-size-dependent) validation floor — not the cost-calibration floor from step 5.

## Ghost ledger: counterfactual coverage and candidate-universe scope

If the ghost ledger only ever logs candidates that already pass the incumbent VRP≥1.1 filter, there is no way to later ask "would 1.05 have been better?" — those candidates were never recorded. Full-universe quote-capture is too expensive (sequential per-candidate IBKR calls), so bound it instead of skipping it: log prescreen inputs (VRP, price/MA150 gate, GEX regime) for every ticker each session — cheap, already computed — and extend quote-capture past the accepted set to a declared band below threshold (VRP 1.0–1.1), not the whole universe.

**Acceptance check:** candidates captured within that extra VRP band must receive the same subsequent marks/exits as accepted candidates, not just an entry quote. Entry quotes alone support friction analysis; they cannot support a candidate-vs-incumbent performance comparison, which needs the full lifecycle.

## Explicitly out of scope

- **Five separate agent personas.** Unnecessary — 3 existing mechanisms plus 1 new ledger file already separate the roles.
- **An "overnight monitoring gap," beyond what's already flagged below.** Existing guards (`exit-guard.sh`, `gtc-guard.sh`, `morning_briefing.py`) cover established pre-open checks. Assignment reconciliation remains an identified coverage gap (see below) — verify existing schedules and watchdog coverage before adding any duplicate monitoring for it.
- **A calibrated GEX measurement-error model.** Not available — SqueezeMetrics is a black-box feed with no probabilistic error model to perturb. Missing-feed, delayed-feed, and threshold-perturbation scenarios remain cheap, useful tests — but they are deterministic stress scenarios, not a claim about measurement-error probabilities, and don't require one. Point-in-time is enforced by `date < as_of_date`, but that alone doesn't prove the underlying feed wasn't itself revised/backfilled after that date — fetching a historical series today doesn't establish what the historical version actually contained at the time. Mitigation: record source observation date, `fetched_at_utc`, and an immutable value snapshot per row in `dix_cache.csv` — provenance, not a new error model.
- **Nightly winner selection, in any form.** Rejected outright by both reviewers. Scheduling more search overnight against a fixed dataset does not add independent confirmation evidence — bounded development checks (VRP sensitivity, cost sensitivity) are fine per the roadmap above, but only as logged development, never as an implicit re-confirmation.
- **`signal_core.py` (v1's EMA150/RSI/candle gate) as a tuning target.** Off-limits — the 2026-09-14 EMA fix was a dashboard-fidelity correction, not a knob. Tunable surface is v2-only (VRP threshold, GEX percentile, liquidity/skew gates).

## New operational risk: early assignment

A short put leg can be assigned before expiration, independent of the long leg. Corrected math (the original framing here was wrong): this does **not** by itself break the spread's max-loss bound. Example: short 100P assigned → forced to buy shares at $100; long 90P still open → exercise it to sell those shares at $90. Loss = $10/share width − $2/share credit already received = $8/share, exactly the width-minus-credit bound the spread was designed for. **Caution:** this bound only holds if the hedge is preserved until the stock exposure is fully resolved — exercise the protective put, or close the stock and the long put together in the same action. Simply selling the long put while continuing to hold the assigned shares removes the protection and leaves naked long stock exposure. The width-minus-credit bound also excludes financing, fees, and execution costs, which are real and additive on top of it (cf. [OCC's bull put spread / assignment funding explanation](https://www.optionseducation.org/strategies/all-strategies/bull-put-spread-credit-put-spread)).

The real risk is operational, not a break in the defined-risk math: funding the forced stock purchase in the interim, whether the broker liquidates it awkwardly, and exposure if the long leg is mishandled, sold off without closing the stock, or lapses first. Not covered by any current monitoring. Action item, not yet implemented: confirm whether `get_account_positions` / IBKR notices surface early-assignment events, then add explicit handling to `exit-guard.sh` or a dedicated check — tracking the resulting stock position and the surviving long option together, not just the short leg, and never treating a lone put sale as resolving the position while the shares remain.

---

_Companion doc (hosted, editable): https://claude.ai/artifact/23vrWkG8rhdFFPx1fiWPfp_
