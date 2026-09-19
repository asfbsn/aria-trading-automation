# Experiment Proposal

## Run Metadata
- **Date**: 2026-09-18 (final — registered run `20260918T174731Z_998760cb225f` completed, hypothesis rejected (FAIL) on all gates; see Section 8 Results and Revision Log. No open decisions remain.)
- **Knob / Parameter Name**: `z_ma150_proximity_upper_bound`, primary value `0.50`; `0.15`/`0.30` exploratory-only, non-gating (Section 3).
- **Track**: Development — bounded historical work on already-explored history, per `docs/self-improvement-roadmap.md`. Not walk-forward, not confirmation, not eligible to promote any parameter to live use on its own. **Verdicts and the bootstrap in Section 7 use OOS only**, matching the 09-17 proposal's own discipline — stated explicitly here, not left implicit.
- **Relationship to the accepted 2026-09-17 proposal**: same underlying `vrp_only` entry pool, same `z_ma150` definition, same IS/OOS split, same GEX regime source. Distinct hypothesis, not a revision of the frozen 09-17 proposal, which stays untouched. Nothing here touches `ghost_prescreen_v2.py`, `bps_signal_engine_v2.py`, or any live/cron script; the live Ghost System's `z_ma150 >= 1.5` filter is unaffected.

---

## 1. Hypothesis

**Stated hypothesis, narrowed to what this design can actually claim, per Astra round 2 item 2:** entries near EMA150 (`0 < z_ma150 <= 0.50`, lower bound guaranteed by pool membership) during POSITIVE GEX reach the 80% profit-capture level within 10 calendar days more often than the same entries without the proximity filter — **without** a materially worse eventual (full-holding-period) reach rate, terminal breach rate, or intraperiod breach rate than the unfiltered-by-proximity comparator. All three "without worse X" claims are now gated (Section 4/7), not just terminal breach as in round 2 — closing the promise/gate mismatch Astra flagged. "Technically strong" stays dropped: `vrp_only` never calls `entry_checks()`, no RSI/candle vetting on this pool.

**Mechanism claim, unchanged, stated as an assumed prior:** a position entered near its EMA150 resolves its "close call" nature quickly in the bullish direction, and POSITIVE GEX's dampened volatility lets that happen without a drawdown large enough to threaten the short strike. Neither half established by prior work here.

**Gate-overlap question — arithmetic from round 2 kept, diagnostic corrected per Astra round 2 item 4 ("overlap"):**
- `vrp_only` never calls `entry_checks()` — confirmed directly. This pool's near-support gate is not already enforced.
- Production `near_ma150_support` also uses EMA150 (`signal_core.py:287/390/443/485`, confirmed) — the real difference from this proposal's `z_ma150` is normalization (plain percentage, `(close-ema150)/ema150`, fixed band `[0, 0.10]`) vs. volatility-scaled (`(close-ema150)/(close*sigma)`), not choice of moving average.
- At 2% representative daily volatility, `z_ma150 <= 0.50` ≈ `(close-ema150)/close <= 1.0%`, roughly an order of magnitude narrower than production's fixed 0-10% band at that volatility level.
- **Diagnostic corrected (Astra: "report full 2×2 membership table... one-way overlap can be 100% even when proximity selects a tiny subset of the production band"):** Section 4 now specifies a full 2×2 contingency table — `{in proximity band, in production band} × {yes, no}` — over the same candidate pool, not a single one-way containment fraction. A one-way "X% of proximity-band candidates are also in the production band" number alone can look like near-total redundancy even when the production band is far larger and mostly empty of proximity-band members; the 2×2 table makes both directions visible at once.

