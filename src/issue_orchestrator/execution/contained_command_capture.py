"""POSIX deep owner for streamed command capture and group containment."""

from __future__ import annotations

import codecs
import os
import selectors
import subprocess
import threading
from dataclasses import dataclass, field
from typing import BinaryIO, cast

from ..domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCaptureFailed,
    ContainedCommandCaptureInterrupted,
    ContainedCommandCleanupFailed,
    ContainedCommandCleanupNotStarted,
    ContainedCommandCompleted,
    ContainedCommandExited,
    ContainedCommandExitUnknown,
    ContainedCommandFailure,
    ContainedCommandMetrics,
    ContainedCommandNotStarted,
    ContainedCommandOutputPolicy,
    ContainedCommandResult,
    ContainedCommandStarted,
    ContainedCommandSupervised,
)
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupUnboundedWait,
)
from ..ports.contained_command import (
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedShellCommand,
)
from ..ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)


def _combine_failures(
    primary: ContainedCommandFailure,
    secondary: ContainedCommandFailure | None,
    message: str,
) -> ContainedCommandFailure:
    if secondary is None:
        return primary
    return ContainedCommandFailure(
        BaseExceptionGroup(message, (primary.error, secondary.error))
    )


@dataclass(frozen=True, slots=True)
class _OutputPumpFinalized:
    """The pump stopped and its stdout stream closed."""


@dataclass(frozen=True, slots=True)
class _OutputPumpFinalizationFailed:
    """Pump shutdown failed after the command group was already contained."""

    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        if type(self.failure) is not ContainedCommandFailure:
            raise ValueError(
                "_OutputPumpFinalizationFailed.failure must be a "
                "ContainedCommandFailure"
            )


_OutputPumpFinalization = _OutputPumpFinalized | _OutputPumpFinalizationFailed


@dataclass(frozen=True, slots=True)
class _OutputPumpDetached:
    """The pump can no longer call either caller-owned output sink."""


@dataclass(frozen=True, slots=True)
class _OutputPumpDetachmentFailed:
    """The pump could not be detached from caller-owned sinks."""

    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        if type(self.failure) is not ContainedCommandFailure:
            raise ValueError(
                "_OutputPumpDetachmentFailed.failure must be a ContainedCommandFailure"
            )


_OutputPumpDetachment = _OutputPumpDetached | _OutputPumpDetachmentFailed


@dataclass(frozen=True, slots=True)
class _OutputReadPending:
    """No bytes were ready; the pump should continue polling."""


@dataclass(frozen=True, slots=True)
class _OutputReadFinished:
    """EOF or the bounded final drain ended output consumption."""


@dataclass(frozen=True, slots=True)
class _OutputReadChunk:
    """One byte chunk plus the remaining post-containment drain budget."""

    data: bytes
    remaining_final_bytes: int

    def __post_init__(self) -> None:
        if type(self.data) is not bytes or not self.data:
            raise ValueError("_OutputReadChunk.data must not be empty")
        if (
            type(self.remaining_final_bytes) is not int
            or self.remaining_final_bytes < 0
        ):
            raise ValueError(
                "_OutputReadChunk.remaining_final_bytes must be non-negative"
            )


_OutputRead = _OutputReadPending | _OutputReadFinished | _OutputReadChunk


