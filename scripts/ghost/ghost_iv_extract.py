#!/usr/bin/env python3
"""
scripts/ghost/ghost_iv_extract.py

Deterministic extractor and CSV logger for IBKR implied volatility (IV).
Part of Ghost Phase 2 Step 1 (log-only calibration data collector).
STDLIB ONLY: no external dependencies.

CLI usage:
  python3 scripts/ghost/ghost_iv_extract.py --stream <stream.jsonl> --pool <pool.json> --out-csv <out.csv>
  python3 scripts/ghost/ghost_iv_extract.py --test
"""

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile

CSV_FIELDS = [
    "capture_date",
    "signal_bar_date",
    "pool_id",
    "ticker",
    "conid",
    "status",
    "ibkr_iv",
    "ibkr_hv_info",
    "top_status",
    "capture_ts_utc",
    "capture_ts_source",
    "local_hv30",
    "vrp_ibkr",
    "dolt_iv_signal_bar",
    "dolt_hv_signal_bar",
    "z_ma150",
    "pool_size_pre_sample",
    "sampled",
    "code_version_hash",
]


def compute_code_hash() -> str:
    """Return SHA-256 hash of this script file."""
    try:
        script_path = Path(__file__).resolve()
        if script_path.exists():
            return hashlib.sha256(script_path.read_bytes()).hexdigest()
    except Exception:
        pass
    return ""


def parse_stream(stream_path: Path) -> tuple[dict[str, dict], list[dict], int]:
    """
    Parse an Anthropic Claude stream JSONL file.
    Re-implements stream parsing approach of scripts/harvest_extract_raw_responses.py.
    Never raises on malformed lines; skips and counts them.
    Returns: (tool_uses_by_id, completed_tool_calls, malformed_line_count)
    """
    malformed_lines = 0
    raw_lines = []
    if stream_path.exists():
        with stream_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw_lines.append(json.loads(stripped))
                except Exception:
                    malformed_lines += 1

    tool_uses = {}
    for line in raw_lines:
        msg_type = line.get("type") or line.get("role")
        if msg_type == "assistant":
            msg = line.get("message")
            content = msg.get("content") if isinstance(msg, dict) else line.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tu_id = block.get("id")
                        name = str(block.get("name", ""))
                        norm_name = name.rsplit("__", 1)[-1]
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        if tu_id:
                            tool_uses[tu_id] = {
                                "id": tu_id,
                                "tool": norm_name,
                                "raw_name": name,
                                "input": inp,
                            }

    tool_calls = []
    for line in raw_lines:
        msg_type = line.get("type") or line.get("role")
        if msg_type == "user":
            msg = line.get("message")
            content = msg.get("content") if isinstance(msg, dict) else line.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tu_id = block.get("tool_use_id")
                        if tu_id not in tool_uses:
                            continue
                        tu = tool_uses[tu_id]
                        is_err = bool(block.get("is_error"))
                        c = block.get("content")
                        parsed = None
                        parse_err = False
                        if isinstance(c, dict):
                            parsed = c
                        elif isinstance(c, list):
                            text = "\n".join(
                                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                                for item in c
                            )
                            try:
                                parsed = json.loads(text)
                            except Exception:
                                parsed = text
                                parse_err = True
                        else:
                            text = "" if c is None else str(c)
                            try:
                                parsed = json.loads(text)
                            except Exception:
                                parsed = text
                                parse_err = True

                        tool_calls.append({
                            "id": tu_id,
                            "tool": tu["tool"],
                            "raw_name": tu["raw_name"],
                            "input": tu["input"],
                            "is_error": is_err,
                            "parse_error": parse_err,
                            "response": parsed,
                        })

    return tool_uses, tool_calls, malformed_lines


