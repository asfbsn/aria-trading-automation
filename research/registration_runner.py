#!/usr/bin/env python3
"""ARIA Experiment Registration Runner.

Registers an experiment attempt BEFORE running it and records its outcome after,
to an immutable append-only event ledger (research/runs.csv). Operationalizes
docs/self-improvement-roadmap.md's protocol item 1 ("Register before running")
and Sequenced-roadmap item 1.

Pure Python stdlib + CSV + subprocess. No network, no IBKR.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

DEFAULT_RESEARCH_DIR = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = DEFAULT_RESEARCH_DIR.parent

RUN_EVENT_FIELDS = [
    "run_id",
    "event_type",
    "ts_utc",
    "proposal_path",
    "command",
    "exit_code",
    "duration_seconds",
    "log_path",
    "git_head",
    "git_dirty",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read all rows from a CSV file into a list of dicts."""
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def append(path: Path, fields: list[str], row: dict[str, str]) -> None:
    """Append a row to CSV, enforcing schema agreement on existing headers."""
    header = not path.exists() or path.stat().st_size == 0
    if not header:
        with path.open("r", newline="", encoding="utf-8") as existing:
            existing_header = next(csv.reader(existing), [])
        if existing_header != fields:
            raise ValueError(
                f"{path} header does not match current fields -- would silently "
                f"misalign every column from here on. Existing: {existing_header}. "
                f"Current: {fields}. Migrate or archive the old file before writing."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if header:
            writer.writeheader()
        writer.writerow(row)


def append_with_lock(
    path: Path, lock_path: Path, fields: list[str], row: dict[str, str]
) -> None:
    """Exclusive flock held strictly for the duration of the append operation."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            append(path, fields, row)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def get_git_provenance(repo_root: Path) -> tuple[str, str]:
    """Compute git_head commit hash and git_dirty status at repo root."""
    git_head = ""
    git_dirty = "False"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        git_head = res.stdout.strip()
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        git_dirty = "True" if res.stdout.strip() else "False"
    except Exception:
        pass

    return git_head, git_dirty


def generate_run_id() -> str:
    """Generate unique and chronologically sortable run_id."""
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%dT%H%M%SZ")
    random_hex = uuid.uuid4().hex[:12]
    return f"{ts}_{random_hex}"


class RegistrationRunner:
    """Registers and executes an experiment command with stdout/stderr teeing."""

    def __init__(
        self,
        proposal_path: Path,
        command: list[str],
        research_dir: Path | None = None,
        repo_root: Path | None = None,
    ):
        self.proposal_path = Path(proposal_path)
        self.command = list(command)
        self.research_dir = (
            Path(research_dir) if research_dir else DEFAULT_RESEARCH_DIR
        )
        self.repo_root = (
            Path(repo_root) if repo_root else DEFAULT_REPO_ROOT
        )
        self.runs_csv = self.research_dir / "runs.csv"
        self.lock_path = self.research_dir / ".runs.lock"
        self.runs_dir = self.research_dir / "runs"

        self.run_id = generate_run_id()
        self.log_path = self.runs_dir / f"{self.run_id}.log"
        self.git_head, self.git_dirty = get_git_provenance(self.repo_root)
        self.start_time = 0.0
        self.terminal_written = False
        self.registered = False
        self.proc: subprocess.Popen | None = None

    def validate_proposal(self) -> bool:
        """Validate proposal file existence and non-empty content."""
        if not self.proposal_path.exists():
            sys.stderr.write(
                f"Error: proposal file does not exist: {self.proposal_path}\n"
            )
            return False
        if not self.proposal_path.is_file():
            sys.stderr.write(
                f"Error: proposal path is not a regular file: {self.proposal_path}\n"
            )
            return False
        try:
            content = self.proposal_path.read_text(encoding="utf-8")
        except Exception as e:
            sys.stderr.write(
                f"Error: unable to read proposal file {self.proposal_path}: {e}\n"
            )
            return False

        if not content.strip():
            sys.stderr.write(
                f"Error: proposal file is empty or whitespace-only: {self.proposal_path}\n"
            )
            return False
        return True

    def register(self) -> None:
        """Register the run attempt before subprocess execution begins."""
        if self.registered:
            return
        if not self.validate_proposal():
            raise ValueError(f"Invalid proposal file: {self.proposal_path}")
        self.start_time = time.monotonic()
        now_utc = datetime.now(timezone.utc).isoformat()
        row = {
            "run_id": self.run_id,
            "event_type": "registered",
            "ts_utc": now_utc,
            "proposal_path": str(self.proposal_path),
            "command": shlex.join(self.command),
            "exit_code": "",
            "duration_seconds": "",
            "log_path": str(self.log_path),
            "git_head": self.git_head,
            "git_dirty": self.git_dirty,
        }
        append_with_lock(self.runs_csv, self.lock_path, RUN_EVENT_FIELDS, row)
        self.registered = True

    def record_terminal_event(self, event_type: str, exit_code: str = "") -> None:
        """Record completed, failed, or aborted terminal event."""
        if self.terminal_written:
            return
        self.terminal_written = True
        elapsed = (
            max(0.0, time.monotonic() - self.start_time)
            if self.start_time > 0
            else 0.0
        )
        now_utc = datetime.now(timezone.utc).isoformat()
        row = {
            "run_id": self.run_id,
            "event_type": event_type,
            "ts_utc": now_utc,
            "proposal_path": str(self.proposal_path),
            "command": shlex.join(self.command),
            "exit_code": str(exit_code) if exit_code != "" else "",
            "duration_seconds": f"{elapsed:.4f}",
            "log_path": str(self.log_path),
            "git_head": self.git_head,
            "git_dirty": self.git_dirty,
        }
        append_with_lock(self.runs_csv, self.lock_path, RUN_EVENT_FIELDS, row)

    def handle_signal(self, signum: int, frame=None) -> None:
        """Handle termination signal by writing aborted row and cleaning up child."""
        if self.registered:
            self.record_terminal_event("aborted", exit_code="")
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        exit_code = (
            130
            if signum == signal.SIGINT
            else (143 if signum == signal.SIGTERM else 1)
        )
        sys.exit(exit_code)

    def _execute_process(self) -> int:
        """Run subprocess, inheriting stdout/stderr to terminal and teeing to log."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "wb") as log_file:
            try:
                self.proc = subprocess.Popen(
                    self.command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError as e:
                err = f"Command not found: {self.command[0]}: {e}\n"
                sys.stderr.write(err)
                log_file.write(err.encode("utf-8"))
                return 127
            except PermissionError as e:
                err = f"Permission denied: {self.command[0]}: {e}\n"
                sys.stderr.write(err)
                log_file.write(err.encode("utf-8"))
                return 126
            except Exception as e:
                err = f"Error executing command {self.command}: {e}\n"
                sys.stderr.write(err)
                log_file.write(err.encode("utf-8"))
                return 1

            log_lock = threading.Lock()

            def stream_tee(pipe, dest):
                try:
                    while True:
                        chunk = pipe.read1(4096)
                        if not chunk:
                            break
                        if hasattr(dest, "buffer"):
                            dest.buffer.write(chunk)
                            dest.buffer.flush()
                        else:
                            dest.write(chunk.decode("utf-8", errors="replace"))
                            dest.flush()
                        with log_lock:
                            log_file.write(chunk)
                            log_file.flush()
                except Exception:
                    pass

            t_out = threading.Thread(
                target=stream_tee, args=(self.proc.stdout, sys.stdout)
            )
            t_err = threading.Thread(
                target=stream_tee, args=(self.proc.stderr, sys.stderr)
            )
            t_out.daemon = True
            t_err.daemon = True
            t_out.start()
            t_err.start()

            ret = self.proc.wait()
            t_out.join(timeout=5)
            t_err.join(timeout=5)

            if ret < 0:
                return 128 + abs(ret)
            return ret

    def run(self) -> int:
        """Register, execute command, and write terminal event."""
        if not self.validate_proposal():
            return 1

        self.register()

        old_sigint = signal.signal(signal.SIGINT, self.handle_signal)
        old_sigterm = signal.signal(signal.SIGTERM, self.handle_signal)

        try:
            exit_code = self._execute_process()
            if exit_code == 0:
                self.record_terminal_event("completed", exit_code="0")
            else:
                self.record_terminal_event("failed", exit_code=str(exit_code))
            return exit_code
        except KeyboardInterrupt:
            self.handle_signal(signal.SIGINT, None)
            return 130
        except Exception:
            self.record_terminal_event("failed", exit_code="")
            raise
        finally:
            try:
                signal.signal(signal.SIGINT, old_sigint)
            except (ValueError, TypeError):
                pass
            try:
                signal.signal(signal.SIGTERM, old_sigterm)
            except (ValueError, TypeError):
                pass


def self_test() -> int:
    """Run hermetic self-test suite in an isolated temporary directory."""
    failures = 0

    def test_wrapped_self_test():
        from unittest.mock import patch

        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir)
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [sys.executable, "-c", "import sys; sys.exit(7)", "--self-test"]
            with patch.dict(main.__globals__, {
                "DEFAULT_RESEARCH_DIR": temp_research,
                "self_test": lambda: 99,
            }):
                ec = main(["--proposal", str(prop), "--", *cmd])
            assert ec == 7, f"Expected wrapped command exit code 7, got {ec}"
            r = read_rows(temp_research / "runs.csv")
            assert [row["event_type"] for row in r] == ["registered", "failed"]
            assert r[0]["command"] == shlex.join(cmd)

    def test_missing_proposal():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            missing_path = temp_research / "nonexistent.md"
            runner = RegistrationRunner(
                proposal_path=missing_path,
                command=[sys.executable, "-c", "pass"],
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec != 0, f"Expected non-zero exit code, got {ec}"
            assert not (temp_research / "runs.csv").exists(), (
                "research/runs.csv was created despite missing proposal"
            )

    def test_empty_proposal():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            empty_path = temp_research / "empty.md"
            empty_path.write_bytes(b"")
            runner = RegistrationRunner(
                proposal_path=empty_path,
                command=[sys.executable, "-c", "pass"],
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec != 0, f"Expected non-zero exit code, got {ec}"
            assert not (temp_research / "runs.csv").exists(), (
                "research/runs.csv was created despite empty proposal"
            )

    def test_whitespace_proposal():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            ws_path = temp_research / "ws.md"
            ws_path.write_text("   \n\t  \n  \n")
            runner = RegistrationRunner(
                proposal_path=ws_path,
                command=[sys.executable, "-c", "pass"],
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec != 0, f"Expected non-zero exit code, got {ec}"
            assert not (temp_research / "runs.csv").exists(), (
                "research/runs.csv was created despite whitespace proposal"
            )

    def test_trivial_success():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
            runner = RegistrationRunner(
                proposal_path=prop,
                command=cmd,
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec == 0, f"Expected 0 exit code, got {ec}"
            csv_path = temp_research / "runs.csv"
            assert csv_path.exists(), "runs.csv was not created"
            r = read_rows(csv_path)
            assert len(r) == 2, f"Expected 2 rows, got {len(r)}"
            assert r[0]["event_type"] == "registered"
            assert r[1]["event_type"] == "completed"
            assert r[0]["run_id"] == r[1]["run_id"] == runner.run_id
            assert r[0]["exit_code"] == "", (
                f"registered row exit_code should be empty, got {r[0]['exit_code']}"
            )
            assert r[1]["exit_code"] == "0", (
                f"completed row exit_code should be '0', got {r[1]['exit_code']}"
            )
            assert r[0]["duration_seconds"] == "", (
                f"registered duration should be empty, got {r[0]['duration_seconds']}"
            )
            assert r[1]["duration_seconds"] != "", (
                "completed row must have non-empty duration"
            )
            dur = float(r[1]["duration_seconds"])
            assert dur >= 0.0, f"Duration must be non-negative, got {dur}"
            log_p = Path(r[1]["log_path"])
            assert log_p.exists(), f"Log file {log_p} does not exist"
            _ = log_p.read_bytes()

    def test_trivial_failure():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [sys.executable, "-c", "import sys; sys.exit(7)"]
            runner = RegistrationRunner(
                proposal_path=prop,
                command=cmd,
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec == 7, f"Expected exit code 7, got {ec}"
            csv_path = temp_research / "runs.csv"
            r = read_rows(csv_path)
            assert len(r) == 2
            assert r[0]["event_type"] == "registered"
            assert r[1]["event_type"] == "failed"
            assert r[1]["exit_code"] == "7"
            assert r[1]["run_id"] == runner.run_id
            assert float(r[1]["duration_seconds"]) >= 0.0

    def test_interrupted_run_signal_handler():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [sys.executable, "-c", "pass"]
            runner = RegistrationRunner(
                proposal_path=prop,
                command=cmd,
                research_dir=temp_research,
            )
            runner.register()
            try:
                runner.handle_signal(signal.SIGINT, None)
            except SystemExit as exc:
                assert exc.code == 130, f"Expected 130 on SIGINT, got {exc.code}"

            r = read_rows(temp_research / "runs.csv")
            assert len(r) == 2
            assert r[0]["event_type"] == "registered"
            assert r[1]["event_type"] == "aborted"
            assert r[1]["run_id"] == runner.run_id
            assert r[1]["exit_code"] == "", (
                f"aborted row exit_code must be empty, got {r[1]['exit_code']}"
            )
            assert r[1]["duration_seconds"] != "", "aborted row must record duration"
            assert float(r[1]["duration_seconds"]) >= 0.0

    def test_interrupted_run_keyboard_interrupt():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
            runner = RegistrationRunner(
                proposal_path=prop,
                command=cmd,
                research_dir=temp_research,
            )

            def mock_execute():
                raise KeyboardInterrupt()

            runner._execute_process = mock_execute

            try:
                runner.run()
            except SystemExit as exc:
                assert exc.code == 130, f"Expected 130, got {exc.code}"

            r = read_rows(temp_research / "runs.csv")
            assert len(r) == 2
            assert r[0]["event_type"] == "registered"
            assert r[1]["event_type"] == "aborted"
            assert r[1]["run_id"] == runner.run_id
            assert r[1]["exit_code"] == ""
            assert r[1]["duration_seconds"] != ""

    def test_schema_mismatch():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            csv_path = temp_research / "runs.csv"
            csv_path.write_text("wrong_col_1,wrong_col_2\nval1,val2\n")
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            runner = RegistrationRunner(
                proposal_path=prop,
                command=[sys.executable, "-c", "pass"],
                research_dir=temp_research,
            )
            raised = False
            try:
                runner.run()
            except ValueError as exc:
                raised = True
                assert "header does not match current fields" in str(exc)
            assert raised, "Expected ValueError on header mismatch"

    def test_two_consecutive_runs():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")

            runner1 = RegistrationRunner(
                proposal_path=prop,
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                research_dir=temp_research,
            )
            ec1 = runner1.run()
            assert ec1 == 0
            rows1 = read_rows(temp_research / "runs.csv")
            assert len(rows1) == 2

            runner2 = RegistrationRunner(
                proposal_path=prop,
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                research_dir=temp_research,
            )
            ec2 = runner2.run()
            assert ec2 == 0
            rows2 = read_rows(temp_research / "runs.csv")
            assert len(rows2) == 4
            assert len(rows2) > len(rows1), "Second run must append rows"
            assert runner1.run_id != runner2.run_id, "Different runs must have distinct run_ids"
            assert rows1[0]["run_id"] == runner1.run_id
            assert rows2[2]["run_id"] == runner2.run_id

    def test_output_tee():
        with tempfile.TemporaryDirectory(prefix="runner-test-") as temp_dir:
            temp_research = Path(temp_dir) / "research"
            temp_research.mkdir()
            prop = temp_research / "prop.md"
            prop.write_text("# Proposal\nValid content")
            cmd = [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('OUT_TEE_TEST\\n'); sys.stderr.write('ERR_TEE_TEST\\n')",
            ]
            runner = RegistrationRunner(
                proposal_path=prop,
                command=cmd,
                research_dir=temp_research,
            )
            ec = runner.run()
            assert ec == 0
            log_p = runner.log_path
            assert log_p.exists()
            content = log_p.read_text(encoding="utf-8")
            assert "OUT_TEE_TEST" in content
            assert "ERR_TEE_TEST" in content

    checks = [
        ("wrapped self-test flag runs wrapped command", test_wrapped_self_test),
        ("missing proposal path refuses", test_missing_proposal),
        ("empty proposal file refuses", test_empty_proposal),
        ("whitespace-only proposal file refuses", test_whitespace_proposal),
        ("trivial successful command registers and completes", test_trivial_success),
        ("trivial failing command registers and fails", test_trivial_failure),
        (
            "simulated interrupted run writes aborted event via signal handler",
            test_interrupted_run_signal_handler,
        ),
        (
            "simulated interrupted run writes aborted event via keyboard interrupt",
            test_interrupted_run_keyboard_interrupt,
        ),
        ("schema mismatch fails loudly", test_schema_mismatch),
        (
            "two consecutive runs append strictly more rows with distinct run_ids",
            test_two_consecutive_runs,
        ),
        ("subprocess output teed to log file", test_output_tee),
    ]

    for label, check in checks:
        try:
            check()
            print(f"PASS: {label}")
        except Exception as error:
            failures += 1
            print(f"FAIL: {label}: {error}")

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for registration runner."""
    if argv is None:
        argv = sys.argv[1:]

    if "--" in argv:
        sep_idx = argv.index("--")
        runner_argv = argv[:sep_idx]
        cmd = argv[sep_idx + 1 :]
    else:
        runner_argv = argv
        cmd = []

    if "--self-test" in runner_argv:
        return self_test()

    parser = argparse.ArgumentParser(
        description="ARIA Experiment Registration Runner",
        prog="registration_runner.py",
    )
    parser.add_argument(
        "--proposal",
        type=Path,
        required=True,
        help="Path to experiment proposal markdown file",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run hermetic self-test suite and exit",
    )

    try:
        args = parser.parse_args(runner_argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    if not cmd:
        sys.stderr.write("Error: command required after '--'\n")
        return 2

    runner = RegistrationRunner(proposal_path=args.proposal, command=cmd)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
