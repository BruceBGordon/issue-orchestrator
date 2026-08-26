"""POSIX deep owner for bounded validation process-tree execution."""

from __future__ import annotations

import locale
import os
import time
from dataclasses import dataclass
from typing import TypeVar, cast

from ..domain.contained_command import ContainedCommandOutputPolicy
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupUnboundedWait,
)
from ..domain.posix_process import (
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandDeadlineExceeded,
    ValidationCommandDeadlinePending,
    ValidationCommandDeadlineStatus,
    ValidationCommandDeadlineTracker,
    ValidationCommandCleanupFailed,
    ValidationCommandCleanupNotStarted,
    ValidationCommandExecution,
    ValidationCommandExited,
    ValidationCommandExitUnknown,
    ValidationCommandNotStarted,
    ValidationCommandOutput,
    ValidationExecutionDeadline,
    validation_cleanup_from_supervision,
)
from ..infra.validation_executor_handshake import ValidationExecutorHandshakeDecoder
from ..ports.posix_pipe import PosixPipeReader
from ..ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLauncher,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from ..ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from ..ports.validation_pipe_capture import (
    ValidationPipeCapture,
    ValidationPipeCaptureFactory,
    ValidationPipeCaptureResult,
)
from ..ports.validation_launch_pipes import (
    ValidationLaunchPipes,
    ValidationLaunchPipesClosed,
    ValidationLaunchPipesCloseFailed,
    ValidationLaunchPipesFactory,
    ValidationLaunchReaders,
)
from .validation_pipe_resources import (
    ValidationPipeResourceOwner,
    ValidationPipeRole,
    ValidationPipeSelectorFactory,
)


_ExactValue = TypeVar("_ExactValue")


def _require_exact_type(
    value: object,
    expected_type: type[_ExactValue],
    field_name: str,
) -> _ExactValue:
    if type(value) is not expected_type:
        raise ValueError(f"{field_name} must be {expected_type.__name__}")
    return cast(_ExactValue, value)


def _require_positive_float(value: float, field_name: str) -> None:
    if type(value) is not float or value <= 0:
        raise ValueError(f"{field_name} must be a positive float")


def _combined_error(
    message: str,
    primary: BaseException,
    secondary: BaseException | None,
) -> BaseException:
    if secondary is None:
        return primary
    return BaseExceptionGroup(message, (primary, secondary))


@dataclass(frozen=True, slots=True)
class _StartedValidationCommand:
    process: PosixProcessHandle
    readers: ValidationLaunchReaders
    started_at_monotonic: float

    def __post_init__(self) -> None:
        if not isinstance(self.process, PosixProcessHandle):
            raise ValueError("started validation process must implement its port")
        if type(self.readers) is not ValidationLaunchReaders:
            raise ValueError("started validation readers must be typed")
        if (
            type(self.started_at_monotonic) is not float
            or self.started_at_monotonic <= 0
        ):
            raise ValueError("started validation monotonic time must be positive")


