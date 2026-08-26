"""Behavior tests for the isolated validation profiler."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import time
from io import TextIOBase
from types import ModuleType, TracebackType
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorFairnessGroup,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorCommandFinalizationFailed,
    ExecutorEventPage,
    ExecutorEventMetadata,
    ExecutorFairnessGroupEventsQuery,
    ExecutorFinalizationFailureDetail,
    ExecutorMonitoredWork,
    ExecutorPolicyChanged,
    ExecutorRepositoryReference,
    ExecutorRequestId,
    ExecutorResourceUsage,
    ExecutorStatus,
    ExecutorStatusQuery,
)
from issue_orchestrator.execution.posix_file_lock import (
    PosixFileLockAcquired,
    PosixFileLockContended,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SCRIPT = REPO_ROOT / "repo-specific/scripts/validate_profile.py"


class _AggregateArtifactView(Protocol):
    executor_before: object
    executor_after: object
    executor_events: object


class _CompleteProfileArtifactView(Protocol):
    config: object
    cold_validate_pr_raw_run: _AggregateArtifactView


class _RegisterThenRaiseWorktreeCommandRunner:
    """Publish real Git registration, then fail before returning its result."""

    def __init__(self, profile: ModuleType) -> None:
        self._profile = profile

    def add(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        profiled_commit_sha: str,
        operation_name: str,
        dry_run: bool,
        artifacts: object,
    ) -> object:
        del operation_name, dry_run, artifacts
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                profiled_commit_sha,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        raise KeyboardInterrupt("injected before add result publication")

    def remove(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        operation_name: str,
        dry_run: bool,
        artifacts: object,
    ) -> object:
        del dry_run, artifacts
        command = (
            "git",
            "-C",
            str(repo_root),
            "worktree",
            "remove",
            "--force",
            str(worktree),
        )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        return self._profile.CommandResult(
            f"{operation_name}:worktree-remove",
            command,
            0.0,
            completed.returncode,
            None,
            str(worktree.parent / "remove.log"),
        )


def _load_profile_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "validate_profile_under_test",
        PROFILE_SCRIPT,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load validation profiler module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _gnu_make() -> str:
    make_bin = shutil.which("gmake") or shutil.which("make")
    if make_bin is None:
        pytest.fail("GNU make is required to validate the profiler")
    version = subprocess.run(
        (make_bin, "--version"),
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0 or "GNU Make" not in version.stdout:
        pytest.fail("GNU make is required to validate the profiler")
    return make_bin


def _profile_command_owner(profile: ModuleType) -> object:
    return profile.build_profile_command_owner(30)


def _profile_artifact_publisher(profile: ModuleType) -> object:
    return profile.build_profile_artifact_publisher()


class _FaultingProfileCommandLogAppender:
    def __init__(
        self,
        handle: TextIOBase,
        fault_operation: str,
        attempts: list[str],
    ) -> None:
        self._handle = handle
        self._fault_operation = fault_operation
        self._attempts = attempts

    def write(self, text: str) -> None:
        self._attempts.append("write")
        if self._fault_operation == "write":
            raise OSError("injected footer write failure")
        self._handle.write(text)

    def flush(self) -> None:
        self._attempts.append("flush")
        if self._fault_operation == "flush":
            raise OSError("injected footer flush failure")
        self._handle.flush()

    def sync(self) -> None:
        self._attempts.append("sync")
        if self._fault_operation == "sync":
            raise OSError("injected footer sync failure")
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._attempts.append("close")
        self._handle.close()
        if self._fault_operation == "close":
            raise OSError("injected log close failure")


class _FaultingProfileCommandLogAppenderFactory:
    def __init__(self, fault_operation: str, attempts: list[str]) -> None:
        self._fault_operation = fault_operation
        self._attempts = attempts

    def open(self, path: Path) -> _FaultingProfileCommandLogAppender:
        return _FaultingProfileCommandLogAppender(
            path.open("a", encoding="utf-8"),
            self._fault_operation,
            self._attempts,
        )


class _RecordingProfileArtifactPublisher:
    def __init__(self, profile: ModuleType, events: list[str]) -> None:
        self._publisher = profile.build_profile_artifact_publisher()
        self._events = events

    def publish(self, output_path: Path, artifact: object) -> None:
        self._events.append("publish")
        self._publisher.publish(output_path, artifact)


class _RecordingProfileDirectoryRemover:
    def __init__(self, expected_root: Path, events: list[str]) -> None:
        self._expected_root = expected_root
        self._events = events

    def remove(self, directory: Path) -> None:
        assert directory == self._expected_root
        assert directory.exists()
        self._events.append("remove")
        shutil.rmtree(directory)


class _FailingProfileArtifactPublisher:
    def publish(self, _output_path: Path, _artifact: object) -> None:
        raise OSError("injected report publication failure")


class _ForbiddenProfileDirectoryRemover:
    def remove(self, _directory: Path) -> None:
        raise AssertionError("session cleanup must not precede durable evidence")


def _run_git(repository: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _profile_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "profile-repository"
    repository.mkdir()
    (repository / "Makefile").write_text(
        """\
VALIDATE_PR_LANES := smoke

worktree-setup:
\t@:

smoke:
\t@:

test-vscode:
\t@:

validate-pr-raw:
\t@test "$(VALIDATE_LANE_JOBS)" = "11"
\t@test -n "$$ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
\t@echo '[validate-timing] START target=fixture at=2026-08-25T00:00:00Z'
\t@echo '[validate-timing] END target=fixture status=0 elapsed=1s at=2026-08-25T00:00:01Z'
""",
        encoding="utf-8",
    )
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.name", "Profiler Test")
    _run_git(repository, "config", "user.email", "profiler@example.invalid")
    _run_git(repository, "add", "Makefile")
    _run_git(repository, "commit", "-q", "-m", "profile fixture")
    return repository


def _failure_profile_repository(tmp_path: Path, failure_stage: str) -> Path:
    repository = tmp_path / f"failure-profile-{failure_stage}"
    repository.mkdir()
    if failure_stage == "cold-aggregate":
        aggregate_recipe = "\t@exit 7"
        smoke_recipe = "\t@:"
    elif failure_stage == "target":
        aggregate_recipe = "\t@:"
        smoke_recipe = "\t@exit 8"
    elif failure_stage == "learned-aggregate":
        aggregate_recipe = """\
\t@mkdir -p "$$ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
\t@if test -f "$$ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR/aggregate-seen"; then \\
\t\texit 9; \\
\telse \\
\t\ttouch "$$ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR/aggregate-seen"; \\
\tfi"""
        smoke_recipe = "\t@:"
    else:
        raise AssertionError(f"unsupported fixture failure stage: {failure_stage}")
    (repository / "Makefile").write_text(
        f"""\
VALIDATE_PR_LANES := smoke

worktree-setup:
\t@:

smoke:
{smoke_recipe}

test-vscode:
\t@:

