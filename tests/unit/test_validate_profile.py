"""Behavior tests for the isolated validation profiler."""

from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import sys
from types import ModuleType
from pathlib import Path

import pytest


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
    assert report["schema_version"] == 2
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
        profile.CommandResult("static", ("make", "static"), 30.0, 0, None),
        profile.CommandResult("unit", ("make", "unit"), 45.0, 0, None),
        profile.CommandResult("codex", ("make", "codex"), 70.0, 0, None),
    )
    aggregate = profile.CommandResult(
        "aggregate",
        ("make", "validate-pr-raw"),
        82.0,
        0,
        None,
    )

    summary = profile.summarize(
        target_results=target_results,
        cold_validate_pr_raw_result=profile.CommandResult(
            "cold",
            ("make", "validate-pr-raw"),
            96.0,
            0,
            None,
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
