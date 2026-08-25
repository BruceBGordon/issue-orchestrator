"""Public-boundary lifecycle proofs for streamed command containment."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from enum import StrEnum
from io import TextIOBase
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TextIO, cast

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
from tests.process_tree_fixture import ProcessTreeMember


@dataclass(frozen=True, slots=True)
class _StartedProcessRecorder(ContainedCommandOutput):
    process_id_path: Path

    def child_started(self, started: ContainedCommandStarted) -> None:
        self.process_id_path.write_text(str(started.process_id), encoding="utf-8")

    def write_line(self, line: str) -> None:
        del line


class _RejectUnexpectedLine(ContainedCommandLineObserver):
    def observe_line(self, line: str) -> None:
        raise AssertionError(f"an unstarted output pump observed a line: {line!r}")


class _ThreadFailurePoint(StrEnum):
    CONSTRUCTION = "construction"
    START = "start"


class _PumpFinalizationFailurePoint(StrEnum):
    JOIN = "join"
    CLOSE = "close"


class _SupervisionFailureTiming(StrEnum):
    IMMEDIATE = "immediate"
    AFTER_OUTPUT_INTERRUPTION = "after-output-interruption"


@dataclass(frozen=True, slots=True)
class _CloseFailingTextStream:
    delegate: TextIOBase
    failure: OSError

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, TextIOBase):
            raise ValueError("_CloseFailingTextStream.delegate must be text I/O")
        if type(self.failure) is not OSError:
            raise ValueError("_CloseFailingTextStream.failure must be an OSError")

    def __iter__(self) -> Iterator[str]:
        return iter(self.delegate)

    def close(self) -> None:
        try:
            self.delegate.close()
        finally:
            raise self.failure


@dataclass(frozen=True, slots=True)
class _FailingLineObserver(ContainedCommandLineObserver):
    failure: ValueError

    def __post_init__(self) -> None:
        if type(self.failure) is not ValueError:
            raise ValueError("_FailingLineObserver.failure must be a ValueError")

    def observe_line(self, line: str) -> None:
        del line
        raise self.failure


@dataclass(frozen=True, slots=True)
class _FailingSupervision(ProcessGroupSupervisor):
    delegate: ProcessGroupSupervisor
    failure: RuntimeError
    timing: _SupervisionFailureTiming

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, ProcessGroupSupervisor):
            raise ValueError("_FailingSupervision.delegate must be a supervisor")
        if type(self.failure) is not RuntimeError:
            raise ValueError("_FailingSupervision.failure must be a RuntimeError")
        if type(self.timing) is not _SupervisionFailureTiming:
            raise ValueError(
                "_FailingSupervision.timing must be _SupervisionFailureTiming"
            )

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        del leader, wait
        if self.timing is _SupervisionFailureTiming.AFTER_OUTPUT_INTERRUPTION:
            if not interruption.wait_for_request(1.0):
                raise AssertionError("output capture did not request interruption")
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
    ProcessTreeMember(process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.parametrize("failure_point", tuple(_PumpFinalizationFailurePoint))
@pytest.mark.parametrize("recover_from_supervision_failure", (False, True))
def test_output_pump_finalization_failure_is_typed_after_group_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: _PumpFinalizationFailurePoint,
    recover_from_supervision_failure: bool,
) -> None:
    process_id_path = tmp_path / "finalized-process.pid"
    finalization_failure = OSError(
        f"injected output pump {failure_point.value} failure"
    )
    supervision_failure = RuntimeError("injected command supervision failure")

    def reject_thread_join(_thread: threading.Thread) -> None:
        raise finalization_failure

    original_popen = subprocess.Popen

    def spawn_with_close_failure(
        command: str,
        *,
        shell: bool,
        cwd: Path,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        start_new_session: bool,
    ) -> subprocess.Popen[str]:
        process = cast(
            "subprocess.Popen[str]",
            original_popen(
                command,
                shell=shell,
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                text=text,
                bufsize=bufsize,
                start_new_session=start_new_session,
            ),
        )
        if not isinstance(process.stdout, TextIOBase):
            raise AssertionError("contained command did not expose text stdout")
        process.stdout = cast(
            TextIO,
            _CloseFailingTextStream(process.stdout, finalization_failure),
        )
        return process

    if failure_point is _PumpFinalizationFailurePoint.JOIN:
        monkeypatch.setattr(threading.Thread, "join", reject_thread_join)
    else:
        monkeypatch.setattr(subprocess, "Popen", spawn_with_close_failure)
    process_group_supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            )
        )
    )
    capture_supervisor: ProcessGroupSupervisor = (
        _FailingSupervision(
            process_group_supervisor,
            supervision_failure,
            _SupervisionFailureTiming.IMMEDIATE,
        )
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
            finalization_failure,
        )
    else:
        assert type(result.cleanup) is ContainedCommandSupervised
        assert result.failure.error is finalization_failure
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    ProcessTreeMember(process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
def test_capture_and_supervision_failures_are_both_preserved(
    tmp_path: Path,
) -> None:
    process_id_path = tmp_path / "multi-failure-process.pid"
    capture_failure = ValueError("injected output capture failure")
    supervision_failure = RuntimeError("injected command supervision failure")
    process_group_supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            )
        )
    )
    capture = PosixContainedCommandCapture(
        _FailingSupervision(
            process_group_supervisor,
            supervision_failure,
            _SupervisionFailureTiming.AFTER_OUTPUT_INTERRUPTION,
        )
    )
    child_program = "import signal; print('trigger', flush=True); signal.pause()"

    result = capture.capture(
        ContainedShellCommand(
            command=(
                f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child_program)}"
            ),
            working_directory=tmp_path,
        ),
        _StartedProcessRecorder(process_id_path),
        _FailingLineObserver(capture_failure),
    )

    assert type(result) is ContainedCommandCaptureFailed
    assert type(result.cleanup) is ContainedCommandCaptureAborted
    assert type(result.failure.error) is ExceptionGroup
    assert result.failure.error.exceptions == (
        capture_failure,
        supervision_failure,
    )
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    ProcessTreeMember(process_id).assert_contained()
