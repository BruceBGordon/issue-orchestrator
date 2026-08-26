"""POSIX deep owner for streamed command capture and group containment."""

from __future__ import annotations

import codecs
import os
import selectors
import signal
import tempfile
from dataclasses import dataclass, field
from typing import TextIO, cast

from ..domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCapture,
    ContainedCommandCaptureFailed,
    ContainedCommandCaptureInterrupted,
    ContainedCommandCaptureSucceeded,
    ContainedCommandCleanupFailed,
    ContainedCommandCleanupNotStarted,
    ContainedCommandCompleted,
    ContainedCommandExited,
    ContainedCommandExitUnknown,
    ContainedCommandFailure,
    ContainedCommandFinalizationFailed,
    ContainedCommandMetrics,
    ContainedCommandNotStarted,
    ContainedCommandOutputPolicy,
    ContainedCommandOutputPipeClosed,
    ContainedCommandOutputPipeCloseFailed,
    ContainedCommandResult,
    ContainedCommandStarted,
    ContainedCommandSupervised,
)
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupTermination,
    ProcessGroupUnboundedWait,
)
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..ports.contained_command import (
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedCommandOutputPipe,
    ContainedCommandOutputPipeFactory,
    ContainedCommandOutputReader,
    ContainedShellCommand,
)
from ..ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from ..ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLauncher,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from .independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
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


def _combine_base_errors(
    message: str,
    primary: BaseException,
    secondary: BaseException | None,
) -> BaseException:
    if secondary is None:
        return primary
    return BaseExceptionGroup(message, (primary, secondary))


class OsContainedCommandOutputPipe:
    """Incrementally own the one parent/child output pipe."""

    def __init__(self) -> None:
        self._read_descriptor, self._write_descriptor = os.pipe()

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return (
            PosixDescriptorMapping(self._write_descriptor, 1),
            PosixDescriptorMapping(self._write_descriptor, 2),
        )

    def open_reader_after_launch(self) -> ContainedCommandOutputReader:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
        )
        reader: ContainedCommandOutputReader | None = None
        try:
            os.close(self._write_descriptor)
            self._write_descriptor = -1
            reader = os.fdopen(self._read_descriptor, "rb", buffering=0)
            self._read_descriptor = -1
        except BaseException as transfer_error:
            restoration_error = _restore_signal_mask(previous_mask)
            reader_close_error = _close_reader(reader)
            raise _combine_many_errors(
                "output reader transfer failed",
                transfer_error,
                restoration_error,
                reader_close_error,
            )
        restoration_error = _restore_signal_mask(previous_mask)
        if restoration_error is not None:
            reader_close_error = _close_reader(reader)
            raise _combine_many_errors(
                "output reader signal restoration failed",
                restoration_error,
                reader_close_error,
            )
        return reader

    def close(
        self,
    ) -> ContainedCommandOutputPipeClosed | ContainedCommandOutputPipeCloseFailed:
        actions = tuple(
            CleanupAction(
                f"captured-command-fd-{descriptor}-close",
                lambda fd=descriptor: os.close(fd),
            )
            for descriptor in (self._read_descriptor, self._write_descriptor)
            if descriptor >= 0
        )
        self._read_descriptor = -1
        self._write_descriptor = -1
        outcome = IndependentCleanupPlan(actions).run()
        if type(outcome) is CleanupSucceeded:
            return ContainedCommandOutputPipeClosed()
        if type(outcome) is not CleanupFailed:
            raise AssertionError("cleanup outcome is a closed union")
        errors = tuple(failure.error for failure in outcome.failures)
        if len(errors) == 1:
            return ContainedCommandOutputPipeCloseFailed(errors[0])
        return ContainedCommandOutputPipeCloseFailed(
            BaseExceptionGroup("captured command pipe cleanup failed", errors)
        )


class OsContainedCommandOutputPipeFactory:
    """Acquire the production kernel pipe behind its typed port."""

    def create(self) -> ContainedCommandOutputPipe:
        return OsContainedCommandOutputPipe()


