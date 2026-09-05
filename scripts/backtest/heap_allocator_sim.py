"""Shared 'Global Heap' allocator simulation.

Reimplementation of the ranked-allocator sim from the 2026-09-02 session
(never committed as a standalone script -- see git log 5f1b314/48967e9 for
the doc-only changes that described it). This file exists so the loosened-
gate ("structural_2of3") test and the 7-of-7 baseline run through the exact
same allocation logic, not two hand-written variants.

Spec (README.md / ibkr-live-workflow.md, "Global Heap" model):
  - heap = heap_fraction * net_liq (default 50%), a shared risk pool.
  - per-trade ceiling = per_trade_fraction * net_liq (default 25%), so no
    single name can swallow the whole heap.
  - Candidates are processed in chronological entry-date order (matching
    the live nightly-scan cadence -- you only rank what's in front of you
    that night, not future signals). Within a date, candidates are ranked
    by R/R ascending (richest credit-per-risk first).
  - For each candidate, try width widest-to-narrowest; accept the first
    width whose max_loss lets contracts = floor(min(per_trade_cap,
    heap_remaining) / max_loss_per_contract) be >= 1. No width fits ->
    BLOCKED (skipped, not sized to zero).
  - One open spread per sector at a time (sector diversification).
  - Capital releases on the ACTUAL close date (max timestamp across a
    spread's expire/exercise/early_exercise legs), not planned expiry --
    matches the 48967e9 CodeRabbit fix (early exercise can close early).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

WIDTHS_WIDEST_FIRST = [10.0, 5.0, 2.5, 1.0]


def test_rr_band(max_loss: float, credit: float) -> bool:
    if pd.isna(max_loss) or pd.isna(credit) or credit <= 0:
        return False
    rr = max_loss / (credit * 100.0)
    return (1.5 - 1e-6) <= rr <= (2.5 + 1e-6)


def _primary_rr(row: pd.Series) -> float | None:
    """R/R of the widest width that clears the 1.5-2.5 band, for ranking."""
    for w in WIDTHS_WIDEST_FIRST:
        suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
        ml = row.get(f"max_loss_{suffix}")
        cr = row.get(f"credit_{suffix}")
        if test_rr_band(ml, cr):
            return ml / (cr * 100.0)
    return None


@dataclass
class OpenPosition:
    code: str
    sector: str
    exit_date: pd.Timestamp
    reserved: float


@dataclass
class AllocatorResult:
    allocated: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)

    @property
    def allocated_count(self) -> int:
        return len(self.allocated)

    @property
    def total_pnl(self) -> float:
        return sum(r["scaled_pnl"] for r in self.allocated)

    @property
    def wins(self) -> int:
        return sum(1 for r in self.allocated if r["scaled_pnl"] > 0)


def simulate_heap_allocation(
    merged: pd.DataFrame,
    net_liq: float,
    heap_fraction: float = 0.5,
    per_trade_fraction: float = 0.25,
    enforce_sector_diversification: bool = True,
    sector_map: dict[str, str] | None = None,
    rank_ascending: bool = True,
    commission_per_spread: float = 0.0,
) -> AllocatorResult:
    """merged: outer-joined per-signal frame with per-width max_loss_W/credit_W/
    spread_pnl_W/exit_date_W columns (W in 10, 5, 2_5, 1), plus code, entry_date.
    """
    heap_total = heap_fraction * net_liq
    per_trade_cap_base = per_trade_fraction * net_liq
    heap_remaining = heap_total

    merged = merged.copy()
    merged["entry_date"] = pd.to_datetime(merged["entry_date"])
    merged["rr_rank"] = merged.apply(_primary_rr, axis=1)
    candidates = merged[merged["rr_rank"].notna()].copy()

    result = AllocatorResult()
    open_positions: list[OpenPosition] = []

    for entry_date, day_group in candidates.groupby("entry_date"):
        still_open = []
        for pos in open_positions:
            if pos.exit_date <= entry_date:
                heap_remaining += pos.reserved
            else:
                still_open.append(pos)
        open_positions = still_open

        open_sectors = {p.sector for p in open_positions}

        for _, row in day_group.sort_values("rr_rank", ascending=rank_ascending).iterrows():
            code = row["code"]
            sector = (sector_map or {}).get(code, "UNKNOWN")

            if enforce_sector_diversification and sector in open_sectors:
                result.blocked.append({
                    "code": code, "entry_date": entry_date, "reason": "sector_correlation",
                })
                continue

            allocated_this = None
            for w in WIDTHS_WIDEST_FIRST:
                suffix = str(w).rstrip("0").rstrip(".").replace(".", "_")
                ml = row.get(f"max_loss_{suffix}")
                cr = row.get(f"credit_{suffix}")
                pnl1 = row.get(f"spread_pnl_{suffix}")
                exit_d = row.get(f"exit_date_{suffix}")
                if not test_rr_band(ml, cr):
                    continue
                per_trade_cap = min(per_trade_cap_base, heap_remaining)
                contracts = math.floor(per_trade_cap / ml) if ml > 0 else 0
                if contracts >= 1:
                    # Open-only commission (2 legs x $0.65 = $1.30/spread); a
                    # bought-back-early close adds another ~$1.30, an
                    # expired-worthless close adds $0 -- not distinguishable
                    # from backtest data, so this is a floor, not a ceiling.
                    commission = contracts * commission_per_spread
                    allocated_this = {
                        "code": code, "entry_date": entry_date, "width": w,
                        "max_loss": ml, "credit": cr, "contracts": contracts,
                        "reserved": contracts * ml,
                        "scaled_pnl": contracts * pnl1 - commission,
                        "exit_date": pd.to_datetime(exit_d) if pd.notna(exit_d) else entry_date + pd.Timedelta(days=30),
                    }
                    break

            if allocated_this is None:
                result.blocked.append({
                    "code": code, "entry_date": entry_date, "reason": "heap_exhausted_all_widths",
                })
                continue

            heap_remaining -= allocated_this["reserved"]
            open_positions.append(OpenPosition(
                code=code, sector=sector,
                exit_date=allocated_this["exit_date"], reserved=allocated_this["reserved"],
            ))
            open_sectors.add(sector)
            result.allocated.append(allocated_this)

    return result
