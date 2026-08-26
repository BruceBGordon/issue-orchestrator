"""Public CLI contract tests for the host executor deep module."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

import pytest

from issue_orchestrator.execution.host_executor import (
    ExecutorRequestIdentityFactory,
    host_policy,
)
from issue_orchestrator.infra.validation_executor_handshake import (
    validate_executor_handshake_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
AGGRESSIVENESS_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_AGGRESSIVENESS_PERCENT"
ACTIVE_TIMEOUT_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_ACTIVE_TIMEOUT_SECONDS"
ABSOLUTE_TIMEOUT_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_ABSOLUTE_TIMEOUT_SECONDS"
HANDSHAKE_ENV = "ISSUE_ORCHESTRATOR_VALIDATION_EXECUTOR_HANDSHAKE_FD"


def _integer_event_field(line: str, field: str) -> int:
    match = re.search(rf"(?:^| ){re.escape(field)}=(\d+)(?: |$)", line)
    if match is None:
        raise AssertionError(f"event has no integer {field!r} field: {line}")
    return int(match.group(1))


def _run_cli(
    pool_dir: Path,
    *arguments: str,
    environment_aggressiveness: str | None = None,
    deadline_environment: dict[str, str] | None = None,
    inherited_descriptors: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment[POOL_DIR_ENV] = str(pool_dir)
    environment.pop("ISSUE_ORCHESTRATOR_EXECUTOR_GROUP", None)
    environment.pop(AGGRESSIVENESS_ENV, None)
    environment.pop(ACTIVE_TIMEOUT_ENV, None)
    environment.pop(ABSOLUTE_TIMEOUT_ENV, None)
    environment.pop(HANDSHAKE_ENV, None)
    if environment_aggressiveness is not None:
        environment[AGGRESSIVENESS_ENV] = environment_aggressiveness
    if deadline_environment is not None:
        environment.update(deadline_environment)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli",
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        pass_fds=inherited_descriptors,
    )


def test_run_requires_complete_demand_and_fairness_group(tmp_path: Path) -> None:
    missing_demand = _run_cli(
        tmp_path / "pool",
        "executor-run",
        "--work-key",
        "io:unit",
        "--group",
        "validation-1",
        "--",
        "true",
    )
    missing_group = _run_cli(
        tmp_path / "pool",
        "executor-run",
        "--work-key",
        "io:unit",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "4",
        "--",
        "true",
    )

    assert missing_demand.returncode == 2
    assert "--min-concurrency and --max-concurrency" in missing_demand.stdout
    assert missing_group.returncode == 2
    assert "--group or ISSUE_ORCHESTRATOR_EXECUTOR_GROUP is required" in (
        missing_group.stdout
    )


def test_run_rejects_partial_inherited_deadline_contract(tmp_path: Path) -> None:
    result = _run_cli(
        tmp_path / "pool",
        "executor-run",
        "--work-key",
        "io:deadline-contract",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "validation-deadline-contract",
        "--",
        "true",
        deadline_environment={ACTIVE_TIMEOUT_ENV: "30"},
    )

    assert result.returncode == 2
    assert "requires both environment" in result.stdout
    assert "variables:" in result.stdout


def test_run_acknowledges_validation_before_executor_admission(
    tmp_path: Path,
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    try:
        result = _run_cli(
            tmp_path / "pool",
            "executor-run",
            "--work-key",
            "io:validation-handshake",
            "--min-concurrency",
            "1",
            "--max-concurrency",
            "1",
            "--group",
            "validation-handshake",
            "--",
            "true",
            deadline_environment={HANDSHAKE_ENV: str(write_descriptor)},
            inherited_descriptors=(write_descriptor,),
        )
        os.close(write_descriptor)
        write_descriptor = -1

        assert result.returncode == 0
        acknowledgements = validate_executor_handshake_payload(
            os.read(read_descriptor, 64)
        )
        assert len(acknowledgements) == 1
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


def test_path_resolution_preserves_executable_symlink_identity(
    tmp_path: Path,
) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    target = executable_dir / "identity-target"
    target.write_text('#!/bin/sh\nprintf \'%s\\n\' "$0"\n', encoding="utf-8")
    target.chmod(0o755)
    symlink = executable_dir / "identity-link"
    symlink.symlink_to(target.name)
    inherited_path = os.environ["PATH"]

    result = _run_cli(
        tmp_path / "pool",
        "executor-run",
        "--work-key",
        "test:path-symlink",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "path-symlink",
        "--",
        symlink.name,
        deadline_environment={"PATH": f"{executable_dir}:{inherited_path}"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"{symlink}\n"


def test_events_cli_preserves_human_identity_and_scheduler_rationale(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    result = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:typed-smoke",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "3",
        "--group",
        "io-validation-1042",
        "--exclusive",
        "browser",
        "--",
        sys.executable,
        "-c",
        "pass",
    )

    assert result.returncode == 0
    events = _run_cli(pool_dir, "executor-events", "--limit", "10")

    assert events.returncode == 0
    lines = events.stdout.splitlines()
    enqueued = next(line for line in lines if " enqueued " in line)
    admitted = next(line for line in lines if " admitted " in line)
    completed = next(line for line in lines if " completed " in line)
    assert "work=io:typed-smoke" in enqueued
    assert "group=io-validation-1042" in enqueued
    assert "concurrency=1-3" in enqueued
    assert "successful_samples=0" in enqueued
    assert "queue_settle=0.100s" in enqueued
    assert "policy_source=" in enqueued
    assert "host_load_1m=" in enqueued
    assert "host_load_5m=" in enqueued
    assert "host_load_15m=" in enqueued
    assert "exclusive=browser" in enqueued
    admitted_concurrency = _integer_event_field(admitted, "concurrency")
    assert 1 <= admitted_concurrency <= 3
    assert "reserved_for_queued_peers=0" in admitted
    assert "host_cpu_busy=" in admitted
    assert "sample=" in admitted
    assert "host_load_1m=" in admitted
    assert f"exit=0 concurrency={admitted_concurrency}" in completed
    assert "successful_samples=1" in completed
    assert "learned_cores_per_worker=" in completed
    assert "host_load_1m=" in completed


def test_failed_commands_remain_diagnostic_but_do_not_enter_learning_history(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    failed = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:failure-evidence",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "io-validation-failed",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(7)",
    )
    succeeded = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:failure-evidence",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "io-validation-succeeded",
        "--",
        sys.executable,
        "-c",
        "pass",
    )

    assert failed.returncode == 7
    assert succeeded.returncode == 0
    events = _run_cli(pool_dir, "executor-events", "--limit", "10")
    failed_completion = next(
        line
        for line in events.stdout.splitlines()
        if " completed " in line and "exit=7" in line
    )
    succeeding_enqueue = next(
        line
        for line in events.stdout.splitlines()
        if " enqueued " in line and "group=io-validation-succeeded" in line
    )
    succeeding_completion = next(
        line
        for line in events.stdout.splitlines()
        if " completed " in line and "group=io-validation-succeeded" in line
    )
    assert "successful_samples=0" in failed_completion
    assert "successful_samples=0" in succeeding_enqueue
    assert "successful_samples=1" in succeeding_completion


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX signal forwarding")
def test_run_forwards_the_command_termination_signal(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    terminated = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:signal-status",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "io-validation-signal",
        "--",
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
    )

    assert terminated.returncode == -signal.SIGTERM
    events = _run_cli(pool_dir, "executor-events", "--limit", "10")
    completion = next(
        line for line in events.stdout.splitlines() if " completed " in line
    )
    assert "exit=-15" in completion


def test_command_lifecycle_failure_is_durable_and_human_traceable(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    missing_executable = "/definitely/missing/executor-command"
    failed = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:guardian-failure-evidence",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "io-validation-guardian-failed",
        "--",
        missing_executable,
    )

    assert failed.returncode == 2
    events = _run_cli(pool_dir, "executor-events", "--limit", "10")

    assert events.returncode == 0
    failure = next(
        line
        for line in events.stdout.splitlines()
        if " command-lifecycle-failed " in line
    )
    assert "work=io:guardian-failure-evidence" in failure
    assert "group=io-validation-guardian-failed" in failure
    assert "ExecutorGuardianCommandStartError" in failure
    assert missing_executable in failure


def test_status_cli_exposes_policy_and_human_learning_identity(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    completed = _run_cli(
        pool_dir,
        "executor-run",
        "--work-key",
        "io:status-smoke",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--group",
        "io-status-validation",
        "--",
        sys.executable,
        "-c",
        "pass",
    )

    assert completed.returncode == 0
    status = _run_cli(pool_dir, "executor-status")

    assert status.returncode == 0
    assert "Executor host CPU slots:" in status.stdout
    assert "Executor aggressiveness: 100% (default)" in status.stdout
    assert "Successful learning samples: 1" in status.stdout
    assert "Excluded historical failure samples: 0" in status.stdout
    assert "Learning fingerprint:" in status.stdout
    assert "repo=issue-orchestrator" in status.stdout
    assert "work=io:status-smoke" in status.stdout
    assert "estimated_cores_per_worker=" in status.stdout


def test_status_cli_filters_and_pages_profiles_without_dumping_global_history(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    for work_key in ("io:alpha", "io:beta", "io:gamma"):
        completed = _run_cli(
            pool_dir,
            "executor-run",
            "--work-key",
            work_key,
            "--min-concurrency",
            "1",
            "--max-concurrency",
            "1",
            "--group",
            f"validation-{work_key}",
            "--",
            sys.executable,
            "-c",
            "pass",
        )
        assert completed.returncode == 0

    status = _run_cli(
        pool_dir,
        "executor-status",
        "--repository",
        "issue-orchestrator",
        "--offset",
        "1",
        "--limit",
        "1",
    )

    assert status.returncode == 0
    assert "Profile page: offset=1 shown=1 matching=3 total=3" in status.stdout
    assert "work=io:beta" in status.stdout
    assert "work=io:alpha" not in status.stdout
    assert "work=io:gamma" not in status.stdout


def test_status_cli_reports_valid_legacy_failures_without_learning_from_them(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    history_dir = pool_dir / "work-history"
    history_dir.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "repository_key": "repo-key",
        "repository_label": "migrated-repo",
        "work_key": "io:migrated-static",
        "observations": [
            {
                "concurrency": 1,
                "wall_seconds": 2.0,
                "cpu_seconds": 1.0,
                "max_rss_bytes": 1024,
                "input_blocks": 0,
                "output_blocks": 0,
                "exit_code": 7,
                "recorded_at_unix": 1.0,
            },
            {
                "concurrency": 2,
                "wall_seconds": 1.0,
                "cpu_seconds": 1.0,
                "max_rss_bytes": 2048,
                "input_blocks": 0,
                "output_blocks": 0,
                "exit_code": 0,
                "recorded_at_unix": 2.0,
            },
        ],
    }
    (history_dir / "migrated.json").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )

    status = _run_cli(pool_dir, "executor-status")

    assert status.returncode == 0
    assert "Successful learning samples: 1" in status.stdout
    assert "Excluded historical failure samples: 1" in status.stdout
    assert "repo=migrated-repo work=io:migrated-static" in status.stdout
    assert "successful_samples=1" in status.stdout
    assert "excluded_failed_samples=1" in status.stdout


def test_status_cli_fails_fast_on_corrupt_learning_history(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    history_dir = pool_dir / "work-history"
    history_dir.mkdir(parents=True)
    (history_dir / "corrupt.json").write_text("{}\n", encoding="utf-8")

    status = _run_cli(pool_dir, "executor-status")

    assert status.returncode == 2
    assert "invalid executor work history" in status.stdout


def test_events_cli_fails_fast_on_corrupt_event_store(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    (pool_dir / "executor-events-v4.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run_cli(pool_dir, "executor-events")

    assert result.returncode == 2
    assert "invalid executor event" in result.stdout


def test_events_cli_reports_empty_store(tmp_path: Path) -> None:
    result = _run_cli(tmp_path / "pool", "executor-events")

    assert result.returncode == 0
    assert result.stdout == "No executor events recorded.\n"


def test_policy_reports_persisted_value_and_explicit_environment_override(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    configured = _run_cli(
        pool_dir,
        "executor-policy",
        "--aggressiveness",
        "125",
    )
    overridden = _run_cli(
        pool_dir,
        "executor-policy",
        "--aggressiveness",
        "150",
        environment_aggressiveness="175",
    )
    persisted = _run_cli(pool_dir, "executor-policy")

    assert configured.returncode == 0
    assert "Executor aggressiveness: 125% (persisted)" in configured.stdout
    assert overridden.returncode == 0
    assert "saved as 150%; effective value is 175% from environment" in (
        overridden.stdout
    )
    assert persisted.returncode == 0
    assert "Executor aggressiveness: 150% (persisted)" in persisted.stdout


def test_host_cpu_slots_are_detected_internally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_policy.os, "cpu_count", lambda: 18)

    assert host_policy.detected_executor_cpu_count() == 18


def test_request_identity_factory_uses_monotonic_queue_order_during_rollback() -> None:
    monotonic_values = iter((100, 101))
    wall_values = iter((500, 400))
    nonces = iter(("a" * 32, "b" * 32))
    factory = ExecutorRequestIdentityFactory(
        wall_time_nanoseconds=lambda: next(wall_values),
        monotonic_nanoseconds=lambda: next(monotonic_values),
        process_id=lambda: 42,
        request_nonce=lambda: next(nonces),
    )

    first = factory.create()
    second = factory.create()

    assert first.queue_sequence < second.queue_sequence
    assert first.request_id != second.request_id
    assert first.request_id.value.startswith("00000000000000000500-42-")
    assert second.request_id.value.startswith("00000000000000000400-42-")


def test_unrelated_cli_help_does_not_require_posix_executor_modules() -> None:
    script = """
