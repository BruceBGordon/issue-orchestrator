"""Public-boundary lifecycle proofs for streamed command containment."""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
import shlex
import signal
import sys
import threading
from dataclasses import dataclass

import pytest

from issue_orchestrator.domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCaptureFailed,
    ContainedCommandSupervised,
    ContainedCommandStarted,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupWait,
)
from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.execution.contained_command_capture import (
    PosixContainedCommandCapture,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.ports.contained_command import (
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedShellCommand,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)


@dataclass(frozen=True, slots=True)
class _StartedProcessRecorder(ContainedCommandOutput):
    process_id_path: Path

    def child_started(self, started: ContainedCommandStarted) -> None:
        self.process_id_path.write_text(str(started.process_id), encoding="utf-8")

    def write_line(self, line: str) -> None:
        raise AssertionError(f"an unstarted output pump wrote a line: {line!r}")


class _RejectUnexpectedLine(ContainedCommandLineObserver):
    def observe_line(self, line: str) -> None:
        raise AssertionError(f"an unstarted output pump observed a line: {line!r}")


class _ThreadFailurePoint(StrEnum):
    CONSTRUCTION = "construction"
    START = "start"


@dataclass(frozen=True, slots=True)
class _FailingSupervision(ProcessGroupSupervisor):
    delegate: ProcessGroupSupervisor
    failure: RuntimeError

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, ProcessGroupSupervisor):
            raise ValueError("_FailingSupervision.delegate must be a supervisor")
        if type(self.failure) is not RuntimeError:
            raise ValueError("_FailingSupervision.failure must be a RuntimeError")

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        del leader, wait, interruption
        raise self.failure

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        return self.delegate.abort(leader)


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.parametrize("failure_point", tuple(_ThreadFailurePoint))
def test_output_pump_setup_failure_contains_and_reaps_started_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: _ThreadFailurePoint,
) -> None:
    process_id_path = tmp_path / "started-process.pid"
    setup_failure = RuntimeError(
        f"injected output pump thread {failure_point.value} failure"
    )

    def reject_thread_start(_thread: threading.Thread) -> None:
        raise setup_failure

    def reject_thread_construction(
        *,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> threading.Thread:
        del target, name, daemon
        raise setup_failure

    if failure_point is _ThreadFailurePoint.CONSTRUCTION:
        monkeypatch.setattr(threading, "Thread", reject_thread_construction)
    else:
        monkeypatch.setattr(threading.Thread, "start", reject_thread_start)
    capture = PosixContainedCommandCapture(
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                ExecutorProcessTerminationPolicy(
                    graceful_shutdown_seconds=0.01,
                    forceful_shutdown_seconds=1.0,
                )
            )
        )
    )
    child_program = (
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()"
    )

    result = capture.capture(
        ContainedShellCommand(
            command=(
                f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child_program)}"
            ),
            working_directory=tmp_path,
        ),
        _StartedProcessRecorder(process_id_path),
        _RejectUnexpectedLine(),
    )

    assert type(result) is ContainedCommandCaptureFailed
    assert type(result.cleanup) is ContainedCommandCaptureAborted
    assert result.failure.error is setup_failure
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, signal.SIGCONT)


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.parametrize("recover_from_supervision_failure", (False, True))
def test_output_pump_join_failure_is_typed_after_group_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recover_from_supervision_failure: bool,
) -> None:
    process_id_path = tmp_path / "finalized-process.pid"
    join_failure = OSError("injected output pump join failure")
    supervision_failure = RuntimeError("injected command supervision failure")

    def reject_thread_join(_thread: threading.Thread) -> None:
        raise join_failure

    monkeypatch.setattr(threading.Thread, "join", reject_thread_join)
    process_group_supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            )
        )
    )
    capture_supervisor: ProcessGroupSupervisor = (
        _FailingSupervision(process_group_supervisor, supervision_failure)
        if recover_from_supervision_failure
        else process_group_supervisor
    )
    child_program = (
        "import signal; signal.pause()" if recover_from_supervision_failure else "pass"
    )

    result = PosixContainedCommandCapture(capture_supervisor).capture(
        ContainedShellCommand(
            command=(
                f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child_program)}"
            ),
            working_directory=tmp_path,
        ),
        _StartedProcessRecorder(process_id_path),
        _RejectUnexpectedLine(),
    )

    assert type(result) is ContainedCommandCaptureFailed
    if recover_from_supervision_failure:
        assert type(result.cleanup) is ContainedCommandCaptureAborted
        assert type(result.failure.error) is ExceptionGroup
        assert result.failure.error.exceptions == (
            supervision_failure,
            join_failure,
        )
    else:
        assert type(result.cleanup) is ContainedCommandSupervised
        assert result.failure.error is join_failure
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, signal.SIGCONT)
