"""Public-boundary lifecycle proofs for streamed command containment."""

from __future__ import annotations

import os
import signal
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import shlex
import sys
import threading
from dataclasses import dataclass

import pytest

from issue_orchestrator.domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCaptureFailed,
    ContainedCommandCleanupNotStarted,
    ContainedCommandCompleted,
    ContainedCommandNotStarted,
    ContainedCommandOutputPolicy,
    ContainedCommandOutputPipeClose,
    ContainedCommandOutputPipeClosed,
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
    OsContainedCommandOutputPipeFactory,
    PosixContainedCommandCapture,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
)
from issue_orchestrator.domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessActivationPolicy,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
)
from issue_orchestrator.ports.contained_command import (
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedCommandOutputPipe,
    ContainedCommandOutputPipeFactory,
    ContainedCommandOutputReader,
    ContainedShellCommand,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from issue_orchestrator.ports.posix_process import (
    PosixProcessLaunch,
    PosixProcessLauncher,
)
from tests.process_completion_fixture import (
    PROCESS_COMPLETION_WATCHDOG,
    build_test_process_group_observer,
)
from tests.process_tree_fixture import ProcessTreeMember


@dataclass(frozen=True, slots=True)
class _StartedProcessRecorder(ContainedCommandOutput):
    process_id_path: Path

    def child_started(self, started: ContainedCommandStarted) -> None:
        self.process_id_path.write_text(str(started.process_id), encoding="utf-8")

    def write_line(self, line: str) -> None:
        del line


class _IgnoreLines(ContainedCommandLineObserver):
    def observe_line(self, line: str) -> None:
        del line


@dataclass(frozen=True, slots=True)
class _BlockingOutput(ContainedCommandOutput):
    callback_started: threading.Event
    release_callback: threading.Event

    def child_started(self, started: ContainedCommandStarted) -> None:
        del started

    def write_line(self, line: str) -> None:
        del line
        self.callback_started.set()
        PROCESS_COMPLETION_WATCHDOG.wait_for_event(
            self.release_callback,
            operation="release blocked contained-command output callback",
        )


class _CloseFailingBinaryStream(io.RawIOBase):
    def __init__(
        self,
        delegate: ContainedCommandOutputReader,
        failure: OSError,
    ) -> None:
        if type(failure) is not OSError:
            raise ValueError("_CloseFailingBinaryStream.failure must be an OSError")
        self.delegate = delegate
        self.failure = failure

    def fileno(self) -> int:
        return self.delegate.fileno()

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.delegate.close()
        finally:
            super().close()
            raise self.failure


@dataclass(frozen=True, slots=True)
class _CloseFailingOutputPipe:
    delegate: ContainedCommandOutputPipe
    failure: OSError

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return self.delegate.descriptor_mappings

    def open_reader_after_launch(self) -> ContainedCommandOutputReader:
        return _CloseFailingBinaryStream(
            self.delegate.open_reader_after_launch(),
            self.failure,
        )

    def close(self) -> ContainedCommandOutputPipeClose:
        return self.delegate.close()


@dataclass(frozen=True, slots=True)
class _CloseFailingOutputPipeFactory:
    failure: OSError

    def create(self) -> ContainedCommandOutputPipe:
        return _CloseFailingOutputPipe(
            OsContainedCommandOutputPipeFactory().create(),
            self.failure,
        )


@dataclass(slots=True)
class _RecordingOutput(ContainedCommandOutput):
    process_id_path: Path
    lines: list[str]

    def child_started(self, started: ContainedCommandStarted) -> None:
        self.process_id_path.write_text(str(started.process_id), encoding="utf-8")

    def write_line(self, line: str) -> None:
        self.lines.append(line)


class _ObservableOutputPipe(ContainedCommandOutputPipe):
    """Real endpoints whose closure is observable through the public pipe port."""

    def __init__(self) -> None:
        self._read_descriptor, self._write_descriptor = os.pipe()

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return (
            PosixDescriptorMapping(self._write_descriptor, 1),
            PosixDescriptorMapping(self._write_descriptor, 2),
        )

    def open_reader_after_launch(self) -> ContainedCommandOutputReader:
        raise AssertionError("a rejected activation cannot transfer the pipe reader")

    def close(self) -> ContainedCommandOutputPipeClose:
        os.close(self._read_descriptor)
        os.close(self._write_descriptor)
        return ContainedCommandOutputPipeClosed()

    def assert_endpoints_closed(self) -> None:
        for descriptor in (self._read_descriptor, self._write_descriptor):
            with pytest.raises(OSError):
                os.fstat(descriptor)


class _DescriptorMappingFailingOutputPipe(_ObservableOutputPipe):
    def __init__(self, failure: RuntimeError) -> None:
        super().__init__()
        self._failure = failure

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        raise self._failure


@dataclass(frozen=True, slots=True)
class _OneOutputPipeFactory(ContainedCommandOutputPipeFactory):
    pipe: _ObservableOutputPipe

    def create(self) -> ContainedCommandOutputPipe:
        return self.pipe


@dataclass(frozen=True, slots=True)
class _RaisingProcessLauncher(PosixProcessLauncher):
    failure: RuntimeError

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        del specification
        raise self.failure


_OUTPUT_POLICY = ContainedCommandOutputPolicy(
    poll_interval_seconds=0.01,
    shutdown_timeout_seconds=1.0,
    final_drain_byte_limit=1_048_576,
)


def _capture(supervisor: ProcessGroupSupervisor) -> PosixContainedCommandCapture:
    return _capture_with_pipe_factory(
        supervisor,
        OsContainedCommandOutputPipeFactory(),
    )


def _capture_with_pipe_factory(
    supervisor: ProcessGroupSupervisor,
    output_pipe_factory: ContainedCommandOutputPipeFactory,
) -> PosixContainedCommandCapture:
    return PosixContainedCommandCapture(
        RetainedPosixProcessLauncher(
            PosixProcessProgram(
                (
                    str(Path(sys.executable)),
                    "-m",
                    "issue_orchestrator.entrypoints.posix_process_child",
                )
            ),
            MaskedPosixSpawnPrimitive(),
            supervisor,
            PosixProcessActivationPolicy(2.0),
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.05,
                forceful_shutdown_seconds=1.0,
            ),
        ),
        supervisor,
        _OUTPUT_POLICY,
        output_pipe_factory,
    )


