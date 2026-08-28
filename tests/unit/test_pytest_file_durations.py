"""The capture half of the loop: what a slice run teaches the store.

The subprocess tests drive a real pytest against a throwaway
repository, because the load-bearing risk here is not the arithmetic —
it is whether the hooks fire where we think they do, including under
xdist, where the controller sees its workers' reports and each worker
sees only its own shard.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.adapters.json_file_duration_history import (
    FileDurationHistoryError,
)
from issue_orchestrator.infra.file_duration_store import STORE_DIRNAME
from issue_orchestrator.infra.pytest_file_durations import (
    FileDurationRecorder,
    whole_file_selections,
)

PLUGIN = "issue_orchestrator.infra.pytest_file_durations"


# --- stubs at the pytest boundary ---------------------------------------------


@dataclass
class StubInvocationParams:
    dir: Path


@dataclass
class StubConfig:
    args: list[str]
    rootpath: Path
    invocation_params: StubInvocationParams


@dataclass
class StubReport:
    nodeid: str
    duration: float


@dataclass
class StubSession:
    exitstatus: int = 0


class RecordingHistory:
    def __init__(self) -> None:
        self.recorded: list[dict[str, float]] = []

    def record_success(self, durations: dict[str, float]) -> None:
        self.recorded.append(dict(durations))

    def pinned_weights(self, epoch: str) -> dict[str, float]:
        del epoch
        return {}


class BrokenHistory:
    def record_success(self, durations: dict[str, float]) -> None:
        del durations
        raise FileDurationHistoryError("store is corrupt (delete the file)")

    def pinned_weights(self, epoch: str) -> dict[str, float]:
        del epoch
        return {}


def config_for(tmp_path: Path, *args: str) -> StubConfig:
    return StubConfig(
        args=list(args),
        rootpath=tmp_path,
        invocation_params=StubInvocationParams(dir=tmp_path),
    )


# --- which selections are allowed to teach ------------------------------------


def test_whole_file_arguments_are_the_ones_that_teach(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "test_b.py").write_text("", encoding="utf-8")
    config = config_for(tmp_path, "test_a.py", "test_b.py")
    assert whole_file_selections(config) == frozenset({"test_a.py", "test_b.py"})


def test_a_node_level_selection_teaches_nothing(tmp_path: Path) -> None:
    """A slice that runs a third of a fat file must not record that
    third as the file's weight: the next run would judge it thin, run
    it whole, judge it fat again, and oscillate forever."""
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    config = config_for(tmp_path, "test_a.py::test_one", "test_a.py::test_two")
    assert whole_file_selections(config) == frozenset()


def test_a_file_named_both_ways_teaches_nothing(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("", encoding="utf-8")
    config = config_for(tmp_path, "test_a.py", "test_a.py::test_one")
    assert whole_file_selections(config) == frozenset()


def test_directories_and_missing_paths_teach_nothing(tmp_path: Path) -> None:
    (tmp_path / "suite").mkdir()
    config = config_for(tmp_path, "suite", "test_gone.py")
    assert whole_file_selections(config) == frozenset()


def test_selections_outside_the_rootdir_teach_nothing(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("", encoding="utf-8")
    config = config_for(tmp_path / "root", str(outside))
    assert whole_file_selections(config) == frozenset()


# --- what the recorder totals -------------------------------------------------


def test_every_phase_of_a_selected_file_counts() -> None:
    history = RecordingHistory()
    recorder = FileDurationRecorder(
        whole_file_selections=frozenset({"test_a.py"}), history=history
    )
    for duration in (0.5, 2.0, 0.25):
        recorder.pytest_runtest_logreport(StubReport("test_a.py::test_one", duration))
    recorder.pytest_sessionfinish(StubSession(), 0)
    assert history.recorded == [{"test_a.py": 2.75}]


def test_reports_from_unselected_files_are_ignored() -> None:
    history = RecordingHistory()
    recorder = FileDurationRecorder(
        whole_file_selections=frozenset({"test_a.py"}), history=history
    )
    recorder.pytest_runtest_logreport(StubReport("test_fat.py::test_one", 9.0))
    recorder.pytest_sessionfinish(StubSession(), 0)
    assert history.recorded == [{"test_a.py": 0.0}]


def test_a_fully_deselected_file_records_what_it_truly_costs() -> None:
    """Zero, not the naive default: the lane's marker expression skips
    it every run, so it is genuinely free here."""
    history = RecordingHistory()
    recorder = FileDurationRecorder(
        whole_file_selections=frozenset({"test_a.py"}), history=history
    )
    recorder.pytest_sessionfinish(StubSession(), 0)
    assert history.recorded == [{"test_a.py": 0.0}]


def test_only_a_green_run_teaches() -> None:
    """An aborted run (-x) stops early and would teach every file it
    never reached that it is free."""
    history = RecordingHistory()
    recorder = FileDurationRecorder(
        whole_file_selections=frozenset({"test_a.py"}), history=history
    )
    recorder.pytest_runtest_logreport(StubReport("test_a.py::test_one", 3.0))
    recorder.pytest_sessionfinish(StubSession(), 1)
    assert history.recorded == []


def test_a_store_that_cannot_be_written_fails_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loud, never silent: a learning loop that stops learning quietly
    decays back into the baked constants this change deleted."""
    recorder = FileDurationRecorder(
        whole_file_selections=frozenset({"test_a.py"}), history=BrokenHistory()
    )
    session = StubSession()
    recorder.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 70
    assert "delete the file" in capsys.readouterr().err


