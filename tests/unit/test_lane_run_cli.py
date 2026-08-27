"""Composition-root behavior of the lane-run CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
)
from issue_orchestrator.adapters.json_lane_runtime_history import (
    JsonLaneRuntimeHistory,
)
from issue_orchestrator.domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneOutcome,
    LaneResources,
    LaneWorkKey,
)
from issue_orchestrator.entrypoints.cli_tools import lane_run as lane_run_module
from issue_orchestrator.entrypoints.cli_tools.lane_run import (
    BACKEND_ENVIRONMENT_VARIABLE,
    main,
)

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(autouse=True)
def isolated_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> JsonLaneRuntimeHistory:
    """Every test gets its own history store.

    Without this, any test that drives main() to a successful lane
    would read from and record into the repository's real shared
    runtime history — polluting the store that orders the actual gate.
    """
    history = JsonLaneRuntimeHistory(tmp_path / "lane-runtime-history")
    monkeypatch.setattr(lane_run_module, "_build_history", lambda: history)
    return history


def _run(*command: str, flags: tuple[str, ...] = (), timeout: str = "60") -> int:
    return main(
        [
            "--work-key",
            "cli.test",
            "--request-cpus",
            "1",
            "--timeout-seconds",
            timeout,
            *flags,
            "--",
            *command,
        ]
    )


class _CapturingExecutor:
    """Records what crosses the port; completes with a fixed outcome."""

    def __init__(self, outcome: LaneOutcome) -> None:
        self.outcome = outcome
        self.resources: list[LaneResources] = []

    def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
        self.resources.append(resources)
        return self.outcome


def _capture(
    monkeypatch: pytest.MonkeyPatch, outcome: LaneOutcome
) -> _CapturingExecutor:
    executor = _CapturingExecutor(outcome)
    monkeypatch.setattr(
        lane_run_module, "_build_executor", lambda backend: executor
    )
    return executor


def test_direct_backend_returns_the_lane_exit_code() -> None:
    assert _run(sys.executable, "-c", "raise SystemExit(0)") == 0
    assert _run(sys.executable, "-c", "raise SystemExit(9)") == 9


def test_deadline_reports_124() -> None:
    assert _run(sys.executable, "-c", "import time; time.sleep(60)", timeout="1") == 124


def test_missing_separator_is_a_usage_error() -> None:
    assert (
        main(
            [
                "--work-key",
                "cli.test",
                "--request-cpus",
                "1",
                "--timeout-seconds",
                "60",
                "/usr/bin/true",
            ]
        )
        == 78
    )


def test_missing_lane_executable_fails_as_configuration_error() -> None:
    assert _run("definitely-not-a-real-binary-anywhere") == 78


def test_opted_in_condor_without_tools_fails_loudly_not_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "no-pool")
    )
    assert _run("/usr/bin/true", flags=("--backend", "condor")) == 78


def test_environment_variable_selects_the_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(BACKEND_ENVIRONMENT_VARIABLE, "condor")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(
        PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "no-pool")
    )
    assert _run("/usr/bin/true") == 78


def test_memory_budget_crosses_the_port_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct adapter ignores scheduling hints, so a run-and-check
    test is vacuous: main() could drop the flag and still exit 0. A
    capturing executor at the composition seam proves the value the
    port actually receives."""
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0))
    assert _run("/usr/bin/true", flags=("--request-memory-mb", "2048")) == 0
    assert len(executor.resources) == 1
    assert executor.resources[0].request_memory_mb == 2048


def test_priority_cannot_be_declared_by_the_client() -> None:
    """Dispatch order is learned, never declared: the flag must not
    exist. A client's only vocabulary is the logical work key."""
    with pytest.raises(SystemExit):
        _run("/usr/bin/true", flags=("--priority", "7"))


def test_empty_history_is_the_naive_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero history means priority 0 — exactly today's behavior."""
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].priority == 0


def test_learned_priority_crosses_the_port_boundary(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """The rolling median of recorded runtimes becomes the submitted
    priority — the whole learning loop, observed at the port."""
    key = LaneWorkKey("cli.test")
    for runtime in (30.0, 90.0, 60.0):
        isolated_history.record_success(key, runtime)
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].priority == 60


def test_successful_lane_seeds_the_next_run(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    _capture(monkeypatch, LaneCompleted(0, 42.0))
    assert _run("/usr/bin/true") == 0
    assert isolated_history.learned_priority(LaneWorkKey("cli.test")) == 42


def test_failed_lane_teaches_nothing(
    monkeypatch: pytest.MonkeyPatch, isolated_history: JsonLaneRuntimeHistory
) -> None:
    """A failed run's duration is the failure's, not the lane's — a
    482s provider stall must never become a lane's learned weight."""
    _capture(monkeypatch, LaneCompleted(1, 482.0))
    assert _run("/usr/bin/true") == 1
    assert isolated_history.learned_priority(LaneWorkKey("cli.test")) == 0


def test_corrupt_history_fails_loudly_not_naively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "corrupt-history"
    directory.mkdir()
    (directory / "cli.test.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        lane_run_module,
        "_build_history",
        lambda: JsonLaneRuntimeHistory(directory),
    )
    assert _run("/usr/bin/true") == 70


def test_memory_budget_domain_validation_rejects_nonsense() -> None:
    bad_values: tuple[int, ...] = (0, -5, True)
    for bad in bad_values:
        with pytest.raises(ValueError):
            LaneResources(request_cpus=1, request_memory_mb=bad)
    with pytest.raises(ValueError):
        LaneResources(
            request_cpus=1,
            request_memory_mb=1.5,  # type: ignore[arg-type]
        )
    assert LaneResources(request_cpus=1, request_memory_mb=2048).request_memory_mb == 2048


def test_observed_runtime_domain_validation_rejects_nonsense() -> None:
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            LaneCompleted(0, bad)
    with pytest.raises(ValueError):
        # The annotation's numeric tower accepts an int; the runtime
        # guard does not — observed runtimes are always measured floats.
        LaneCompleted(0, 5)
    assert LaneCompleted(0, 0.0).observed_runtime_seconds == 0.0


def test_suspendability_crosses_the_port_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live lanes declare they must never be frozen mid-run; the port
    must receive that declaration, and the default must be suspendable."""
    executor = _capture(monkeypatch, LaneCompleted(0, 1.0))
    assert _run("/usr/bin/true") == 0
    assert executor.resources[0].suspendable is True
    assert _run("/usr/bin/true", flags=("--not-suspendable",)) == 0
    assert executor.resources[1].suspendable is False


def test_suspendable_domain_validation_rejects_non_bool() -> None:
    for bad in (1, "yes", None):
        with pytest.raises(ValueError):
            LaneResources(request_cpus=1, suspendable=bad)  # type: ignore[arg-type]
