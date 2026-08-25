"""Behavior tests for the shared real-process completion fixture."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

from tests.process_completion_fixture import (
    GuardianPidFile,
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    ProcessCleanupPlan,
    ProcessCleanupStep,
    ProcessCompletionWatchdog,
    TextProcessInvocation,
)
from tests.process_tree_fixture import PROCESS_CONTAINMENT_WATCHDOG_SECONDS
from tests.unit.executor_pressure_dsl import (
    HungPressureCommand,
    PressureRig,
    PressureWork,
)


pytestmark = pytest.mark.timeout(180)


def test_completion_watchdog_must_dominate_process_containment() -> None:
    with pytest.raises(ValueError, match="at least the process-containment watchdog"):
        ProcessCompletionWatchdog(PROCESS_CONTAINMENT_WATCHDOG_SECONDS - 1.0)


def test_text_invocation_requires_absolute_working_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute Path"):
        TextProcessInvocation(
            operation="invalid invocation",
            arguments=(sys.executable, "-c", "pass"),
            working_directory=Path("relative"),
            environment=os.environ,
            timeout_containment=NoDescendantProcessContainment(),
        )


def test_text_invocation_runs_under_shared_watchdog(tmp_path: Path) -> None:
    result = PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="captured fixture command",
            arguments=(sys.executable, "-c", "print('COMPLETE')"),
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            timeout_containment=NoDescendantProcessContainment(),
        )
    )

    assert result.returncode == 0
    assert result.stdout == "COMPLETE\n"


def test_recorded_guardian_group_is_force_contained(tmp_path: Path) -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(300)"),
        start_new_session=True,
    )
    guardian = GuardianPidFile((tmp_path / "guardian.pid").resolve())
    guardian.path.write_text(str(process.pid), encoding="utf-8")
    try:
        guardian.contain_if_recorded()
        exit_code = PROCESS_COMPLETION_WATCHDOG.wait(
            process,
            operation="reap force-contained fixture guardian",
        )
    finally:
        if process.poll() is None:
            process.kill()
            PROCESS_COMPLETION_WATCHDOG.wait(
                process,
                operation="reap fixture guardian after failed containment test",
            )

    assert exit_code < 0


def test_guardian_containment_assertion_fails_when_identity_was_not_recorded(
    tmp_path: Path,
) -> None:
    guardian = GuardianPidFile((tmp_path / "missing-guardian.pid").resolve())

    with pytest.raises(AssertionError, match="guardian identity was not recorded"):
        guardian.require_contained()


def test_completed_future_timeout_error_remains_the_worker_error() -> None:
    future: Future[int] = Future()
    future.set_exception(TimeoutError("worker timeout"))

    with pytest.raises(TimeoutError, match="worker timeout") as caught:
        PROCESS_COMPLETION_WATCHDOG.future_result(
            future,
            operation="completed worker",
        )

    assert type(caught.value) is TimeoutError


def test_cleanup_plan_attempts_every_step_before_raising() -> None:
    calls: list[str] = []

    def fail_first() -> None:
        calls.append("first")
        raise RuntimeError("first failed")

    def succeed_second() -> None:
        calls.append("second")

    def fail_third() -> None:
        calls.append("third")
        raise AssertionError("third failed")

    plan = ProcessCleanupPlan(
        operation="exercise independent cleanup",
        steps=(
            ProcessCleanupStep("first cleanup", fail_first),
            ProcessCleanupStep("second cleanup", succeed_second),
            ProcessCleanupStep("third cleanup", fail_third),
        ),
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        plan.execute()

    assert calls == ["first", "second", "third"]
    assert tuple(type(error) for error in caught.value.exceptions) == (
        RuntimeError,
        AssertionError,
    )


def test_pressure_scenario_failure_force_contains_hung_command_and_guardian(
    tmp_path: Path,
) -> None:
    hung = HungPressureCommand(
        guardian_pid_path=(tmp_path / "hung-guardian.pid").resolve(),
        command_pid_path=(tmp_path / "hung-command.pid").resolve(),
    )
    rig = PressureRig(tmp_path / "pool", host_cpu_slots=1)
    job = rig.submit(
        PressureWork(
            "ABORTED",
            "pressure-aborted",
            command=hung,
        )
    )

    with pytest.raises(RuntimeError, match="pressure scenario failed deliberately"):
        with rig:
            rig.require_started(job)
            raise RuntimeError("pressure scenario failed deliberately")

    rig.require_cleanup_complete(job)
    hung.require_command_contained()


def test_pressure_cleanup_failure_does_not_skip_containment_or_closing(
    tmp_path: Path,
) -> None:
    hung = HungPressureCommand(
        guardian_pid_path=(tmp_path / "corrupted-guardian.pid").resolve(),
        command_pid_path=(tmp_path / "contained-command.pid").resolve(),
    )
    rig = PressureRig(tmp_path / "pool", host_cpu_slots=1)
    job = rig.submit(
        PressureWork(
            "CORRUPTED-CLEANUP",
            "pressure-corrupted-cleanup",
            command=hung,
        )
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        with rig:
            rig.require_started(job)
            hung.guardian_pid_path.write_text("not-a-process-id", encoding="utf-8")
            raise RuntimeError("trigger cleanup after corrupt command record")

    scenario_errors, _remaining = caught.value.split(RuntimeError)
    cleanup_errors, _remaining = caught.value.split(ValueError)
    assert scenario_errors is not None
    assert cleanup_errors is not None
    rig.require_cleanup_complete(job)
    hung.require_command_contained()