def extract_ticker_row(
    ticker_entry: dict,
    pool_meta: dict,
    tool_uses: dict[str, dict],
    tool_calls: list[dict],
    wall_clock_utc: datetime,
    code_hash: str,
) -> dict:
    """
    Deterministic rule engine per pool ticker (fail-closed, never trusting agent selection).
    """
    ticker = str(ticker_entry.get("ticker", "")).strip()
    signal_bar_date = str(pool_meta.get("signal_bar_date", ""))
    pool_id = str(pool_meta.get("pool_id", ""))
    pool_size_pre_sample = pool_meta.get("pool_size_pre_sample", "")
    sampled = pool_meta.get("sampled", "")

    # Base row
    row = {
        "capture_date": wall_clock_utc.strftime("%Y-%m-%d"),
        "signal_bar_date": signal_bar_date,
        "pool_id": pool_id,
        "ticker": ticker,
        "conid": "",
        "status": "",
        "ibkr_iv": "",
        "ibkr_hv_info": "",
        "top_status": "",
        "capture_ts_utc": wall_clock_utc.isoformat(),
        "capture_ts_source": "wallclock",
        "local_hv30": ticker_entry.get("local_hv30", "") if ticker_entry.get("local_hv30") is not None else "",
        "vrp_ibkr": "",
        "dolt_iv_signal_bar": ticker_entry.get("dolt_iv_signal_bar", "") if ticker_entry.get("dolt_iv_signal_bar") is not None else "",
        "dolt_hv_signal_bar": ticker_entry.get("dolt_hv_signal_bar", "") if ticker_entry.get("dolt_hv_signal_bar") is not None else "",
        "z_ma150": ticker_entry.get("z_ma150", "") if ticker_entry.get("z_ma150") is not None else "",
        "pool_size_pre_sample": pool_size_pre_sample,
        "sampled": sampled,
        "code_version_hash": code_hash,
    }

    # Step 1: Find search_contracts calls
    matching_searches = []
    for call in tool_calls:
        if call["tool"] == "search_contracts":
            q = str(call["input"].get("query", "")).strip()
            if q.lower() == ticker.lower():
                matching_searches.append(call)

    if not matching_searches:
        any_search_in_stream = any(call["tool"] == "search_contracts" for call in tool_calls) or any(
            tu["tool"] == "search_contracts" for tu in tool_uses.values()
        )
        if any_search_in_stream:
            row["status"] = "no_search"
        else:
            row["status"] = "no_response"
        return row

    # Find first successful search call
    successful_searches = [
        c for c in matching_searches
        if not c["is_error"] and not c["parse_error"] and isinstance(c["response"], dict)
    ]
    if not successful_searches:
        row["status"] = "tool_error"
        return row

    chosen_search = successful_searches[0]
    search_resp = chosen_search["response"]
    results = search_resp.get("results")
    if not isinstance(results, list):
        row["status"] = "conid_not_found"
        return row

    # KNOWN LIMITATION to document in a comment (do not alias in Step 1):
    # The universe CSV writes BRKB/BFB without dots while IBKR's symbol is e.g. 'BRK B',
    # so those tickers will produce conid_not_found -- expected and acceptable.
    valid_matches = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if "symbol" not in r or "underlying_contract_id" not in r:
            continue
        if r["symbol"] is None or r["underlying_contract_id"] is None:
            continue
        if str(r["symbol"]).strip().upper() != ticker.upper():
            continue
        if r.get("country_code") != "US":
            continue
        sections = r.get("sections")
        if not isinstance(sections, list):
            continue
        sec_types = {s.get("security_type") for s in sections if isinstance(s, dict)}
        if not {"STK", "OPT"}.issubset(sec_types):
            continue
        valid_matches.append(r)

    if len(valid_matches) == 0:
        row["status"] = "conid_not_found"
        return row
    if len(valid_matches) > 1:
        row["status"] = "conid_ambiguous"
        return row

    raw_conid = valid_matches[0]["underlying_contract_id"]
    try:
        expected_conid = int(raw_conid)
    except (ValueError, TypeError):
        row["status"] = "conid_not_found"
        return row

    row["conid"] = str(expected_conid)

    # Step 2: Find get_price_snapshot calls for expected_conid
    matching_snapshots = []
    for call in tool_calls:
        if call["tool"] == "get_price_snapshot":
            raw_cid = call["input"].get("contract_id")
            try:
                cid = int(raw_cid)
            except (ValueError, TypeError):
                cid = None
            if cid is not None and cid == expected_conid:
                matching_snapshots.append(call)

    if not matching_snapshots:
        row["status"] = "no_snapshot"
        return row

    successful_snapshots = [
        c for c in matching_snapshots
        if not c["is_error"] and not c["parse_error"] and isinstance(c["response"], dict)
    ]
    if not successful_snapshots:
        row["status"] = "tool_error"
        return row

    chosen_snapshot = successful_snapshots[0]
    snap_resp = chosen_snapshot["response"]

    # Step 3: Extract fields from snapshot response
    # Keys are hyphenated; underscore keys NOT accepted
    ts_obj = snap_resp.get("top-status")
    if isinstance(ts_obj, dict) and ts_obj.get("status"):
        row["top_status"] = str(ts_obj.get("status"))

    last_obj = snap_resp.get("last")
    if isinstance(last_obj, dict) and "ts" in last_obj:
        raw_ts = last_obj.get("ts")
        if not isinstance(raw_ts, bool) and isinstance(raw_ts, (int, float)) and math.isfinite(raw_ts):
            try:
                row["capture_ts_utc"] = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat()
                row["capture_ts_source"] = "last_ts"
            except (ValueError, TypeError, OverflowError):
                row["capture_ts_utc"] = wall_clock_utc.isoformat()
                row["capture_ts_source"] = "wallclock"

    hv_obj = snap_resp.get("historical-vol")
    if isinstance(hv_obj, dict):
        ann_pct = hv_obj.get("annual_pct")
        if not isinstance(ann_pct, bool) and isinstance(ann_pct, (int, float)) and math.isfinite(ann_pct) and ann_pct > 0:
            row["ibkr_hv_info"] = float(ann_pct)

    # Implied vol underlying check
    ivu_obj = snap_resp.get("implied-vol-underlying")
    iv_val = None
    if isinstance(ivu_obj, dict) and ivu_obj.get("is_valid") is True:
        raw_iv = ivu_obj.get("annual_iv")
        if not isinstance(raw_iv, bool) and isinstance(raw_iv, (int, float)) and math.isfinite(raw_iv):
            f_iv = float(raw_iv)
            if 0 < f_iv <= 5.0:
                iv_val = f_iv

    if iv_val is None:
        row["status"] = "iv_invalid"
        row["ibkr_iv"] = ""
        row["vrp_ibkr"] = ""
        return row

    # Step 4: status "ok"
    row["status"] = "ok"
    row["ibkr_iv"] = iv_val

    local_hv30_raw = ticker_entry.get("local_hv30")
    lhv = None
    try:
        if local_hv30_raw is not None and not isinstance(local_hv30_raw, bool):
            lhv = float(local_hv30_raw)
    except (ValueError, TypeError):
        lhv = None

    if lhv is not None and math.isfinite(lhv) and lhv > 0:
        row["vrp_ibkr"] = iv_val / lhv
    else:
        row["vrp_ibkr"] = ""

    return row