def _capture_with_launcher_and_pipe(
    process_launcher: PosixProcessLauncher,
    supervisor: ProcessGroupSupervisor,
    output_pipe_factory: ContainedCommandOutputPipeFactory,
) -> PosixContainedCommandCapture:
    return PosixContainedCommandCapture(
        process_launcher,
        supervisor,
        _OUTPUT_POLICY,
        output_pipe_factory,
    )


@pytest.mark.parametrize("mapping_failure", (False, True))
def test_prelaunch_failure_closes_every_acquired_output_pipe_endpoint(
    tmp_path: Path,
    mapping_failure: bool,
) -> None:
    expected_failure = RuntimeError("injected prelaunch failure")
    pipe = (
        _DescriptorMappingFailingOutputPipe(expected_failure)
        if mapping_failure
        else _ObservableOutputPipe()
    )
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.05,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )
    launcher_failure = (
        RuntimeError("launcher must not run after mapping failure")
        if mapping_failure
        else expected_failure
    )

    result = _capture_with_launcher_and_pipe(
        _RaisingProcessLauncher(launcher_failure),
        supervisor,
        _OneOutputPipeFactory(pipe),
    ).capture(
        ContainedShellCommand("true", tmp_path.resolve()),
        _StartedProcessRecorder((tmp_path / "unexpected.pid").resolve()),
        _IgnoreLines(),
    )

    assert type(result) is ContainedCommandCaptureFailed
    assert type(result.child) is ContainedCommandNotStarted
    assert type(result.cleanup) is ContainedCommandCleanupNotStarted
    assert result.failure.error is expected_failure
    pipe.assert_endpoints_closed()


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
class _FailingAfterOutputReadySupervision(ProcessGroupSupervisor):
    delegate: ProcessGroupSupervisor
    failure: RuntimeError
    output_ready_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.delegate, ProcessGroupSupervisor):
            raise ValueError(
                "_FailingAfterOutputReadySupervision.delegate must be a supervisor"
            )
        if type(self.failure) is not RuntimeError:
            raise ValueError(
                "_FailingAfterOutputReadySupervision.failure must be RuntimeError"
            )
        if not self.output_ready_path.is_absolute():
            raise ValueError(
                "_FailingAfterOutputReadySupervision.output_ready_path must be absolute"
            )

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        del leader, wait, interruption
        PROCESS_COMPLETION_WATCHDOG.wait_for_path(
            self.output_ready_path,
            operation="contained output publication readiness",
        )
        raise self.failure

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        return self.delegate.abort(leader)


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.parametrize("recover_from_supervision_failure", (False, True))
def test_output_collector_close_failure_is_typed_after_group_containment(
    tmp_path: Path,
    recover_from_supervision_failure: bool,
) -> None:
    process_id_path = tmp_path / "finalized-process.pid"
    finalization_failure = OSError("injected output collector close failure")
    supervision_failure = RuntimeError("injected command supervision failure")

    process_group_supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )
    capture_supervisor: ProcessGroupSupervisor = (
        _FailingAfterOutputReadySupervision(
            process_group_supervisor,
            supervision_failure,
            (tmp_path / "diagnostic-ready").resolve(),
        )
        if recover_from_supervision_failure
        else process_group_supervisor
    )
    ready_path = (tmp_path / "diagnostic-ready").resolve()
    child_program = "print('diagnostic', flush=True)"
    if recover_from_supervision_failure:
        child_program += (
            "; import pathlib,signal; "
            f"pathlib.Path({str(ready_path)!r}).write_text('ready'); signal.pause()"
        )
    output_lines: list[str] = []

    result = _capture_with_pipe_factory(
        capture_supervisor,
        _CloseFailingOutputPipeFactory(finalization_failure),
    ).capture(
        ContainedShellCommand(
            command=(
                f"exec {shlex.quote(sys.executable)} -c {shlex.quote(child_program)}"
            ),
            working_directory=tmp_path,
        ),
        _RecordingOutput(process_id_path, output_lines),
        _IgnoreLines(),
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
    assert output_lines == ["diagnostic\n"]
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    ProcessTreeMember(process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
def test_capture_and_supervision_failures_are_both_preserved(
    tmp_path: Path,
) -> None:
    process_id_path = tmp_path / "multi-failure-process.pid"
    capture_failure = ValueError("injected output capture failure")
    supervision_failure = RuntimeError("injected command supervision failure")
    output_ready_path = (tmp_path / "output-ready").resolve()
    process_group_supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )
    capture = _capture(
        _FailingAfterOutputReadySupervision(
            process_group_supervisor,
            supervision_failure,
            output_ready_path,
        )
    )
    child_program = (
        "import pathlib,signal; print('trigger', flush=True); "
        f"pathlib.Path({str(output_ready_path)!r}).write_text('ready'); "
        "signal.pause()"
    )

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
        supervision_failure,
        capture_failure,
    )
    process_id = int(process_id_path.read_text(encoding="utf-8"))
    ProcessTreeMember(process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor ownership")
@pytest.mark.timeout(10)
def test_escaped_descendant_holding_stdout_cannot_block_capture_finalization(
    tmp_path: Path,
) -> None:
    escaped_pid_path = (tmp_path / "escaped-stdout-holder.pid").resolve()
    child_program = "import time; time.sleep(300)"
    leader_program = (
        "import pathlib,subprocess,sys; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}],"
        "start_new_session=True); "
        f"pathlib.Path({str(escaped_pid_path)!r}).write_text(str(child.pid)); "
        "print('leader-complete',flush=True)"
    )
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )

    result = _capture(supervisor).capture(
        ContainedShellCommand(
            command=(
                f"exec {shlex.quote(sys.executable)} -c {shlex.quote(leader_program)}"
            ),
            working_directory=tmp_path,
        ),
        _StartedProcessRecorder(tmp_path / "leader.pid"),
        _IgnoreLines(),
    )

    escaped_process_id = int(escaped_pid_path.read_text(encoding="utf-8"))
    try:
        assert type(result) is ContainedCommandCompleted
    finally:
        os.kill(escaped_process_id, signal.SIGKILL)
        ProcessTreeMember(escaped_process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_capture_does_not_return_while_caller_output_is_in_flight(
    tmp_path: Path,
) -> None:
    callback_started = threading.Event()
    release_callback = threading.Event()
    output = _BlockingOutput(callback_started, release_callback)
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.01,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        result_future = workers.submit(
            _capture(supervisor).capture,
            ContainedShellCommand(
                command="printf 'one line\\n'",
                working_directory=tmp_path,
            ),
            output,
            _IgnoreLines(),
        )
        try:
            PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                callback_started,
                operation="caller output callback entered",
            )
            assert result_future.done() is False
        finally:
            release_callback.set()
        result = PROCESS_COMPLETION_WATCHDOG.future_result(
            result_future,
            operation="capture after caller output release",
        )

    assert type(result) is ContainedCommandCompleted