validate-pr-raw:
{aggregate_recipe}
""",
        encoding="utf-8",
    )
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.name", "Profiler Failure Test")
    _run_git(repository, "config", "user.email", "profiler@example.invalid")
    _run_git(repository, "add", "Makefile")
    _run_git(repository, "commit", "-q", "-m", "failure profile fixture")
    return repository


def test_profile_jobs_control_outer_make_and_inner_lane_limit(
    tmp_path: Path,
) -> None:
    repository = _profile_repository(tmp_path)
    output_path = tmp_path / "profile.json"

    completed = subprocess.run(
        (
            sys.executable,
            str(PROFILE_SCRIPT),
            "--repo-root",
            str(repository),
            "--make-bin",
            _gnu_make(),
            "--targets",
            "smoke",
            "--jobs",
            "11",
            "--aggressiveness",
            "125",
            "--output",
            str(output_path),
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    cold_aggregate = report["cold_validate_pr_raw_run"]
    learned_aggregate = report["learned_validate_pr_raw_run"]
    assert cold_aggregate["command_result"]["command"] == [
        _gnu_make(),
        "-j11",
        "--output-sync=target",
        "VALIDATE_LANE_JOBS=11",
        "validate-pr-raw",
    ]
    assert report["config"]["aggregate_target"] == "validate-pr-raw"
    assert report["config"]["executor_learning"] == (
        "one fresh pool: cold aggregate, lane training, learned aggregate"
    )
    assert report["schema_version"] == 8
    assert report["outcome"] == "complete"
    assert len(report["config"]["profiled_commit_sha"]) == 40
    profiled_commit_sha = report["config"]["profiled_commit_sha"]
    assert report["config"]["aggressiveness"] == {
        "percent": 125,
        "selection_source": "command-line",
    }
    assert report["config"]["host"]["cpu_count"] >= 1
    assert cold_aggregate["executor_before"]["policy_source"] == "environment"
    assert cold_aggregate["executor_before"]["successful_observation_count"] == 0
    assert learned_aggregate["executor_after"]["successful_observation_count"] == 0
    assert (
        cold_aggregate["executor_before"]["learning_fingerprint_sha256"]
        == (learned_aggregate["executor_after"]["learning_fingerprint_sha256"])
    )
    assert report["config"]["external_caches"] == "preserved"
    assert report["config"]["command_timeout_seconds"] == 3600
    artifact_directory = Path(report["config"]["artifact_directory"])
    assert artifact_directory.is_dir()
    worktree_add_log = (
        artifact_directory / "cold-aggregate-validate-pr-raw-worktree-add.log"
    ).read_text(encoding="utf-8")
    worktree_add_argv = json.loads(
        next(
            line.removeprefix("[profile-command] argv=")
            for line in worktree_add_log.splitlines()
            if line.startswith("[profile-command] argv=")
        )
    )
    assert worktree_add_argv[-1] == profiled_commit_sha
    assert "HEAD" not in worktree_add_argv
    for result in (
        cold_aggregate["command_result"],
        learned_aggregate["command_result"],
    ):
        output_log = Path(result["output_log_path"])
        assert output_log.parent == artifact_directory
        assert output_log.is_file()
        output = output_log.read_text(encoding="utf-8")
        assert "[validate-timing] END target=fixture status=0 elapsed=1s" in output
        assert "env.ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR=" in output
        assert "[profile-command] exit=0" in output
    for aggregate in (cold_aggregate, learned_aggregate):
        assert aggregate["cleanup_failures"] == []
        assert aggregate["executor_events"] == {
            "query_limit": 1000,
            "total_matching_event_count": 0,
            "possibly_truncated": False,
            "events": [],
        }
    assert not Path(cold_aggregate["command_result"]["worktree_path"]).exists()
    assert not Path(learned_aggregate["command_result"]["worktree_path"]).exists()


def test_profile_rejects_noncanonical_job_count(tmp_path: Path) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            str(PROFILE_SCRIPT),
            "--dry-run",
            "--jobs",
            "011",
            "--targets",
            "smoke",
            "--output",
            str(tmp_path / "unused.json"),
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "positive base-ten integer without padding" in completed.stderr
    assert not (tmp_path / "unused.json").exists()


@pytest.mark.parametrize(
    ("failure_stage", "expected_exit"),
    [
        ("cold-aggregate", 2),
        ("target", 2),
        ("learned-aggregate", 2),
    ],
)
def test_profile_stops_at_first_failure_and_writes_typed_partial_report(
    tmp_path: Path,
    failure_stage: str,
    expected_exit: int,
) -> None:
    repository = _failure_profile_repository(tmp_path, failure_stage)
    output_path = tmp_path / f"{failure_stage}.json"

    completed = subprocess.run(
        (
            sys.executable,
            str(PROFILE_SCRIPT),
            "--repo-root",
            str(repository),
            "--make-bin",
            _gnu_make(),
            "--targets",
            "smoke",
            "--jobs",
            "2",
            "--aggressiveness",
            "125",
            "--output",
            str(output_path),
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == expected_exit, completed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 8
    assert report["outcome"] == "failed"
    assert report["failure"]["stage"] == failure_stage
    assert report["failure"]["command_result"]["exit_code"] == expected_exit
    assert report["failure"]["cleanup_failures"] == []
    assert "summary" not in report
    artifact_directory = Path(report["config"]["artifact_directory"])
    if failure_stage == "cold-aggregate":
        assert not (artifact_directory / "target-smoke.log").exists()
    elif failure_stage == "target":
        assert report["completed_target_runs"] == []
        assert not (
            artifact_directory / "learned-aggregate-validate-pr-raw.log"
        ).exists()
    else:
        assert len(report["target_runs"]) == 2
        assert report["failed_aggregate"]["command_result"]["exit_code"] == 2


def _git_wrapper_failing_worktree_removal(
    tmp_path: Path,
    *,
    removal_number: int,
) -> Path:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.fail("git is required to validate profiler cleanup")
    wrapper_directory = tmp_path / f"git-wrapper-{removal_number}"
    wrapper_directory.mkdir()
    counter_path = wrapper_directory / "remove-count"
    wrapper_path = wrapper_directory / "git"
    wrapper_path.write_text(
        f"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
counter_path = Path({str(counter_path)!r})
is_worktree_remove = len(arguments) >= 4 and arguments[2:4] == ["worktree", "remove"]
if is_worktree_remove:
    previous = int(counter_path.read_text()) if counter_path.exists() else 0
    current = previous + 1
    counter_path.write_text(str(current))
    if current == {removal_number}:
        raise SystemExit(23)
os.execv({real_git!r}, [{real_git!r}, *arguments])
""",
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)
    return wrapper_directory


