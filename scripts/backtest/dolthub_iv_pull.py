"""Pull DoltHub `post-no-preference/options`.`volatility_history` for the
production universe and cache it to disk.

Non-production research artifact. Output lives under
scripts/backtest/run_out_dolthub_iv/ (gitignored by the run_out* pattern).

Query shape -- measured 2026-09-11, this is the load-bearing decision:
  The table's only index is PRIMARY KEY (`date`, `act_symbol`). A per-symbol
  query (`WHERE act_symbol = 'X'`) is therefore a full-table scan; it ran in
  ~2s while the server had the table hot and then hit DoltHub's ~55s server
  side deadline ("context deadline exceeded") on nearly every call. A
  50-symbol IN-list time series is both a full scan AND over the API's
  1000-row response cap (query_execution_status "RowLimit").
  A per-DATE query with the whole universe in the IN list uses the PK prefix:
  `WHERE date = D AND act_symbol IN (<751>)` returned 579 rows in 2.5s and
  can never exceed 751 rows (PK is unique per (date, symbol)), so the cap is
  irrelevant. DoltHub only has rows on roughly every other weekday (Mon/Wed/
  Fri in the June-2024 sample, ~1540 symbols per date); empty weekdays cost
  ~1.4s. So: one query per weekday 2023-07-01..2026-07-31, ~800 queries.

Any status other than "Success" (RowLimit, Error, Timeout) is a failed
attempt -- the API can return "Error" with a partial row set, which must
never be accepted. Each date is written to raw_by_date/<DATE>.json as it
lands (empty dates included, so they are not re-queried) and the job resumes
after an interruption. http.client.HTTPException (IncompleteRead) is caught
explicitly -- it is not an OSError and it killed the previous per-symbol run.

Symbol spelling: universe.csv says BRKB / BFB; DoltHub says BRK.B / BF.B.
The cache is keyed by the universe.csv spelling.

Validation (--validate, default on): the earlier per-symbol run left
raw/<CODE>.json for 289 symbols (236 with a complete window). Those were
accepted only on "Success", so on every symbol present in both, the by-date
rows must reproduce them exactly (same date set, same iv_current). That is a
genuinely independent second pull of the same cells. A COUNT(*) spot-check
against the server is NOT used -- it is a full scan and times out now.
Also pulls the complete symbol list for one recent date (2 pages) and lists
which universe codes are absent from DoltHub, so a spelling mismatch can be
told apart from a genuinely untracked name.

Usage:
    python3 dolthub_iv_pull.py            # pull (resumable) + build cache + validate
    python3 dolthub_iv_pull.py --build    # rebuild the pickle from raw_by_date/ only
Outputs:
    run_out_dolthub_iv/raw_by_date/<DATE>.json  one file per weekday (rows as returned)
    run_out_dolthub_iv/volatility_history.pkl   {code: DataFrame(date-indexed, float cols)}
    run_out_dolthub_iv/coverage.csv             per-symbol row counts / date span / null counts
    run_out_dolthub_iv/validate_vs_persymbol.csv  by-date vs per-symbol cross-check
    run_out_dolthub_iv/dolt_symbols_<DATE>.txt  full DoltHub symbol list on one date
"""
from __future__ import annotations

import argparse
import csv
import http.client
import json
import pickle
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

BASE = Path(__file__).resolve().parent
UNIVERSE_CSV = BASE.parent.parent / "data" / "universe.csv"
OUT_DIR = BASE / "run_out_dolthub_iv"
RAW_DATE_DIR = OUT_DIR / "raw_by_date"
RAW_SYM_DIR = OUT_DIR / "raw"  # legacy per-symbol pull, used only for validation
CACHE_PKL = OUT_DIR / "volatility_history.pkl"

API = "https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master?q="
DATE_FROM, DATE_TO = "2023-07-01", "2026-07-31"
DOLT_ALIASES = {"BRKB": "BRK.B", "BFB": "BF.B"}
COLS = ["date", "act_symbol", "hv_current", "hv_week_ago", "hv_month_ago", "hv_year_high",
        "hv_year_high_date", "hv_year_low", "hv_year_low_date", "iv_current", "iv_week_ago",
        "iv_month_ago", "iv_year_high", "iv_year_high_date", "iv_year_low", "iv_year_low_date"]
