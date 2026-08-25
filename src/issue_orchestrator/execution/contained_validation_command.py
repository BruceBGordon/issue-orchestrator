"""POSIX deep owner for bounded validation process-tree execution."""

from __future__ import annotations

import locale
import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from typing import BinaryIO, TypeVar, cast

from ..domain.contained_command import ContainedCommandOutputPolicy
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupUnboundedWait,
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
from ..infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
    ValidationExecutorHandshakeDecoder,
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


from ..infra.shutdown_signals import child_signal_reset_preexec
from ..ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)


def _combined_error(
    message: str,
    primary: BaseException,
    secondary: BaseException | None,
) -> BaseException:
    if secondary is None:
        return primary
    return BaseExceptionGroup(message, (primary, secondary))


@dataclass(frozen=True, slots=True)
class _CapturedValidationStreams:
    output: ValidationCommandOutput
    failure: BaseException | None

    def __post_init__(self) -> None:
        _require_exact_type(
            self.output,
            ValidationCommandOutput,
            "captured validation output",
        )


@dataclass(frozen=True, slots=True)
class _StartedValidationCommand:
    process: subprocess.Popen[bytes]
    handshake_reader: BinaryIO
    started_at_monotonic: float

    def __post_init__(self) -> None:
        if not isinstance(self.process, subprocess.Popen):
            raise ValueError("started validation process must be subprocess.Popen")
        if not hasattr(self.handshake_reader, "fileno"):
            raise ValueError("validation handshake reader must expose fileno")
        if (
            type(self.started_at_monotonic) is not float
            or self.started_at_monotonic <= 0
        ):
            raise ValueError("started validation monotonic time must be positive")