@dataclass(slots=True)
class _CapturedOutputPump(ProcessGroupInterruption):
    """Drain output while exposing the first observer failure as interruption."""

    stdout: BinaryIO
    output: ContainedCommandOutput
    line_observer: ContainedCommandLineObserver
    policy: ContainedCommandOutputPolicy
    _interruption: threading.Event = field(default_factory=threading.Event, init=False)
    _stop_requested: threading.Event = field(
        default_factory=threading.Event,
        init=False,
    )
    _sink_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread = field(init=False)
    _failure: ContainedCommandFailure | None = field(default=None, init=False)
    _accepting_output: bool = field(default=True, init=False)
    _line_count: int = field(default=0, init=False)
    _byte_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.output, ContainedCommandOutput):
            raise ValueError(
                "_CapturedOutputPump.output must implement ContainedCommandOutput"
            )
        if not isinstance(self.line_observer, ContainedCommandLineObserver):
            raise ValueError(
                "_CapturedOutputPump.line_observer must implement "
                "ContainedCommandLineObserver"
            )
        if type(self.policy) is not ContainedCommandOutputPolicy:
            raise ValueError(
                "_CapturedOutputPump.policy must be ContainedCommandOutputPolicy"
            )
        self._thread = threading.Thread(
            target=self._drain,
            name="contained-command-output",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wait_for_request(self, timeout_seconds: float) -> bool:
        if type(timeout_seconds) is not float or timeout_seconds <= 0.0:
            raise ValueError("output interruption wait must be a positive float")
        return self._interruption.wait(timeout_seconds)

    def finalize_after_containment(self) -> _OutputPumpFinalization:
        failure: ContainedCommandFailure | None = None
        self._stop_requested.set()
        try:
            self._thread.join(timeout=self.policy.shutdown_timeout_seconds)
            if self._thread.is_alive():
                raise TimeoutError(
                    "contained command output pump did not stop after group "
                    f"containment within {self.policy.shutdown_timeout_seconds}s"
                )
        except BaseException as error:
            failure = ContainedCommandFailure(error)
            detachment = self.detach_after_cleanup_failure()
            if type(detachment) is _OutputPumpDetachmentFailed:
                failure = _combine_failures(
                    failure,
                    detachment.failure,
                    "output pump join and sink detach both failed",
                )
        try:
            self.stdout.close()
        except BaseException as close_error:
            close_failure = ContainedCommandFailure(close_error)
            failure = (
                close_failure
                if failure is None
                else _combine_failures(
                    failure,
                    close_failure,
                    "output pump finalization failed more than once",
                )
            )
        if failure is None:
            return _OutputPumpFinalized()
        return _OutputPumpFinalizationFailed(failure)

    def detach_after_cleanup_failure(self) -> _OutputPumpDetachment:
        """Prevent a still-blocked daemon pump from touching caller-owned sinks."""
        try:
            with self._sink_lock:
                self._accepting_output = False
        except BaseException as error:
            return _OutputPumpDetachmentFailed(ContainedCommandFailure(error))
        return _OutputPumpDetached()

    @property
    def failure(self) -> ContainedCommandFailure | None:
        with self._sink_lock:
            return self._failure

    @property
    def metrics(self) -> ContainedCommandMetrics:
        with self._sink_lock:
            return ContainedCommandMetrics(
                line_count=self._line_count,
                byte_count=self._byte_count,
            )

    def _drain(self) -> None:
        selector = selectors.DefaultSelector()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            selector.register(self.stdout.fileno(), selectors.EVENT_READ)
            pending_text = self._drain_chunks(selector, decoder)
            pending_text += decoder.decode(b"", final=True)
            if pending_text:
                self._emit_line(pending_text)
        except BaseException as error:
            self._record_failure(error)
        finally:
            selector.close()

    def _drain_chunks(
        self,
        selector: selectors.BaseSelector,
        decoder: codecs.IncrementalDecoder,
    ) -> str:
        pending_text = ""
        remaining_final_bytes = self.policy.final_drain_byte_limit
        while True:
            output_read = self._read_next_chunk(selector, remaining_final_bytes)
            if type(output_read) is _OutputReadPending:
                continue
            if type(output_read) is _OutputReadFinished:
                return pending_text
            if type(output_read) is not _OutputReadChunk:
                raise AssertionError("output read is a closed union")
            self._record_byte_count(len(output_read.data))
            pending_text = self._consume_decoded_text(
                pending_text + decoder.decode(output_read.data)
            )
            remaining_final_bytes = output_read.remaining_final_bytes

    def _read_next_chunk(
        self,
        selector: selectors.BaseSelector,
        remaining_final_bytes: int,
    ) -> _OutputRead:
        stopping = self._stop_requested.is_set()
        if stopping and remaining_final_bytes == 0:
            return _OutputReadFinished()
        selected = selector.select(
            timeout=0.0 if stopping else self.policy.poll_interval_seconds
        )
        if not selected:
            return _OutputReadFinished() if stopping else _OutputReadPending()
        read_size = min(65_536, remaining_final_bytes) if stopping else 65_536
        chunk = os.read(self.stdout.fileno(), read_size)
        if not chunk:
            return _OutputReadFinished()
        return _OutputReadChunk(
            chunk,
            remaining_final_bytes - len(chunk) if stopping else remaining_final_bytes,
        )

    def _record_byte_count(self, byte_count: int) -> None:
        with self._sink_lock:
            if self._accepting_output:
                self._byte_count += byte_count

    def _consume_decoded_text(self, text: str) -> str:
        while True:
            newline = text.find("\n")
            if newline < 0:
                return text
            self._emit_line(text[: newline + 1])
            text = text[newline + 1 :]

    def _emit_line(self, line: str) -> None:
        self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        # Never hold the detachment lock across arbitrary caller code.  A sink
        # may block forever; containment still has to return at its declared
        # shutdown bound and atomically prevent any later callback from
        # starting.
        with self._sink_lock:
            if not self._accepting_output:
                return
            output = self.output
            line_observer = self.line_observer
            self._line_count += 1
        try:
            output.write_line(line)
            with self._sink_lock:
                if not self._accepting_output or self._failure is not None:
                    return
            line_observer.observe_line(line)
        except BaseException as error:
            self._record_failure(error)

    def _record_failure(self, error: BaseException) -> None:
        with self._sink_lock:
            if not self._accepting_output or self._failure is not None:
                return
            self._failure = ContainedCommandFailure(error)
        self._interruption.set()


class PosixContainedCommandCapture:
    """Own Popen, the output pump, group containment, and terminal classification."""

    def __init__(
        self,
        process_group_supervisor: ProcessGroupSupervisor,
        output_policy: ContainedCommandOutputPolicy,
    ) -> None:
        if not isinstance(process_group_supervisor, ProcessGroupSupervisor):
            raise ValueError(
                "PosixContainedCommandCapture.process_group_supervisor must "
                "implement ProcessGroupSupervisor"
            )
        self._process_group_supervisor = process_group_supervisor
        if type(output_policy) is not ContainedCommandOutputPolicy:
            raise ValueError(
                "PosixContainedCommandCapture.output_policy must be "
                "ContainedCommandOutputPolicy"
            )
        self._output_policy = output_policy

    def capture(
        self,
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        """Return a closed typed result without leaking operational exceptions."""
        self._require_capture_request(command, output, line_observer)
        try:
            process = self._spawn(command)
        except BaseException as error:
            return self._not_started(error)
        return self._capture_started_process(process, output, line_observer)

    @staticmethod
    def _require_capture_request(
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> None:
        if type(command) is not ContainedShellCommand:
            raise ValueError(
                "PosixContainedCommandCapture.capture requires ContainedShellCommand"
            )
        if not isinstance(output, ContainedCommandOutput):
            raise ValueError(
                "PosixContainedCommandCapture.capture requires ContainedCommandOutput"
            )
        if not isinstance(line_observer, ContainedCommandLineObserver):
            raise ValueError(
                "PosixContainedCommandCapture.capture requires "
                "ContainedCommandLineObserver"
            )

    @staticmethod
    def _spawn(command: ContainedShellCommand) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            command.command,
            shell=True,
            cwd=command.working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
            start_new_session=True,
        )

    @staticmethod
    def _not_started(error: BaseException) -> ContainedCommandCaptureFailed:
        return ContainedCommandCaptureFailed(
            child=ContainedCommandNotStarted(),
            cleanup=ContainedCommandCleanupNotStarted(),
            failure=ContainedCommandFailure(error),
            metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
        )

    def _capture_started_process(
        self,
        process: subprocess.Popen[bytes],
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        leader = OwnedProcessGroupLeader(process.pid)
        try:
            output.child_started(ContainedCommandStarted(process.pid))
        except BaseException as error:
            return self._abort_without_pump(
                process,
                leader,
                ContainedCommandFailure(error),
            )

        if process.stdout is None:
            return self._abort_without_pump(
                process,
                leader,
                ContainedCommandFailure(
                    RuntimeError("contained command did not expose stdout")
                ),
            )

        try:
            pump = _CapturedOutputPump(
                cast(BinaryIO, process.stdout),
                output,
                line_observer,
                self._output_policy,
            )
            pump.start()
        except BaseException as error:
            return self._abort_without_pump(
                process,
                leader,
                ContainedCommandFailure(error),
            )
        try:
            supervision = self._process_group_supervisor.supervise(
                leader,
                ProcessGroupUnboundedWait(),
                pump,
            )
        except BaseException as supervision_error:
            return self._recover_after_supervision_failure(
                process,
                leader,
                pump,
                ContainedCommandFailure(supervision_error),
            )

        process.returncode = supervision.termination.leader_exit_code
        finalization = pump.finalize_after_containment()
        child = ContainedCommandExited(process.pid, process.returncode)
        return self._closed_result_after_supervision(
            supervision,
            child,
            pump,
            finalization,
        )

    @staticmethod
    def _closed_result_after_supervision(
        supervision: ProcessGroupSupervision,
        child: ContainedCommandExited,
        pump: _CapturedOutputPump,
        finalization: _OutputPumpFinalization,
    ) -> ContainedCommandResult:
        """Interpret terminal supervision and pump evidence after containment."""
        if type(supervision) is ProcessGroupInterrupted:
            if pump.failure is None:
                raise AssertionError(
                    "process-group interruption requires captured failure evidence"
                )
            failure = pump.failure
            if type(finalization) is _OutputPumpFinalizationFailed:
                failure = _combine_failures(
                    failure,
                    finalization.failure,
                    "output capture and pump finalization both failed",
                )
            return ContainedCommandCaptureFailed(
                child=child,
                cleanup=ContainedCommandCaptureAborted(),
                failure=failure,
                metrics=pump.metrics,
            )
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("an unbounded contained command cannot time out")
        failure = pump.failure
        if type(finalization) is _OutputPumpFinalizationFailed:
            failure = (
                finalization.failure
                if failure is None
                else _combine_failures(
                    failure,
                    finalization.failure,
                    "output capture and pump finalization both failed",
                )
            )
        if failure is not None:
            return ContainedCommandCaptureFailed(
                child=child,
                cleanup=ContainedCommandSupervised(),
                failure=failure,
                metrics=pump.metrics,
            )
        return ContainedCommandCompleted(child=child, metrics=pump.metrics)

    def _abort_without_pump(
        self,
        process: subprocess.Popen[bytes],
        leader: OwnedProcessGroupLeader,
        capture_failure: ContainedCommandFailure,
    ) -> ContainedCommandResult:
        try:
            termination = self._process_group_supervisor.abort(leader)
        except BaseException as cleanup_error:
            stdout_close_failure = self._close_unpumped_stdout(process)
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.pid),
                capture=ContainedCommandCaptureInterrupted(capture_failure),
                cleanup_failure=_combine_failures(
                    ContainedCommandFailure(cleanup_error),
                    stdout_close_failure,
                    "contained command group abort and stdout close both failed",
                ),
                metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
            )
        process.returncode = termination.leader_exit_code
        stdout_close_failure = self._close_unpumped_stdout(process)
        return ContainedCommandCaptureFailed(
            child=ContainedCommandExited(process.pid, process.returncode),
            cleanup=ContainedCommandCaptureAborted(),
            failure=_combine_failures(
                capture_failure,
                stdout_close_failure,
                "contained command capture and stdout close both failed",
            ),
            metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
        )

    @staticmethod
    def _close_unpumped_stdout(
        process: subprocess.Popen[bytes],
    ) -> ContainedCommandFailure | None:
        stdout = process.stdout
        if stdout is None:
            return None
        try:
            stdout.close()
        except BaseException as error:
            return ContainedCommandFailure(error)
        return None

    def _recover_after_supervision_failure(
        self,
        process: subprocess.Popen[bytes],
        leader: OwnedProcessGroupLeader,
        pump: _CapturedOutputPump,
        supervision_failure: ContainedCommandFailure,
    ) -> ContainedCommandResult:
        if pump.failure is None:
            failure = supervision_failure
        else:
            failure = _combine_failures(
                pump.failure,
                supervision_failure,
                "output capture and command supervision both failed",
            )
        try:
            termination = self._process_group_supervisor.abort(leader)
        except BaseException as cleanup_error:
            cleanup_failure = ContainedCommandFailure(cleanup_error)
            detachment = pump.detach_after_cleanup_failure()
            if type(detachment) is _OutputPumpDetachmentFailed:
                cleanup_failure = _combine_failures(
                    cleanup_failure,
                    detachment.failure,
                    "command group abort and output sink detach both failed",
                )
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.pid),
                capture=ContainedCommandCaptureInterrupted(failure),
                cleanup_failure=cleanup_failure,
                metrics=pump.metrics,
            )
        process.returncode = termination.leader_exit_code
        finalization = pump.finalize_after_containment()
        if type(finalization) is _OutputPumpFinalizationFailed:
            failure = _combine_failures(
                failure,
                finalization.failure,
                "command supervision and pump finalization both failed",
            )
        return ContainedCommandCaptureFailed(
            child=ContainedCommandExited(process.pid, process.returncode),
            cleanup=ContainedCommandCaptureAborted(),
            failure=failure,
            metrics=pump.metrics,
        )