class _ValidationPipeCapture(ProcessGroupInterruption):
    """Drain both pipes while the supervisor keeps the leader unreaped."""

    def __init__(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
        resource_owner: ValidationPipeResourceOwner,
    ) -> None:
        self._policy = _require_exact_type(
            policy,
            ContainedCommandOutputPolicy,
            "validation pipe capture policy",
        )
        self._streams: dict[int, PosixPipeReader] = {}
        self._buffers: dict[int, bytearray] = {}
        if type(resource_owner) is not ValidationPipeResourceOwner:
            raise ValueError(
                "validation capture resource_owner must be ValidationPipeResourceOwner"
            )
        self._resources = resource_owner
        self._stdout_descriptor = resource_owner.descriptor(ValidationPipeRole.STDOUT)
        self._stderr_descriptor = resource_owner.descriptor(ValidationPipeRole.STDERR)
        self._handshake_descriptor = resource_owner.descriptor(
            ValidationPipeRole.EXECUTOR_HANDSHAKE
        )
        self._streams[self._stdout_descriptor] = stdout
        self._streams[self._stderr_descriptor] = stderr
        self._buffers[self._stdout_descriptor] = bytearray()
        self._buffers[self._stderr_descriptor] = bytearray()
        deadline = _require_exact_type(
            deadline,
            ValidationExecutionDeadline,
            "validation capture deadline",
        )
        if type(started_at_monotonic) is not float or started_at_monotonic <= 0:
            raise ValueError(
                "validation capture monotonic start must be a positive float"
            )
        self._deadline_tracker = ValidationCommandDeadlineTracker(
            deadline,
            started_at_monotonic,
        )
        self._handshake_decoder = ValidationExecutorHandshakeDecoder()
        self._deadline_status: ValidationCommandDeadlineStatus = (
            ValidationCommandDeadlinePending()
        )

    def wait_for_request(self, timeout_seconds: float) -> bool:
        _require_positive_float(timeout_seconds, "validation pipe wait")
        self._read_ready(timeout_seconds)
        self._deadline_status = self._deadline_tracker.status(time.monotonic())
        if type(self._deadline_status) is ValidationCommandDeadlineExceeded:
            return True
        return False

    @property
    def deadline_status(self) -> ValidationCommandDeadlineStatus:
        """Return the exact clock state observed by the capture owner."""
        return self._deadline_status

    def finalize(self) -> ValidationPipeCaptureResult:
        failure: BaseException | None = None
        deadline = time.monotonic() + self._policy.shutdown_timeout_seconds
        remaining_bytes = self._policy.final_drain_byte_limit
        try:
            while self._streams and remaining_bytes > 0:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        "validation output pipes did not close after process-group "
                        f"containment within {self._policy.shutdown_timeout_seconds}s"
                    )
                remaining_bytes -= self._read_ready(
                    min(self._policy.poll_interval_seconds, remaining_seconds),
                    maximum_bytes=remaining_bytes,
                )
            if self._streams and remaining_bytes <= 0:
                raise RuntimeError(
                    "validation final output drain exceeded "
                    f"{self._policy.final_drain_byte_limit} bytes"
                )
        except BaseException as error:
            failure = error
        close_failure = self._resources.close()
        failure = (
            close_failure
            if failure is None
            else _combined_error(
                "validation output finalization failed more than once",
                failure,
                close_failure,
            )
        )
        encoding = locale.getpreferredencoding(False)
        return ValidationPipeCaptureResult(
            ValidationCommandOutput(
                stdout=bytes(self._buffers[self._stdout_descriptor]).decode(
                    encoding,
                    errors="replace",
                ),
                stderr=bytes(self._buffers[self._stderr_descriptor]).decode(
                    encoding,
                    errors="replace",
                ),
            ),
            failure,
        )

    def _read_ready(
        self,
        timeout_seconds: float,
        *,
        maximum_bytes: int = 131_072,
    ) -> int:
        bytes_read = 0
        for descriptor in self._resources.select(timeout_seconds):
            if descriptor == self._handshake_descriptor:
                payload = os.read(descriptor, 4096)
                if payload:
                    observed_at_monotonic = time.monotonic()
                    for acknowledgement in self._handshake_decoder.consume(payload):
                        if (
                            acknowledgement.acknowledged_at_monotonic
                            > observed_at_monotonic
                        ):
                            raise RuntimeError(
                                "validation executor acknowledgement is in the future"
                            )
                        self._deadline_tracker.acknowledge_executor(
                            acknowledgement.acknowledged_at_monotonic
                        )
                else:
                    self._handshake_decoder.finish()
                    self._resources.unregister(ValidationPipeRole.EXECUTOR_HANDSHAKE)
                continue
            read_size = min(65_536, maximum_bytes - bytes_read)
            if read_size <= 0:
                break
            chunk = os.read(descriptor, read_size)
            if chunk:
                self._buffers[descriptor].extend(chunk)
                bytes_read += len(chunk)
                continue
            role = (
                ValidationPipeRole.STDOUT
                if descriptor == self._stdout_descriptor
                else ValidationPipeRole.STDERR
            )
            self._resources.unregister(role)
            self._streams.pop(descriptor)
        return bytes_read


class PosixValidationPipeCaptureFactory:
    """Create one real capture with all-or-nothing resource ownership."""

    def __init__(self, selector_factory: ValidationPipeSelectorFactory) -> None:
        if not callable(selector_factory):
            raise ValueError(
                "PosixValidationPipeCaptureFactory.selector_factory must be callable"
            )
        self._selector_factory = selector_factory

    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
    ) -> ValidationPipeCapture:
        resources = ValidationPipeResourceOwner(
            stdout,
            stderr,
            handshake_reader,
            self._selector_factory,
        )
        try:
            return _ValidationPipeCapture(
                stdout,
                stderr,
                handshake_reader,
                policy,
                deadline,
                started_at_monotonic,
                resources,
            )
        except BaseException as setup_error:
            cleanup_error = resources.close()
            raise _combined_error(
                "validation capture setup and cleanup both failed",
                setup_error,
                cleanup_error,
            )


@dataclass(frozen=True, slots=True)
class _ValidationActivationClosed:
    execution: ValidationCommandExecution

    def __post_init__(self) -> None:
        if type(self.execution) is not ValidationCommandExecution:
            raise ValueError("closed validation activation must contain an execution")


_ValidationActivation = _StartedValidationCommand | _ValidationActivationClosed