class _ValidationPipeCapture(ProcessGroupInterruption):
    """Drain both pipes while the supervisor keeps the leader unreaped."""

    def __init__(
        self,
        stdout: BinaryIO,
        stderr: BinaryIO,
        handshake_reader: BinaryIO,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
    ) -> None:
        self._policy = _require_exact_type(
            policy,
            ContainedCommandOutputPolicy,
            "validation pipe capture policy",
        )
        self._selector = selectors.DefaultSelector()
        self._streams: dict[int, BinaryIO] = {}
        self._buffers: dict[int, bytearray] = {}
        self._stdout_descriptor = self._register(stdout)
        self._stderr_descriptor = self._register(stderr)
        self._all_streams = (stdout, stderr)
        self._handshake_reader = handshake_reader
        self._handshake_descriptor = handshake_reader.fileno()
        self._selector.register(self._handshake_descriptor, selectors.EVENT_READ)
        self._handshake_open = True
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

    def _register(self, stream: BinaryIO) -> int:
        descriptor = stream.fileno()
        self._selector.register(descriptor, selectors.EVENT_READ)
        self._streams[descriptor] = stream
        self._buffers[descriptor] = bytearray()
        return descriptor

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

    def finalize(self) -> _CapturedValidationStreams:
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
        close_failure = self._close_all()
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
        return _CapturedValidationStreams(
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
        for key, _event_mask in self._selector.select(timeout_seconds):
            descriptor = int(key.fd)
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
                    self._selector.unregister(descriptor)
                    self._handshake_open = False
                continue
            read_size = min(65_536, maximum_bytes - bytes_read)
            if read_size <= 0:
                break
            chunk = os.read(descriptor, read_size)
            if chunk:
                self._buffers[descriptor].extend(chunk)
                bytes_read += len(chunk)
                continue
            self._selector.unregister(descriptor)
            self._streams.pop(descriptor)
        return bytes_read

    def _close_all(self) -> BaseException | None:
        failures: list[BaseException] = []
        for descriptor in tuple(self._streams):
            self._selector.unregister(descriptor)
            self._streams.pop(descriptor, None)
        for stream in self._all_streams:
            try:
                stream.close()
            except BaseException as error:
                failures.append(error)
        if self._handshake_open:
            try:
                self._selector.unregister(self._handshake_descriptor)
            except BaseException as error:
                failures.append(error)
            self._handshake_open = False
        try:
            self._handshake_reader.close()
        except BaseException as error:
            failures.append(error)
        self._selector.close()
        if not failures:
            return None
        if len(failures) == 1:
            return failures[0]
        return BaseExceptionGroup("validation output streams did not close", failures)


class PosixContainedValidationCommandRunner:
    """Start, capture, contain, and reap one validation process tree."""

    def __init__(
        self,
        process_group_supervisor: ProcessGroupSupervisor,
        output_policy: ContainedCommandOutputPolicy,
    ) -> None:
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

    def run(self, command: ContainedValidationCommand) -> ValidationCommandExecution:
        command = _require_exact_type(
            command,
            ContainedValidationCommand,
            "PosixContainedValidationCommandRunner.run command",
        )
        try:
            started = self._spawn(command)
        except BaseException as error:
            return ValidationCommandExecution(
                child=ValidationCommandNotStarted(error),
                cleanup=ValidationCommandCleanupNotStarted(),
                output=ValidationCommandOutput("", ""),
            )
        return self._run_started(started, command.deadline)

    @staticmethod
    def _spawn(
        command: ContainedValidationCommand,
    ) -> _StartedValidationCommand:
        started_at_monotonic = time.monotonic()
        read_descriptor, write_descriptor = os.pipe()
        try:
            environment = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.child_environment(
                command.environment,
                write_descriptor,
            )
            process = subprocess.Popen(
                command.command,
                shell=True,
                cwd=command.working_directory,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                start_new_session=True,
                preexec_fn=child_signal_reset_preexec(),
                pass_fds=(write_descriptor,),
            )
        except BaseException:
            os.close(read_descriptor)
            os.close(write_descriptor)
            raise
        os.close(write_descriptor)
        return _StartedValidationCommand(
            process,
            cast(BinaryIO, os.fdopen(read_descriptor, "rb", buffering=0)),
            started_at_monotonic,
        )

    def _run_started(
        self,
        started: _StartedValidationCommand,
        deadline: ValidationExecutionDeadline,
    ) -> ValidationCommandExecution:
        process = started.process
        if process.stdout is None or process.stderr is None:
            started.handshake_reader.close()
            return self._abort_without_capture(
                process,
                RuntimeError("validation command did not expose both output pipes"),
            )
        capture = _ValidationPipeCapture(
            cast(BinaryIO, process.stdout),
            cast(BinaryIO, process.stderr),
            started.handshake_reader,
            self._output_policy,
            deadline,
            started.started_at_monotonic,
        )
        leader = OwnedProcessGroupLeader(process.pid)
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
        process.returncode = supervision.termination.leader_exit_code
        captured = capture.finalize()
        child = ValidationCommandExited(process.pid, process.returncode)
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
        process: subprocess.Popen[bytes],
        original_error: BaseException,
    ) -> ValidationCommandExecution:
        leader = OwnedProcessGroupLeader(process.pid)
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            return ValidationCommandExecution(
                child=ValidationCommandExitUnknown(process.pid),
                cleanup=ValidationCommandCleanupFailed(
                    _combined_error(
                        "validation setup and cleanup both failed",
                        original_error,
                        cleanup_error,
                    )
                ),
                output=ValidationCommandOutput("", ""),
            )
        process.returncode = termination.leader_exit_code
        return ValidationCommandExecution(
            child=ValidationCommandExited(process.pid, process.returncode),
            cleanup=ValidationCommandCleanupFailed(original_error),
            output=ValidationCommandOutput("", ""),
        )

    def _recover_after_supervision_failure(
        self,
        process: subprocess.Popen[bytes],
        leader: OwnedProcessGroupLeader,
        capture: _ValidationPipeCapture,
        supervision_error: BaseException,
    ) -> ValidationCommandExecution:
        try:
            termination = self._supervisor.abort(leader)
        except BaseException as cleanup_error:
            captured = capture.finalize()
            return ValidationCommandExecution(
                child=ValidationCommandExitUnknown(process.pid),
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
        process.returncode = termination.leader_exit_code
        captured = capture.finalize()
        return ValidationCommandExecution(
            child=ValidationCommandExited(process.pid, process.returncode),
            cleanup=ValidationCommandCleanupFailed(
                _combined_error(
                    "validation supervision and output finalization both failed",
                    supervision_error,
                    captured.failure,
                )
            ),
            output=captured.output,
        )