# --- end to end, through a real pytest ----------------------------------------


def build_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repository / "test_alpha.py").write_text(
        "def test_one() -> None:\n    assert True\n"
        "def test_two() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    (repository / "test_beta.py").write_text(
        "def test_three() -> None:\n    assert True\n", encoding="utf-8",
    )
    return repository


def run_pytest(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-p",
            PLUGIN,
            *arguments,
        ),
        cwd=repository,
        capture_output=True,
        text=True,
    )


def history_path(repository: Path) -> Path:
    return repository / ".git" / "issue-orchestrator" / STORE_DIRNAME / "history.json"


def stored(repository: Path) -> dict[str, list[float]]:
    path = history_path(repository)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["durations"]


def test_a_green_run_records_every_file_it_ran_whole(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    completed = run_pytest(repository, "test_alpha.py", "test_beta.py")
    assert completed.returncode == 0, completed.stdout
    durations = stored(repository)
    assert sorted(durations) == ["test_alpha.py", "test_beta.py"]
    assert all(len(window) == 1 and window[0] >= 0.0 for window in durations.values())


def test_a_red_run_records_nothing(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    (repository / "test_beta.py").write_text(
        "def test_three() -> None:\n    assert False\n", encoding="utf-8"
    )
    completed = run_pytest(repository, "test_alpha.py", "test_beta.py")
    assert completed.returncode != 0
    assert stored(repository) == {}


def test_a_node_level_run_records_nothing_for_that_file(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    completed = run_pytest(
        repository, "test_alpha.py::test_one", "test_beta.py"
    )
    assert completed.returncode == 0, completed.stdout
    assert sorted(stored(repository)) == ["test_beta.py"]


def test_the_controller_records_the_whole_truth_under_xdist(tmp_path: Path) -> None:
    """The shape the gate actually runs. Each worker sees one shard, so
    a worker-side recorder would persist a fraction of a file as the
    file's weight; only the controller has every report."""
    repository = build_repository(tmp_path)
    completed = run_pytest(
        repository,
        "test_alpha.py",
        "test_beta.py",
        "-n",
        "2",
        "--dist=loadgroup",
    )
    assert completed.returncode == 0, completed.stdout
    durations = stored(repository)
    assert sorted(durations) == ["test_alpha.py", "test_beta.py"]
    assert all(len(window) == 1 for window in durations.values())


def test_a_corrupt_store_fails_an_otherwise_green_run(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    store = history_path(repository)
    store.parent.mkdir(parents=True)
    store.write_text("{not json", encoding="utf-8")
    completed = run_pytest(repository, "test_alpha.py")
    assert completed.returncode == 70, completed.stdout
    assert "delete the file" in completed.stderr


def test_runs_accumulate_a_rolling_window(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    for _ in range(3):
        assert run_pytest(repository, "test_alpha.py").returncode == 0
    assert len(stored(repository)["test_alpha.py"]) == 3


def test_outside_a_repository_the_plugin_persists_nothing(tmp_path: Path) -> None:
    repository = build_repository(tmp_path)
    (repository / ".git").rmdir()
    completed = run_pytest(repository, "test_alpha.py")
    assert completed.returncode == 0, completed.stdout
    assert not list(repository.rglob(STORE_DIRNAME))