FLOAT_COLS = [c for c in COLS if c.startswith(("hv_", "iv_")) and not c.endswith("_date")]

WORKERS = 2
MAX_ATTEMPTS = 6
BACKOFF = [3, 6, 12, 24, 48]
HTTP_TIMEOUT = 120
NET_EXC = (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException,
           TimeoutError, OSError, json.JSONDecodeError)


def load_universe() -> List[str]:
    with open(UNIVERSE_CSV) as f:
        return [r["ticker"].strip() for r in csv.DictReader(f) if r.get("ticker", "").strip()]


def dolt_query(sql: str) -> Dict[str, Any]:
    url = API + urllib.parse.quote(sql)
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def query_with_retry(sql: str, label: str) -> Dict[str, Any]:
    """Returns {"rows": [...], "attempts": n, "elapsed": s} or {"rows": None, "error": ...}."""
    last_err = ""
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.time()
        try:
            d = dolt_query(sql)
            status = d.get("query_execution_status")
            if status == "Success":
                return {"rows": d.get("rows", []), "attempts": attempt + 1, "elapsed": round(time.time() - t0, 1)}
            last_err = f"{status}: {str(d.get('query_execution_message', ''))[:160]}"
        except NET_EXC as e:  # noqa: PERF203
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"  retry {label} attempt {attempt + 1} failed after {time.time() - t0:.1f}s: {last_err}", flush=True)
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)] + random.uniform(0, 3))
    return {"rows": None, "attempts": MAX_ATTEMPTS, "error": last_err}


def in_list(codes: List[str]) -> str:
    return ", ".join(f"'{DOLT_ALIASES.get(c, c)}'" for c in codes)


def fetch_date(date: str, inl: str) -> Dict[str, Any]:
    sql = f"SELECT {', '.join(COLS)} FROM volatility_history WHERE date = '{date}' AND act_symbol IN ({inl})"
    r = query_with_retry(sql, date)
    r["date"] = date
    return r


def weekdays(a: str, b: str) -> List[str]:
    return [str(d.date()) for d in pd.bdate_range(a, b)]


def pull(codes: List[str]) -> None:
    RAW_DATE_DIR.mkdir(parents=True, exist_ok=True)
    inl = in_list(codes)
    dates = weekdays(DATE_FROM, DATE_TO)
    todo = [d for d in dates if not (RAW_DATE_DIR / f"{d}.json").exists()]
    print(f"weekdays {len(dates)}  already cached {len(dates) - len(todo)}  to pull {len(todo)}  "
          f"(universe {len(codes)} symbols per query, {WORKERS} workers)", flush=True)
    t_start = time.time()
    done = failed = nonempty = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_date, d, inl): d for d in todo}
        for fut in as_completed(futs):
            r = fut.result()
            if r["rows"] is None:
                failed += 1
                print(f"FAIL {r['date']} after {r['attempts']} attempts: {r['error']}", flush=True)
                continue
            # Atomic replace: `todo` above skips any date whose .json file
            # already exists (a resume mechanism for interrupted pulls) --
            # a crash mid-write here would otherwise leave a truncated file
            # that looks "already cached" forever, silently corrupting or
            # blocking that date instead of getting re-fetched on the next
            # resume run (CodeRabbit finding, 2026-09-15).
            dest = RAW_DATE_DIR / f"{r['date']}.json"
            tmp = dest.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(r, f)
            tmp.replace(dest)
            done += 1
            nonempty += bool(r["rows"])
            if done % 25 == 0 or r["attempts"] > 1:
                el = time.time() - t_start
                print(f"[{done + failed}/{len(todo)}] {r['date']}: {len(r['rows'])} rows, "
                      f"{r['attempts']} attempt(s), {r['elapsed']}s  | non-empty so far {nonempty} | elapsed {el:.0f}s "
                      f"eta {el / (done + failed) * (len(todo) - done - failed):.0f}s", flush=True)
    print(f"pull finished: ok={done} (non-empty {nonempty}) failed={failed} in {time.time() - t_start:.0f}s", flush=True)
    if failed:
        print(f"WARNING: {failed} dates failed -- re-run to resume them before building", flush=True)


