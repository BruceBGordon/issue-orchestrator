"""Whole-tree containment proofs for the POSIX process-group owner."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import IO

import pytest

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupExecutable,
    ProcessGroupPermissionDenied,
    ProcessGroupUnboundedWait,
    ProcessGroupZombiesOnly,
)
from issue_orchestrator.execution.process_group_supervisor import (
    NeverInterruptProcessGroup,
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ExitingTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.unit.process_group_observer_helpers import RecordingProcessGroupObserver
from tests.process_completion_fixture import (
    PROCESS_COMPLETION_WATCHDOG,
    ProcessCleanupPlan,
    ProcessCleanupStep,
    build_test_process_group_observer,
)


pytestmark = pytest.mark.timeout(45)


def test_term_resistant_descendant_dies_when_cooperative_leader_exits(
    tmp_path: Path,
) -> None:
    """A leader's TERM exit must not suppress the whole-group SIGKILL."""
    descendant_pid_path = (tmp_path / "cooperative-descendant.pid").resolve()
    leader = CooperativeTermResistantProcessTreeProgram(
        descendant_pid_path,
        300,
        ("TREE-READY",),
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader.python_source()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise AssertionError("leader readiness pipe was not created")
    readiness = process.stdout.readline()
    assert readiness == "TREE-READY\n", (
        f"leader readiness mismatch: line={readiness!r} "
        f"returncode={process.poll()!r}"
    )
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
    terminator = PosixProcessGroupTerminator(
        ExecutorProcessTerminationPolicy(
            graceful_shutdown_seconds=0.1,
            forceful_shutdown_seconds=1.0,
        ),
        build_test_process_group_observer(),
    )

    try:
        termination = terminator.terminate(OwnedProcessGroupLeader(process.pid))
        process.returncode = termination.leader_exit_code
        assert process.returncode == 0
        ProcessTreeMember(descendant_pid).assert_contained()
    finally:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        cleanup_steps: list[ProcessCleanupStep] = []
        if process.returncode is None:
            cleanup_steps.extend(
                (
                    ProcessCleanupStep(
                        "kill process-group fixture leader",
                        lambda: _kill_process_group_if_present(process.pid),
                    ),
                    ProcessCleanupStep(
                        "reap process-group fixture leader",
                        lambda: _reap_with_watchdog(
                            process,
                            operation="process-group fixture leader cleanup",
                        ),
                    ),
                )
            )
        cleanup_steps.extend(
            (
                ProcessCleanupStep(
                    "close process-group fixture stdout",
                    lambda: _close_stream(process.stdout),
                ),
                ProcessCleanupStep(
                    "close process-group fixture stderr",
                    lambda: _close_stream(process.stderr),
                ),
            )
        )
        ProcessCleanupPlan(
            "process-group fixture cleanup",
            tuple(cleanup_steps),
        ).execute(preceding_error=sys.exception())


def test_natural_leader_exit_contains_descendant_before_reaping(
    tmp_path: Path,
) -> None:
    descendant_pid_path = (tmp_path / "natural-descendant.pid").resolve()
    natural_leader = ExitingTermResistantProcessTreeProgram(
        descendant_pid_path,
        300,
        0,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", natural_leader.python_source()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )

    try:
        supervision = supervisor.supervise(
            OwnedProcessGroupLeader(process.pid),
            ProcessGroupUnboundedWait(),
            NeverInterruptProcessGroup(),
        )
        process.returncode = supervision.termination.leader_exit_code
        assert type(supervision) is ProcessGroupCompleted
        assert process.returncode == 0
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        ProcessTreeMember(descendant_pid).assert_contained()
    finally:
        if descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_courtesy_observation_failure_is_typed_after_forced_containment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        ["/bin/sh", "-c", "exec /bin/sleep 300"],
        start_new_session=True,
    )
    courtesy_failure = OSError("injected courtesy wait observation failure")
    terminator = PosixProcessGroupTerminator(
        ExecutorProcessTerminationPolicy(0.1, 1.0),
        build_test_process_group_observer(),
    )

    def fail_courtesy_observation(
        leader: OwnedProcessGroupLeader,
        *,
        timeout_seconds: float,
    ) -> None:
        del leader, timeout_seconds
        raise courtesy_failure

    monkeypatch.setattr(
        terminator,
        "_await_leader_exit_without_reaping",
        fail_courtesy_observation,
    )

    termination = terminator.terminate(OwnedProcessGroupLeader(process.pid))
    process.returncode = termination.leader_exit_code

    degraded = termination.courtesy_failure()
    assert degraded is not None
    assert degraded.error is courtesy_failure
    ProcessTreeMember(process.pid).assert_contained()


def test_reaped_leader_observation_failure_still_forces_descendant_containment(
    tmp_path: Path,
) -> None:
    descendant_pid_path = (tmp_path / "pre-reaped-descendant.pid").resolve()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            ExitingTermResistantProcessTreeProgram(
                descendant_pid_path,
                300,
                0,
            ).python_source(),
        ],
        start_new_session=True,
    )
    PROCESS_COMPLETION_WATCHDOG.wait(
        process,
        operation="pre-reaped process-group leader",
    )
    assert process.returncode == 0
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
    terminator = PosixProcessGroupTerminator(
        ExecutorProcessTerminationPolicy(0.1, 1.0),
        build_test_process_group_observer(),
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        terminator.terminate(OwnedProcessGroupLeader(process.pid))

    assert len(caught.value.exceptions) == 2
    assert "reaped outside" in str(caught.value.exceptions[0])
    assert "reaped outside" in str(caught.value.exceptions[1])
    ProcessTreeMember(descendant_pid).assert_contained()


def test_supervisor_accepts_macos_eperm_only_after_zombie_only_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    observer = RecordingProcessGroupObserver(
        group_observation=ProcessGroupZombiesOnly(1)
    )
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(0.1, 1.0),
            observer,
        )
    )

    def deny_group_signal(
        process_group_id: int,
        signal_number: int,
    ) -> None:
        del process_group_id, signal_number
        raise PermissionError("injected macOS zombie-only EPERM")

    monkeypatch.setattr(os, "killpg", deny_group_signal)
    supervision = supervisor.supervise(
        OwnedProcessGroupLeader(process.pid),
        ProcessGroupUnboundedWait(),
        NeverInterruptProcessGroup(),
    )
    process.returncode = supervision.termination.leader_exit_code

    assert type(supervision) is ProcessGroupCompleted
    assert process.returncode == 0
    assert observer.process_group_ids == [process.pid, process.pid]
    _close_stream(process.stdout)
    _close_stream(process.stderr)


