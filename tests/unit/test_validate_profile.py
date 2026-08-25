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

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorPolicySource,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorEventMetadata,
    ExecutorEventTimeline,
    ExecutorPolicyChanged,
    ExecutorRecentEventsQuery,
    ExecutorStatus,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SCRIPT = REPO_ROOT / "repo-specific/scripts/validate_profile.py"


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
    assert report["schema_version"] == 4
    assert len(report["config"]["profiled_commit_sha"]) == 40
    assert report["config"]["aggressiveness"] == {
        "percent": 125,
        "selection_source": "command-line",
    }
    assert report["config"]["host"]["cpu_count"] >= 1
    assert cold_aggregate["executor_before"]["policy_source"] == "environment"
    assert cold_aggregate["executor_before"]["successful_observation_count"] == 0
    assert learned_aggregate["executor_after"]["successful_observation_count"] == 0
    assert cold_aggregate["executor_before"]["learning_fingerprint_sha256"] == (
        learned_aggregate["executor_after"]["learning_fingerprint_sha256"]
    )
    assert report["config"]["external_caches"] == "preserved"
    artifact_directory = Path(report["config"]["artifact_directory"])
    assert artifact_directory.is_dir()
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
        assert aggregate["executor_events"] == {
            "query_limit": 1000,
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


def test_discovery_profiles_static_once_as_an_aggregate_execution_lane() -> None:
    profile = _load_profile_module()

    targets = profile.discover_validate_targets(REPO_ROOT, _gnu_make())

    assert "_validate-static-lane" in targets
    assert "_validate-static-impl" not in targets
    assert "typecheck" not in targets
    assert "lint-arch" not in targets
    assert "quality-guardrails" not in targets
    assert len(targets) == len(set(targets))


def test_summary_counts_each_execution_lane_exactly_once() -> None:
    profile = _load_profile_module()
    target_results = (
        profile.CommandResult(
            "static", ("make", "static"), 30.0, 0, None, "/tmp/static.log"
        ),
        profile.CommandResult(
            "unit", ("make", "unit"), 45.0, 0, None, "/tmp/unit.log"
        ),
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


def test_aggregate_event_capture_is_typed_bounded_and_restores_environment(
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
    returned_timeline = ExecutorEventTimeline((old_event, *(new_event,) * 999))

    class StaticMonitor:
        def recent_events(
            self,
            query: ExecutorRecentEventsQuery,
        ) -> ExecutorEventTimeline:
            assert query.limit == 1000
            return returned_timeline

        def status(self) -> ExecutorStatus:
            raise AssertionError("status is outside this event-capture test")

    monkeypatch.setattr(profile, "build_executor_monitor", StaticMonitor)
    monkeypatch.setenv(profile.EXECUTOR_POOL_DIR_ENV, "original-pool")
    monkeypatch.setenv(profile.EXECUTOR_AGGRESSIVENESS_ENV, "75")

    capture = profile.capture_executor_events(
        tmp_path / "profile-pool",
        profile.ProfileAggressiveness(125, "command-line"),
        recorded_since_unix=recorded_since,
    )

    assert capture.query_limit == 1000
    assert capture.possibly_truncated is True
    assert tuple(record.event for record in capture.events) == (new_event,) * 999
    assert all(
        record.event_type is profile.ProfileExecutorEventType.POLICY_CHANGED
        for record in capture.events
    )
    assert os.environ[profile.EXECUTOR_POOL_DIR_ENV] == "original-pool"
    assert os.environ[profile.EXECUTOR_AGGRESSIVENESS_ENV] == "75"
    serialized = json.dumps(asdict(capture))
    assert '"event_type": "policy-changed"' in serialized
    assert '"effective_source": "environment"' in serialized
