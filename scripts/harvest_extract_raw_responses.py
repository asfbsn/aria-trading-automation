#!/usr/bin/env python3
"""
Extracts real IBKR tool_use/tool_result pairs from a harvest_ibkr_edge_cases.sh
stream-json capture into individual raw-response files under
state/jev_training_raw/. Runs locally, outside the agent's own permissions --
the harvest agent itself has no write capability (see harvest_ibkr_edge_cases.sh).

Each output file carries the real tool name, real input args, the real
response (or real error), and an `intended_label` field -- that is a HYPOTHESIS
from the prompt's design intent, not a verified ground-truth label. Every
fixture still needs independent review before being trusted as ground truth,
the same way the earlier live-capture fixtures were reviewed by hand.

Usage: python3 scripts/harvest_extract_raw_responses.py <stream.jsonl>
"""
import hashlib
import json
import sys
from pathlib import Path

ARIA_HOME = Path(__file__).resolve().parent.parent
OUT_DIR = ARIA_HOME / "state" / "jev_training_raw"

# Call order matches prompts/harvest-ibkr-edge-cases.md exactly -- used only to
# attach an `intended_label` hint per position. Not authoritative; a call can
# land on a different real label than intended (e.g. call 6 might come back
# TOOL_ERROR or a bare empty NO_DATA-shaped response depending on how the
# connector actually handles an absurd contract id -- that's real information,
# not a scoring failure).
INTENDED_LABELS_BY_POSITION = {
    1: "VALID", 2: "VALID",
    3: "TOOL_ERROR", 4: "TOOL_ERROR", 5: "TOOL_ERROR", 6: "TOOL_ERROR",
    7: "NO_DATA", 8: "NO_DATA",
    9: "MALFORMED_DATA", 10: "MALFORMED_DATA", 11: "MALFORMED_DATA", 12: "MALFORMED_DATA",
    13: "NO_DATA", 14: "MALFORMED_DATA", 15: "MALFORMED_DATA", 16: "MALFORMED_DATA",
    17: "VALID_OR_MALFORMED_EDGE", 18: "VALID_EDGE_UNBOUNDED",
}


def extract(stream_path: Path) -> list[dict]:
    lines = [json.loads(l) for l in stream_path.read_text().splitlines() if l.strip()]

    tool_uses = {}
    order = []
    for line in lines:
        if line.get("type") == "assistant":
            for block in line.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name", "").startswith(
                    "mcp__claude_ai_Interactive_Brokers_IBKR"
                ):
                    tool_uses[block["id"]] = {"name": block["name"], "input": block.get("input")}
                    order.append(block["id"])

    results = []
    position = 0
    for line in lines:
        if line.get("type") != "user":
            continue
        for block in line.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            tu_id = block.get("tool_use_id")
            if tu_id not in tool_uses:
                continue
            position += 1
            tu = tool_uses[tu_id]
            content = block.get("content")
            if isinstance(content, list):
                text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            else:
                text = str(content)
            try:
                parsed_response = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed_response = text

            results.append({
                "position": position,
                "intended_label": INTENDED_LABELS_BY_POSITION.get(position, "unknown"),
                "tool": tu["name"].rsplit("__", 1)[-1],
                "input": tu["input"],
                "is_error": block.get("is_error"),
                "response": parsed_response,
            })
    return results


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: harvest_extract_raw_responses.py <stream.jsonl>", file=sys.stderr)
        return 2
    stream_path = Path(sys.argv[1])
    if not stream_path.exists():
        print(f"not found: {stream_path}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = extract(stream_path)
    if not results:
        print("no IBKR tool_use/tool_result pairs found in this stream", file=sys.stderr)
        return 1

    for r in results:
        digest = hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:12]
        out_path = OUT_DIR / f"{r['position']:02d}_{r['tool']}_{digest}.json"
        out_path.write_text(json.dumps(r, indent=2, default=str))
        print(f"wrote {out_path.relative_to(ARIA_HOME)}  intended={r['intended_label']}  is_error={r['is_error']}")

    print(f"\n{len(results)} raw responses extracted to {OUT_DIR.relative_to(ARIA_HOME)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
