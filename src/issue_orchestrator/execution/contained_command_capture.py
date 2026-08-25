"""POSIX deep owner for streamed command capture and group containment."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import TextIO, cast

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


@dataclass(slots=True)
class _CapturedOutputPump(ProcessGroupInterruption):
    """Drain output while exposing the first observer failure as interruption."""

    stdout: TextIO
    output: ContainedCommandOutput
    line_observer: ContainedCommandLineObserver
    _interruption: threading.Event = field(default_factory=threading.Event, init=False)
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
        try:
            self._thread.join()
        except BaseException as error:
            failure = ContainedCommandFailure(error)
            try:
                self.detach_after_cleanup_failure()
            except BaseException as detach_error:
                failure = _combine_failures(
                    failure,
                    ContainedCommandFailure(detach_error),
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

    def detach_after_cleanup_failure(self) -> None:
        """Prevent a still-blocked daemon pump from touching caller-owned sinks."""
        with self._sink_lock:
            self._accepting_output = False

    @property
    def failure(self) -> ContainedCommandFailure | None:
        return self._failure

    @property
    def metrics(self) -> ContainedCommandMetrics:
        return ContainedCommandMetrics(
            line_count=self._line_count,
            byte_count=self._byte_count,
        )

    def _drain(self) -> None:
        try:
            for line in self.stdout:
                self._line_count += 1
                self._byte_count += len(line.encode("utf-8", errors="replace"))
                self._consume_line(line)
        except BaseException as error:
            self._record_failure(error)

    def _consume_line(self, line: str) -> None:
        with self._sink_lock:
            if not self._accepting_output:
                return
            try:
                self.output.write_line(line)
                if self._failure is None:
                    self.line_observer.observe_line(line)
            except BaseException as error:
                self._record_failure(error)

    def _record_failure(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = ContainedCommandFailure(error)
            self._interruption.set()


class PosixContainedCommandCapture:
    """Own Popen, the output pump, group containment, and terminal classification."""

    def __init__(self, process_group_supervisor: ProcessGroupSupervisor) -> None:
        if not isinstance(process_group_supervisor, ProcessGroupSupervisor):
            raise ValueError(
                "PosixContainedCommandCapture.process_group_supervisor must "
                "implement ProcessGroupSupervisor"
            )
        self._process_group_supervisor = process_group_supervisor

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
    def _spawn(command: ContainedShellCommand) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command.command,
            shell=True,
            cwd=command.working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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
        process: subprocess.Popen[str],
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
                cast(TextIO, process.stdout),
                output,
                line_observer,
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
        process: subprocess.Popen[str],
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
        process: subprocess.Popen[str],
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
        process: subprocess.Popen[str],
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
            pump.detach_after_cleanup_failure()
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.pid),
                capture=ContainedCommandCaptureInterrupted(failure),
                cleanup_failure=ContainedCommandFailure(cleanup_error),
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