@pytest.mark.parametrize(
    ("removal_number", "expected_stage"),
    ((1, "cold-aggregate"), (2, "target")),
)
def test_profile_retains_worktree_cleanup_failure_in_typed_partial_report(
    tmp_path: Path,
    removal_number: int,
    expected_stage: str,
) -> None:
    repository = _profile_repository(tmp_path)
    output_path = tmp_path / f"cleanup-{expected_stage}.json"
    wrapper_directory = _git_wrapper_failing_worktree_removal(
        tmp_path,
        removal_number=removal_number,
    )
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_directory) + os.pathsep + environment["PATH"]

    completed = subprocess.run(
        (
            sys.executable,
            str(PROFILE_SCRIPT),
            "--repo-root",
            str(repository),
            "--make-bin",
            _gnu_make(),
            "--targets",
            "smoke",
            "--jobs",
            "11",
            "--aggressiveness",
            "125",
            "--output",
            str(output_path),
        ),
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 23, completed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 8
    assert report["outcome"] == "failed"
    assert report["failure"]["stage"] == expected_stage
    assert report["failure"]["command_result"]["exit_code"] == 0
    [cleanup_failure] = report["failure"]["cleanup_failures"]
    assert cleanup_failure["operation"] == "worktree-remove"
    assert cleanup_failure["command_result"]["exit_code"] == 23
    if expected_stage == "cold-aggregate":
        assert report["failed_aggregate"]["cleanup_failures"] == [cleanup_failure]
        assert not (
            Path(report["config"]["artifact_directory"]) / "target-smoke.log"
        ).exists()
    else:
        assert report["cold_validate_pr_raw_run"]["cleanup_failures"] == []
        assert report["completed_target_runs"] == []


def test_unexpected_post_add_failure_unregisters_and_removes_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    repository = _profile_repository(tmp_path)
    artifacts = profile.ProfileArtifactStore(tmp_path / "unexpected-artifacts")
    artifacts.initialize()
    command_owner = _profile_command_owner(profile)

    def interrupt_after_add(**_arguments: object) -> object:
        raise KeyboardInterrupt("injected after Git registration")

    monkeypatch.setattr(profile, "prepare_worktree", interrupt_after_add)

    with pytest.raises(profile.IsolatedProfileWorktreeError) as raised:
        profile.run_in_isolated_worktree(
            command_owner=command_owner,
            repo_root=repository.resolve(),
            make_bin=_gnu_make(),
            name="target:smoke",
            make_target="smoke",
            dry_run=False,
            jobs=None,
            executor_pool_dir=(tmp_path / "executor-pool").resolve(),
            executor_aggressiveness_percent=125,
            artifacts=artifacts,
            profiled_commit_sha=profile.resolve_profiled_commit(
                repository,
                command_owner,
                artifacts,
            ),
            fairness_group=ExecutorFairnessGroup("profile:test:unexpected"),
        )

    failure = raised.value
    assert type(failure.primary_error) is KeyboardInterrupt
    assert failure.cleanup_failures == ()
    assert not failure.worktree.exists()
    registration = profile.GitProfileWorktreeRegistrationObserver(
        command_owner,
        artifacts,
    ).observe(
        repository.resolve(),
        failure.worktree,
        "target:smoke:postcondition",
    )
    assert registration is profile.ProfileWorktreeRegistration.ABSENT


def test_add_result_publication_failure_queries_registration_before_cleanup(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    repository = _profile_repository(tmp_path).resolve()
    artifacts = profile.ProfileArtifactStore(tmp_path / "indeterminate-artifacts")
    artifacts.initialize()
    command_owner = _profile_command_owner(profile)
    owner = profile.IsolatedProfileWorktree.create(
        repo_root=repository,
        operation_name="target:smoke",
        profiled_commit_sha=profile.resolve_profiled_commit(
            repository,
            command_owner,
            artifacts,
        ),
        dry_run=False,
        artifacts=artifacts,
        directory_remover=profile.ShutilProfileDirectoryRemover(),
        registration_observer=profile.GitProfileWorktreeRegistrationObserver(
            command_owner,
            artifacts,
        ),
        command_runner=_RegisterThenRaiseWorktreeCommandRunner(profile),
        temporary_prefix="io-profile-indeterminate-test-",
    )

    with pytest.raises(
        KeyboardInterrupt,
        match="before add result publication",
    ):
        owner.add()

    worktree = owner.worktree
    assert owner.close() == ()
    assert owner.close() == ()
    assert not worktree.exists()
    assert (
        profile.GitProfileWorktreeRegistrationObserver(
            command_owner,
            artifacts,
        ).observe(
            repository,
            worktree,
            "target:smoke:postcondition",
        )
        is profile.ProfileWorktreeRegistration.ABSENT
    )


def test_unexpected_stage_failure_writes_typed_partial_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    artifacts = profile.ProfileArtifactStore(tmp_path / "partial-artifacts")
    artifacts.initialize()
    complete = _complete_profile_artifact(profile, tmp_path)
    worktree = (tmp_path / "unexpected-worktree").resolve()
    cleanup_failure = profile.ProfileCleanupFilesystemFailure(
        profile.ProfileCleanupOperation.TEMPORARY_ROOT_REMOVE,
        "PermissionError",
        "temporary root is busy",
    )

    def fail_cold_aggregate(**_arguments: object) -> object:
        raise profile.IsolatedProfileWorktreeError(
            "cold-aggregate:validate-pr-raw",
            worktree,
            KeyboardInterrupt("injected profiler interruption"),
            (cleanup_failure,),
        )

    monkeypatch.setattr(profile, "run_profile_aggregate", fail_cold_aggregate)
    request = profile.ProfileMeasurementRequest(
        repo_root=tmp_path.resolve(),
        make_bin="make",
        jobs=18,
        dry_run=False,
        targets=("unit",),
        executor_pool_dir=(tmp_path / "executor-pool").resolve(),
        aggressiveness=profile.ProfileAggressiveness(125, "command-line"),
        artifacts=artifacts,
        profiled_commit_sha="0" * 40,
        configuration=complete.config,
        command_owner=_profile_command_owner(profile),
    )

    artifact = profile.measure_profile(request)
    output_path = tmp_path / "unexpected-report.json"
    profile.write_profile_artifact(
        output_path,
        artifact,
        _profile_artifact_publisher(profile),
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 8
    assert report["outcome"] == "failed"
    assert report["completed_aggregate_runs"] == []
    assert report["incomplete_aggregate_runs"] == []
    assert report["completed_target_runs"] == []
    assert report["failure"] == {
        "stage": "cold-aggregate",
        "operation_name": "cold-aggregate:validate-pr-raw",
        "error_type": "KeyboardInterrupt",
        "error_message": "injected profiler interruption",
        "cleanup_failures": [
            {
                "operation": "temporary-root-remove",
                "error_type": "PermissionError",
                "error_message": "temporary root is busy",
            }
        ],
    }


def test_partial_report_serializes_exact_command_lifecycle_evidence(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    execution = profile.ValidationCommandExecution(
        profile.ValidationCommandExitUnknown(4312),
        profile.ValidationCommandTimedOutCleanupFailed(
            profile.ValidationCommandTimeoutPhase.OUTER,
            RuntimeError("process-tree close failed exactly"),
        ),
        profile.ValidationCommandOutput(
            "retained stdout exactly\n",
            "retained stderr exactly\n",
        ),
    )
    lifecycle_error = profile.ProfileCommandLifecycleError(
        "target:unit",
        execution,
        91.25,
        profile.ValidationExecutionDeadline.for_active_timeout(30),
    )
    worktree_cleanup = profile.ProfileCleanupFilesystemFailure(
        profile.ProfileCleanupOperation.TEMPORARY_ROOT_REMOVE,
        "PermissionError",
        "temporary root remained",
    )
    failure = profile.profile_unexpected_failure(
        stage=profile.ProfileStage.TARGET,
        operation_name="target:unit",
        error=profile.IsolatedProfileWorktreeError(
            "target:unit",
            (tmp_path / "worktree").resolve(),
            lifecycle_error,
            (worktree_cleanup,),
        ),
    )
    complete = _complete_profile_artifact(profile, tmp_path)
    artifact = profile.UnexpectedProfileFailureReport(
        8,
        "failed",
        complete.config,
        (),
        (),
        (),
        failure,
    )
    output_path = (tmp_path / "lifecycle-report.json").resolve()

    profile.write_profile_artifact(
        output_path,
        artifact,
        _profile_artifact_publisher(profile),
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["failure"] == {
        "stage": "target",
        "operation_name": "target:unit",
        "command_name": "target:unit",
        "wall_seconds": 91.25,
        "deadline": {
            "active_timeout_seconds": 30.0,
            "absolute_timeout_seconds": 60.0,
            "outer_timeout_seconds": 90,
        },
        "child": {"state": "exit-unknown", "process_id": 4312},
        "cleanup": {
            "state": "timed-out-cleanup-failed",
            "phase": "outer",
            "error_type": "RuntimeError",
            "error_message": "process-tree close failed exactly",
        },
        "output": {
            "stdout": "retained stdout exactly\n",
            "stderr": "retained stderr exactly\n",
        },
        "cleanup_failures": [
            {
                "operation": "temporary-root-remove",
                "error_type": "PermissionError",
                "error_message": "temporary root remained",
            }
        ],
    }


@pytest.mark.parametrize(
    ("failure_boundary", "expected_progress", "retains_after_status"),
    (
        ("status", "command-completed", False),
        ("events", "after-status-captured", True),
    ),
)
def test_post_command_observation_failure_retains_partial_aggregate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
    expected_progress: str,
    retains_after_status: bool,
) -> None:
    profile = _load_profile_module()
    artifacts = profile.ProfileArtifactStore(tmp_path / "partial-aggregate-artifacts")
    artifacts.initialize()
    complete = _complete_profile_artifact(profile, tmp_path)
    executor_status = profile.ProfileExecutorStatus(
        18,
        125,
        "environment",
        "a" * 64,
        3,
        (),
    )
    command_result = profile.CommandResult(
        "cold-aggregate:validate-pr-raw",
        ("make", "validate-pr-raw"),
        84.5,
        0,
        str(tmp_path / "isolated-worktree"),
        str(tmp_path / "aggregate.log"),
    )
    cleanup_failure = profile.ProfileCleanupFilesystemFailure(
        profile.ProfileCleanupOperation.TEMPORARY_ROOT_REMOVE,
        "PermissionError",
        "aggregate temporary root remained",
    )
    isolated_run = profile.IsolatedWorktreeRun(
        command_result,
        (cleanup_failure,),
    )
    status_capture_count = 0

    def capture_status(
        _executor_pool_dir: Path,
        _aggressiveness: object,
    ) -> object:
        nonlocal status_capture_count
        status_capture_count += 1
        if failure_boundary == "status" and status_capture_count == 2:
            raise OSError("injected post-command status failure")
        return executor_status

    def capture_events(
        _executor_pool_dir: Path,
        _aggressiveness: object,
        *,
        fairness_group: object,
    ) -> object:
        del fairness_group
        if failure_boundary != "events":
            raise AssertionError("event capture must not follow status failure")
        raise OSError("injected post-command event failure")

    monkeypatch.setattr(
        profile,
        "run_in_isolated_worktree",
        lambda **_arguments: isolated_run,
    )
    monkeypatch.setattr(profile, "capture_executor_status", capture_status)
    monkeypatch.setattr(profile, "capture_executor_events", capture_events)
    request = profile.ProfileMeasurementRequest(
        repo_root=tmp_path.resolve(),
        make_bin="make",
        jobs=18,
        dry_run=False,
        targets=("unit",),
        executor_pool_dir=(tmp_path / "executor-pool").resolve(),
        aggressiveness=profile.ProfileAggressiveness(125, "command-line"),
        artifacts=artifacts,
        profiled_commit_sha="0" * 40,
        configuration=complete.config,
        command_owner=_profile_command_owner(profile),
    )

    artifact = profile.measure_profile(request)
    output_path = tmp_path / f"partial-{failure_boundary}.json"
    profile.write_profile_artifact(
        output_path,
        artifact,
        _profile_artifact_publisher(profile),
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["completed_aggregate_runs"] == []
    [partial] = report["incomplete_aggregate_runs"]
    assert partial["progress"] == expected_progress
    assert partial["executor_before"]["successful_observation_count"] == 3
    assert partial["isolated_run"]["command_result"]["wall_seconds"] == 84.5
    assert partial["isolated_run"]["cleanup_failures"] == [
        {
            "operation": "temporary-root-remove",
            "error_type": "PermissionError",
            "error_message": "aggregate temporary root remained",
        }
    ]
    assert ("executor_after" in partial) is retains_after_status
    assert report["failure"]["operation_name"].endswith(
        "executor-after-status" if failure_boundary == "status" else "executor-events"
    )
    assert report["failure"]["error_type"] == "OSError"
    assert (
        report["failure"]["cleanup_failures"]
        == partial["isolated_run"]["cleanup_failures"]
    )


def test_main_writes_typed_report_when_target_discovery_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    output_path = tmp_path / "discovery-failure.json"
    worktree = (tmp_path / "discovery-worktree").resolve()
    cleanup_failure = profile.ProfileCleanupFilesystemFailure(
        profile.ProfileCleanupOperation.WORKTREE_REMOVE,
        "RuntimeError",
        "Git cleanup failed",
    )
    arguments = profile.ProfileArguments(
        "make",
        18,
        output_path,
        False,
        None,
        tmp_path.resolve(),
        125,
        30,
    )
    monkeypatch.setattr(profile, "parse_args", lambda: arguments)
    monkeypatch.setattr(
        profile,
        "resolve_profiled_commit",
        lambda _root, _command_owner, _artifacts: "0" * 40,
    )
    monkeypatch.setattr(
        profile,
        "source_worktree_is_dirty",
        lambda _root, _command_owner, _artifacts: False,
    )
    monkeypatch.setattr(
        profile,
        "profile_host",
        lambda: profile.ProfileHost("host", "Darwin", "1", "arm64", 18, 64),
    )
    monkeypatch.setattr(
        profile,
        "resolve_aggressiveness",
        lambda _arguments: profile.ProfileAggressiveness(125, "command-line"),
    )

    def fail_discovery(*_arguments: object) -> object:
        raise profile.IsolatedProfileWorktreeError(
            "target-discovery",
            worktree,
            RuntimeError("injected discovery failure"),
            (cleanup_failure,),
        )

    monkeypatch.setattr(profile, "discover_validate_targets_at_commit", fail_discovery)

    assert profile.main() == 1

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 8
    assert report["outcome"] == "failed"
    assert report["initialization"]["repo_root"] == str(tmp_path.resolve())
    assert report["failure"] == {
        "stage": "target-discovery",
        "operation_name": "target-discovery",
        "error_type": "RuntimeError",
        "error_message": "injected discovery failure",
        "cleanup_failures": [
            {
                "operation": "worktree-remove",
                "error_type": "RuntimeError",
                "error_message": "Git cleanup failed",
            }
        ],
    }


def _complete_profile_artifact(
    profile: ModuleType,
    tmp_path: Path,
) -> _CompleteProfileArtifactView:
    command_result = profile.CommandResult(
        "aggregate",
        ("make", "validate-pr-raw"),
        85.0,
        0,
        None,
        str(tmp_path / "aggregate.log"),
    )
    executor_status = profile.ProfileExecutorStatus(
        18,
        125,
        "environment",
        "0" * 64,
        0,
        (),
    )
    aggregate = profile.ProfileAggregateRun(
        command_result,
        executor_status,
        executor_status,
        profile.ProfileExecutorEventCapture(1000, 0, False, ()),
        (),
    )
    configuration = profile.ValidateProfileConfiguration(
        "make",
        str(tmp_path),
        18,
        False,
        ("unit",),
        "validate-pr-raw",
        profile.PROFILE_METHOD,
        "0" * 40,
        False,
        profile.ProfileHost("host", "Darwin", "1", "arm64", 18, 64),
        profile.ProfileAggressiveness(125, "command-line"),
        "test learning",
        "preserved",
        str(tmp_path / "artifacts"),
        30,
    )
    summary = profile.ValidateProfileSummary(
        "2026-08-25T00:00:00+00:00",
        18,
        90.0,
        85.0,
        -5.0,
        85.0,
        85.0,
        0.0,
        (command_result,),
    )
    return cast(
        _CompleteProfileArtifactView,
        profile.ValidateProfileReport(
            8,
            "complete",
            configuration,
            aggregate,
            (command_result,),
            aggregate,
            summary,
        ),
    )


class _FailingProfileDirectoryRemover:
    def __init__(self, expected_directory: Path) -> None:
        self._expected_directory = expected_directory

    def remove(self, directory: Path) -> None:
        assert directory == self._expected_directory
        raise PermissionError("profile root is busy")


def test_complete_profile_retains_measurements_when_session_cleanup_fails(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    profile_root = tmp_path / "complete-profile-root"
    profile_root.mkdir()
    output_path = tmp_path / "complete-cleanup-failure.json"

    exit_code = profile.finalize_profile_session(
        output_path=output_path,
        profile_root=profile_root,
        artifact=_complete_profile_artifact(profile, tmp_path),
        directory_remover=_FailingProfileDirectoryRemover(profile_root),
        publisher=_profile_artifact_publisher(profile),
    )

    assert exit_code == 1
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["outcome"] == "failed"
    assert report["summary"]["learned_validate_pr_raw_seconds"] == 85.0
    assert report["failure"]["stage"] == "profile-session-cleanup"
    [cleanup_failure] = report["failure"]["cleanup_failures"]
    assert cleanup_failure == {
        "operation": "profile-session-root-remove",
        "error_type": "PermissionError",
        "error_message": "profile root is busy",
    }


def test_stage_failure_retains_primary_and_session_cleanup_failures(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    profile_root = tmp_path / "failed-profile-root"
    profile_root.mkdir()
    output_path = tmp_path / "stage-and-cleanup-failure.json"
    complete = _complete_profile_artifact(profile, tmp_path)
    failed_command = profile.CommandResult(
        "cold-aggregate",
        ("make", "validate-pr-raw"),
        2.0,
        7,
        None,
        str(tmp_path / "cold.log"),
    )
    failed_aggregate = profile.ProfileAggregateRun(
        failed_command,
        complete.cold_validate_pr_raw_run.executor_before,
        complete.cold_validate_pr_raw_run.executor_after,
        complete.cold_validate_pr_raw_run.executor_events,
        (),
    )
    artifact = profile.ColdAggregateFailureReport(
        8,
        "failed",
        complete.config,
        failed_aggregate,
        profile.ProfileFailure(
            profile.ProfileStage.COLD_AGGREGATE,
            failed_command,
            (),
        ),
    )

    exit_code = profile.finalize_profile_session(
        output_path=output_path,
        profile_root=profile_root,
        artifact=artifact,
        directory_remover=_FailingProfileDirectoryRemover(profile_root),
        publisher=_profile_artifact_publisher(profile),
    )

    assert exit_code == 7
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["failure"]["stage"] == "cold-aggregate"
    assert report["failure"]["command_result"]["exit_code"] == 7
    [cleanup_failure] = report["failure"]["cleanup_failures"]
    assert cleanup_failure["operation"] == "profile-session-root-remove"


@pytest.mark.parametrize(
    ("fault_operation", "expected_operation"),
    (
        ("write", "footer-write"),
        ("close", "close"),
    ),
)
def test_real_nonzero_command_retains_result_when_log_finalization_fails(
    tmp_path: Path,
    fault_operation: str,
    expected_operation: str,
) -> None:
    profile = _load_profile_module()
    attempts: list[str] = []
    owner = profile.ContainedProfileCommandOwner(
        profile.build_validation_command_runner(),
        profile.ValidationExecutionDeadline.for_active_timeout(10),
        profile.DurableProfileCommandLogFinalizer(
            _FaultingProfileCommandLogAppenderFactory(
                fault_operation,
                attempts,
            )
        ),
    )
    output_log = (tmp_path / f"{fault_operation}.log").resolve()

    with pytest.raises(profile.ProfileCommandFinalizationError) as raised:
        owner.execute(
            profile.ProfileCommandRequest(
                invocation=profile.ProfileCommandInvocation(
                    name=f"fault:{fault_operation}",
                    command=(sys.executable, "-c", "raise SystemExit(17)"),
                    dry_run=False,
                    working_directory=tmp_path.resolve(),
                    worktree=profile.ProfileCommandOutsideWorktree(),
                    environment=os.environ.copy(),
                ),
                output_log_path=output_log,
                runner_stderr_path=(
                    tmp_path / f"{fault_operation}.runner-stderr.log"
                ).resolve(),
            )
        )

    assert raised.value.command_result.exit_code == 17
    assert raised.value.command_result.wall_seconds > 0
    assert raised.value.command_result.output_log_path == str(output_log)
    assert [failure.operation.value for failure in raised.value.failures] == [
        expected_operation
    ]
    assert attempts == ["write", "flush", "sync", "close"]
    assert "[profile-command] name=" in output_log.read_text(encoding="utf-8")
    failure = profile.profile_unexpected_failure(
        stage=profile.ProfileStage.TARGET,
        operation_name=f"fault:{fault_operation}",
        error=raised.value,
    )
    serialized_failure = asdict(failure)
    assert serialized_failure["command_result"]["exit_code"] == 17
    assert serialized_failure["command_result"]["wall_seconds"] > 0
    assert [
        item["operation"] for item in serialized_failure["finalization_failures"]
    ] == [expected_operation]


def test_profile_command_owner_contains_descendants_before_return(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    owner = profile.build_profile_command_owner(10)
    child_pid_path = (tmp_path / "background-child.pid").resolve()
    background_program = shlex.join(
        (sys.executable, "-c", "import time; time.sleep(30)")
    )
    shell_program = (
        f"{background_program} & echo $! > {shlex.quote(str(child_pid_path))}; exit 0"
    )

    execution = owner.execute(
        profile.ProfileCommandRequest(
            invocation=profile.ProfileCommandInvocation(
                name="descendant-containment",
                command=("/bin/sh", "-c", shell_program),
                dry_run=False,
                working_directory=tmp_path.resolve(),
                worktree=profile.ProfileCommandOutsideWorktree(),
                environment=os.environ.copy(),
            ),
            output_log_path=(tmp_path / "descendant.log").resolve(),
            runner_stderr_path=(tmp_path / "descendant.runner-stderr.log").resolve(),
        )
    )

    assert execution.result.exit_code == 0
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    process_state = subprocess.run(
        ("/bin/ps", "-o", "state=", "-p", str(child_pid)),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    ).stdout.strip()
    assert not process_state or process_state.startswith("Z")


def test_default_output_identity_is_traceable_and_collision_resistant(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    expected_identity = profile.ProfileOutputIdentity(
        datetime(2026, 8, 26, 14, 3, 9, 123456, tzinfo=UTC),
        4312,
        UUID("12345678-1234-5678-9234-567812345678"),
    )
    first_system_path = profile.default_output_path(
        tmp_path.resolve(),
        profile.SystemProfileOutputIdentityFactory(),
    )
    second_system_path = profile.default_output_path(
        tmp_path.resolve(),
        profile.SystemProfileOutputIdentityFactory(),
    )

    assert expected_identity.filename_component == (
        "20260826T140309.123456Z-pid-4312-run-12345678-1234-5678-9234-567812345678"
    )
    assert first_system_path != second_system_path
    assert f"-pid-{os.getpid()}-run-" in first_system_path.name


def test_publication_lock_excludes_another_process_without_timing(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    output_path = (tmp_path / "locked-report.json").resolve()
    ready_reader, ready_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_reader)
        os.close(release_writer)
        try:
            publication_lock = profile.PosixProfileArtifactPublicationLock(
                profile.PosixFileLockOwner()
            )
            with publication_lock.hold(output_path):
                os.write(ready_writer, b"L")
                if os.read(release_reader, 1) != b"R":
                    raise RuntimeError("parent did not release the lock holder")
        except BaseException:
            os.write(ready_writer, b"E")
            os._exit(1)
        os._exit(0)

    os.close(ready_writer)
    os.close(release_reader)
    try:
        assert os.read(ready_reader, 1) == b"L"
        specification = profile.PosixFileLockSpecification(
            path=profile.PosixProfileArtifactPublicationLock.lock_path(output_path),
            mode=profile.PosixFileLockMode.EXCLUSIVE,
            acquisition=profile.PosixFileLockAcquisition.NON_BLOCKING,
            file_presence=profile.PosixFileLockFilePresence.CREATE_IF_MISSING,
        )
        contended = profile.PosixFileLockOwner().acquire(specification)
        assert type(contended) is PosixFileLockContended
        contended.lease.release()
    finally:
        os.write(release_writer, b"R")
        os.close(release_writer)
        os.close(ready_reader)
        waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    acquired = profile.PosixFileLockOwner().acquire(specification)
    assert type(acquired) is PosixFileLockAcquired
    acquired.lease.release()


@pytest.mark.parametrize(
    "failure_boundary",
    ("write", "fsync", "replace", "directory-fsync"),
)
def test_atomic_report_failure_preserves_older_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    profile = _load_profile_module()
    output_path = (tmp_path / "profile.json").resolve()
    older_report = '{"generation":"older"}\n'
    output_path.write_text(older_report, encoding="utf-8")
    publisher = profile.build_profile_artifact_publisher()

    if failure_boundary == "write":
        monkeypatch.setattr(
            profile.os,
            "write",
            lambda _descriptor, _payload: (_ for _ in ()).throw(
                OSError("injected report write failure")
            ),
        )
    elif failure_boundary == "fsync":
        monkeypatch.setattr(
            profile.os,
            "fsync",
            lambda _descriptor: (_ for _ in ()).throw(
                OSError("injected report fsync failure")
            ),
        )
    elif failure_boundary == "replace":
        monkeypatch.setattr(
            profile.os,
            "replace",
            lambda _source, _target: (_ for _ in ()).throw(
                OSError("injected report replace failure")
            ),
        )
    elif failure_boundary == "directory-fsync":
        real_fsync = profile.os.fsync
        fsync_count = 0

        def fail_new_generation_directory_sync(descriptor: int) -> None:
            nonlocal fsync_count
            fsync_count += 1
            if fsync_count == 3:
                raise OSError("injected report directory-fsync failure")
            real_fsync(descriptor)

        monkeypatch.setattr(
            profile.os,
            "fsync",
            fail_new_generation_directory_sync,
        )
    else:
        raise AssertionError("atomic report failure boundary is a closed set")

    with pytest.raises(OSError, match=f"report {failure_boundary} failure"):
        publisher.publish(
            output_path,
            _complete_profile_artifact(profile, tmp_path),
        )

    assert output_path.read_text(encoding="utf-8") == older_report
    assert tuple(tmp_path.glob(".profile.json.*.tmp")) == ()


def test_report_rollback_remains_inside_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    output_path = (tmp_path / "profile.json").resolve()
    older_report = '{"generation":"older"}\n'
    output_path.write_text(older_report, encoding="utf-8")

    class ObservedPublicationLock:
        def __init__(self) -> None:
            self.held = False
            self.events: list[str] = []

        def hold(self, locked_output_path: Path) -> ObservedPublicationLock:
            assert locked_output_path == output_path
            return self

        def __enter__(self) -> None:
            assert not self.held
            self.held = True
            self.events.append("acquired")

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            del exception_type, exception, traceback
            assert self.held
            self.events.append("released")
            self.held = False

    publication_lock = ObservedPublicationLock()
    publisher = profile.PosixAtomicProfileArtifactPublisher(publication_lock)
    real_replace = profile.os.replace
    replace_count = 0

    def observed_replace(source: Path, target: Path) -> None:
        nonlocal replace_count
        assert publication_lock.held
        replace_count += 1
        real_replace(source, target)

    sync_count = 0

    def real_directory_sync(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def fail_new_generation_sync(directory: Path) -> None:
        nonlocal sync_count
        assert publication_lock.held
        sync_count += 1
        if sync_count == 2:
            raise OSError("injected new-generation sync failure")
        real_directory_sync(directory)

    monkeypatch.setattr(profile.os, "replace", observed_replace)
    monkeypatch.setattr(
        profile.PosixAtomicProfileArtifactPublisher,
        "_sync_directory",
        staticmethod(fail_new_generation_sync),
    )

    with pytest.raises(OSError, match="new-generation sync failure"):
        publisher.publish(output_path, _complete_profile_artifact(profile, tmp_path))

    assert publication_lock.events == ["acquired", "released"]
    assert replace_count == 3
    assert sync_count == 3
    assert output_path.read_text(encoding="utf-8") == older_report


def test_profile_report_is_durable_before_session_root_removal(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    profile_root = tmp_path / "profile-root"
    profile_root.mkdir()
    output_path = (tmp_path / "profile.json").resolve()
    events: list[str] = []

    result = profile.finalize_profile_session(
        output_path=output_path,
        profile_root=profile_root,
        artifact=_complete_profile_artifact(profile, tmp_path),
        directory_remover=_RecordingProfileDirectoryRemover(
            profile_root,
            events,
        ),
        publisher=_RecordingProfileArtifactPublisher(profile, events),
    )

    assert result == 0
    assert events == ["publish", "remove"]
    assert output_path.is_file()
    assert not profile_root.exists()


def test_report_publication_failure_retains_disposable_session_root(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    profile_root = tmp_path / "profile-root"
    profile_root.mkdir()

    with pytest.raises(OSError, match="report publication failure"):
        profile.finalize_profile_session(
            output_path=(tmp_path / "profile.json").resolve(),
            profile_root=profile_root,
            artifact=_complete_profile_artifact(profile, tmp_path),
            directory_remover=_ForbiddenProfileDirectoryRemover(),
            publisher=_FailingProfileArtifactPublisher(),
        )

    assert profile_root.is_dir()


def test_cleanup_amendment_failure_preserves_initial_report_and_typed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    profile_root = tmp_path / "profile-root"
    profile_root.mkdir()
    output_path = (tmp_path / "profile.json").resolve()
    artifact = _complete_profile_artifact(profile, tmp_path)
    publication_failure_armed = False

    class ArmingFailingDirectoryRemover:
        def remove(self, directory: Path) -> None:
            nonlocal publication_failure_armed
            assert directory == profile_root
            publication_failure_armed = True
            raise PermissionError("profile root remains busy")

    real_write = profile.os.write

    def fail_amendment_write(descriptor: int, payload: bytes) -> int:
        if publication_failure_armed:
            raise OSError("injected cleanup-amendment publication failure")
        return real_write(descriptor, payload)

    monkeypatch.setattr(profile.os, "write", fail_amendment_write)

    with pytest.raises(
        profile.ProfileSessionCleanupAmendmentPublicationError
    ) as raised:
        profile.finalize_profile_session(
            output_path=output_path,
            profile_root=profile_root,
            artifact=artifact,
            directory_remover=ArmingFailingDirectoryRemover(),
            publisher=_profile_artifact_publisher(profile),
        )

    failure = raised.value
    assert failure.initial_publication.output_path == output_path
    assert failure.initial_publication.artifact is artifact
    assert type(failure.cleanup_amendment) is profile.ProfileSessionCleanupFailureReport
    assert (
        failure.cleanup_amendment.failure.cleanup_failures == failure.cleanup_failures
    )
    [cleanup_failure] = failure.cleanup_failures
    assert (
        cleanup_failure.operation
        is profile.ProfileCleanupOperation.PROFILE_SESSION_ROOT_REMOVE
    )
    assert cleanup_failure.error_type == "PermissionError"
    assert cleanup_failure.error_message == "profile root remains busy"
    assert type(failure.publication_error) is OSError
    initial_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert initial_report["outcome"] == "complete"
    assert "failure" not in initial_report


def test_main_writes_initialization_failure_after_artifact_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    output_path = (tmp_path / "initialization-failure.json").resolve()
    arguments = profile.ProfileArguments(
        "make",
        18,
        output_path,
        False,
        ("smoke",),
        tmp_path.resolve(),
        125,
        30,
    )
    monkeypatch.setattr(profile, "parse_args", lambda: arguments)
    monkeypatch.setattr(
        profile,
        "build_profile_command_owner",
        lambda _timeout: (_ for _ in ()).throw(
            RuntimeError("injected command-owner composition failure")
        ),
    )

    assert profile.main() == 1

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["outcome"] == "failed"
    assert report["failure"]["stage"] == "initialization"
    assert report["failure"]["error_type"] == "RuntimeError"
    artifact_directory = Path(report["startup"]["artifact_directory"])
    assert artifact_directory.is_dir()


def test_main_writes_initialization_failure_when_artifact_directory_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    output_path = (tmp_path / "artifact-initialization-failure.json").resolve()
    artifact_directory = output_path.parent / f"{output_path.stem}-artifacts"
    artifact_directory.mkdir()
    arguments = profile.ProfileArguments(
        "make",
        18,
        output_path,
        False,
        ("smoke",),
        tmp_path.resolve(),
        125,
        30,
    )
    monkeypatch.setattr(profile, "parse_args", lambda: arguments)
    monkeypatch.setattr(
        profile,
        "build_profile_command_owner",
        lambda _timeout: (_ for _ in ()).throw(
            AssertionError("artifact initialization must fail first")
        ),
    )

    assert profile.main() == 1

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["outcome"] == "failed"
    assert report["startup"]["artifact_directory"] == str(artifact_directory)
    assert report["failure"]["stage"] == "initialization"
    assert report["failure"]["operation_name"] == "artifact-directory-initialize"
    assert report["failure"]["error_type"] == "FileExistsError"


def test_discovery_profiles_static_once_as_an_aggregate_execution_lane(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    artifacts = profile.ProfileArtifactStore(tmp_path / "test-discovery-artifacts")
    artifacts.initialize()
    command_owner = _profile_command_owner(profile)

    targets = profile.discover_validate_targets(
        REPO_ROOT,
        _gnu_make(),
        command_owner,
        artifacts,
    )

    assert "_validate-static-lane" in targets
    assert "_validate-static-impl" not in targets
    assert "typecheck" not in targets
    assert "lint-arch" not in targets
    assert "quality-guardrails" not in targets
    assert len(targets) == len(set(targets))


def test_discovery_is_pinned_when_head_moves_and_source_tree_is_dirty(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    repository = _profile_repository(tmp_path)
    artifacts = profile.ProfileArtifactStore(tmp_path / "pinned-discovery-artifacts")
    artifacts.initialize()
    command_owner = _profile_command_owner(profile)
    pinned_sha = profile.resolve_profiled_commit(
        repository,
        command_owner,
        artifacts,
    )
    makefile = repository / "Makefile"
    makefile.write_text(
        makefile.read_text(encoding="utf-8").replace(
            "VALIDATE_PR_LANES := smoke",
            "VALIDATE_PR_LANES := moved-head",
        ),
        encoding="utf-8",
    )
    _run_git(repository, "add", "Makefile")
    _run_git(repository, "commit", "-q", "-m", "move head")
    makefile.write_text(
        makefile.read_text(encoding="utf-8").replace(
            "VALIDATE_PR_LANES := moved-head",
            "VALIDATE_PR_LANES := dirty-source",
        ),
        encoding="utf-8",
    )

    targets = profile.discover_validate_targets_at_commit(
        repository,
        _gnu_make(),
        pinned_sha,
        artifacts,
        command_owner,
    )

    assert targets == ("_validate-static-lane", "smoke", "test-vscode")


def test_summary_counts_each_execution_lane_exactly_once() -> None:
    profile = _load_profile_module()
    target_results = (
        profile.CommandResult(
            "static", ("make", "static"), 30.0, 0, None, "/tmp/static.log"
        ),
        profile.CommandResult("unit", ("make", "unit"), 45.0, 0, None, "/tmp/unit.log"),
        profile.CommandResult(
            "codex", ("make", "codex"), 70.0, 0, None, "/tmp/codex.log"
        ),
    )
    aggregate = profile.CommandResult(
        "aggregate",
        ("make", "validate-pr-raw"),
        82.0,
        0,
        None,
        "/tmp/aggregate.log",
    )

    summary = profile.summarize(
        target_results=target_results,
        cold_validate_pr_raw_result=profile.CommandResult(
            "cold",
            ("make", "validate-pr-raw"),
            96.0,
            0,
            None,
            "/tmp/cold.log",
        ),
        learned_validate_pr_raw_result=aggregate,
        jobs=7,
    )

    assert summary.cold_validate_pr_raw_seconds == 96.0
    assert summary.learned_validate_pr_raw_seconds == 82.0
    assert summary.learned_minus_cold_seconds == -14.0
    assert summary.fresh_worktree_target_sum_seconds == 145.0
    assert summary.fresh_worktree_slowest_target_seconds == 70.0
    assert summary.validate_pr_raw_minus_slowest_target_seconds == 12.0
    assert tuple(result.name for result in summary.top_targets) == (
        "codex",
        "unit",
        "static",
    )


def test_aggregate_event_capture_uses_exact_group_and_survives_clock_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _load_profile_module()
    recorded_since = time.time()
    old_event = ExecutorPolicyChanged(
        ExecutorEventMetadata(recorded_since - 1.0, 100),
        ExecutorAggressiveness(100),
        ExecutorAggressiveness(125),
        ExecutorPolicySource.ENVIRONMENT,
    )
    new_event = ExecutorPolicyChanged(
        ExecutorEventMetadata(recorded_since + 1.0, 101),
        ExecutorAggressiveness(125),
        ExecutorAggressiveness(125),
        ExecutorPolicySource.ENVIRONMENT,
    )
    returned_page = ExecutorEventPage(
        total_matching_event_count=1001,
        events=(old_event, *(new_event,) * 999),
    )
    fairness_group = ExecutorFairnessGroup("profile:test:aggregate")

    class StaticMonitor:
        def events_for_group(
            self,
            query: ExecutorFairnessGroupEventsQuery,
        ) -> ExecutorEventPage:
            assert query.limit == 1000
            assert query.fairness_group == fairness_group
            return returned_page

        def status(self, query: ExecutorStatusQuery) -> ExecutorStatus:
            raise AssertionError("status is outside this event-capture test")

    monkeypatch.setattr(profile, "build_executor_monitor", StaticMonitor)
    monkeypatch.setenv(profile.EXECUTOR_POOL_DIR_ENV, "original-pool")
    monkeypatch.setenv(profile.EXECUTOR_AGGRESSIVENESS_ENV, "75")

    capture = profile.capture_executor_events(
        tmp_path / "profile-pool",
        profile.ProfileAggressiveness(125, "command-line"),
        fairness_group=fairness_group,
    )

    assert capture.query_limit == 1000
    assert capture.possibly_truncated is True
    assert capture.total_matching_event_count == 1001
    assert tuple(record.event for record in capture.events) == (
        old_event,
        *(new_event,) * 999,
    )
    assert all(
        record.event_type is profile.ProfileExecutorEventType.POLICY_CHANGED
        for record in capture.events
    )
    assert os.environ[profile.EXECUTOR_POOL_DIR_ENV] == "original-pool"
    assert os.environ[profile.EXECUTOR_AGGRESSIVENESS_ENV] == "75"
    serialized = json.dumps(asdict(capture))
    assert '"event_type": "policy-changed"' in serialized
    assert '"effective_source": "environment"' in serialized


def test_profile_serializes_command_finalization_failure_without_losing_result() -> (
    None
):
    profile = _load_profile_module()
    event = ExecutorCommandFinalizationFailed(
        metadata=ExecutorEventMetadata(1_800_000_000.0, 4321),
        work=ExecutorMonitoredWork(
            ExecutorRequestId("profile-finalization-failure"),
            ExecutorRepositoryReference("/repo/.git", "io"),
            ExecutorWorkKey("io:unit"),
            ExecutorFairnessGroup("profile:aggregate"),
        ),
        concurrency=8,
        charged_cpu_slots=4,
        exit_code=0,
        resources=ExecutorResourceUsage(
            wall_seconds=85.0,
            cpu_seconds=240.0,
            guardian_process_lifetime_children_max_rss_bytes=4_000_000_000,
            input_blocks=12,
            output_blocks=34,
        ),
        failures=(
            ExecutorFinalizationFailureDetail(
                "record successful command history",
                "OSError",
                "disk unavailable",
            ),
        ),
    )

    record = profile.ProfileExecutorEventRecord(event)
    serialized = json.dumps(asdict(record), sort_keys=True)

    assert (
        record.event_type
        is profile.ProfileExecutorEventType.COMMAND_FINALIZATION_FAILED
    )
    assert '"event_type": "command-finalization-failed"' in serialized
    assert '"exit_code": 0' in serialized
    assert '"wall_seconds": 85.0' in serialized
    assert '"attempt_name": "record successful command history"' in serialized
