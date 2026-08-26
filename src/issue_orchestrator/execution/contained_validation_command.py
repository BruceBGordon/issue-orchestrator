"""POSIX deep owner for bounded validation process-tree execution."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TypeVar, cast

from ..domain.contained_command import ContainedCommandOutputPolicy
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupTerminalCompletionAccepted,
    ProcessGroupTerminalDecision,
    ProcessGroupTerminalInterruptionRequested,
    ProcessGroupTermination,
    ProcessGroupUnboundedWait,
)
from ..domain.posix_process import (
    PosixProcessProgram,
)
from ..domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandDeadlineExceeded,
    ValidationCommandDeadlinePending,
    ValidationCommandDeadlineStatus,
    ValidationCommandDeadlineTracker,
    ValidationCommandCleanupFailed,
    ValidationCommandCleanup,
    ValidationCommandCleanupNotStarted,
    ValidationCommandExecution,
    ValidationCommandExited,
    ValidationCommandExitUnknown,
    ValidationCommandNotStarted,
    ValidationCommandOutput,
    ValidationDeadlineObservationClock,
    ValidationExecutionDeadline,
    validation_cleanup_from_supervision,
    validation_cleanup_with_failure,
)
from ..infra.validation_executor_handshake import ValidationExecutorHandshakeDecoder
from ..ports.posix_pipe import PosixPipeReader
from ..ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
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
from ..ports.validation_process_guardian import (
    ValidationProcessGuardian,
    ValidationProcessGuardianStarted,
    ValidationProcessParentLifetime,
)
from ..ports.validation_output_journal import (
    ValidationOutputJournal,
    ValidationOutputJournalFactory,
    ValidationOutputStream,
)
from .independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
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
    parent_lifetime: ValidationProcessParentLifetime
    readers: ValidationLaunchReaders
    started_at_monotonic: float
    output_journal: ValidationOutputJournal

    def __post_init__(self) -> None:
        if not isinstance(self.process, PosixProcessHandle):
            raise ValueError("started validation process must implement its port")
        if not isinstance(self.parent_lifetime, ValidationProcessParentLifetime):
            raise ValueError(
                "started validation parent lifetime must implement its port"
            )
        if type(self.readers) is not ValidationLaunchReaders:
            raise ValueError("started validation readers must be typed")
        if (
            type(self.started_at_monotonic) is not float
            or self.started_at_monotonic <= 0
        ):
            raise ValueError("started validation monotonic time must be positive")
        if not isinstance(self.output_journal, ValidationOutputJournal):
            raise ValueError(
                "started validation output journal must implement its port"
            )


@dataclass(frozen=True, slots=True)
class _ValidationCaptureFinalized:
    result: ValidationPipeCaptureResult

    def __post_init__(self) -> None:
        if type(self.result) is not ValidationPipeCaptureResult:
            raise ValueError("validation capture finalization result must be typed")


@dataclass(frozen=True, slots=True)
class _ValidationCaptureFinalizationFailed:
    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("validation capture finalization error must be typed")


_ValidationCaptureFinalization = (
    _ValidationCaptureFinalized | _ValidationCaptureFinalizationFailed
)


@dataclass(frozen=True, slots=True)
class _ValidationReapEvidenceRecorded:
    """The retained process handle accepted its external reap evidence."""


@dataclass(frozen=True, slots=True)
class _ValidationReapEvidenceFailed:
    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("validation reap evidence error must be typed")


_ValidationReapEvidence = (
    _ValidationReapEvidenceRecorded | _ValidationReapEvidenceFailed
)


@dataclass(frozen=True, slots=True)
class _ValidationParentLifetimeClosed:
    """The post-containment parent lifetime was released."""


@dataclass(frozen=True, slots=True)
class _ValidationParentLifetimeCloseFailed:
    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("validation parent lifetime failure must be typed")


_ValidationParentLifetimeClose = (
    _ValidationParentLifetimeClosed | _ValidationParentLifetimeCloseFailed
)


@dataclass(frozen=True, slots=True)
class _ValidationLaunchReadersClosed:
    """Every transferred reader was closed after capture rejected ownership."""


@dataclass(frozen=True, slots=True)
class _ValidationLaunchReadersCloseFailed:
    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("validation launch reader cleanup failure must be typed")


_ValidationLaunchReadersClose = (
    _ValidationLaunchReadersClosed | _ValidationLaunchReadersCloseFailed
)


def _close_validation_launch_readers(
    readers: ValidationLaunchReaders,
) -> _ValidationLaunchReadersClose:
    outcome = IndependentCleanupPlan(
        tuple(
            CleanupAction(
                f"validation-capture-rejected-{role}-reader-close",
                reader.close,
            )
            for role, reader in (
                ("stdout", readers.stdout),
                ("stderr", readers.stderr),
                ("executor-handshake", readers.executor_handshake),
            )
        )
    ).run()
    if type(outcome) is CleanupSucceeded:
        return _ValidationLaunchReadersClosed()
    if type(outcome) is not CleanupFailed:
        raise AssertionError("validation reader cleanup is a closed union")
    errors = tuple(failure.error for failure in outcome.failures)
    error = (
        errors[0]
        if len(errors) == 1
        else BaseExceptionGroup("validation launch readers did not close", errors)
    )
    return _ValidationLaunchReadersCloseFailed(error)


@dataclass(frozen=True, slots=True)
class _ValidationCaptureAcquired:
    capture: ValidationPipeCapture

    def __post_init__(self) -> None:
        if not isinstance(self.capture, ValidationPipeCapture):
            raise ValueError("acquired validation capture must implement its port")


@dataclass(frozen=True, slots=True)
class _ValidationCaptureRejected:
    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("rejected validation capture error must be typed")


_ValidationCaptureAcquisition = _ValidationCaptureAcquired | _ValidationCaptureRejected


def _require_acquired_capture(
    acquisition: _ValidationCaptureAcquisition,
) -> ValidationPipeCapture:
    if type(acquisition) is not _ValidationCaptureAcquired:
        raise AssertionError("validation capture acquisition is a closed union")
    return acquisition.capture


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
        output_journal: ValidationOutputJournal,
        deadline_clock: ValidationDeadlineObservationClock,
    ) -> None:
        self._policy = _require_exact_type(
            policy,
            ContainedCommandOutputPolicy,
            "validation pipe capture policy",
        )
        self._streams: dict[int, PosixPipeReader] = {}
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
        if not isinstance(output_journal, ValidationOutputJournal):
            raise ValueError("validation output journal must implement its port")
        self._output_journal = output_journal
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
        self._deadline_clock = _require_exact_type(
            deadline_clock,
            ValidationDeadlineObservationClock,
            "validation deadline observation clock",
        )
        self._handshake_decoder = ValidationExecutorHandshakeDecoder()
        self._deadline_status: ValidationCommandDeadlineStatus = (
            ValidationCommandDeadlinePending()
        )

    def wait_for_request(self, timeout_seconds: float) -> bool:
        _require_positive_float(timeout_seconds, "validation pipe wait")
        self._read_ready(timeout_seconds)
        self._observe_deadline()
        if type(self._deadline_status) is ValidationCommandDeadlineExceeded:
            return True
        return False

    def decide_terminal_observation(self) -> ProcessGroupTerminalDecision:
        """Re-observe the authoritative deadline before accepting completion."""
        self._observe_deadline()
        if type(self._deadline_status) is ValidationCommandDeadlinePending:
            return ProcessGroupTerminalCompletionAccepted()
        if type(self._deadline_status) is ValidationCommandDeadlineExceeded:
            return ProcessGroupTerminalInterruptionRequested()
        raise AssertionError("validation deadline status is a closed union")

    @property
    def deadline_status(self) -> ValidationCommandDeadlineStatus:
        """Return the exact clock state observed by the capture owner."""
        return self._deadline_status

    def _observe_deadline(self) -> None:
        self._deadline_status = self._deadline_tracker.status(
            self._deadline_clock.monotonic_now()
        )

    def finalize(self) -> ValidationPipeCaptureResult:
        failure: BaseException | None = None
        deadline = time.monotonic() + self._policy.shutdown_timeout_seconds
        remaining_bytes = self._policy.final_drain_byte_limit
        try:
            while (
                self._streams
                or self._resources.is_registered(ValidationPipeRole.EXECUTOR_HANDSHAKE)
            ) and remaining_bytes > 0:
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
            if (
                self._streams
                or self._resources.is_registered(ValidationPipeRole.EXECUTOR_HANDSHAKE)
            ) and remaining_bytes <= 0:
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
        journal_result = self._output_journal.finalize()
        failure = (
            journal_result.failure
            if failure is None
            else _combined_error(
                "validation pipe and output journal finalization both failed",
                failure,
                journal_result.failure,
            )
        )
        return ValidationPipeCaptureResult(
            journal_result.output,
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
                stream = (
                    ValidationOutputStream.STDOUT
                    if descriptor == self._stdout_descriptor
                    else ValidationOutputStream.STDERR
                )
                self._output_journal.append(stream, chunk)
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

    def __init__(
        self,
        selector_factory: ValidationPipeSelectorFactory,
        deadline_clock: ValidationDeadlineObservationClock,
    ) -> None:
        if not callable(selector_factory):
            raise ValueError(
                "PosixValidationPipeCaptureFactory.selector_factory must be callable"
            )
        self._selector_factory = selector_factory
        self._deadline_clock = _require_exact_type(
            deadline_clock,
            ValidationDeadlineObservationClock,
            "PosixValidationPipeCaptureFactory.deadline_clock",
        )

    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
        output_journal: ValidationOutputJournal,
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
                output_journal,
                self._deadline_clock,
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
        process_guardian: ValidationProcessGuardian,
        process_group_supervisor: ProcessGroupSupervisor,
        output_policy: ContainedCommandOutputPolicy,
        capture_factory: ValidationPipeCaptureFactory,
        launch_pipes_factory: ValidationLaunchPipesFactory,
        output_journal_factory: ValidationOutputJournalFactory,
    ) -> None:
        if not isinstance(process_guardian, ValidationProcessGuardian):
            raise ValueError(
                "PosixContainedValidationCommandRunner.process_guardian must "
                "implement ValidationProcessGuardian"
            )
        self._process_guardian = process_guardian
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
        if not isinstance(output_journal_factory, ValidationOutputJournalFactory):
            raise ValueError(
                "PosixContainedValidationCommandRunner.output_journal_factory must "
                "implement ValidationOutputJournalFactory"
            )
        self._output_journal_factory = output_journal_factory

    def run(self, command: ContainedValidationCommand) -> ValidationCommandExecution:
        command = _require_exact_type(
            command,
            ContainedValidationCommand,
            "PosixContainedValidationCommandRunner.run command",
        )
        try:
            output_journal = self._output_journal_factory.create(command.output_capture)
        except BaseException as journal_error:
            return self._not_started(journal_error)
        activation = self._activate(command, output_journal)
        if type(activation) is _ValidationActivationClosed:
            return activation.execution
        if type(activation) is not _StartedValidationCommand:
            raise AssertionError("validation activation is a closed union")
        return self._run_started(activation, command.deadline)

    def _activate(
        self,
        command: ContainedValidationCommand,
        output_journal: ValidationOutputJournal,
    ) -> _ValidationActivation:
        try:
            pipes = self._launch_pipes_factory.create()
        except BaseException as acquisition_error:
            return _ValidationActivationClosed(
                self._finalize_unstarted_output(
                    self._not_started(acquisition_error),
                    output_journal,
                )
            )
        try:
            launch = self._process_guardian.launch(
                PosixProcessProgram(("/bin/sh", "-c", command.command)),
                command.working_directory,
                pipes.child_environment(command.environment),
                pipes.descriptor_mappings,
            )
        except BaseException as prelaunch_error:
            return _ValidationActivationClosed(
                self._finalize_unstarted_output(
                    self._not_started(
                        _combined_error(
                            "validation prelaunch setup and pipe cleanup both failed",
                            prelaunch_error,
                            _launch_pipe_close_error(pipes),
                        )
                    ),
                    output_journal,
                )
            )
        if type(launch) is PosixProcessLaunchRejected:
            return _ValidationActivationClosed(
                self._finalize_unstarted_output(
                    self._not_started(
                        _combined_error(
                            "validation launch rejection and pipe cleanup both failed",
                            launch.error,
                            _launch_pipe_close_error(pipes),
                        )
                    ),
                    output_journal,
                )
            )
        if type(launch) is PosixProcessExecRejected:
            return _ValidationActivationClosed(
                self._finalize_unstarted_output(
                    self._not_started(
                        _combined_error(
                            "validation exec rejection and pipe cleanup both failed",
                            launch.as_error(),
                            _launch_pipe_close_error(pipes),
                        )
                    ),
                    output_journal,
                )
            )
        if type(launch) is PosixProcessLaunchRecovered:
            return _ValidationActivationClosed(
                self._finalize_output_journal(
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
                    ),
                    output_journal,
                )
            )
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            cleanup_error = _combined_error(
                "validation activation recovery and pipe cleanup both failed",
                launch.recovery_error,
                _launch_pipe_close_error(pipes),
            )
            return _ValidationActivationClosed(
                self._finalize_output_journal(
                    ValidationCommandExecution(
                        child=ValidationCommandExitUnknown(launch.process_id),
                        cleanup=ValidationCommandCleanupFailed(
                            BaseExceptionGroup(
                                "validation activation and recovery both failed",
                                (launch.activation_error, cleanup_error),
                            )
                        ),
                        output=ValidationCommandOutput("", ""),
                    ),
                    output_journal,
                )
            )
        if type(launch) is not ValidationProcessGuardianStarted:
            raise AssertionError("validation guardian launch is a closed union")
        try:
            readers = pipes.transfer_readers_after_launch()
        except BaseException as transfer_error:
            return _ValidationActivationClosed(
                self._abort_without_capture(
                    launch.process,
                    launch.parent_lifetime,
                    _combined_error(
                        "validation reader transfer and pipe cleanup both failed",
                        transfer_error,
                        _launch_pipe_close_error(pipes),
                    ),
                    output_journal,
                )
            )
        return _StartedValidationCommand(
            launch.process,
            launch.parent_lifetime,
            readers,
            time.monotonic(),
            output_journal,
        )

    @staticmethod
    def _not_started(error: BaseException) -> ValidationCommandExecution:
        return ValidationCommandExecution(
            child=ValidationCommandNotStarted(error),
            cleanup=ValidationCommandCleanupNotStarted(),
            output=ValidationCommandOutput("", ""),
        )

    @staticmethod
    def _finalize_unstarted_output(
        execution: ValidationCommandExecution,
        output_journal: ValidationOutputJournal,
    ) -> ValidationCommandExecution:
        if type(execution.child) is not ValidationCommandNotStarted:
            raise ValueError("unstarted output finalization requires unstarted child")
        try:
            journal_result = output_journal.finalize()
        except BaseException as error:
            return ValidationCommandExecution(
                ValidationCommandNotStarted(
                    _combined_error(
                        "validation start and output journal finalization both failed",
                        execution.child.error,
                        error,
                    )
                ),
                execution.cleanup,
                execution.output,
            )
        child = execution.child
        if journal_result.failure is not None:
            child = ValidationCommandNotStarted(
                _combined_error(
                    "validation start and output journal finalization both failed",
                    child.error,
                    journal_result.failure,
                )
            )
        return ValidationCommandExecution(
            child,
            execution.cleanup,
            journal_result.output,
        )

    @staticmethod
    def _finalize_output_journal(
        execution: ValidationCommandExecution,
        output_journal: ValidationOutputJournal,
    ) -> ValidationCommandExecution:
        try:
            journal_result = output_journal.finalize()
        except BaseException as error:
            return ValidationCommandExecution(
                execution.child,
                validation_cleanup_with_failure(
                    execution.cleanup,
                    error,
                    "validation execution and output journal finalization both failed",
                ),
                execution.output,
            )
        cleanup = execution.cleanup
        if journal_result.failure is not None:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                journal_result.failure,
                "validation execution and output journal finalization both failed",
            )
        return ValidationCommandExecution(
            execution.child,
            cleanup,
            journal_result.output,
        )

    def _run_started(
        self,
        started: _StartedValidationCommand,
        deadline: ValidationExecutionDeadline,
    ) -> ValidationCommandExecution:
        process = started.process
        acquisition = self._acquire_capture(started, deadline)
        if type(acquisition) is _ValidationCaptureRejected:
            return self._abort_without_capture(
                process,
                started.parent_lifetime,
                acquisition.error,
                started.output_journal,
            )
        capture = _require_acquired_capture(acquisition)
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
                started.parent_lifetime,
                leader,
                capture,
                supervision_error,
            )
        parent_lifetime = self._close_parent_lifetime(started.parent_lifetime)
        finalization = self._finalize_capture(capture)
        reap_evidence = self._record_reap_evidence(
            process,
            supervision.termination,
        )
        child = ValidationCommandExited(
            process.process_id,
            supervision.termination.leader_exit_code,
        )
        cleanup = validation_cleanup_from_supervision(
            supervision,
            capture.deadline_status,
        )
        output = ValidationCommandOutput("", "")
        if type(finalization) is _ValidationCaptureFinalized:
            output = finalization.result.output
            if finalization.result.failure is not None:
                cleanup = validation_cleanup_with_failure(
                    cleanup,
                    finalization.result.failure,
                    "validation execution and output finalization both failed",
                )
        elif type(finalization) is _ValidationCaptureFinalizationFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                finalization.error,
                "validation execution and output finalization both failed",
            )
        else:
            raise AssertionError("validation capture finalization is a closed union")
        if type(reap_evidence) is _ValidationReapEvidenceFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                reap_evidence.error,
                "validation output and retained-handle finalization both failed",
            )
        elif type(reap_evidence) is not _ValidationReapEvidenceRecorded:
            raise AssertionError("validation reap evidence is a closed union")
        if type(parent_lifetime) is _ValidationParentLifetimeCloseFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                parent_lifetime.error,
                "validation execution and parent lifetime cleanup both failed",
            )
        elif type(parent_lifetime) is not _ValidationParentLifetimeClosed:
            raise AssertionError("validation parent lifetime close is a closed union")
        return ValidationCommandExecution(child, cleanup, output)

    def _acquire_capture(
        self,
        started: _StartedValidationCommand,
        deadline: ValidationExecutionDeadline,
    ) -> _ValidationCaptureAcquisition:
        """Transfer readers to capture or close every rejected transfer."""
        try:
            capture = self._capture_factory.create(
                started.readers.stdout,
                started.readers.stderr,
                started.readers.executor_handshake,
                self._output_policy,
                deadline,
                started.started_at_monotonic,
                started.output_journal,
            )
        except BaseException as capture_setup_error:
            reader_cleanup = _close_validation_launch_readers(started.readers)
            if type(reader_cleanup) is _ValidationLaunchReadersCloseFailed:
                capture_setup_error = _combined_error(
                    "validation capture rejection and reader cleanup both failed",
                    capture_setup_error,
                    reader_cleanup.error,
                )
            elif type(reader_cleanup) is not _ValidationLaunchReadersClosed:
                raise AssertionError("validation reader cleanup is a closed union")
            return _ValidationCaptureRejected(capture_setup_error)
        return _ValidationCaptureAcquired(capture)

    def _abort_without_capture(
        self,
        process: PosixProcessHandle,
        parent_lifetime: ValidationProcessParentLifetime,
        original_error: BaseException,
        output_journal: ValidationOutputJournal,
    ) -> ValidationCommandExecution:
        leader = OwnedProcessGroupLeader(process.process_id)
        lifetime_close = self._close_parent_lifetime(parent_lifetime)
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            containment_error = cleanup_error
            if type(lifetime_close) is _ValidationParentLifetimeCloseFailed:
                containment_error = _combined_error(
                    "validation containment and parent lifetime cleanup both failed",
                    cleanup_error,
                    lifetime_close.error,
                )
            elif type(lifetime_close) is not _ValidationParentLifetimeClosed:
                raise AssertionError(
                    "validation parent lifetime close is a closed union"
                )
            return self._finalize_output_journal(
                ValidationCommandExecution(
                    child=ValidationCommandExitUnknown(process.process_id),
                    cleanup=ValidationCommandCleanupFailed(
                        _combined_error(
                            "validation setup and cleanup both failed",
                            original_error,
                            containment_error,
                        )
                    ),
                    output=ValidationCommandOutput("", ""),
                ),
                output_journal,
            )
        cleanup: ValidationCommandCleanup = ValidationCommandCleanupFailed(
            original_error
        )
        cleanup = self._add_termination_failures(cleanup, termination)
        if type(lifetime_close) is _ValidationParentLifetimeCloseFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                lifetime_close.error,
                "validation setup and parent lifetime cleanup both failed",
            )
        elif type(lifetime_close) is not _ValidationParentLifetimeClosed:
            raise AssertionError("validation parent lifetime close is a closed union")
        reap_evidence = self._record_reap_evidence(process, termination)
        if type(reap_evidence) is _ValidationReapEvidenceFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                reap_evidence.error,
                "validation setup and retained-handle finalization both failed",
            )
        elif type(reap_evidence) is not _ValidationReapEvidenceRecorded:
            raise AssertionError("validation reap evidence is a closed union")
        return self._finalize_output_journal(
            ValidationCommandExecution(
                child=ValidationCommandExited(
                    process.process_id,
                    termination.leader_exit_code,
                ),
                cleanup=cleanup,
                output=ValidationCommandOutput("", ""),
            ),
            output_journal,
        )

    def _recover_after_supervision_failure(
        self,
        process: PosixProcessHandle,
        parent_lifetime: ValidationProcessParentLifetime,
        leader: OwnedProcessGroupLeader,
        capture: ValidationPipeCapture,
        supervision_error: BaseException,
    ) -> ValidationCommandExecution:
        lifetime_close = self._close_parent_lifetime(parent_lifetime)
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            finalization = self._finalize_capture(capture)
            capture_error, output = self._capture_failure_and_output(finalization)
            recovery_error = _combined_error(
                "validation containment and output finalization both failed",
                cleanup_error,
                capture_error,
            )
            if type(lifetime_close) is _ValidationParentLifetimeCloseFailed:
                recovery_error = _combined_error(
                    "validation containment and parent lifetime cleanup both failed",
                    recovery_error,
                    lifetime_close.error,
                )
            elif type(lifetime_close) is not _ValidationParentLifetimeClosed:
                raise AssertionError(
                    "validation parent lifetime close is a closed union"
                )
            return ValidationCommandExecution(
                child=ValidationCommandExitUnknown(process.process_id),
                cleanup=ValidationCommandCleanupFailed(
                    _combined_error(
                        "validation supervision and containment both failed",
                        supervision_error,
                        recovery_error,
                    )
                ),
                output=output,
            )
        finalization = self._finalize_capture(capture)
        capture_error, output = self._capture_failure_and_output(finalization)
        cleanup: ValidationCommandCleanup = ValidationCommandCleanupFailed(
            _combined_error(
                "validation supervision and output finalization both failed",
                supervision_error,
                capture_error,
            )
        )
        cleanup = self._add_termination_failures(cleanup, termination)
        if type(lifetime_close) is _ValidationParentLifetimeCloseFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                lifetime_close.error,
                "validation recovery and parent lifetime cleanup both failed",
            )
        elif type(lifetime_close) is not _ValidationParentLifetimeClosed:
            raise AssertionError("validation parent lifetime close is a closed union")
        reap_evidence = self._record_reap_evidence(process, termination)
        if type(reap_evidence) is _ValidationReapEvidenceFailed:
            cleanup = validation_cleanup_with_failure(
                cleanup,
                reap_evidence.error,
                "validation recovery and retained-handle finalization both failed",
            )
        elif type(reap_evidence) is not _ValidationReapEvidenceRecorded:
            raise AssertionError("validation reap evidence is a closed union")
        return ValidationCommandExecution(
            child=ValidationCommandExited(
                process.process_id,
                termination.leader_exit_code,
            ),
            cleanup=cleanup,
            output=output,
        )

    @staticmethod
    def _finalize_capture(
        capture: ValidationPipeCapture,
    ) -> _ValidationCaptureFinalization:
        try:
            return _ValidationCaptureFinalized(capture.finalize())
        except BaseException as error:
            return _ValidationCaptureFinalizationFailed(error)

    @staticmethod
    def _capture_failure_and_output(
        finalization: _ValidationCaptureFinalization,
    ) -> tuple[BaseException | None, ValidationCommandOutput]:
        if type(finalization) is _ValidationCaptureFinalized:
            return finalization.result.failure, finalization.result.output
        if type(finalization) is _ValidationCaptureFinalizationFailed:
            return finalization.error, ValidationCommandOutput("", "")
        raise AssertionError("validation capture finalization is a closed union")

    @staticmethod
    def _record_reap_evidence(
        process: PosixProcessHandle,
        termination: ProcessGroupTermination,
    ) -> _ValidationReapEvidence:
        try:
            process.record_external_reap(termination.leader_exit_code)
        except BaseException as error:
            return _ValidationReapEvidenceFailed(error)
        return _ValidationReapEvidenceRecorded()

    @staticmethod
    def _close_parent_lifetime(
        parent_lifetime: ValidationProcessParentLifetime,
    ) -> _ValidationParentLifetimeClose:
        try:
            parent_lifetime.close()
        except BaseException as error:
            return _ValidationParentLifetimeCloseFailed(error)
        return _ValidationParentLifetimeClosed()

    @staticmethod
    def _add_termination_failures(
        cleanup: ValidationCommandCleanup,
        termination: ProcessGroupTermination,
    ) -> ValidationCommandCleanup:
        courtesy_failure = termination.courtesy_failure()
        if courtesy_failure is None:
            return cleanup
        return validation_cleanup_with_failure(
            cleanup,
            courtesy_failure.error,
            "validation failure and courtesy shutdown observation both failed",
        )
