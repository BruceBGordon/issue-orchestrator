from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
DIRECT_EXECUTOR = REPO_ROOT / "scripts" / "executor-run-direct"


def _run_direct(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("ISSUE_ORCHESTRATOR_EXECUTOR_GROUP", None)
    return subprocess.run(
        [str(DIRECT_EXECUTOR), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_direct_executor_runs_command_and_sets_xdist_worker_limit() -> None:
    command = (
        "import json, os, sys; "
        "print(json.dumps([os.environ['PYTEST_XDIST_AUTO_NUM_WORKERS'], sys.argv[1:]]))"
    )

    result = _run_direct(
        "--min-concurrency",
        "3",
        "--max-concurrency",
        "3",
        "--work-key",
        "porchpin:tests",
        "--group",
        "porchpin-validation-17",
        "--",
        sys.executable,
        "-c",
        command,
        "argument with spaces",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["3", ["argument with spaces"]]
    assert result.stderr == ""


def test_direct_executor_uses_largest_accepted_concurrency() -> None:
    result = _run_direct(
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "12",
        "--work-key",
        "io:web",
        "--group",
        "io-validation",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['PYTEST_XDIST_AUTO_NUM_WORKERS'])",
    )

    assert result.returncode == 0
    assert result.stdout == "12\n"
    assert result.stderr == ""


def test_direct_executor_rejects_exclusive_resource_it_cannot_honor() -> None:
    result = _run_direct(
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--work-key",
        "io:claude",
        "--group",
        "io-validation",
        "--exclusive",
        "claude",
        "--",
        "true",
    )

    assert result.returncode == 2
    assert result.stderr == (
        "executor-run-direct cannot honor exclusive resources; "
        "use the pooled executor\n"
    )


def test_direct_executor_preserves_command_exit_status() -> None:
    result = _run_direct(
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--work-key",
        "porchpin:tests",
        "--group",
        "porchpin-validation-17",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(17)",
    )

    assert result.returncode == 17


def test_direct_executor_rejects_invalid_contract() -> None:
    for args in (
        (),
        (
            "--min-concurrency",
            "0",
            "--max-concurrency",
            "1",
            "--work-key",
            "work",
            "--group",
            "run",
            "--",
            "true",
        ),
        (
            "--min-concurrency",
            "many",
            "--max-concurrency",
            "1",
            "--work-key",
            "work",
            "--group",
            "run",
            "--",
            "true",
        ),
        ("--min-concurrency", "1", "--max-concurrency", "1", "--"),
        (
            "--min-concurrency",
            "1",
            "--max-concurrency",
            "1",
            "--work-key",
            "work",
            "--group",
            "run",
            "true",
        ),
        (
            "--min-concurrency",
            "1",
            "--max-concurrency",
            "2",
            "--work-key",
            "work",
            "--",
            "true",
        ),
    ):
        result = _run_direct(*args)

        assert result.returncode == 2
        assert result.stderr.startswith("usage: executor-run-direct ")