import builtins
import sys

real_import = builtins.__import__

def import_without_posix_executor(name, globals=None, locals=None, fromlist=(), level=0):
    if name in {"fcntl", "resource"}:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = import_without_posix_executor
from issue_orchestrator.entrypoints.cli import main
sys.argv = ["issue-orchestrator", "--help"]
raise SystemExit(main())
"""

    completed = subprocess.run(
        (sys.executable, "-c", script),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def test_bootstrap_import_is_safe_and_executor_failure_is_explicit_without_posix() -> (
    None
):
    import_blocker = """
import builtins

real_import = builtins.__import__

def import_without_posix_executor(name, globals=None, locals=None, fromlist=(), level=0):
    if name in {"fcntl", "resource"}:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = import_without_posix_executor
"""
    ordinary_bootstrap = subprocess.run(
        (
            sys.executable,
            "-c",
            import_blocker
            + """
from issue_orchestrator.entrypoints.bootstrap_executor import build_agent_phase_command_scheduler
build_agent_phase_command_scheduler()
print("ordinary bootstrap composition succeeded")
""",
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    explicit_executor_failure = subprocess.run(
        (
            sys.executable,
            "-c",
            import_blocker
            + """
from issue_orchestrator.entrypoints.bootstrap_executor import build_executor_monitor
print("bootstrap_executor import succeeded")
try:
    build_executor_monitor()
except RuntimeError as exc:
    print(exc)
else:
    raise AssertionError("pooled executor unexpectedly composed without POSIX support")
""",
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert ordinary_bootstrap.returncode == 0, ordinary_bootstrap.stderr
    assert "ordinary bootstrap composition succeeded" in ordinary_bootstrap.stdout
    assert explicit_executor_failure.returncode == 0, explicit_executor_failure.stderr
    assert "bootstrap_executor import succeeded" in explicit_executor_failure.stdout
    assert "requires POSIX fcntl and resource support" in (
        explicit_executor_failure.stdout
    )