def _launch_pipe_close_error(pipes: ValidationLaunchPipes) -> BaseException | None:
    try:
        outcome = pipes.close()
    except BaseException as error:
        return error
    if type(outcome) is ValidationLaunchPipesClosed:
        return None
    if type(outcome) is not ValidationLaunchPipesCloseFailed:
        raise AssertionError("validation launch pipe close is a closed union")
    return outcome.error


class PosixContainedValidationCommandRunner:
    """Start, capture, contain, and reap one validation process tree."""

    def __init__(
        self,
        process_launcher: PosixProcessLauncher,
        process_group_supervisor: ProcessGroupSupervisor,
        output_policy: ContainedCommandOutputPolicy,
        capture_factory: ValidationPipeCaptureFactory,
        launch_pipes_factory: ValidationLaunchPipesFactory,
    ) -> None:
        if not isinstance(process_launcher, PosixProcessLauncher):
            raise ValueError(
                "PosixContainedValidationCommandRunner.process_launcher must "
                "implement PosixProcessLauncher"
            )
        self._process_launcher = process_launcher
        if not isinstance(process_group_supervisor, ProcessGroupSupervisor):
            raise ValueError(
                "PosixContainedValidationCommandRunner.process_group_supervisor "
                "must implement ProcessGroupSupervisor"
            )
        self._supervisor = process_group_supervisor
        self._output_policy = _require_exact_type(
            output_policy,
            ContainedCommandOutputPolicy,
            "PosixContainedValidationCommandRunner.output_policy",
        )
        if not isinstance(capture_factory, ValidationPipeCaptureFactory):
            raise ValueError(
                "PosixContainedValidationCommandRunner.capture_factory must "
                "implement ValidationPipeCaptureFactory"
            )
        self._capture_factory = capture_factory
        if not isinstance(launch_pipes_factory, ValidationLaunchPipesFactory):
            raise ValueError(
                "PosixContainedValidationCommandRunner.launch_pipes_factory must "
                "implement ValidationLaunchPipesFactory"
            )
        self._launch_pipes_factory = launch_pipes_factory

    def run(self, command: ContainedValidationCommand) -> ValidationCommandExecution:
        command = _require_exact_type(
            command,
            ContainedValidationCommand,
            "PosixContainedValidationCommandRunner.run command",
        )
        activation = self._activate(command)
        if type(activation) is _ValidationActivationClosed:
            return activation.execution
        if type(activation) is not _StartedValidationCommand:
            raise AssertionError("validation activation is a closed union")
        return self._run_started(activation, command.deadline)

    def _activate(self, command: ContainedValidationCommand) -> _ValidationActivation:
        started_at_monotonic = time.monotonic()
        try:
            pipes = self._launch_pipes_factory.create()
        except BaseException as acquisition_error:
            return _ValidationActivationClosed(self._not_started(acquisition_error))
        try:
            launch = self._process_launcher.launch(
                PosixProcessLaunchSpec(
                    program=PosixProcessProgram(("/bin/sh", "-c", command.command)),
                    working_directory=command.working_directory,
                    environment=pipes.child_environment(command.environment),
                    group_mode=PosixProcessGroupMode.NEW_SESSION,
                    descriptor_mappings=pipes.descriptor_mappings,
                    terminal=PosixProcessWithoutTerminal(),
                )
            )
        except BaseException as prelaunch_error:
            return _ValidationActivationClosed(
                self._not_started(
                    _combined_error(
                        "validation prelaunch setup and pipe cleanup both failed",
                        prelaunch_error,
                        _launch_pipe_close_error(pipes),
                    )
                )
            )
        if type(launch) is PosixProcessLaunchRejected:
            return _ValidationActivationClosed(
                self._not_started(
                    _combined_error(
                        "validation launch rejection and pipe cleanup both failed",
                        launch.error,
                        _launch_pipe_close_error(pipes),
                    )
                )
            )
        if type(launch) is PosixProcessExecRejected:
            return _ValidationActivationClosed(
                self._not_started(
                    _combined_error(
                        "validation exec rejection and pipe cleanup both failed",
                        launch.as_error(),
                        _launch_pipe_close_error(pipes),
                    )
                )
            )
        if type(launch) is PosixProcessLaunchRecovered:
            return _ValidationActivationClosed(
                ValidationCommandExecution(
                    child=ValidationCommandExited(
                        launch.process_id,
                        launch.exit_code,
                    ),
                    cleanup=ValidationCommandCleanupFailed(
                        _combined_error(
                            "validation activation and pipe cleanup both failed",
                            launch.activation_error,
                            _launch_pipe_close_error(pipes),
                        )
                    ),
                    output=ValidationCommandOutput("", ""),
                )
            )
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            cleanup_error = _combined_error(
                "validation activation recovery and pipe cleanup both failed",
                launch.recovery_error,
                _launch_pipe_close_error(pipes),
            )
            return _ValidationActivationClosed(
                ValidationCommandExecution(
                    child=ValidationCommandExitUnknown(launch.process_id),
                    cleanup=ValidationCommandCleanupFailed(
                        BaseExceptionGroup(
                            "validation activation and recovery both failed",
                            (launch.activation_error, cleanup_error),
                        )
                    ),
                    output=ValidationCommandOutput("", ""),
                )
            )
        if type(launch) is not PosixProcessLaunchStarted:
            raise AssertionError("POSIX process launch is a closed union")
        try:
            readers = pipes.transfer_readers_after_launch()
        except BaseException as transfer_error:
            return _ValidationActivationClosed(
                self._abort_without_capture(
                    launch.process,
                    _combined_error(
                        "validation reader transfer and pipe cleanup both failed",
                        transfer_error,
                        _launch_pipe_close_error(pipes),
                    ),
                )
            )
        return _StartedValidationCommand(
            launch.process,
            readers,
            started_at_monotonic,
        )

    @staticmethod
    def _not_started(error: BaseException) -> ValidationCommandExecution:
        return ValidationCommandExecution(
            child=ValidationCommandNotStarted(error),
            cleanup=ValidationCommandCleanupNotStarted(),
            output=ValidationCommandOutput("", ""),
        )

    def _run_started(
        self,
        started: _StartedValidationCommand,
        deadline: ValidationExecutionDeadline,
    ) -> ValidationCommandExecution:
        process = started.process
        try:
            capture = self._capture_factory.create(
                started.readers.stdout,
                started.readers.stderr,
                started.readers.executor_handshake,
                self._output_policy,
                deadline,
                started.started_at_monotonic,
            )
        except BaseException as capture_setup_error:
            return self._abort_without_capture(process, capture_setup_error)
        leader = OwnedProcessGroupLeader(process.process_id)
        try:
            supervision = self._supervisor.supervise(
                leader,
                ProcessGroupUnboundedWait(),
                capture,
            )
        except BaseException as supervision_error:
            return self._recover_after_supervision_failure(
                process,
                leader,
                capture,
                supervision_error,
            )
        process.record_external_reap(supervision.termination.leader_exit_code)
        captured = capture.finalize()
        child = ValidationCommandExited(
            process.process_id,
            supervision.termination.leader_exit_code,
        )
        if captured.failure is not None:
            return ValidationCommandExecution(
                child=child,
                cleanup=ValidationCommandCleanupFailed(captured.failure),
                output=captured.output,
            )
        cleanup = validation_cleanup_from_supervision(
            supervision,
            capture.deadline_status,
        )
        return ValidationCommandExecution(child, cleanup, captured.output)

    def _abort_without_capture(
        self,
        process: PosixProcessHandle,
        original_error: BaseException,
    ) -> ValidationCommandExecution:
        leader = OwnedProcessGroupLeader(process.process_id)
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            return ValidationCommandExecution(
                child=ValidationCommandExitUnknown(process.process_id),
                cleanup=ValidationCommandCleanupFailed(
                    _combined_error(
                        "validation setup and cleanup both failed",
                        original_error,
                        cleanup_error,
                    )
                ),
                output=ValidationCommandOutput("", ""),
            )
        process.record_external_reap(termination.leader_exit_code)
        return ValidationCommandExecution(
            child=ValidationCommandExited(
                process.process_id,
                termination.leader_exit_code,
            ),
            cleanup=ValidationCommandCleanupFailed(original_error),
            output=ValidationCommandOutput("", ""),
        )

    def _recover_after_supervision_failure(
        self,
        process: PosixProcessHandle,
        leader: OwnedProcessGroupLeader,
        capture: ValidationPipeCapture,
        supervision_error: BaseException,
    ) -> ValidationCommandExecution:
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            captured = capture.finalize()
            return ValidationCommandExecution(
                child=ValidationCommandExitUnknown(process.process_id),
                cleanup=ValidationCommandCleanupFailed(
                    _combined_error(
                        "validation supervision and containment both failed",
                        supervision_error,
                        _combined_error(
                            "validation containment and output finalization both failed",
                            cleanup_error,
                            captured.failure,
                        ),
                    )
                ),
                output=captured.output,
            )
        process.record_external_reap(termination.leader_exit_code)
        captured = capture.finalize()
        return ValidationCommandExecution(
            child=ValidationCommandExited(
                process.process_id,
                termination.leader_exit_code,
            ),
            cleanup=ValidationCommandCleanupFailed(
                _combined_error(
                    "validation supervision and output finalization both failed",
                    supervision_error,
                    captured.failure,
                )
            ),
            output=captured.output,
        )
