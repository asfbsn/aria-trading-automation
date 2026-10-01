#!/usr/bin/env python3
"""Systemic-failure classifier for Ghost wrapper reconciliation (stdlib only).

The wrappers' reconciliation only asserted that every candidate/position had *some* terminal
outcome, so a run in which every quote was rejected for a systemic reason (e.g. run outside
market hours) still wrote its success line and the watchdog stayed silent (marks, 2026-09-18).

Two tiers, because not every systemic-looking reason is run-wide:
  CLOCK  - wall-clock/date problems (outside market hours, bad mark date): one row means the run is bad.
  FEED   - data-feed problems that can be a per-ticker fact (one frozen illiquid option, one stale
           contract timestamp): only fail the run when they hit a large share of rows.
"""
from __future__ import annotations

import sys
from collections import Counter

CLOCK_REASONS = ("quote_outside_regular_hours", "missing_or_invalid_mark_date")
FEED_REASONS = (
    "market_data_not_live_or_missing",
    "missing_or_invalid_quote_timestamp",
    "quote_date_does_not_match_mark_date",
)
SYSTEMIC_REASONS = CLOCK_REASONS + FEED_REASONS


def _systemic_code(row: dict) -> str | None:
    if (row.get("outcome") or "") != "rejected":
        return None
    reason = row.get("reason") or ""
    for code in SYSTEMIC_REASONS:
        if reason == code or reason.startswith(code):
            return code
    return None


def systemic_failure(terminal_rows: list[dict], mode: str) -> tuple[bool, str]:
    """Return (failed, message).

    Fails when: any CLOCK-tier rejection; or FEED-tier rejections >= max(2, ceil(n/2)); or (mode 'mark'
    only) every row is rejected. mode 'entry' never applies the all-rejected rule: an entry day where
    every candidate is legitimately rejected (e.g. exact-strike misses) is a normal outcome.
    Sub-threshold FEED rejections print a WARNING to stderr (lands in the wrapper's ERR file)."""
    if mode not in ("mark", "entry"):
        raise ValueError(f"mode must be 'mark' or 'entry', got {mode!r}")
    if not terminal_rows:
        return False, ""
    n = len(terminal_rows)
    rejected = [r for r in terminal_rows if (r.get("outcome") or "") == "rejected"]
    systemic = Counter(code for code in (_systemic_code(r) for r in terminal_rows) if code)
    clock = {k: v for k, v in systemic.items() if k in CLOCK_REASONS}
    feed_n = sum(v for k, v in systemic.items() if k in FEED_REASONS)
    threshold = max(2, -(-n // 2))
    hist = ", ".join(f"{k}={v}" for k, v in sorted(systemic.items()))
    if clock or feed_n >= threshold:
        return True, f"systemic quote rejections: {sum(systemic.values())}/{n} rows; {hist}"[:300]
    if mode == "mark" and len(rejected) == n:
        other = Counter((r.get("reason") or "unknown")[:60] for r in rejected)
        return True, ("every mark rejected: " + f"{n}/{n}; " + ", ".join(f"{k}={v}" for k, v in other.most_common(4)))[:300]
    if feed_n:
        print(f"WARNING: {feed_n}/{n} feed-tier rejections below failure threshold {threshold}: {hist}", file=sys.stderr)
    return False, ""


def _test() -> None:
    R = lambda o, r="": {"outcome": o, "reason": r}  # noqa: E731
    # 2026-09-18 case: 10/10 rejected outside market hours (CLOCK tier)
    f, m = systemic_failure([R("rejected", "quote_outside_regular_hours")] * 10, "mark")
    assert f and "quote_outside_regular_hours=10" in m and "10/10" in m, m
    print("PASS: 09-18 case (10/10 outside-hours) fails in mark mode:", m)
    f, _ = systemic_failure([R("rejected", "quote_outside_regular_hours")] * 10, "entry")
    assert f, "clock-tier must fail entry mode too"
    print("PASS: clock-tier reasons also fail entry mode")
    # ONE clock-tier reject among good rows fails (late retry marking half the book)
    f, _ = systemic_failure([R("marked")] * 13 + [R("rejected", "quote_outside_regular_hours")], "mark")
    assert f
    print("PASS: one clock-tier reject among 13 good rows fails the run")
    # ONE feed-tier reject among good rows only warns
    f, _ = systemic_failure([R("marked")] * 26 + [R("rejected", "market_data_not_live_or_missing: FROZEN")], "mark")
    assert not f
    print("PASS: one feed-tier reject among 27 rows does not fail (warns)")
    # feed-tier at >= max(2, ceil(n/2)) fails
    rows = [R("marked")] * 7 + [R("rejected", "market_data_not_live_or_missing")] * 8
    f, m = systemic_failure(rows, "entry")
    assert f and "market_data_not_live_or_missing=8" in m, m
    f, _ = systemic_failure([R("marked")] * 8 + [R("rejected", "market_data_not_live_or_missing")] * 7, "entry")
    assert not f
    print("PASS: feed-tier fails at >=50% of rows (8/15) but not at 7/15")
    # suffix handling
    f, _ = systemic_failure([R("marked"), R("rejected", "quote_outside_regular_hours: capture 15:52")], "mark")
    assert f
    print("PASS: reason suffix recognised")
    # all rejected, non-systemic: mark fails, entry does not
    rows = [R("rejected", "short_bid_missing_or_nonpositive_or_nonfinite")] * 3
    f, m = systemic_failure(rows, "mark")
    assert f and "every mark rejected" in m, m
    f, _ = systemic_failure([R("rejected", "resolution_not_exact")] * 12, "entry")
    assert not f
    print("PASS: all-rejected non-systemic fails mark mode but NOT entry mode")
    f, _ = systemic_failure([R("marked"), R("rejected", "long_bid_missing_or_nonpositive_or_nonfinite"), R("exited")], "mark")
    assert not f
    print("PASS: mixed per-leg rejects with some marked/exited do not fail")
    # single position with one feed reject: below threshold 2; mark mode backstop catches all-rejected
    f, _ = systemic_failure([R("rejected", "market_data_not_live_or_missing")], "entry")
    assert not f
    f, _ = systemic_failure([R("rejected", "market_data_not_live_or_missing")], "mark")
    assert f
    print("PASS: single-row feed reject: entry ok, mark fails via all-rejected backstop")
    assert systemic_failure([], "mark") == (False, "") and systemic_failure([], "entry") == (False, "")
    print("PASS: empty input does not fail")
    f, _ = systemic_failure([{"outcome": None, "reason": None}, {}], "entry")
    assert not f
    print("PASS: None / missing keys tolerated")
    try:
        systemic_failure([], "bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        print("PASS: bad mode raises ValueError")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
    else:
        print(__doc__)
