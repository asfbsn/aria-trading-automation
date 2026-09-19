Copy this file to research/proposals/<date>-<knob>.md and fill in every section before registering a run -- the registration runner below refuses to run against an empty or missing proposal.

# Experiment Proposal

## Run Metadata
- **Date**: YYYY-MM-DD
- **Knob / Parameter Name**: [e.g. vrp_threshold, gex_percentile, liquidity_gate]
- **Track**: [Development | Confirmation]
  - *If Development*: default to a fixed IS/OOS split on already-explored history (2023–2026), reusing the existing repo-wide split boundaries (both completed Development proposals to date used this). A chronological walk-forward split remains permitted as an alternative, but only when explicitly selected and justified in Section 6 — it is not the default.
  - *If Confirmation* (requires prior freeze before collecting forward ghost data):
    - **Frozen Candidate**: [Exact parameter value or specification to deploy, e.g. VRP=1.10]
    - **Freeze Date**: [YYYY-MM-DD] (Confirmation data must be collected strictly after this date; no performance-driven changes or optional stopping allowed)

---

## 1. Hypothesis
<!-- State the specific, falsifiable hypothesis and economic mechanism being tested. Why should this parameter variation improve performance or robustness? What failure mode does it address? -->
[Describe the hypothesis and causal mechanism here]

## 2. Parameter family
<!-- List the exact parameter space / knob family under test (e.g. VRP threshold in [1.00, 1.20] with step 0.05). Note: signal_core.py (v1 EMA150/RSI/candle) is off-limits per roadmap. Tunable surface is v2-only (VRP threshold, GEX percentile, liquidity/skew gates). -->
[Define the parameters, range, and grid/family under test]

## 3. Search budget
<!-- State the bounded search budget (number of parameter iterations, candidate configurations, or evaluations). Prevents unbounded data mining and selection bias. -->
[Specify maximum number of configurations, sweeps, or iterations evaluated]

## 4. Objective tradeoff
<!-- Explicitly note: NOT just "highest Sharpe". State the actual multi-metric tradeoff being made (e.g. Sharpe vs max drawdown, win rate vs tail loss, trade frequency vs turnover/crossing costs, parameter sensitivity vs cliff edge). -->
[Define the tradeoff criteria and multi-metric objective function]

## 5. Cost model
<!-- State the explicit friction and cost assumptions: commissions, displayed crossing costs, entry/exit spread slippage (e.g. $0.05/share entry-side per friction_analyzer_v2.py, stressed liquidation exit assumptions, financing/assignment fees). -->
[Detail the per-leg/per-share crossing costs and slippage assumptions]

## 6. Split boundaries
<!-- Define split boundaries. Chronological walk-forward by date for development (never random shuffle). For confirmation, specify the forward evaluation window post-freeze. -->
- **In-Sample (IS) Window**: [YYYY-MM-DD to YYYY-MM-DD]
- **Out-of-Sample (OOS) Window**: [YYYY-MM-DD to YYYY-MM-DD]
- **Holdout / Confirmation Window**: [Must strictly post-date freeze date if confirmation]

## 7. Stopping rule
<!-- Pre-declare the criteria for terminating search or accepting/rejecting the hypothesis. "Insufficient evidence" is a normal, allowed outcome. No forced winner selection. -->
[State the exact stopping criteria, minimum effect size floor, and rejection threshold]