**IV mechanism scope, precise naming (Astra round 2 item 3):** the daily reprice loop (Section 5) uses a **frozen signal-date base IV** — not "entry-day," corrected: the existing `vol_by_date` lookup already reads IV as of the signal bar, one session before entry/fill, and this proposal inherits that same value, held constant. **What varies session to session is the smile-adjusted per-leg IV fed into `bs_price`** (`iv_smile_adjustment(current_close, strike, frozen_base_iv, skew, curvature)`, recomputed every session using that session's own close as spot) — the base IV input is frozen, the moneyness-dependent adjustment on top of it is not. **Scope limit, stated plainly (Astra): freezing base IV is acceptable for a narrowly labeled Development screen of "does price movement alone, holding IV fixed, get to target faster" — this design cannot and does not test IV compression as a mechanism.** Any real-world IV decline that would independently accelerate profit capture is invisible to this simulation by construction.

**`STRIKE_BREACH` naming — unchanged from round 2:** this proposal uses the frozen, regime-independent `breach_at_expiry`/`ever_breached_intraperiod` definitions, not the live regime-conditional label. These describe price behavior on a path where **no exit ever intervenes** (Section 4 restates precisely why a stopped-out trade is not simply "never reached" under this design) — not a measurement of live stop-out risk under `compute_exit_signal_v2.py`.

## 2. Parameter family

- `z_ma150` — identical formula, reused unmodified from the 09-17 proposal's computation path.
- **Primary threshold: `z_ma150_proximity_upper_bound = 0.50`.** `0.15`/`0.30` exploratory-only, non-gating (Section 3), declared now, not reclassified after seeing results.
- Lower bound `0` is not a separate filter — guaranteed by pool membership (Section 1).
- Not swept: `gex_percentile_threshold = 0.10`, `gex_lookback = 252`.
- **Four overlapping arms**, same non-disjoint caveat as the 09-17 proposal:
  1. **Baseline** — unfiltered `vrp_only` entries. Threshold-independent. Same entry *membership* as the 09-17 proposal's baseline arm (Section 5 specifies what "reused" means precisely).
  2. **GEX-only** — baseline ∩ `gex_regime == POSITIVE`. Threshold-independent. Same membership as the 09-17 proposal's GEX-only arm. **Primary comparator for every gate in this proposal (Section 4/7)** — not baseline.
  3. **Proximity-only** — baseline ∩ `z_ma150 <= upper_bound`. New `<=`-based mask, never reuses or edits the frozen `>=`-based `arm_masks()` (Section 5).
  4. **Combined** — baseline ∩ `z_ma150 <= upper_bound` ∩ `gex_regime == POSITIVE`.

## 3. Search budget

**8 series, unchanged from round 2:** 4 gating (baseline, GEX-only, proximity-only@0.50, combined@0.50), 4 exploratory-only/non-gating (`{0.15, 0.30}` × {proximity-only, combined}). No further sweeping after seeing results.

## 4. Objective tradeoff

**Primary metric — whole-cohort `reach_by_10`, unchanged definition, wording corrected (Astra: "every eligible candidate after declared exclusions," not literally every candidate):**
- `reach_by_D(candidate) := 1` if `pct_captured(t) >= 0.80` (`PROFIT_TARGET_THRESHOLD`, imported from `compute_exit_signal_v2.py:169`) for some session `t` with `entry_date <= t <= min(entry_date + D days, modeled_expiry)`, else `0`. `D = 10` calendar days — **Astra: "a reasonable predeclared operational definition of 'fast'; no empirical validation implied"** — settled, not a placeholder needing a number, kept as-is.
- **Every eligible candidate — i.e. every candidate surviving the exclusions in Section 5 (`excluded_oos_cutoff`, `excluded_missing_terminal_price`, `excluded_missing_midperiod_price`) — gets a `reach_by_10` value.** Corrected from round 2's "every candidate... nothing is dropped," which contradicted the exclusion rules stated two sections later.
- **Note on the first mark, not a bug:** credit is priced at the entry bar's **open** (Section 5, unchanged from the 09-17 proposal); the first daily mark is taken at the entry bar's **close**. `pct_captured` at the entry date itself is therefore expected to already be non-zero — normal open-to-close drift on the entry session, not a computation error.
- **Primary superiority comparisons, both CI-based (Astra round 2 item 3, corrected round 2's point-estimate-only "must also exceed" language per Astra round 2 item 5):**
  - `reach_by_10(combined)` vs. `reach_by_10(GEX-only)` — primary comparator.
  - `reach_by_10(combined)` vs. `reach_by_10(proximity-only)` — the "combined beats either single arm alone" requirement, now bootstrap-CI-based like every other gate in this design, not a bare point-estimate comparison (Section 7 defines the combination logic).
  - `combined` vs. `baseline` still computed and reported as secondary context, non-gating.
- **Median DIT among reached candidates stays descriptive-only**, reported per arm alongside `reach_by_10`, never gating.

**Non-inferiority constraints — three gates now, not one, closing the promise/gate mismatch (Astra round 2 item 2):**

1. **Terminal breach**, `combined` vs. `GEX-only`, lower-is-better: `deterioration_breach := combined_breach_rate - GEXonly_breach_rate`. Margin: user risk-tolerance choice, not a statistical question — Section 8.
2. **Intraperiod breach**, same direction as terminal (lower-is-better): `deterioration_intraperiod := combined_intraperiod_rate - GEXonly_intraperiod_rate`. **Added this round** — round 2 reported `ever_breached_intraperiod` descriptively only while the hypothesis promised intraperiod protection; this closes that gap by gating it with the same machinery as terminal breach, at its own margin (Section 8 — note the OOS baseline's intraperiod rate, 25.18%, runs roughly 1.8x the terminal rate, 13.96%, per the 09-17 proposal's own registered run output, so a flat margin is proportionally tighter here than for terminal breach; flagged, not resolved unilaterally).
3. **Eventual reach**, `reach_by_expiry` (same `reach_by_D` definition, `D := ` the full holding period through `modeled_expiry` rather than 10 days), **opposite direction from the two above — higher-is-better, sign flipped, stated explicitly to avoid the exact bug class a copy-pasted subtraction would produce**: `deterioration_reach := GEXonly_reach_by_expiry_rate - combined_reach_by_expiry_rate` (positive means combined reaches target less often, eventually, than GEX-only — this is what "deterioration" means for a higher-is-better metric). **Added this round** — directly answers Astra's counterexample: a filter with a higher `reach_by_10` but a lower eventual reach rate is now **evaluated against `margin_reach` and its own uncertainty on this gate**, rather than being invisible to a speed-only design. **Corrected wording (Astra round 3 item 4):** this does not mean any eventual-reach shortfall automatically fails — a small shortfall that stays within `margin_reach` (CI upper bound `<= margin_reach`) legitimately PASSes this gate under the rule as specified (Section 7); the gate exists so that shortfall is *measured against a stated tolerance*, not so any shortfall at all is disqualifying.

**Gating-scope tradeoff, stated rather than silently chosen:** three non-inferiority gates plus two superiority comparisons is five bootstrap CIs that all must land for an overall PASS (Section 7). **Corrected (Astra round 3 item 4): these five CIs are not independent** — they share the same underlying candidates, the same per-iteration block draws (Section 4, "shared draws... across outcomes"), and overlapping outcome definitions (e.g. `reach_by_10` and `reach_by_expiry` are the same underlying path measured at two horizons) — so the real inflation in the overall INCONCLUSIVE rate from requiring all five to clear is smaller than five fully independent coin-flips would suggest, but it is still real and worth naming rather than assuming away. Astra offered two paths for the intraperiod gate specifically: add it, or narrow the hypothesis and keep it descriptive. **This draft adds it** — a fast-bounce thesis is plausibly *about* the path, not just the terminal state, so leaving intraperiod ungated would quietly drop half of what "without increasing breach risk" was meant to promise. The five-CI cost of that choice is accepted here, named, and reversible: dropping the intraperiod gate and narrowing the hypothesis back to terminal-only is a one-section edit if Astra/the user prefers the less conservative four-CI version.

**Uncertainty, all five metrics above, same infrastructure:** all bootstrapped via the same 45-day moving-block design as the 09-17 proposal. **One shared block draw per bootstrap iteration produces all five outcome rates for all four arms in that iteration** — the "shared draws" property extends across outcomes, not only across arms, so every comparison in a given iteration is paired against the same resampled history.

**2×2 overlap diagnostic (Section 1), pool corrected (Astra round 3 item 2 — "combined-eligible pool" reads as already proximity-filtered, which would make both "outside proximity" cells empty by construction):** over the **OOS baseline pool (arm 1 — unfiltered `vrp_only` entries, before either the proximity filter or the GEX filter is applied)**, cross-tabulate membership in `{z_ma150 <= 0.50}` against membership in `{(close-ema150)/ema150 ∈ [0, 0.10]}`, reporting all four cells and both one-way containment fractions. **This is a required implementation output, not a risk-tolerance decision awaiting permission (Astra's recommendation, adopted)** — it runs as part of the implementation, never gates PASS/FAIL.

## 5. Cost model

**Reused unchanged from the 09-17 proposal:** pricing engine (`options_portfolio.py` Black-Scholes), `OPTIONS_CONFIG`, commission, IV/HV source file, entry credit priced once at the entry bar's open using the entry-date signal IV.

**Terminal pricing, corrected — round 2's description of the 09-17 mechanism was wrong, verified directly against `score_trade()` (`gex_pin_ma150_extension_test.py:195-223`) before writing this (Astra round 2 item 1):**
- The 09-17 script does **not** reprice at expiry via Black-Scholes. Entry credit is priced **once**, at entry, via `bs_price` using the **nominal** expiry (`entry["expiry"]`, the unresolved Friday string — not `modeled_expiry`, the holiday-adjusted actual settlement session) for tenor: `tenor_entry = max((nominal_expiry - entry_date).days / 365.0, 0.001)`. Terminal P&L uses **pure intrinsic payoff** on the settlement session's underlying close: `pnl = credit - max(short_strike - terminal_close, 0) + max(long_strike - terminal_close, 0)`.
- **This proposal's daily marks must use the same nominal-expiry-based tenor convention as entry, not `modeled_expiry`**, for every session's Black-Scholes reprice **up to but excluding** the actual settlement session: `tenor(t) = max((nominal_expiry - t).days / 365.0, 0.001)`. Using `modeled_expiry` instead (round 2's error) would silently mismatch the maturity convention between the entry price and every subsequent mark whenever a holiday shifts settlement off the nominal Friday.
- **At the modeled-expiry session itself, the mark is intrinsic, not Black-Scholes**, matching `score_trade()`'s own settlement convention exactly: `cost_to_close(modeled_expiry) = max(short_strike - terminal_close, 0) - max(long_strike - terminal_close, 0)`. Verified algebraically consistent with the frozen script's own `pnl` formula (`credit - cost_to_close = credit - max(short-terminal,0) + max(long-terminal,0)`, matching term for term) before writing this, not just asserted.
- `pct_captured(t) = (credit - cost_to_close(t)) / credit` for every session `t`, using the BS-based `cost_to_close` for `t < modeled_expiry` and the intrinsic formula above for `t == modeled_expiry`.

**IV, corrected naming and scope (Section 1, restated here where the pricing detail lives):** frozen **signal-date** base IV (not "entry-day"), smile-adjusted per-leg IV recomputed every session from that session's own close. Settled per Astra as acceptable for this Development screen, with the stated IV-compression scope limit.

**Bootstrap, adapted not imported (Astra round 2 item 4 — the frozen `block_tables()`/`bootstrap()` hardcode `arm_masks()` (`>=`), a single outcome column (`breach_at_expiry`), and a single fixed comparator subtraction (`baseline - combined`, arm indices 0 and 3); none of those three hardcodes fit this proposal unchanged):**
- **Reused**: the statistical method — 45-day moving/overlapping blocks, entry-date block assignment, the redraw-until-nonempty-combined-arm procedure, the frozen-seed-per-run discipline.
- **New, not a modification of the frozen functions**: `proximity_masks()` (`<=`-based, parallel to but never touching `arm_masks()`); a block-table/bootstrap implementation parameterized by (a) which outcome column to resample (`breach_at_expiry`, `ever_breached_intraperiod`, `reach_by_10`, or `reach_by_expiry` — four, not one) and (b) which two arms to subtract, in which order (`combined - GEXonly` for breach/intraperiod, `GEXonly - combined` for eventual reach, `combined - GEXonly` and `combined - proximity_only` for `reach_by_10` — Section 7 states each explicitly).
- **Acceptance-criteria fixtures, required before any real run, specifically targeting the bug classes this adaptation invites:**
  1. A fixture where using `>=` instead of `<=` for the proximity mask changes a known synthetic result — catches an accidental reuse of the extension-study's mask direction.
  2. A fixture where substituting `baseline` for `GEX-only` as the comparator changes a known synthetic result — catches reverting to the 09-17 proposal's comparator by habit.
  3. A fixture with a known-sign synthetic result catching a reversed subtraction on the **terminal-breach** gate specifically.
  4. **A separate fixture, not folded into (3), catching a reversed subtraction on the `reach_by_expiry` gate specifically** — this gate's higher-is-better convention is the opposite of every other gate in this design, making a copy-pasted subtraction direction from the breach gates the single most likely real bug in this implementation, not a generic case covered by fixture (3).

**Right-censoring**: same modeled-expiry/holiday-calendar logic as the 09-17 proposal, plus the new `excluded_missing_midperiod_price` counter for a session-level data gap occurring strictly inside the holding period (not just at the terminal session).

**Provenance — recoverability mechanism stated first, hashing as the cross-check on top of it (Astra round 2 item 6 — round 2's hash list alone doesn't let anyone recover the exact code that produced a run if the tree was dirty and nothing else was saved):**
- **Corrected (Astra round 3 item 1 — a bare `git diff` misses exactly the case this exists to cover):** `git diff` alone omits staged changes and, critically, **untracked files** — this proposal's own new script will be untracked at run time, so a bare `git diff > run.patch` would not even capture the script that produced the run. Fix: **either** (a) `git diff HEAD > run.patch` (tracked, staged+unstaged) plus an explicit copy of every untracked source file into the run's provenance directory, with the base commit recorded, **or** (b), simpler and stronger, **snapshot copies of every file already enumerated in the hash list below** into the run's provenance directory — the hash list already names exactly the files that matter, so copying those same files closes the recovery gap directly rather than relying on `git diff`'s blind spots (untracked files, staged changes, binary diffs). This proposal adopts (b).
- **Hashed (sha256), as a cross-check that the saved/committed state matches what actually ran, expanded from round 2's list**: this proposal's own new script; **the imported frozen `gex_pin_ma150_extension_test.py`** (this proposal calls into it; if that file's content ever changes, results could silently differ without the new script's own hash catching it); **`bps_signal_engine_v2.py`** (transitive dependency via `generate_baseline()`, itself modified during the 2026-09-17/18 Ghost System integration work — a live-changing file, not a stable one); the IV/HV cache; the OHLCV cache; the GEX/DIX cache; `data/universe.csv`; `us-market-holidays.txt`; **and this proposal document itself**, so a run's provenance record ties back to the exact proposal text that authorized it.
- `git_head` and `git_dirty` recorded as supplementary metadata, same as the 09-17 script, not as the primary recovery mechanism.

**New script location, unchanged**: `scripts/backtest/z_ma150_proximity_dit_test.py`, sibling to the frozen 09-17 script, importing from it rather than duplicating entry-generation/GEX-attachment logic.

## 6. Split boundaries

Unchanged from round 2 / the 09-17 proposal: fixed historical split, not walk-forward. IS 2023-07-26 to 2025-01-26, OOS 2025-01-27 to 2026-07-24. Development track only. **Verdicts and the bootstrap use OOS only** (restated from Run Metadata, per advisor's catch that round 2 left this implicit).

## 7. Stopping rule

For the gating threshold (`z_ma150_proximity_upper_bound = 0.50` only), **on the OOS window only**, after applying right-censoring exclusions (Section 4/5):

- **Hard short-circuit, before any CI is computed, not merely listed first: if `n < 30` combined-arm trades, the result is `INCONCLUSIVE` and no inferential machinery below runs.**
- **Per-gate verdict rule, disjoint by construction (Astra round 2 item 5 — round 2's independent PASS/INCONCLUSIVE conditions could both be true at `upper == margin`; fixed here by strict ordering, check strongest condition first):** for any single CI-based gate with a stated direction and margin (or zero, for the two superiority comparisons):
  1. If the condition for `PASS` holds, the gate is `PASS`.
  2. **Else if** the condition for `FAIL` holds, the gate is `FAIL`.
  3. **Else** the gate is `INCONCLUSIVE`.
  This ordering makes the three outcomes mutually exclusive by construction — no case can satisfy two branches, because each branch is only evaluated if the prior one failed.

**Primary metric — `reach_by_10`, two superiority sub-gates, combination stated explicitly (Astra round 2 item 5 — "define outcome when speed CI passes but combined doesn't beat proximity-only"):**
- **Gate A** (`combined` vs. `GEX-only`): `PASS` if the CI **lower** bound on `(reach_by_10(combined) - reach_by_10(GEXonly))` is `> 0`; `FAIL` if the CI **upper** bound is `< 0`; else `INCONCLUSIVE`.
- **Gate B** (`combined` vs. `proximity-only`): same structure, `(reach_by_10(combined) - reach_by_10(proximity-only))`.
- **Primary metric overall**: `PASS` only if **both** Gate A and Gate B are `PASS`. `FAIL` if **either** is `FAIL`. Otherwise `INCONCLUSIVE`.

**Non-inferiority — three gates, each with its own stated direction (Astra round 2 item 2, advisor's sign-convention catch):**
- **Gate C (terminal breach)**: `deterioration_breach := combined_breach_rate - GEXonly_breach_rate`. `PASS` if CI **upper** bound `<= margin_breach`; `FAIL` if CI **lower** bound `> margin_breach`; else `INCONCLUSIVE`.
- **Gate D (intraperiod breach)**: `deterioration_intraperiod := combined_intraperiod_rate - GEXonly_intraperiod_rate`. Same structure as Gate C, own margin `margin_intraperiod`.
- **Gate E (eventual reach, opposite direction — stated explicitly, not inferred from Gates C/D)**: `deterioration_reach := GEXonly_reach_by_expiry_rate - combined_reach_by_expiry_rate`. `PASS` if CI **upper** bound `<= margin_reach`; `FAIL` if CI **lower** bound `> margin_reach`; else `INCONCLUSIVE`.
- **Non-inferiority overall**: `PASS` only if **all three** of Gates C, D, E are `PASS`. `FAIL` if **any** is `FAIL`. Otherwise `INCONCLUSIVE`.

**Overall verdict**: `PASS` only if the `n >= 30` short-circuit clears, the primary metric overall is `PASS`, and the non-inferiority overall is `PASS` — five CIs total (Gates A, B, C, D, E), all must clear. `FAIL` if either the primary-metric or non-inferiority overall is `FAIL`. Otherwise `INCONCLUSIVE`.

**Margins frozen at `0.0` (Section 8) — strictness consequence, stated here where the gate logic lives, not only in Section 8:** with `margin_breach = margin_intraperiod = margin_reach = 0.0`, Gates C/D/E's `PASS` condition (`CI upper bound <= margin`) requires the combined arm's deterioration CI to be confidently at or below zero — for two arms with genuinely equal true rates, roughly a 2.5% chance by construction (a 95% two-sided CI's own false-positive rate on one side). **Gates C/D/E therefore `PASS` only when the combined arm is confidently *better*, not merely equal** — at zero margin, a non-inferiority gate functions as a superiority test. `INCONCLUSIVE` on one or more of these gates is the **expected** outcome for genuinely comparable arms, not a sign the design is broken. Per Astra: do not loosen these margins after seeing results.

**Uncertainty method**: 45-day moving/overlapping blocks, entry-date block assignment — same construction as the 09-17 proposal, adapted per Section 5.

**Frozen inference parameters, declared now, before any run (Astra round 3 — required before execution):**
- **CI level: 95% (2.5%/97.5% percentiles)** — inherited from the 09-17 proposal's own bootstrap, unchanged, documented directly rather than left implicit.
- **Bootstrap repetitions: 10,000** — same inherited value.
- **Seed: `20260918`** — this proposal's own registration date, following the exact same convention the 09-17 proposal used (`20260917`, its own registration date). A fresh, separately-declared seed, never reused from the 09-17 run, frozen here before any run and never selected after seeing results.

**Exploratory series (`0.15`/`0.30`)**: all five gates computed and reported the same way, purely descriptive, never entering the overall verdict above.

**Acceptance criteria, corrected from round 2's byte-equality claim, extended with Section 5's new fixtures:**
1. Baseline/GEX-only arms have identical candidate-identifying and inherited fields (not full-row equality) to the frozen 09-17 script's own arms, on the same cached universe.
2. Daily-reprice DIT/`reach_by_D` computation matches a hand-computed value on a synthetic price path with a known target-crossing session, including a synthetic case exercising the nominal-vs-modeled-expiry tenor distinction (Section 5) directly. **Fixture corrected (Astra round 3 item 3 — the terminal mark is intrinsic and therefore tenor-independent by construction; asking a tenor-convention difference to change it asks for something the formula cannot produce):** a holiday-shifted expiry where using `modeled_expiry` instead of `nominal_expiry` for the daily tenor must produce **detectably different pre-settlement Black-Scholes marks** along the path, while the **intrinsic terminal mark is identical under both conventions** — the fixture asserts both halves, not a difference in the terminal value itself.
3. Censoring/reach accounting reconciles exactly per arm: `reached_by_10 + not_reached_by_10 == arm candidate count` (after exclusions), and separately `reached_by_expiry + not_reached_by_expiry == arm candidate count`, with `excluded_oos_cutoff + excluded_missing_terminal_price + excluded_missing_midperiod_price` accounted for outside both.
4. The four bootstrap fixtures specified in Section 5 (wrong mask direction, wrong comparator, wrong sign on a lower-is-better gate, wrong sign on the reach-by-expiry gate specifically).

---

## 8. Margins — frozen (Astra, round 4)

**All three margins frozen at `0.0pp` (`0.0` as a fraction — the implementation's unit convention, confirmed against `scripts/backtest/z_ma150_proximity_dit_test.py`'s CLI, where `margin_breach=0.02` means 2pp):**

| Parameter | Value |
|---|---:|
| `margin_breach` | **0.0pp** |
| `margin_intraperiod` | **0.0pp** |
| `margin_reach` | **0.0pp** |

**Astra's reasoning, adopted verbatim:** zero tolerated deterioration matches the hypothesis's own original framing — "without increasing risk" — not an empirically estimated tolerance, a research acceptance criterion. **Margins are independent**, not scaled off one another: the intraperiod base rate running ~1.8x the terminal rate (Section 4) does not justify tolerating 1.8x more deterioration there, and the actual comparator throughout is `GEX-only`, not baseline, so the baseline/intraperiod relationship isn't the relevant one anyway. **Positive margins would change the objective itself** — explicitly trading worse breach/reach outcomes for faster capture — which is not what this proposal's hypothesis claims to test.

**Consequence, stated explicitly so a future reader doesn't mistake the intended strictness for a broken design (advisor catch, round 4):** with every margin at `0.0`, each non-inferiority gate's `PASS` condition (`CI upper bound <= margin`) requires the combined arm's CI to be confidently at or below zero deterioration — for two arms with genuinely equal true rates, that has roughly a 2.5% chance by chance alone (the flip side of a 95% two-sided CI). **In practice, Gates C/D/E will `PASS` only if the combined arm is confidently *better*, not merely equal, on each metric** — a non-inferiority gate at zero margin is a superiority test in non-inferiority framing. `INCONCLUSIVE` is the **expected**, not anomalous, outcome for genuinely comparable arms under this design. This is the intended strictness for a Development-track screen under a "without increasing risk" hypothesis, per Astra: **do not loosen the margins after seeing results** if the run comes back `INCONCLUSIVE` on one or more of these gates — that would be exactly the kind of post-hoc threshold selection this codebase's registration discipline exists to prevent.

Registration is unblocked by this freeze. The 2×2 overlap diagnostic (Section 4) remains a required implementation output, already built, not a separate decision.

## Results (registered run `20260918T174731Z_998760cb225f`, 2026-09-18T17:47:31Z, completed 2026-09-18T17:48:31Z, exit 0, 60.86s)

**Hypothesis rejected — a clean, decisive FAIL, not the strictness-driven INCONCLUSIVE the margin freeze anticipated.** Every gate FAILs confidently at all three thresholds (`z<=0.15`, `0.30`, gating `0.50`); no CI anywhere near zero, `n=491` at the gating threshold (well past the `n>=30` floor).

**Gating threshold (`z_ma150 <= 0.50`), `combined` (n=491) vs. `GEX-only` (n=4194):**
- Gate A (`reach_by_10` superiority): **FAIL** — combined 11.20% vs. GEX-only 29.97%, CI on the difference `[-22.34%, -15.66%]`. The proximity filter reaches the 80% profit target within 10 days *less* than half as often as the comparator, not faster.
- Gate B (`reach_by_10` vs. `proximity-only`): INCONCLUSIVE (CI `[-0.07%, 1.46%]`) — moot given Gate A's FAIL already fails the primary metric overall.
- Gate C (terminal breach non-inferiority): **FAIL** — 27.90% vs. 13.09%, CI `[10.10%, 19.02%]` against a 0.00% margin.
- Gate D (intraperiod breach non-inferiority): **FAIL** — 54.99% vs. 23.80%, CI `[27.79%, 34.42%]`.
- Gate E (eventual reach non-inferiority): **FAIL** — 76.78% vs. 89.87%, CI `[8.56%, 17.11%]`.
- **Overall: FAIL.** Exploratory thresholds `0.15` and `0.30` show the identical pattern, same direction, similar magnitude — narrowing the band does not rescue the result.

**2x2 overlap diagnostic (Section 4/8), OOS baseline pool, n=5121:** 617 candidates (12.05%) in the proximity band; 3378 (65.96%) in production's `near_ma150_support` band; 617 of those 617 proximity-band candidates (100.00%) also fall inside the production band, but the production band is roughly 5.5x larger than the proximity band (`P(in proximity | in production) = 18.27%`) — one-directional near-total containment, not redundancy, exactly the asymmetry Astra's round-3 correction anticipated.

**Interpretation, held to the same standard this document has applied throughout — descriptive, not causal:** within this specific `vrp_only`-gated pool, proximity to EMA150 associates with materially *worse* outcomes on every measured axis, not better. This is directionally consistent with (not a joint test of, and not established alongside) the accepted 09-17 proposal's finding that *extension* away from EMA150 associates with *better* outcomes in the same pool. See the market-mechanics discussion delivered alongside this commit for a structural read of why — held explicitly to the same "not established by this design" standard as every mechanism claim in Section 1.

**No further sweep.** Margins were not loosened after seeing this result, per Section 7/8's own rule. This screening candidate is closed, not iterated on.

## Revision Log

- **2026-09-17, initial → round 1**: first draft.
- **2026-09-18, round 1 → round 2**: primary metric changed to whole-cohort `reach_by_D`; comparator changed to GEX-only; Section 1 corrected on the EMA150/near-support gate-overlap question; fixed the stopped-trade contradiction; simplified the threshold sweep; corrected acceptance criteria and provenance scope. Full detail in the round-2 diff (superseded by this revision, not reproduced here).
- **2026-09-18, round 2 → round 3 (this revision), addressing Astra/Fable round 2's 6-point review plus 4 catches from an independent advisor pass on the round-3 plan before writing:**
  1. Terminal-pricing description corrected after verifying `score_trade()` directly: nominal-expiry tenor before settlement, intrinsic (not Black-Scholes) at modeled settlement — round 2's claim that the 09-17 study used BS-with-tenor-floor at expiry was wrong (Section 5).
  2. Non-inferiority scope widened from one gate to three (terminal breach, intraperiod breach, eventual reach), closing the hypothesis/acceptance-criteria mismatch Astra identified; the eventual-reach gate's opposite (higher-is-better) sign convention is stated explicitly rather than inherited from the breach gates by habit, per an independent advisor catch on this exact risk before writing (Section 4/7).
  3. IV renamed precisely to "frozen signal-date base IV," smile adjustment recomputation stated separately, IV-compression scope limit stated (Section 1/5).
  4. Bootstrap explicitly specified as adapted, not imported — new mask function, parameterized outcome/comparator, four required fixtures including one specifically for the reach-by-expiry sign (Section 5).
  5. Verdict branches made disjoint by explicit ordering (PASS checked before FAIL before INCONCLUSIVE) for every gate; primary-metric combination logic (Gates A/B) and non-inferiority combination logic (Gates C/D/E) both stated explicitly; `n < 30` made a hard short-circuit before any CI computation (Section 7).
  6. Provenance mechanism reordered — clean-tree-or-saved-diff stated as the actual recovery path, hashing as the cross-check on top, hash list expanded to the imported frozen script, `bps_signal_engine_v2.py`, the GEX cache, universe/holiday files, and the proposal document itself (Section 5).
  7. "Every candidate gets a value" corrected to "every eligible candidate after declared exclusions" (Section 4).
  8. Open items (Section 8) reframed as the user's risk-tolerance decisions per Astra's explicit reclassification, not flagged as if pending further statistical or code review.
  9. Independent advisor catches folded in before writing: shared block draws stated to extend across outcomes, not only arms; the non-zero first-mark noted as expected, not a bug; `bps_signal_engine_v2.py` added to the provenance hash list as a live-changing transitive dependency.
- **2026-09-18, round 3 → round 4 (this revision), addressing Astra/Fable round 3's 4 bounded corrections, confirmed by an independent advisor pass (including a direct re-read of `score_trade()` to verify correction 3 before applying it):**
  1. Provenance mechanism corrected: bare `git diff` omits untracked files (including this proposal's own new script, untracked at run time) and staged changes. Adopted snapshot-copies-of-the-hashed-files as the recovery mechanism, replacing the round-3 `git diff > run.patch` approach (Section 5).
  2. 2×2 overlap diagnostic's population corrected from "OOS combined-eligible pool" (ambiguous, could already be proximity-filtered) to the explicit OOS baseline pool (arm 1, before either filter) — Astra's recommendation, adopted (Section 4).
  3. Acceptance-criterion fixture corrected: verified directly against `score_trade()` that the terminal mark is pure intrinsic and never calls `bs_price`, so a tenor-convention difference cannot and must not be asked to change it. Fixture now asserts differing pre-settlement Black-Scholes marks under the nominal-vs-modeled tenor conventions, with an identical intrinsic terminal mark under both (Section 7).
  4. Removed the "independent" characterization of the five bootstrap CIs (they share candidates, block draws, and overlapping outcome definitions) while keeping the INCONCLUSIVE-inflation warning; softened "a lower eventual reach now fails this gate" to "is evaluated against its own margin and uncertainty," since a shortfall within `margin_reach` legitimately PASSes under the rule as specified (Section 4).
  5. Astra settled two of round 2's remaining open items directly: intraperiod gate kept (matches the hypothesis's path-risk framing), overlap diagnostic reclassified as a required implementation output, not a permission-gated decision. Section 8 now lists only the three margins.
  6. Added a "Frozen inference parameters" declaration before any run: 95% CI, 10,000 bootstrap repetitions (both inherited from the 09-17 proposal), fresh seed `20260918` (this proposal's own registration date, same convention as the 09-17 proposal's `20260917`) (Section 7).
  7. Per Astra's own framing — "ready for implementation after four bounded corrections; registration still needs margins frozen" — this revision closes the implementation gate. The three margins remain open and gate registration/execution only, not the script build or its fixtures.
- **2026-09-18, round 4 → round 5 (this revision) — margins frozen (Astra), registration unblocked:**
  1. All three margins frozen at `0.0pp` (Section 8), per Astra's explicit recommendation: zero tolerated deterioration matches the hypothesis's own "without increasing risk" framing, margins kept independent (the 1.8x intraperiod/terminal base-rate ratio does not justify a scaled margin), positive margins would change the objective rather than measure it.
  2. Strictness consequence of zero margins — Gates C/D/E function as superiority tests at this setting, `INCONCLUSIVE` is the expected outcome for genuinely comparable arms — stated explicitly in both Section 7 (where the gate logic lives) and Section 8, per an independent advisor catch that this needed to be written down before freezing, not left implicit.
  3. Confirmed against the implementation's actual CLI (`--margin-breach`/`--margin-intraperiod`/`--margin-reach`, `type=float`, fraction units, `None` default) before writing the frozen values, rather than assuming the unit convention.
