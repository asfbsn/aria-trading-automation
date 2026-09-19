#!/usr/bin/env python3
"""
Jev (TypeSafe) shadow classifier for Ghost System IBKR tool responses.
Shadow mode ONLY: observes and logs to state/ghost/jev_shadow_log.jsonl.
Must NEVER fail or crash the caller (always exit 0).
"""

import argparse
import base64
import contextlib
from datetime import datetime, timezone
import fcntl
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import MagicMock, patch

# Support direct script invocation as well as imports from the repository root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from typesafe_sdk import Choice, TypeSafeClient
except Exception:
    Choice = None
    TypeSafeClient = None

try:
    from scripts.ghost.jev_triage_rules import CRITERIA, INSTRUCTIONS
except Exception:
    CRITERIA = None
    INSTRUCTIONS = None

DEFAULT_LOG_PATH = ROOT / "state" / "ghost" / "jev_shadow_log.jsonl"


class SafeArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises ValueError instead of sys.exit(2) on error."""

    def error(self, message: str) -> None:
        raise ValueError(f"CLI argument error: {message}")


def classify_payload(payload_b64: str, log_path: Path = DEFAULT_LOG_PATH) -> bool:
    """Decodes payload, calls Jev system_one classifier, and appends to shadow log."""
    if TypeSafeClient is None or Choice is None:
        raise RuntimeError("typesafe_sdk package not available")
    if CRITERIA is None or INSTRUCTIONS is None:
        raise RuntimeError("jev_triage_rules not available")

    raw_bytes = base64.b64decode(payload_b64)
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Decoded payload is not a JSON object")

    tool = payload.get("tool")
    requested_input = payload.get("requested_input")
    response_data = payload.get("response")
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}

    response_is_error_flag = None
    if "response_is_error_flag" in payload:
        val = payload["response_is_error_flag"]
        response_is_error_flag = bool(val) if val is not None else None
    elif "is_error" in payload:
        val = payload["is_error"]
        response_is_error_flag = bool(val) if val is not None else None

    state = {
        "ibkr_tool_response": {
            "tool": tool,
            "requested_input": requested_input,
            "response": response_data,
            "response_is_error_flag": response_is_error_flag,
        }
    }

    with TypeSafeClient() as client:
        ts_resp = client.system_one(
            state=state,
            model="jev-latest",
            questions={"triage": Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
        )

    triage_answer = getattr(ts_resp, "choices", {}).get("triage")
    if triage_answer is None and hasattr(ts_resp, "answers"):
        triage_answer = ts_resp.answers.get("triage")
    if triage_answer is None:
        raise ValueError("Missing triage choice in TypeSafe response")

    probs = getattr(triage_answer, "probabilities", None)
    log_entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_id": context.get("candidate_id"),
        "run_id": context.get("run_id"),
        "leg": context.get("leg"),
        "tool": tool,
        "label": getattr(triage_answer, "choice", None),
        "confidence": getattr(triage_answer, "confidence", None),
        "probabilities": dict(probs) if probs is not None else {},
        "returned_model": getattr(ts_resp, "model", None),
        "requested_model": "jev-latest",
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(log_entry) + "\n")
            f.flush()
        finally:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass

    return True


def self_test() -> int:
    """Offline test suite matching repo conventions."""
    failures = 0

    with tempfile.TemporaryDirectory(prefix="jev-shadow-test-") as temp:
        temp_dir = Path(temp)
        test_log_path = temp_dir / "jev_shadow_log.jsonl"

        mock_answer = MagicMock()
        mock_answer.choice = "VALID"
        mock_answer.confidence = 0.98
        mock_answer.probabilities = {
            "VALID": 0.98,
            "TOOL_ERROR": 0.01,
            "NO_DATA": 0.0,
            "MALFORMED_DATA": 0.01,
        }

        mock_resp = MagicMock()
        mock_resp.choices = {"triage": mock_answer}
        mock_resp.model = "jev-1.13.0"

        sample_payload = {
            "tool": "get_price_snapshot",
            "requested_input": {
                "contract_id": 265598,
                "market_data_names": ["bid_ask", "top_status"],
            },
            "response": {
                "bid_ask": {"bid": 10.5, "ask": 10.7},
                "top_status": "REALTIME",
            },
            "context": {
                "candidate_id": "test_cand_123",
                "run_id": "test_run_456",
                "leg": "short",
            },
        }
        valid_b64 = base64.b64encode(json.dumps(sample_payload).encode("utf-8")).decode("utf-8")

        def check_decode_and_log():
            with patch(f"{__name__}.TypeSafeClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.system_one.return_value = mock_resp
                mock_client_cls.return_value = mock_client

                ret = classify_payload(valid_b64, log_path=test_log_path)
                assert ret is True
                assert mock_client.system_one.call_count == 1
                call_kwargs = mock_client.system_one.call_args[1]
                assert call_kwargs["model"] == "jev-latest"
                assert "triage" in call_kwargs["questions"]
                state_arg = call_kwargs["state"]["ibkr_tool_response"]
                assert state_arg["tool"] == "get_price_snapshot"
                assert state_arg["requested_input"]["contract_id"] == 265598
                assert state_arg["response"]["top_status"] == "REALTIME"
                assert state_arg["response_is_error_flag"] is None

                lines = test_log_path.read_text().strip().split("\n")
                assert len(lines) == 1
                logged = json.loads(lines[0])
                assert logged["candidate_id"] == "test_cand_123"
                assert logged["run_id"] == "test_run_456"
                assert logged["leg"] == "short"
                assert logged["tool"] == "get_price_snapshot"
                assert logged["label"] == "VALID"
                assert logged["confidence"] == 0.98
                assert logged["probabilities"] == {
                    "VALID": 0.98,
                    "TOOL_ERROR": 0.01,
                    "NO_DATA": 0.0,
                    "MALFORMED_DATA": 0.01,
                }
                assert logged["returned_model"] == "jev-1.13.0"
                assert logged["requested_model"] == "jev-latest"
                assert "ts_utc" in logged
                datetime.fromisoformat(logged["ts_utc"])

        def check_failsafe_malformed_b64():
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(["--payload-b64", "not-valid-base64!@#$"])
            assert code == 0

        def check_failsafe_malformed_json():
            bad_json_b64 = base64.b64encode(b"{not valid json}").decode("utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(["--payload-b64", bad_json_b64])
            assert code == 0

        def check_failsafe_missing_args():
            with contextlib.redirect_stderr(io.StringIO()):
                code = main([])
            assert code == 0

        def check_failsafe_client_exception():
            with patch(f"{__name__}.TypeSafeClient", side_effect=RuntimeError("connection dropped")):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = main(["--payload-b64", valid_b64])
            assert code == 0

        def check_failsafe_missing_auth():
            with patch(f"{__name__}.TypeSafeClient", side_effect=ValueError("Missing API key")):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = main(["--payload-b64", valid_b64])
            assert code == 0

        cases = [
            ("decode valid payload, build state, and write shadow log", check_decode_and_log),
            ("fail-safe: malformed base64 exits 0 without error", check_failsafe_malformed_b64),
            ("fail-safe: malformed JSON exits 0 without error", check_failsafe_malformed_json),
            ("fail-safe: missing CLI arguments exits 0 without error", check_failsafe_missing_args),
            ("fail-safe: client/network exception exits 0 without error", check_failsafe_client_exception),
            ("fail-safe: missing API key exits 0 without error", check_failsafe_missing_auth),
        ]

        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")) as network_guard:
            for label, check in cases:
                try:
                    check()
                    assert network_guard.call_count == 0
                    print(f"PASS: {label}")
                except Exception as error:
                    failures += 1
                    print(f"FAIL: {label}: {error}")

    return bool(failures)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if "--self-test" in argv:
        return self_test()

    try:
        parser = SafeArgumentParser(description=__doc__)
        parser.add_argument("--payload-b64", required=True, help="Base64 encoded payload JSON")
        args = parser.parse_args(argv)
        classify_payload(args.payload_b64)
    except (Exception, SystemExit) as exc:
        sys.stderr.write(f"jev_shadow_classify: {exc}\n")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