def load_by_date() -> pd.DataFrame:
    frames = []
    n_files = n_empty = 0
    for p in sorted(RAW_DATE_DIR.glob("*.json")):
        with open(p) as f:
            r = json.load(f)
        n_files += 1
        if not r["rows"]:
            n_empty += 1
            continue
        frames.append(pd.DataFrame(r["rows"]))
    if not frames:
        sys.exit("no by-date rows found -- run the pull first")
    df = pd.concat(frames, ignore_index=True)
    print(f"by-date files {n_files} (empty {n_empty}); rows {len(df)}; dates {df['date'].nunique()} "
          f"[{df['date'].min()}..{df['date'].max()}]; dolt symbols {df['act_symbol'].nunique()}", flush=True)
    return df


def build(codes: List[str]) -> Dict[str, pd.DataFrame]:
    # Hard-fail on a missing date FILE, unconditionally (not gated by
    # --no-validate -- validate() only cross-checks row VALUES between two
    # independent pulls, it never checks date-file completeness). --build
    # rebuilds straight from whatever raw_by_date/*.json already exists on
    # disk, so a partial/interrupted earlier pull (or a stale directory from
    # before DATE_TO was extended) would otherwise bake silent date gaps
    # into volatility_history.pkl with no warning (CodeRabbit finding).
    expected = set(weekdays(DATE_FROM, DATE_TO))
    present = {p.stem for p in RAW_DATE_DIR.glob("*.json")}
    missing = sorted(expected - present)
    if missing:
        sys.exit(
            f"cannot build: {len(missing)}/{len(expected)} expected dates missing from "
            f"{RAW_DATE_DIR} (first: {missing[0]}, last: {missing[-1]}) -- run pull() to "
            f"resume them first, or narrow DATE_FROM/DATE_TO to match what's actually cached."
        )
    df = load_by_date()
    rev = {DOLT_ALIASES.get(c, c): c for c in codes}
    df["code"] = df["act_symbol"].map(rev)
    unknown = df["code"].isna().sum()
    if unknown:
        print(f"WARNING: {unknown} rows with act_symbol not in universe (should be 0): "
              f"{df.loc[df['code'].isna(), 'act_symbol'].unique()[:10]}", flush=True)
        df = df.dropna(subset=["code"])
    df["date"] = pd.to_datetime(df["date"])
    for c in FLOAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    cache: Dict[str, pd.DataFrame] = {}
    cov_rows = []
    for code in codes:
        g = df[df["code"] == code].drop(columns=["code"]).drop_duplicates("date").set_index("date").sort_index()
        if g.empty:
            cov_rows.append({"code": code, "status": "no_rows", "rows": 0, "dolt_symbol": DOLT_ALIASES.get(code, code)})
            continue
        cache[code] = g
        iv = g["iv_current"]
        cov_rows.append({"code": code, "status": "ok", "dolt_symbol": DOLT_ALIASES.get(code, code), "rows": len(g),
                         "first": str(g.index.min().date()), "last": str(g.index.max().date()),
                         "iv_null": int(iv.isna().sum()), "iv_nonnull": int(iv.notna().sum()),
                         "iv_median": float(iv.median()) if iv.notna().any() else float("nan"),
                         "hv_null": int(g["hv_current"].isna().sum())})
    cov = pd.DataFrame(cov_rows)
    cov.to_csv(OUT_DIR / "coverage.csv", index=False)
    with open(CACHE_PKL, "wb") as f:
        pickle.dump(cache, f)
    ok = cov[cov["status"] == "ok"]
    print(f"cache built: {len(cache)} symbols with rows, {len(cov) - len(cache)} without "
          f"({cov['status'].value_counts().to_dict()}); wrote {CACHE_PKL}", flush=True)
    if len(ok):
        print(f"rows/symbol min/median/max {ok['rows'].min()}/{ok['rows'].median():.0f}/{ok['rows'].max()}; "
              f"symbols with >0 null iv: {int((ok['iv_null'] > 0).sum())}; "
              f"symbols with <300 non-null iv rows: {int((ok['iv_nonnull'] < 300).sum())}", flush=True)
    return cache