def _pipe_close_error(pipe: ContainedCommandOutputPipe) -> BaseException | None:
    try:
        outcome = pipe.close()
    except BaseException as error:
        return error
    if type(outcome) is ContainedCommandOutputPipeClosed:
        return None
    if type(outcome) is not ContainedCommandOutputPipeCloseFailed:
        raise AssertionError("output pipe close result is a closed union")
    return outcome.error


def _restore_signal_mask(
    previous_mask: set[int | signal.Signals],
) -> BaseException | None:
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    except BaseException as error:
        return error
    return None


def _close_reader(reader: ContainedCommandOutputReader | None) -> BaseException | None:
    if reader is None:
        return None
    try:
        reader.close()
    except BaseException as error:
        return error
    return None


def _combine_many_errors(
    message: str,
    first: BaseException,
    *others: BaseException | None,
) -> BaseException:
    errors = (first, *(error for error in others if error is not None))
    if len(errors) == 1:
        return first
    return BaseExceptionGroup(message, errors)


@dataclass(frozen=True, slots=True)
class _CapturedCommandActivated:
    process: PosixProcessHandle
    stdout: ContainedCommandOutputReader

    def __post_init__(self) -> None:
        if not isinstance(self.process, PosixProcessHandle):
            raise ValueError("captured command process must implement its port")
        if not hasattr(self.stdout, "fileno"):
            raise ValueError("captured command stdout must expose fileno")


@dataclass(frozen=True, slots=True)
class _CapturedCommandActivationClosed:
    result: ContainedCommandResult


_CapturedCommandActivation = (
    _CapturedCommandActivated | _CapturedCommandActivationClosed
)


@dataclass(frozen=True, slots=True)
class _OutputPumpFinalized:
    """The pump stopped and its stdout stream closed."""


@dataclass(frozen=True, slots=True)
class _OutputPumpFinalizationFailed:
    """Pump stopped, but finalization or owned-resource closure failed."""

    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        if type(self.failure) is not ContainedCommandFailure:
            raise ValueError(
                "_OutputPumpFinalizationFailed.failure must be a "
                "ContainedCommandFailure"
            )


_OutputPumpFinalization = _OutputPumpFinalized | _OutputPumpFinalizationFailed