@pytest.mark.parametrize(
    ("observation", "expected_detail"),
    (
        (ProcessGroupExecutable(2), "2 executable member"),
        (ProcessGroupPermissionDenied("process table denied"), "observation denied"),
    ),
)
def test_supervisor_fails_fast_when_eperm_does_not_prove_containment(
    monkeypatch: pytest.MonkeyPatch,
    observation: ProcessGroupExecutable | ProcessGroupPermissionDenied,
    expected_detail: str,
) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,sys; print('READY', flush=True); signal.pause()",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise AssertionError("EPERM fixture readiness pipe was not created")
    assert process.stdout.readline() == "READY\n"
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(0.1, 1.0),
            RecordingProcessGroupObserver(group_observation=observation),
        )
    )

    try:
        with monkeypatch.context() as patcher:
            def deny_group_signal(
                process_group_id: int,
                signal_number: int,
            ) -> None:
                del process_group_id, signal_number
                raise PermissionError("injected executable-group EPERM")

            patcher.setattr(os, "killpg", deny_group_signal)
            with pytest.raises(BaseExceptionGroup) as caught:
                supervisor.abort(OwnedProcessGroupLeader(process.pid))

        failures = caught.value.exceptions
        assert len(failures) == 3
        assert expected_detail in str(failures[0])
        assert "SIGKILL" in str(failures[1])
        assert "did not reap" in str(failures[2])
    finally:
        ProcessCleanupPlan(
            "EPERM process-group fixture cleanup",
            (
                ProcessCleanupStep(
                    "kill EPERM fixture group",
                    lambda: _kill_process_group_if_present(process.pid),
                ),
                ProcessCleanupStep(
                    "reap EPERM fixture leader",
                    lambda: _reap_with_watchdog(
                        process,
                        operation="EPERM process-group fixture cleanup",
                    ),
                ),
                ProcessCleanupStep(
                    "close EPERM fixture stdout",
                    lambda: _close_stream(process.stdout),
                ),
                ProcessCleanupStep(
                    "close EPERM fixture stderr",
                    lambda: _close_stream(process.stderr),
                ),
            ),
        ).execute(preceding_error=sys.exception())


def _kill_process_group_if_present(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _reap_with_watchdog(
    process: subprocess.Popen[str],
    *,
    operation: str,
) -> None:
    PROCESS_COMPLETION_WATCHDOG.wait(process, operation=operation)


def _close_stream(stream: IO[str] | None) -> None:
    if stream is not None:
        stream.close()
