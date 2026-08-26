"""Behavior tests for the isolated validation profiler."""

from __future__ import annotations

from dataclasses import asdict
import json
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from types import ModuleType
from pathlib import Path
from typing import Protocol, cast

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
    assert report["schema_version"] == 7
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
    assert report["schema_version"] == 7
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
    assert report["schema_version"] == 7
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

    def interrupt_after_add(**_arguments: object) -> object:
        raise KeyboardInterrupt("injected after Git registration")

    monkeypatch.setattr(profile, "prepare_worktree", interrupt_after_add)

    with pytest.raises(profile.IsolatedProfileWorktreeError) as raised:
        profile.run_in_isolated_worktree(
            repo_root=repository.resolve(),
            make_bin=_gnu_make(),
            name="target:smoke",
            make_target="smoke",
            dry_run=False,
            jobs=None,
            executor_pool_dir=(tmp_path / "executor-pool").resolve(),
            executor_aggressiveness_percent=125,
            artifacts=artifacts,
            profiled_commit_sha=profile.resolve_profiled_commit(repository),
            fairness_group=ExecutorFairnessGroup("profile:test:unexpected"),
        )

    failure = raised.value
    assert type(failure.primary_error) is KeyboardInterrupt
    assert failure.cleanup_failures == ()
    assert not failure.worktree.exists()
    registration = profile.GitProfileWorktreeRegistrationObserver().observe(
        repository.resolve(),
        failure.worktree,
    )
    assert registration is profile.ProfileWorktreeRegistration.ABSENT


def test_add_result_publication_failure_queries_registration_before_cleanup(
    tmp_path: Path,
) -> None:
    profile = _load_profile_module()
    repository = _profile_repository(tmp_path).resolve()
    artifacts = profile.ProfileArtifactStore(tmp_path / "indeterminate-artifacts")
    artifacts.initialize()
    owner = profile.IsolatedProfileWorktree.create(
        repo_root=repository,
        operation_name="target:smoke",
        profiled_commit_sha=profile.resolve_profiled_commit(repository),
        dry_run=False,
        artifacts=artifacts,
        directory_remover=profile.ShutilProfileDirectoryRemover(),
        registration_observer=profile.GitProfileWorktreeRegistrationObserver(),
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
        profile.GitProfileWorktreeRegistrationObserver().observe(
            repository,
            worktree,
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
    )

    artifact = profile.measure_profile(request)
    output_path = tmp_path / "unexpected-report.json"
    profile.write_profile_artifact(output_path, artifact)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 7
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
    )

    artifact = profile.measure_profile(request)
    output_path = tmp_path / f"partial-{failure_boundary}.json"
    profile.write_profile_artifact(output_path, artifact)

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
    assert report["failure"]["cleanup_failures"] == partial["isolated_run"][
        "cleanup_failures"
    ]


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
    )
    monkeypatch.setattr(profile, "parse_args", lambda: arguments)
    monkeypatch.setattr(profile, "resolve_profiled_commit", lambda _root: "0" * 40)
    monkeypatch.setattr(profile, "source_worktree_is_dirty", lambda _root: False)
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
    assert report["schema_version"] == 7
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
            7,
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
        7,
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
    )

    assert exit_code == 7
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["failure"]["stage"] == "cold-aggregate"
    assert report["failure"]["command_result"]["exit_code"] == 7
    [cleanup_failure] = report["failure"]["cleanup_failures"]
    assert cleanup_failure["operation"] == "profile-session-root-remove"


def test_discovery_profiles_static_once_as_an_aggregate_execution_lane() -> None:
    profile = _load_profile_module()

    targets = profile.discover_validate_targets(REPO_ROOT, _gnu_make())

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
    pinned_sha = profile.resolve_profiled_commit(repository)
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

    artifacts = profile.ProfileArtifactStore(tmp_path / "discovery-artifacts")
    artifacts.initialize()
    targets = profile.discover_validate_targets_at_commit(
        repository,
        _gnu_make(),
        pinned_sha,
        artifacts,
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


def test_profile_serializes_command_finalization_failure_without_losing_result(
) -> None:
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
            executor_process_lifetime_children_max_rss_bytes=4_000_000_000,
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
