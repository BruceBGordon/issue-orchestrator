"""Composition-root behavior of the lane-run CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.condor.lane_executor import (
    PERSONAL_POOL_HOME_ENVIRONMENT_VARIABLE,
)
from issue_orchestrator.entrypoints.cli_tools.lane_run import (
    BACKEND_ENVIRONMENT_VARIABLE,
    main,
)

pytestmark = pytest.mark.timeout(180)


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
    from issue_orchestrator.domain.lane_execution import (
        LaneCommand,
        LaneCompleted,
        LaneResources,
    )
    import issue_orchestrator.entrypoints.cli_tools.lane_run as lane_run_module

    captured: list[LaneResources] = []

    class _CapturingExecutor:
        def run(
            self, command: LaneCommand, resources: LaneResources
        ) -> LaneCompleted:
            captured.append(resources)
            return LaneCompleted(0)

    monkeypatch.setattr(
        lane_run_module, "_build_executor", lambda backend: _CapturingExecutor()
    )
    code = _run(
        "/usr/bin/true",
        flags=("--request-memory-mb", "2048", "--priority", "7"),
    )
    assert code == 0
    assert len(captured) == 1
    resources = captured[0]
    assert resources.request_memory_mb == 2048
    assert resources.priority == 7


def test_memory_budget_domain_validation_rejects_nonsense() -> None:
    from issue_orchestrator.domain.lane_execution import LaneResources

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