def validate(cache: Dict[str, pd.DataFrame], codes: List[str]) -> None:
    # 1. by-date pull vs the earlier per-symbol pull (independent second read of the same cells)
    out = []
    for p in sorted(RAW_SYM_DIR.glob("*.json")) if RAW_SYM_DIR.exists() else []:
        with open(p) as f:
            r = json.load(f)
        code = r["code"]
        rows = r["rows"] or []
        ps = pd.DataFrame(rows)
        rec: Dict[str, Any] = {"code": code, "persym_rows": len(ps), "bydate_rows": len(cache.get(code, []))}
        if not len(ps) or code not in cache:
            rec["match"] = (len(ps) == 0) == (code not in cache)
            rec["note"] = "both empty" if rec["match"] else "present in only one pull"
            out.append(rec)
            continue
        ps["date"] = pd.to_datetime(ps["date"])
        ps = ps.drop_duplicates("date").set_index("date").sort_index()
        bd = cache[code]
        same_dates = ps.index.equals(bd.index)
        common = ps.index.intersection(bd.index)
        a = pd.to_numeric(ps.loc[common, "iv_current"], errors="coerce")
        b = bd.loc[common, "iv_current"]
        iv_mismatch = int(((a.isna() != b.isna()) | ((a - b).abs() > 1e-9)).sum())
        rec.update({"same_date_set": bool(same_dates), "n_common": len(common), "iv_mismatch": iv_mismatch,
                    "match": bool(same_dates and iv_mismatch == 0),
                    "note": "" if same_dates else f"persym-only {len(ps.index.difference(bd.index))}, bydate-only {len(bd.index.difference(ps.index))}"})
        out.append(rec)
    if out:
        v = pd.DataFrame(out)
        v.to_csv(OUT_DIR / "validate_vs_persymbol.csv", index=False)
        nonempty = v[v["persym_rows"] > 0]
        print(f"validate vs per-symbol pull: {int(v['match'].sum())}/{len(v)} symbols match "
              f"({int(nonempty['match'].sum())}/{len(nonempty)} among symbols with per-symbol rows); "
              f"total common cells {int(nonempty['n_common'].sum())}, iv mismatches {int(nonempty['iv_mismatch'].sum())}", flush=True)
        bad = v[~v["match"]]
        if len(bad):
            print(bad.to_string(index=False), flush=True)
    # 2. which universe codes does DoltHub not track at all (on one recent date, all ~1540 symbols)?
    date = max(str(d.index.max().date()) for d in cache.values())
    syms: List[str] = []
    for page in range(5):
        r = query_with_retry(f"SELECT act_symbol FROM volatility_history WHERE date = '{date}' "
                             f"ORDER BY act_symbol LIMIT 1000 OFFSET {page * 1000}", f"symbols p{page}")
        if r["rows"] is None:
            print(f"symbol-list page {page} failed: {r['error']}", flush=True)
            break
        syms += [x["act_symbol"] for x in r["rows"]]
        if len(r["rows"]) < 1000:
            break
    if syms:
        (OUT_DIR / f"dolt_symbols_{date}.txt").write_text("\n".join(syms))
        sset = set(syms)
        absent = [c for c in codes if DOLT_ALIASES.get(c, c) not in sset]
        punct = sorted(s for s in syms if any(ch in s for ch in ".-/ "))
        print(f"DoltHub tracks {len(syms)} symbols on {date}; universe codes absent that day: {len(absent)}", flush=True)
        print(f"  absent: {absent}", flush=True)
        print(f"  DoltHub symbols containing punctuation (possible spelling variants): {punct}", flush=True)
        no_rows = [c for c in codes if c not in cache]
        print(f"  universe codes with NO rows in the whole window: {len(no_rows)}; of those present on {date}: "
              f"{[c for c in no_rows if DOLT_ALIASES.get(c, c) in sset]}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="rebuild cache from raw_by_date/ without pulling")
    ap.add_argument("--no-validate", action="store_true")
    a = ap.parse_args()
    OUT_DIR.mkdir(exist_ok=True)
    codes = load_universe()
    if not a.build:
        pull(codes)
    cache = build(codes)
    if not a.no_validate and cache:
        validate(cache, codes)


if __name__ == "__main__":
    main()