class _CapturedOutputJournal:
    """Own output between synchronous collection and caller publication."""

    def __init__(self) -> None:
        self._stream = cast(
            TextIO,
            tempfile.SpooledTemporaryFile(
                max_size=1_048_576,
                mode="w+t",
                encoding="utf-8",
                newline="",
            ),
        )
        self._closed = False

    def append(self, line: str) -> None:
        if self._closed:
            raise RuntimeError("captured output journal is closed")
        self._stream.write(line)

    def publish_and_close(
        self,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandFailure | None:
        """Invoke caller code synchronously, then close this owned journal."""
        failure: ContainedCommandFailure | None = None
        try:
            self._stream.seek(0)
            for line in self._stream:
                output.write_line(line)
                line_observer.observe_line(line)
        except BaseException as error:
            failure = ContainedCommandFailure(error)
        close_failure = self.close()
        if close_failure is None:
            return failure
        if failure is None:
            return close_failure
        return _combine_failures(
            failure,
            close_failure,
            "output publication and journal close both failed",
        )

    def close(self) -> ContainedCommandFailure | None:
        if self._closed:
            return None
        self._closed = True
        try:
            self._stream.close()
        except BaseException as error:
            return ContainedCommandFailure(error)
        return None


@dataclass(slots=True)
class _CapturedOutputPump(ProcessGroupInterruption):
    """Synchronously drain output from the supervisor's polling loop."""

    stdout: ContainedCommandOutputReader
    policy: ContainedCommandOutputPolicy
    _journal: _CapturedOutputJournal = field(init=False)
    _selector: selectors.BaseSelector = field(init=False)
    _decoder: codecs.IncrementalDecoder = field(init=False)
    _failure: ContainedCommandFailure | None = field(default=None, init=False)
    _pending_text: str = field(default="", init=False)
    _finished: bool = field(default=False, init=False)
    _line_count: int = field(default=0, init=False)
    _byte_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if type(self.policy) is not ContainedCommandOutputPolicy:
            raise ValueError(
                "_CapturedOutputPump.policy must be ContainedCommandOutputPolicy"
            )
        self._journal = _CapturedOutputJournal()
        self._selector = selectors.DefaultSelector()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            self._selector.register(self.stdout.fileno(), selectors.EVENT_READ)
        except BaseException as setup_error:
            failures: list[BaseException] = [setup_error]
            for close in (self._selector.close, self.stdout.close):
                try:
                    close()
                except BaseException as cleanup_error:
                    failures.append(cleanup_error)
            journal_failure = self._journal.close()
            if journal_failure is not None:
                failures.append(journal_failure.error)
            if len(failures) == 1:
                raise setup_error
            raise BaseExceptionGroup(
                "output collector setup and cleanup both failed",
                failures,
            )

    def wait_for_request(self, timeout_seconds: float) -> bool:
        if type(timeout_seconds) is not float or timeout_seconds <= 0.0:
            raise ValueError("output interruption wait must be a positive float")
        if self._failure is not None:
            return True
        try:
            self._consume_one_read(timeout_seconds, 65_536)
        except BaseException as error:
            self._record_failure(error)
        return self._failure is not None

    def finalize_after_containment(self) -> _OutputPumpFinalization:
        finalization_failure: ContainedCommandFailure | None = None
        remaining_bytes = self.policy.final_drain_byte_limit
        try:
            while not self._finished and remaining_bytes > 0:
                consumed = self._consume_one_read(0.0, remaining_bytes)
                if consumed == 0:
                    break
                remaining_bytes -= consumed
            if not self._finished and remaining_bytes == 0:
                finalization_failure = ContainedCommandFailure(
                    RuntimeError(
                        "contained command final output drain exceeded "
                        f"{self.policy.final_drain_byte_limit} bytes"
                    )
                )
        except BaseException as error:
            finalization_failure = ContainedCommandFailure(error)
        close_failure = self._close_capture_resources()
        if finalization_failure is None and close_failure is None:
            return _OutputPumpFinalized()
        failure = finalization_failure
        if failure is None:
            if close_failure is None:
                raise AssertionError("output finalization failure must exist")
            failure = close_failure
        else:
            failure = _combine_failures(
                failure,
                close_failure,
                "output drain and resource close both failed",
            )
        return _OutputPumpFinalizationFailed(failure)

    def stop_without_waiting(
        self,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandFailure | None:
        """Close collectors and publish retained output after failed containment."""
        close_failure = self._close_capture_resources()
        publication_failure = self._journal.publish_and_close(output, line_observer)
        if close_failure is None:
            return publication_failure
        return _combine_failures(
            close_failure,
            publication_failure,
            "collector close and retained-output publication both failed",
        )

    def publish_after_finalization(
        self,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandFailure | None:
        """Publish only after synchronous collection has reached containment."""
        return self._journal.publish_and_close(output, line_observer)

    @property
    def failure(self) -> ContainedCommandFailure | None:
        return self._failure

    @property
    def metrics(self) -> ContainedCommandMetrics:
        return ContainedCommandMetrics(
            line_count=self._line_count,
            byte_count=self._byte_count,
        )

    def _consume_one_read(self, timeout_seconds: float, byte_limit: int) -> int:
        if self._finished:
            return 0
        selected = self._selector.select(timeout_seconds)
        if not selected:
            return 0
        read_size = min(65_536, byte_limit)
        chunk = os.read(self.stdout.fileno(), read_size)
        if not chunk:
            self._finished = True
            self._pending_text += self._decoder.decode(b"", final=True)
            if self._pending_text:
                self._emit_line(self._pending_text)
                self._pending_text = ""
            return 0
        self._byte_count += len(chunk)
        self._pending_text = self._consume_decoded_text(
            self._pending_text + self._decoder.decode(chunk)
        )
        return len(chunk)

    def _consume_decoded_text(self, text: str) -> str:
        while True:
            newline = text.find("\n")
            if newline < 0:
                return text
            self._emit_line(text[: newline + 1])
            text = text[newline + 1 :]

    def _emit_line(self, line: str) -> None:
        if self._failure is not None:
            return
        try:
            self._journal.append(line)
        except BaseException as error:
            self._record_failure(error)
            return
        self._line_count += 1

    def _record_failure(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = ContainedCommandFailure(error)

    def _close_capture_resources(self) -> ContainedCommandFailure | None:
        failures: list[BaseException] = []
        for close in (self._selector.close, self.stdout.close):
            try:
                close()
            except BaseException as error:
                failures.append(error)
        if not failures:
            return None
        if len(failures) == 1:
            return ContainedCommandFailure(failures[0])
        return ContainedCommandFailure(
            BaseExceptionGroup("output collector resource close failed", failures)
        )


@dataclass(frozen=True, slots=True)
class _CapturedCommandCaptureClosed:
    """Capture and caller publication completed without failure."""


@dataclass(frozen=True, slots=True)
class _CapturedCommandCaptureCloseFailed:
    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        if type(self.failure) is not ContainedCommandFailure:
            raise ValueError("capture close failure must be typed")


_CapturedCommandCaptureClosure = (
    _CapturedCommandCaptureClosed | _CapturedCommandCaptureCloseFailed
)


@dataclass(frozen=True, slots=True)
class _ContainedCommandLifecycleClosed:
    """Retained-handle and courtesy-shutdown evidence completed."""


@dataclass(frozen=True, slots=True)
class _ContainedCommandLifecycleCloseFailed:
    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        if type(self.failure) is not ContainedCommandFailure:
            raise ValueError("contained-command lifecycle failure must be typed")


_ContainedCommandLifecycleClosure = (
    _ContainedCommandLifecycleClosed | _ContainedCommandLifecycleCloseFailed
)


@dataclass(frozen=True, slots=True)
class _ContainedCommandPostContainmentEvidence:
    capture: _CapturedCommandCaptureClosure
    lifecycle: _ContainedCommandLifecycleClosure
    metrics: ContainedCommandMetrics

    def __post_init__(self) -> None:
        if type(self.capture) not in (
            _CapturedCommandCaptureClosed,
            _CapturedCommandCaptureCloseFailed,
        ):
            raise ValueError("post-containment capture evidence must be typed")
        if type(self.lifecycle) not in (
            _ContainedCommandLifecycleClosed,
            _ContainedCommandLifecycleCloseFailed,
        ):
            raise ValueError("post-containment lifecycle evidence must be typed")
        if type(self.metrics) is not ContainedCommandMetrics:
            raise ValueError("post-containment metrics must be typed")


class _ContainedCommandPostContainmentOwner:
    """Independently close capture, publication, and retained process evidence."""

    def __init__(
        self,
        process: PosixProcessHandle,
        pump: _CapturedOutputPump,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> None:
        if not isinstance(process, PosixProcessHandle):
            raise ValueError("post-containment process must implement its port")
        if type(pump) is not _CapturedOutputPump:
            raise ValueError("post-containment pump must be typed")
        if not isinstance(output, ContainedCommandOutput):
            raise ValueError("post-containment output must implement its port")
        if not isinstance(line_observer, ContainedCommandLineObserver):
            raise ValueError("post-containment observer must implement its port")
        self._process = process
        self._pump = pump
        self._output = output
        self._line_observer = line_observer

    def close(
        self,
        termination: ProcessGroupTermination,
        preceding_capture_failures: tuple[ContainedCommandFailure, ...],
    ) -> _ContainedCommandPostContainmentEvidence:
        if type(termination) is not ProcessGroupTermination:
            raise ValueError("post-containment termination must be typed")
        if any(
            type(failure) is not ContainedCommandFailure
            for failure in preceding_capture_failures
        ):
            raise ValueError("preceding capture failures must all be typed")
        return _ContainedCommandPostContainmentEvidence(
            capture=self._close_capture(preceding_capture_failures),
            lifecycle=self._close_lifecycle(termination),
            metrics=self._pump.metrics,
        )

    def _close_capture(
        self,
        preceding_failures: tuple[ContainedCommandFailure, ...],
    ) -> _CapturedCommandCaptureClosure:
        capture_failures = list(preceding_failures)
        pump_failure = self._pump.failure
        if pump_failure is not None:
            capture_failures.append(pump_failure)
        finalization_failure = self._finalize_pump()
        if finalization_failure is not None:
            capture_failures.append(finalization_failure)
        publication_failure = self._publish_output()
        if publication_failure is not None:
            capture_failures.append(publication_failure)
        return _capture_closure(tuple(capture_failures))

    def _finalize_pump(self) -> ContainedCommandFailure | None:
        try:
            finalization = self._pump.finalize_after_containment()
        except BaseException as error:
            return ContainedCommandFailure(error)
        if type(finalization) is _OutputPumpFinalizationFailed:
            return finalization.failure
        if type(finalization) is _OutputPumpFinalized:
            return None
        raise AssertionError("output pump finalization is a closed union")

    def _publish_output(self) -> ContainedCommandFailure | None:
        try:
            return self._pump.publish_after_finalization(
                self._output,
                self._line_observer,
            )
        except BaseException as error:
            return ContainedCommandFailure(error)

    def _close_lifecycle(
        self,
        termination: ProcessGroupTermination,
    ) -> _ContainedCommandLifecycleClosure:
        lifecycle_failures: list[ContainedCommandFailure] = []
        try:
            self._process.record_external_reap(termination.leader_exit_code)
        except BaseException as error:
            lifecycle_failures.append(ContainedCommandFailure(error))
        courtesy_failure = termination.courtesy_failure()
        if courtesy_failure is not None:
            lifecycle_failures.append(ContainedCommandFailure(courtesy_failure.error))
        return _lifecycle_closure(tuple(lifecycle_failures))


def _capture_closure(
    failures: tuple[ContainedCommandFailure, ...],
) -> _CapturedCommandCaptureClosure:
    if not failures:
        return _CapturedCommandCaptureClosed()
    return _CapturedCommandCaptureCloseFailed(
        _combine_failure_sequence(
            failures,
            "contained command capture failed more than once",
        )
    )


def _lifecycle_closure(
    failures: tuple[ContainedCommandFailure, ...],
) -> _ContainedCommandLifecycleClosure:
    if not failures:
        return _ContainedCommandLifecycleClosed()
    return _ContainedCommandLifecycleCloseFailed(
        _combine_failure_sequence(
            failures,
            "contained command post-containment finalization failed more than once",
        )
    )


def _combine_failure_sequence(
    failures: tuple[ContainedCommandFailure, ...],
    message: str,
) -> ContainedCommandFailure:
    if not failures:
        raise ValueError("cannot combine an empty failure sequence")
    if len(failures) == 1:
        return failures[0]
    return ContainedCommandFailure(
        BaseExceptionGroup(message, tuple(failure.error for failure in failures))
    )


class PosixContainedCommandCapture:
    """Own Popen, the output pump, group containment, and terminal classification."""

    def __init__(
        self,
        process_launcher: PosixProcessLauncher,
        process_group_supervisor: ProcessGroupSupervisor,
        output_policy: ContainedCommandOutputPolicy,
        output_pipe_factory: ContainedCommandOutputPipeFactory,
    ) -> None:
        if not isinstance(process_launcher, PosixProcessLauncher):
            raise ValueError(
                "PosixContainedCommandCapture.process_launcher must implement "
                "PosixProcessLauncher"
            )
        self._process_launcher = process_launcher
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
        if not isinstance(output_pipe_factory, ContainedCommandOutputPipeFactory):
            raise ValueError(
                "PosixContainedCommandCapture.output_pipe_factory must implement "
                "ContainedCommandOutputPipeFactory"
            )
        self._output_pipe_factory = output_pipe_factory

    def capture(
        self,
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        """Return a closed typed result without leaking operational exceptions."""
        self._require_capture_request(command, output, line_observer)
        try:
            activation = self._activate(command)
        except BaseException as error:
            return self._not_started(error)
        if type(activation) is _CapturedCommandActivationClosed:
            return activation.result
        if type(activation) is not _CapturedCommandActivated:
            raise AssertionError("captured command activation is a closed union")
        return self._capture_started_process(activation, output, line_observer)

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

    def _activate(self, command: ContainedShellCommand) -> _CapturedCommandActivation:
        pipes = self._output_pipe_factory.create()
        try:
            launch = self._process_launcher.launch(
                PosixProcessLaunchSpec(
                    program=PosixProcessProgram(("/bin/sh", "-c", command.command)),
                    working_directory=command.working_directory,
                    environment=PosixProcessEnvironment.from_mapping(os.environ),
                    group_mode=PosixProcessGroupMode.NEW_SESSION,
                    descriptor_mappings=pipes.descriptor_mappings,
                    terminal=PosixProcessWithoutTerminal(),
                )
            )
        except BaseException as activation_error:
            return _CapturedCommandActivationClosed(
                self._not_started(
                    _combine_base_errors(
                        "captured command pre-launch setup and pipe cleanup failed",
                        activation_error,
                        _pipe_close_error(pipes),
                    )
                )
            )
        if type(launch) is PosixProcessLaunchRejected:
            cleanup_error = _pipe_close_error(pipes)
            return _CapturedCommandActivationClosed(
                self._not_started(
                    _combine_base_errors(
                        "captured command activation and pipe cleanup both failed",
                        launch.error,
                        cleanup_error,
                    )
                )
            )
        if type(launch) is PosixProcessExecRejected:
            return _CapturedCommandActivationClosed(
                self._not_started(
                    _combine_base_errors(
                        "captured command exec and pipe cleanup both failed",
                        launch.as_error(),
                        _pipe_close_error(pipes),
                    )
                )
            )
        if type(launch) is PosixProcessLaunchRecovered:
            cleanup_error = _pipe_close_error(pipes)
            return _CapturedCommandActivationClosed(
                ContainedCommandCaptureFailed(
                    child=ContainedCommandExited(
                        launch.process_id,
                        launch.exit_code,
                    ),
                    cleanup=ContainedCommandCaptureAborted(),
                    failure=ContainedCommandFailure(
                        _combine_base_errors(
                            "captured command activation and pipe cleanup both failed",
                            launch.activation_error,
                            cleanup_error,
                        )
                    ),
                    metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
                )
            )
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            cleanup_error = _pipe_close_error(pipes)
            return _CapturedCommandActivationClosed(
                ContainedCommandCleanupFailed(
                    child=ContainedCommandExitUnknown(launch.process_id),
                    capture=ContainedCommandCaptureInterrupted(
                        ContainedCommandFailure(launch.activation_error)
                    ),
                    cleanup_failure=ContainedCommandFailure(
                        _combine_base_errors(
                            "captured command recovery and pipe cleanup both failed",
                            launch.recovery_error,
                            cleanup_error,
                        )
                    ),
                    metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
                )
            )
        if type(launch) is not PosixProcessLaunchStarted:
            raise AssertionError("POSIX process launch is a closed union")
        return self._finish_activation(
            launch.process,
            pipes,
        )

    def _finish_activation(
        self,
        process: PosixProcessHandle,
        pipes: ContainedCommandOutputPipe,
    ) -> _CapturedCommandActivation:
        try:
            stdout = pipes.open_reader_after_launch()
        except BaseException as setup_error:
            return _CapturedCommandActivationClosed(
                self._abort_after_activation_setup_failure(
                    process,
                    pipes,
                    setup_error,
                )
            )
        return _CapturedCommandActivated(process, stdout)

    def _abort_after_activation_setup_failure(
        self,
        process: PosixProcessHandle,
        pipes: ContainedCommandOutputPipe,
        setup_error: BaseException,
    ) -> ContainedCommandResult:
        descriptor_error = _pipe_close_error(pipes)
        capture_error = _combine_base_errors(
            "captured command setup and descriptor cleanup both failed",
            setup_error,
            descriptor_error,
        )
        try:
            termination = self._process_group_supervisor.abort(
                OwnedProcessGroupLeader(process.process_id)
            )
        except BaseException as recovery_error:
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.process_id),
                capture=ContainedCommandCaptureInterrupted(
                    ContainedCommandFailure(capture_error)
                ),
                cleanup_failure=ContainedCommandFailure(recovery_error),
                metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
            )
        child = ContainedCommandExited(
            process.process_id,
            termination.leader_exit_code,
        )
        evidence = _ContainedCommandPostContainmentEvidence(
            capture=_capture_closure((ContainedCommandFailure(capture_error),)),
            lifecycle=_record_reap_and_courtesy(process, termination),
            metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
        )
        return self._closed_result_after_containment(
            child,
            ContainedCommandCaptureAborted(),
            evidence,
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
        started: _CapturedCommandActivated,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        process = started.process
        leader = OwnedProcessGroupLeader(process.process_id)
        try:
            output.child_started(ContainedCommandStarted(process.process_id))
        except BaseException as error:
            return self._abort_without_pump(
                process,
                started.stdout,
                leader,
                ContainedCommandFailure(error),
            )

        try:
            pump = _CapturedOutputPump(
                started.stdout,
                self._output_policy,
            )
        except BaseException as error:
            return self._abort_without_pump(
                process,
                started.stdout,
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
                output,
                line_observer,
                ContainedCommandFailure(supervision_error),
            )

        child = ContainedCommandExited(
            process.process_id,
            supervision.termination.leader_exit_code,
        )
        evidence = _ContainedCommandPostContainmentOwner(
            process,
            pump,
            output,
            line_observer,
        ).close(supervision.termination, ())
        if type(supervision) is ProcessGroupInterrupted:
            cleanup = ContainedCommandCaptureAborted()
        elif type(supervision) is ProcessGroupCompleted:
            cleanup = ContainedCommandSupervised()
        else:
            raise AssertionError("an unbounded contained command cannot time out")
        return self._closed_result_after_containment(
            child,
            cleanup,
            evidence,
        )

    @staticmethod
    def _closed_result_after_containment(
        child: ContainedCommandExited,
        cleanup: ContainedCommandSupervised | ContainedCommandCaptureAborted,
        evidence: _ContainedCommandPostContainmentEvidence,
    ) -> ContainedCommandResult:
        """Interpret independent capture and lifecycle evidence after containment."""
        capture = evidence.capture
        lifecycle = evidence.lifecycle
        if type(cleanup) is ContainedCommandCaptureAborted:
            if type(capture) is not _CapturedCommandCaptureCloseFailed:
                raise AssertionError(
                    "process-group interruption requires captured failure evidence"
                )
            capture_fact: ContainedCommandCapture = ContainedCommandCaptureInterrupted(
                capture.failure
            )
        elif type(cleanup) is ContainedCommandSupervised:
            if type(capture) is _CapturedCommandCaptureClosed:
                capture_fact = ContainedCommandCaptureSucceeded()
            elif type(capture) is _CapturedCommandCaptureCloseFailed:
                capture_fact = ContainedCommandCaptureInterrupted(capture.failure)
            else:
                raise AssertionError("contained-command capture is a closed union")
        else:
            raise AssertionError("contained cleanup is a closed union")
        if type(lifecycle) is _ContainedCommandLifecycleCloseFailed:
            return ContainedCommandFinalizationFailed(
                child=child,
                capture=capture_fact,
                cleanup=cleanup,
                finalization_failure=lifecycle.failure,
                metrics=evidence.metrics,
            )
        if type(lifecycle) is not _ContainedCommandLifecycleClosed:
            raise AssertionError("contained-command lifecycle is a closed union")
        if type(capture) is _CapturedCommandCaptureCloseFailed:
            return ContainedCommandCaptureFailed(
                child=child,
                cleanup=cleanup,
                failure=capture.failure,
                metrics=evidence.metrics,
            )
        if type(capture) is not _CapturedCommandCaptureClosed:
            raise AssertionError("contained-command capture is a closed union")
        return ContainedCommandCompleted(child=child, metrics=evidence.metrics)

    def _abort_without_pump(
        self,
        process: PosixProcessHandle,
        stdout: ContainedCommandOutputReader,
        leader: OwnedProcessGroupLeader,
        capture_failure: ContainedCommandFailure,
    ) -> ContainedCommandResult:
        try:
            termination = self._process_group_supervisor.abort(leader)
        except BaseException as cleanup_error:
            stdout_close_failure = self._close_unpumped_stdout(stdout)
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.process_id),
                capture=ContainedCommandCaptureInterrupted(capture_failure),
                cleanup_failure=_combine_failures(
                    ContainedCommandFailure(cleanup_error),
                    stdout_close_failure,
                    "contained command group abort and stdout close both failed",
                ),
                metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
            )
        stdout_close_failure = self._close_unpumped_stdout(stdout)
        failures = [capture_failure]
        if stdout_close_failure is not None:
            failures.append(stdout_close_failure)
        child = ContainedCommandExited(
            process.process_id,
            termination.leader_exit_code,
        )
        evidence = _ContainedCommandPostContainmentEvidence(
            capture=_capture_closure(tuple(failures)),
            lifecycle=_record_reap_and_courtesy(process, termination),
            metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
        )
        return self._closed_result_after_containment(
            child,
            ContainedCommandCaptureAborted(),
            evidence,
        )

    @staticmethod
    def _close_unpumped_stdout(
        stdout: ContainedCommandOutputReader,
    ) -> ContainedCommandFailure | None:
        try:
            stdout.close()
        except BaseException as error:
            return ContainedCommandFailure(error)
        return None

    def _recover_after_supervision_failure(
        self,
        process: PosixProcessHandle,
        leader: OwnedProcessGroupLeader,
        pump: _CapturedOutputPump,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
        supervision_failure: ContainedCommandFailure,
    ) -> ContainedCommandResult:
        initial_pump_failure = pump.failure
        if initial_pump_failure is None:
            failure = supervision_failure
        else:
            failure = _combine_failures(
                initial_pump_failure,
                supervision_failure,
                "output capture and command supervision both failed",
            )
        try:
            termination = self._process_group_supervisor.abort(leader)
        except BaseException as cleanup_error:
            emergency_close_failure = pump.stop_without_waiting(
                output,
                line_observer,
            )
            return ContainedCommandCleanupFailed(
                child=ContainedCommandExitUnknown(process.process_id),
                capture=ContainedCommandCaptureInterrupted(failure),
                cleanup_failure=_combine_failures(
                    ContainedCommandFailure(cleanup_error),
                    emergency_close_failure,
                    "group abort and emergency capture finalization both failed",
                ),
                metrics=pump.metrics,
            )
        evidence = _ContainedCommandPostContainmentOwner(
            process,
            pump,
            output,
            line_observer,
        ).close(termination, (failure,))
        return self._closed_result_after_containment(
            ContainedCommandExited(
                process.process_id,
                termination.leader_exit_code,
            ),
            ContainedCommandCaptureAborted(),
            evidence,
        )


def _record_reap_and_courtesy(
    process: PosixProcessHandle,
    termination: ProcessGroupTermination,
) -> _ContainedCommandLifecycleClosure:
    failures: list[ContainedCommandFailure] = []
    try:
        process.record_external_reap(termination.leader_exit_code)
    except BaseException as error:
        failures.append(ContainedCommandFailure(error))
    courtesy_failure = termination.courtesy_failure()
    if courtesy_failure is not None:
        failures.append(ContainedCommandFailure(courtesy_failure.error))
    return _lifecycle_closure(tuple(failures))