def append_csv_rows(csv_path: Path, fields: list[str], new_rows: list[dict]) -> tuple[int, int]:
    """
    Append rows with fcntl.flock locking, header validation (failing loud on mismatch),
    and idempotency on (signal_bar_date, ticker) with status == 'ok'.
    Returns: (appended_count, skipped_count)
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = csv_path.parent / f".{csv_path.name}.lock"
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        is_new = not csv_path.exists() or csv_path.stat().st_size == 0
        existing_ok = set()
        if not is_new:
            with csv_path.open("r", newline="", encoding="utf-8") as existing:
                reader = csv.reader(existing)
                existing_header = next(reader, [])
                if existing_header != fields:
                    raise ValueError(
                        f"{csv_path} header does not match current fields -- would silently "
                        f"misalign every column from here on. Existing: {existing_header}. "
                        f"Current: {fields}. Migrate or archive the old file before writing."
                    )
            with csv_path.open("r", newline="", encoding="utf-8") as existing:
                dict_reader = csv.DictReader(existing)
                for r in dict_reader:
                    if r.get("status") == "ok":
                        existing_ok.add((str(r.get("signal_bar_date")), str(r.get("ticker"))))

        appended = 0
        skipped = 0
        rows_to_write = []
        for r in new_rows:
            key = (str(r.get("signal_bar_date")), str(r.get("ticker")))
            # Idempotency: skip appending if an existing row has (signal_bar_date, ticker) AND status == "ok"
            if key in existing_ok:
                skipped += 1
                continue
            rows_to_write.append(r)
            if r.get("status") == "ok":
                existing_ok.add(key)
            appended += 1

        if rows_to_write or is_new:
            with csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                if is_new:
                    writer.writeheader()
                for r in rows_to_write:
                    writer.writerow(r)

        return appended, skipped


def process_stream_and_pool(stream_path: Path, pool_path: Path, out_csv_path: Path) -> dict[str, int]:
    """Main extraction pipeline for a stream and pool file."""
    wall_clock_utc = datetime.now(timezone.utc)
    code_hash = compute_code_hash()

    tool_uses, tool_calls, malformed_lines = parse_stream(stream_path)

    pool_data = {}
    if pool_path.exists():
        try:
            pool_data = json.loads(pool_path.read_text(encoding="utf-8"))
        except Exception:
            pool_data = {}

    tickers_list = pool_data.get("tickers", [])
    if not isinstance(tickers_list, list):
        tickers_list = []

    rows = []
    status_counts = {}
    for entry in tickers_list:
        if not isinstance(entry, dict):
            continue
        row = extract_ticker_row(
            entry, pool_data, tool_uses, tool_calls, wall_clock_utc, code_hash
        )
        rows.append(row)
        st = row["status"]
        status_counts[st] = status_counts.get(st, 0) + 1

    appended, skipped = append_csv_rows(out_csv_path, CSV_FIELDS, rows)

    counts_str = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
    total = len(rows)
    print(f"summary: {counts_str or 'none'}, total={total} (malformed_stream_lines={malformed_lines}, appended={appended}, skipped_idempotent={skipped})")
    return status_counts


# ============================================================================
# SELF TESTS (--test)
# ============================================================================

def make_stream_line(role: str, content_blocks: list) -> str:
    return json.dumps({"type": role, "message": {"content": content_blocks}})


def run_tests() -> None:
    """Comprehensive test suite covering all Item E requirements."""
    code_hash = compute_code_hash()
    wall_clock = datetime(2026, 10, 1, 14, 0, 0, tzinfo=timezone.utc)

    # 1. ok path
    tu1 = {"type": "tool_use", "id": "tu_1", "name": "mcp__claude_ai_Interactive_Brokers_IBKR__search_contracts", "input": {"query": "CSCO"}}
    tr1 = {
        "type": "tool_result", "tool_use_id": "tu_1", "is_error": False,
        "content": json.dumps({
            "results": [
                {
                    "underlying_contract_id": 268084, "exchange": "NASDAQ", "symbol": "CSCO",
                    "description": "CISCO SYSTEMS INC", "country_code": "US",
                    "sections": [{"security_type": "STK"}, {"security_type": "OPT"}]
                }
            ]
        })
    }
    tu2 = {"type": "tool_use", "id": "tu_2", "name": "mcp__claude_ai_Interactive_Brokers_IBKR__get_price_snapshot", "input": {"contract_id": 268084}}
    tr2 = {
        "type": "tool_result", "tool_use_id": "tu_2", "is_error": False,
        "content": json.dumps({
            "last": {"price": 108.08, "ts": 1790864457, "halted": False, "is_close": False},
            "top-status": {"status": "REALTIME"},
            "historical-vol": {"daily_pct": 0.0154941, "annual_pct": 0.245961},
            "implied-vol-underlying": {"daily_iv": 0.0188, "annual_iv": 0.29844, "is_valid": True}
        })
    }

    with tempfile.TemporaryDirectory(prefix="ghost_iv_test_") as tmpdir:
        tmp = Path(tmpdir)

        # Write stream
        stream_path = tmp / "stream.jsonl"
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2]),
        ]))

        pool_meta = {
            "pool_id": "pool_test_1", "signal_bar_date": "2026-09-30",
            "pool_size_pre_sample": 10, "sampled": False
        }
        ticker_entry = {
            "ticker": "CSCO", "signal_close": 100.0, "z_ma150": 1.6,
            "local_hv30": 0.20, "dolt_iv_signal_bar": 0.25, "dolt_hv_signal_bar": 0.22
        }

        uses, calls, bad = parse_stream(stream_path)
        assert bad == 0
        r_ok = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_ok["status"] == "ok", f"Expected ok, got {r_ok['status']}"
        assert r_ok["conid"] == "268084"
        assert math.isclose(float(r_ok["ibkr_iv"]), 0.29844, rel_tol=1e-5)
        assert math.isclose(float(r_ok["vrp_ibkr"]), 0.29844 / 0.20, rel_tol=1e-5)
        assert r_ok["top_status"] == "REALTIME"
        assert r_ok["capture_ts_source"] == "last_ts"
        assert r_ok["capture_ts_utc"] == datetime.fromtimestamp(1790864457, tz=timezone.utc).isoformat()

        # 2. -1.0 sentinel -> iv_invalid
        tr2_sentinel = {
            "type": "tool_result", "tool_use_id": "tu_2", "is_error": False,
            "content": json.dumps({
                "last": {"ts": 1790864457},
                "implied-vol-underlying": {"daily_iv": -1.0, "annual_iv": -1.0, "is_valid": True}
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2_sentinel]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_sentinel = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_sentinel["status"] == "iv_invalid"
        assert r_sentinel["ibkr_iv"] == ""
        assert r_sentinel["vrp_ibkr"] == ""

        # 3. is_valid false -> iv_invalid
        tr2_is_valid_false = {
            "type": "tool_result", "tool_use_id": "tu_2", "is_error": False,
            "content": json.dumps({
                "implied-vol-underlying": {"annual_iv": 0.298, "is_valid": False}
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2_is_valid_false]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_iv_f = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_iv_f["status"] == "iv_invalid"

        # 4. missing implied-vol-underlying key -> iv_invalid
        tr2_missing_key = {
            "type": "tool_result", "tool_use_id": "tu_2", "is_error": False,
            "content": json.dumps({
                "top-status": {"status": "REALTIME"}
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2_missing_key]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_miss = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_miss["status"] == "iv_invalid"

        # 5. agent snapshot with the WRONG contract_id -> no_snapshot
        tu2_wrong_cid = {"type": "tool_use", "id": "tu_2", "name": "get_price_snapshot", "input": {"contract_id": 999999}}
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2_wrong_cid]),
            make_stream_line("user", [tr2]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_wrong_cid = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_wrong_cid["status"] == "no_snapshot"
        assert r_wrong_cid["conid"] == "268084"

        # 6. two US STK+OPT rows -> conid_ambiguous
        tr1_ambiguous = {
            "type": "tool_result", "tool_use_id": "tu_1", "is_error": False,
            "content": json.dumps({
                "results": [
                    {"underlying_contract_id": 268084, "symbol": "CSCO", "country_code": "US", "sections": [{"security_type": "STK"}, {"security_type": "OPT"}]},
                    {"underlying_contract_id": 999111, "symbol": "CSCO", "country_code": "US", "sections": [{"security_type": "STK"}, {"security_type": "OPT"}]}
                ]
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1_ambiguous]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_amb = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_amb["status"] == "conid_ambiguous"
        assert r_amb["conid"] == ""

        # 7. no US row -> conid_not_found
        tr1_no_us = {
            "type": "tool_result", "tool_use_id": "tu_1", "is_error": False,
            "content": json.dumps({
                "results": [
                    {"underlying_contract_id": 38708253, "symbol": "CSCO", "country_code": "MX", "sections": [{"security_type": "STK"}]}
                ]
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1_no_us]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_not_found = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_not_found["status"] == "conid_not_found"

        # 8. tool_result is_error -> tool_error
        tr1_err = {
            "type": "tool_result", "tool_use_id": "tu_1", "is_error": True,
            "content": "IBKR connector timeout"
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1_err]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_terr = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_terr["status"] == "tool_error"

        # 9. truncated stream (ticker with no calls) -> no_response
        stream_path.write_text("")
        uses, calls, _ = parse_stream(stream_path)
        r_no_resp = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_no_resp["status"] == "no_response"

        # 10. idempotent re-run adds no duplicate ok row
        test_csv = tmp / "test_log.csv"
        rows_to_test = [r_ok]
        app1, skip1 = append_csv_rows(test_csv, CSV_FIELDS, rows_to_test)
        assert app1 == 1 and skip1 == 0
        app2, skip2 = append_csv_rows(test_csv, CSV_FIELDS, rows_to_test)
        assert app2 == 0 and skip2 == 1
        with test_csv.open("r", newline="", encoding="utf-8") as handle:
            total_csv_rows = list(csv.DictReader(handle))
        assert len(total_csv_rows) == 1, f"Expected exactly 1 row, got {len(total_csv_rows)}"

        # 11. hyphenated keys honored
        assert r_ok["top_status"] == "REALTIME"
        assert math.isclose(float(r_ok["ibkr_hv_info"]), 0.245961, rel_tol=1e-5)

        # 12. underscore keys NOT accepted -> iv_invalid
        tr2_underscore = {
            "type": "tool_result", "tool_use_id": "tu_2", "is_error": False,
            "content": json.dumps({
                "last": {"ts": 1790864457},
                "top_status": {"status": "REALTIME"},
                "historical_vol": {"annual_pct": 0.245961},
                "implied_vol_underlying": {"annual_iv": 0.29844, "is_valid": True}
            })
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2_underscore]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_underscore = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_underscore["status"] == "iv_invalid"
        assert r_underscore["top_status"] == ""
        assert r_underscore["ibkr_hv_info"] == ""

        # 13. no_search (a search_contracts call exists but none matched the ticker query)
        tu_other = {"type": "tool_use", "id": "tu_other", "name": "search_contracts", "input": {"query": "OTHER"}}
        tr_other = {"type": "tool_result", "tool_use_id": "tu_other", "is_error": False, "content": "{}"}
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu_other]),
            make_stream_line("user", [tr_other]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_no_search = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_no_search["status"] == "no_search"

        # 14. an unparseable/non-JSON tool_result -> tool_error
        tr1_bad_json = {
            "type": "tool_result", "tool_use_id": "tu_1", "is_error": False,
            "content": "502 Bad Gateway: not json"
        }
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1_bad_json]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_bad_json = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_bad_json["status"] == "tool_error"

        # 15. coerce input.contract_id with int() inside try/except (non-numeric -> no_snapshot)
        tu2_non_num = {"type": "tool_use", "id": "tu_2", "name": "get_price_snapshot", "input": {"contract_id": "non_numeric_val"}}
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2_non_num]),
            make_stream_line("user", [tr2]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_non_num = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_non_num["status"] == "no_snapshot"

        # 16. snapshot tool_result is_error -> tool_error
        tu2_snap_err = {"type": "tool_use", "id": "tu_2", "name": "get_price_snapshot", "input": {"contract_id": 268084}}
        tr2_snap_err = {"type": "tool_result", "tool_use_id": "tu_2", "is_error": True, "content": "Snapshot failed"}
        stream_path.write_text("\n".join([
            make_stream_line("assistant", [tu1]),
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2_snap_err]),
            make_stream_line("user", [tr2_snap_err]),
        ]))
        uses, calls, _ = parse_stream(stream_path)
        r_snap_err = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_snap_err["status"] == "tool_error"

        # 17. non-ok earlier row does not block new ok row
        test_non_ok_csv = tmp / "test_non_ok.csv"
        app_err, _ = append_csv_rows(test_non_ok_csv, CSV_FIELDS, [r_snap_err])
        assert app_err == 1
        app_ok_after_err, _ = append_csv_rows(test_non_ok_csv, CSV_FIELDS, [r_ok])
        assert app_ok_after_err == 1
        with test_non_ok_csv.open("r", newline="", encoding="utf-8") as handle:
            rows_after = list(csv.DictReader(handle))
        assert len(rows_after) == 2
        assert rows_after[0]["status"] == "tool_error"
        assert rows_after[1]["status"] == "ok"

        # 18. malformed stream lines counted and not crash
        stream_path.write_text("\n".join([
            "{not valid json",
            make_stream_line("assistant", [tu1]),
            "also not valid json",
            make_stream_line("user", [tr1]),
            make_stream_line("assistant", [tu2]),
            make_stream_line("user", [tr2]),
        ]))
        uses, calls, bad = parse_stream(stream_path)
        assert bad == 2
        r_after_bad = extract_ticker_row(ticker_entry, pool_meta, uses, calls, wall_clock, code_hash)
        assert r_after_bad["status"] == "ok"

        # 19. CSV header mismatch fails loud
        bad_header_csv = tmp / "bad_header.csv"
        bad_header_csv.write_text("wrong,header,columns\n1,2,3\n")
        try:
            append_csv_rows(bad_header_csv, CSV_FIELDS, [r_ok])
            raise AssertionError("Header mismatch should have raised ValueError")
        except ValueError as e:
            assert "header does not match current fields" in str(e)

    print("ALL TESTS PASSED successfully.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic IBKR IV extractor and CSV logger")
    parser.add_argument("--stream", type=str, help="Path to stream JSONL file")
    parser.add_argument("--pool", type=str, help="Path to pool JSON file")
    parser.add_argument("--out-csv", type=str, help="Path to output CSV file")
    parser.add_argument("--test", action="store_true", help="Run self-tests")

    args = parser.parse_args()

    if args.test:
        run_tests()
        return 0

    if not args.stream or not args.pool or not args.out_csv:
        parser.print_help(sys.stderr)
        return 2

    stream_path = Path(args.stream)
    pool_path = Path(args.pool)
    out_csv_path = Path(args.out_csv)

    if not stream_path.exists():
        print(f"Error: stream file not found: {stream_path}", file=sys.stderr)
        return 1
    if not pool_path.exists():
        print(f"Error: pool file not found: {pool_path}", file=sys.stderr)
        return 1

    process_stream_and_pool(stream_path, pool_path, out_csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
