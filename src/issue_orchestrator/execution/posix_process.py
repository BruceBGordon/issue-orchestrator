# pyright: strict
"""Deep POSIX process module with gap-free PID and process-group ownership."""

from __future__ import annotations

import fcntl
import os
import resource
import selectors
import signal
import termios
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..domain.executor import ExecutorProcessTerminationPolicy
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessActivationPolicy,
    PosixProcessControllingTerminal,
    PosixProcessGroup,
    PosixProcessGroupMode,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..domain.process_group import OwnedProcessGroupLeader
from ..ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessLaunch,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from ..ports.posix_spawn_primitive import (
    PosixSpawnPrimitive,
    PosixSpawnPrimitiveIndeterminate,
    PosixSpawnPrimitiveRejected,
    PosixSpawnPrimitiveRequest,
    PosixSpawnPrimitiveStarted,
)
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from .independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
)
from .strict_wire_record import StrictWireRecord


_ACTIVATION_SIGNALS = frozenset((signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
_CHILD_DEFAULT_SIGNALS = frozenset(
    (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGTERM)
)
_ACTIVATION_GRANT = b"G"
_MAX_EXEC_STATUS_BYTES = 4096
_MAX_EXEC_ERROR_REPR = 2048


class _WithoutTerminalRecord(StrictWireRecord):
    kind: Literal["without-terminal"] = "without-terminal"


class _ControllingTerminalRecord(StrictWireRecord):
    kind: Literal["controlling-terminal"] = "controlling-terminal"
    child_file_descriptor: int = Field(ge=0)


_TerminalRecord = Annotated[
    _WithoutTerminalRecord | _ControllingTerminalRecord,
    Field(discriminator="kind"),
]


class _PosixProcessChildInvocation(StrictWireRecord):
    schema_version: Literal[3] = 3
    arguments: tuple[str, ...]
    working_directory: str = Field(min_length=1)
    inherited_file_descriptors: tuple[int, ...]
    activation_gate_file_descriptor: int = Field(ge=0)
    exec_status_file_descriptor: int = Field(ge=0)
    terminal: _TerminalRecord


class _PosixExecRejectedRecord(StrictWireRecord):
    schema_version: Literal[1] = 1
    error_type: str = Field(min_length=1, max_length=256)
    error_repr: str = Field(min_length=1, max_length=_MAX_EXEC_ERROR_REPR)


@dataclass(frozen=True, slots=True)
class _DuplicatedDescriptor:
    """One parent-owned transit FD and its exact child target."""

    transit_file_descriptor: int
    child_file_descriptor: int


class _SpawnDescriptorOwner:
    """Incrementally duplicate and independently close all spawn-source FDs."""

    def __init__(self, mappings: tuple[PosixDescriptorMapping, ...]) -> None:
        targets = tuple(mapping.child_file_descriptor for mapping in mappings)
        minimum_descriptor = max((64, *(target + 1 for target in targets)))
        duplicated: list[_DuplicatedDescriptor] = []
        try:
            for mapping in mappings:
                transit = fcntl.fcntl(
                    mapping.source_file_descriptor,
                    fcntl.F_DUPFD_CLOEXEC,
                    minimum_descriptor,
                )
                duplicated.append(
                    _DuplicatedDescriptor(transit, mapping.child_file_descriptor)
                )
                minimum_descriptor = transit + 1
        except BaseException as primary_error:
            cleanup = self._close_descriptors(tuple(duplicated))
            raise _error_with_cleanup(
                "spawn descriptor acquisition and cleanup failed",
                primary_error,
                cleanup,
            )
        self._descriptors = tuple(duplicated)
        self._closed = False

    @property
    def file_actions(self) -> tuple[tuple[int, ...], ...]:
        if self._closed:
            raise RuntimeError("spawn descriptor owner is closed")
        actions: list[tuple[int, ...]] = []
        for descriptor in self._descriptors:
            actions.append(
                (
                    os.POSIX_SPAWN_DUP2,
                    descriptor.transit_file_descriptor,
                    descriptor.child_file_descriptor,
                )
            )
            actions.append((os.POSIX_SPAWN_CLOSE, descriptor.transit_file_descriptor))
        return tuple(actions)

    def close(self) -> CleanupOutcome:
        if self._closed:
            return CleanupSucceeded()
        self._closed = True
        return self._close_descriptors(self._descriptors)

    @staticmethod
    def _close_descriptors(
        descriptors: tuple[_DuplicatedDescriptor, ...],
    ) -> CleanupOutcome:
        return IndependentCleanupPlan(
            tuple(
                CleanupAction(
                    f"spawn-transit-fd-{descriptor.transit_file_descriptor}-close",
                    lambda fd=descriptor.transit_file_descriptor: os.close(fd),
                )
                for descriptor in descriptors
            )
        ).run()


@dataclass(frozen=True, slots=True)
class _ActivationGateReleased:
    """The child received its grant and the process owns the remaining endpoint."""

    process: OwnedPosixProcess


@dataclass(frozen=True, slots=True)
class _ActivationGateReleaseFailed:
    """The child received no complete grant and every endpoint was closed."""

    error: BaseException


_ActivationGateRelease = _ActivationGateReleased | _ActivationGateReleaseFailed


@dataclass(frozen=True, slots=True)
class _PosixExecSucceeded:
    """The wrapper's close-on-exec status writer closed without a record."""


@dataclass(frozen=True, slots=True)
class _PosixExecRejected:
    """The wrapper reported one bounded setup/exec failure record."""

    record: _PosixExecRejectedRecord


_PosixExecStatus = _PosixExecSucceeded | _PosixExecRejected


class _ProcessActivationGate:
    """Keep opaque child code behind a one-byte parent ownership boundary."""

    def __init__(self, mapped_child_descriptors: tuple[int, ...]) -> None:
        if type(mapped_child_descriptors) is not tuple or any(
            type(descriptor) is not int or descriptor < 0
            for descriptor in mapped_child_descriptors
        ):
            raise ValueError("activation gate child descriptors must be typed")
        self._reader, self._writer = os.pipe()
        try:
            self._exec_status_reader, self._exec_status_writer = os.pipe()
        except BaseException as acquisition_error:
            cleanup = IndependentCleanupPlan(
                (
                    CleanupAction(
                        "partial-activation-gate-reader-close",
                        lambda: os.close(self._reader),
                    ),
                    CleanupAction(
                        "partial-activation-gate-writer-close",
                        lambda: os.close(self._writer),
                    ),
                )
            ).run()
            raise _error_with_cleanup(
                "exec status acquisition and activation gate cleanup failed",
                acquisition_error,
                cleanup,
            )
        maximum_child_descriptor = max((64, *(mapped_child_descriptors)))
        self._child_reader = maximum_child_descriptor + 1
        self._child_exec_status_writer = maximum_child_descriptor + 2
        self._reader_open = True
        self._writer_open = True
        self._exec_status_reader_open = True
        self._exec_status_writer_open = True

    @property
    def child_reader(self) -> int:
        return self._child_reader

    @property
    def child_exec_status_writer(self) -> int:
        return self._child_exec_status_writer

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return (
            PosixDescriptorMapping(self._reader, self._child_reader),
            PosixDescriptorMapping(
                self._exec_status_writer,
                self._child_exec_status_writer,
            ),
        )

    def release(self, process_id: int) -> _ActivationGateRelease:
        if (
            not self._reader_open
            or not self._writer_open
            or not self._exec_status_reader_open
            or not self._exec_status_writer_open
        ):
            raise RuntimeError("activation gate is not fully owned")
        try:
            os.close(self._reader)
            self._reader_open = False
            os.close(self._exec_status_writer)
            self._exec_status_writer_open = False
            if os.write(self._writer, _ACTIVATION_GRANT) != len(_ACTIVATION_GRANT):
                raise RuntimeError("process activation gate performed a short write")
        except BaseException as release_error:
            return _ActivationGateReleaseFailed(
                _error_with_cleanup(
                    "process activation grant and cleanup failed",
                    release_error,
                    self.close(),
                )
            )
        writer = self._writer
        exec_status_reader = self._exec_status_reader
        self._writer_open = False
        self._exec_status_reader_open = False
        return _ActivationGateReleased(
            OwnedPosixProcess(process_id, writer, exec_status_reader)
        )

    def close(self) -> CleanupOutcome:
        actions: list[CleanupAction] = []
        if self._reader_open:
            actions.append(
                CleanupAction(
                    "activation-gate-reader-close",
                    lambda: os.close(self._reader),
                )
            )
            self._reader_open = False
        if self._writer_open:
            actions.append(
                CleanupAction(
                    "activation-gate-writer-close",
                    lambda: os.close(self._writer),
                )
            )
            self._writer_open = False
        if self._exec_status_reader_open:
            actions.append(
                CleanupAction(
                    "exec-status-reader-close",
                    lambda: os.close(self._exec_status_reader),
                )
            )
            self._exec_status_reader_open = False
        if self._exec_status_writer_open:
            actions.append(
                CleanupAction(
                    "exec-status-writer-close",
                    lambda: os.close(self._exec_status_writer),
                )
            )
            self._exec_status_writer_open = False
        return IndependentCleanupPlan(tuple(actions)).run()


def _cleanup_errors(outcome: CleanupOutcome) -> tuple[BaseException, ...]:
    if type(outcome) is CleanupSucceeded:
        return ()
    if type(outcome) is not CleanupFailed:
        raise AssertionError("cleanup outcome is a closed union")
    return tuple(failure.error for failure in outcome.failures)


def _error_with_cleanup(
    message: str,
    primary_error: BaseException,
    cleanup: CleanupOutcome,
) -> BaseException:
    cleanup_errors = _cleanup_errors(cleanup)
    if not cleanup_errors:
        return primary_error
    return BaseExceptionGroup(message, (primary_error, *cleanup_errors))


class MaskedPosixSpawnPrimitive:
    """Call ``posix_spawn`` with activation signals blocked through PID retention."""

    def start(
        self,
        request: PosixSpawnPrimitiveRequest,
    ) -> (
        PosixSpawnPrimitiveStarted
        | PosixSpawnPrimitiveRejected
        | PosixSpawnPrimitiveIndeterminate
    ):
        if type(request) is not PosixSpawnPrimitiveRequest:
            raise ValueError("MaskedPosixSpawnPrimitive requires a typed request")
        try:
            descriptors = _SpawnDescriptorOwner(request.descriptor_mappings)
        except BaseException as error:
            return PosixSpawnPrimitiveRejected(error)
        try:
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                _ACTIVATION_SIGNALS,
            )
        except BaseException as error:
            return PosixSpawnPrimitiveRejected(
                _error_with_cleanup(
                    "spawn signal-mask setup and descriptor cleanup failed",
                    error,
                    descriptors.close(),
                )
            )
        try:
            process_id = self._spawn(request, descriptors.file_actions)
        except BaseException as spawn_error:
            restoration = self._restore_mask(previous_mask)
            descriptor_cleanup = descriptors.close()
            errors = (
                spawn_error,
                *_cleanup_errors(restoration),
                *_cleanup_errors(descriptor_cleanup),
            )
            return PosixSpawnPrimitiveRejected(
                errors[0]
                if len(errors) == 1
                else BaseExceptionGroup("posix spawn was rejected", errors)
            )
        restoration = self._restore_mask(previous_mask)
        descriptor_cleanup = descriptors.close()
        finalization_errors = (
            *_cleanup_errors(restoration),
            *_cleanup_errors(descriptor_cleanup),
        )
        if finalization_errors:
            return PosixSpawnPrimitiveIndeterminate(
                process_id,
                finalization_errors[0]
                if len(finalization_errors) == 1
                else BaseExceptionGroup(
                    "posix spawn parent finalization failed",
                    finalization_errors,
                ),
            )
        return PosixSpawnPrimitiveStarted(process_id)

    @staticmethod
    def _spawn(
        request: PosixSpawnPrimitiveRequest,
        file_actions: tuple[tuple[int, ...], ...],
    ) -> int:
        arguments = request.program.arguments
        environment = request.environment.as_mapping()
        if request.group_mode is PosixProcessGroupMode.NEW_SESSION:
            return os.posix_spawn(
                arguments[0],
                arguments,
                environment,
                file_actions=file_actions,
                setsigmask=(),
                setsigdef=_CHILD_DEFAULT_SIGNALS,
                setsid=True,
            )
        if request.group_mode is PosixProcessGroupMode.NEW_PROCESS_GROUP:
            return os.posix_spawn(
                arguments[0],
                arguments,
                environment,
                file_actions=file_actions,
                setsigmask=(),
                setsigdef=_CHILD_DEFAULT_SIGNALS,
                setpgroup=0,
            )
        if type(request.group_mode) is PosixProcessJoinGroup:
            return os.posix_spawn(
                arguments[0],
                arguments,
                environment,
                file_actions=file_actions,
                setsigmask=(),
                setsigdef=_CHILD_DEFAULT_SIGNALS,
                setpgroup=request.group_mode.process_group_id,
            )
        raise AssertionError("PosixProcessGroup is a closed union")

    @staticmethod
    def _restore_mask(
        previous_mask: set[int | signal.Signals],
    ) -> CleanupOutcome:
        return IndependentCleanupPlan(
            (
                CleanupAction(
                    "activation-signal-mask-restore",
                    lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask),
                ),
            )
        ).run()


class OwnedPosixProcess:
    """Exact wait/kill handle for a child created by the retained spawn owner."""

    def __init__(
        self,
        process_id: int,
        activation_gate_writer: int,
        exec_status_reader: int,
    ) -> None:
        if type(process_id) is not int or process_id <= 1:
            raise ValueError("OwnedPosixProcess.process_id must be above 1")
        if type(activation_gate_writer) is not int or activation_gate_writer < 0:
            raise ValueError(
                "OwnedPosixProcess.activation_gate_writer must be non-negative"
            )
        if type(exec_status_reader) is not int or exec_status_reader < 0:
            raise ValueError(
                "OwnedPosixProcess.exec_status_reader must be non-negative"
            )
        self._process_id = process_id
        self._activation_gate_writer = activation_gate_writer
        self._activation_gate_writer_open = True
        self._exec_status_reader = exec_status_reader
        self._exec_status_reader_open = True
        self._exec_status_consumed = False
        self._return_code: int | None = None

    @property
    def process_id(self) -> int:
        return self._process_id

    @property
    def return_code(self) -> int | None:
        return self._return_code

    def poll(self) -> int | None:
        if self._return_code is not None:
            return self._return_code
        reaped_id, wait_status = os.waitpid(self._process_id, os.WNOHANG)
        if reaped_id == 0:
            return None
        if reaped_id != self._process_id:
            raise RuntimeError("waitpid returned a different child")
        self._return_code = os.waitstatus_to_exitcode(wait_status)
        self._close_activation_resources()
        return self._return_code

    def wait(self, timeout_seconds: float) -> int:
        if type(timeout_seconds) is not float or timeout_seconds <= 0:
            raise ValueError("OwnedPosixProcess.wait timeout must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            return_code = self.poll()
            if return_code is not None:
                return return_code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"process {self._process_id} did not exit within "
                    f"{timeout_seconds:.3f}s"
                )
            time.sleep(min(0.01, remaining))

    def kill(self) -> None:
        try:
            os.kill(self._process_id, signal.SIGKILL)
        except ProcessLookupError:
            return

    def record_external_reap(self, exit_code: int) -> None:
        if type(exit_code) is not int:
            raise ValueError("external process exit code must be an int")
        if self._return_code is not None:
            raise RuntimeError("process reaping was recorded twice")
        self._return_code = exit_code
        self._close_activation_resources()

    def await_exec_status(self, timeout_seconds: float) -> _PosixExecStatus:
        if type(timeout_seconds) is not float or timeout_seconds <= 0:
            raise ValueError("POSIX exec status timeout must be positive")
        if not self._exec_status_reader_open or self._exec_status_consumed:
            raise RuntimeError("POSIX exec status was already consumed")
        with selectors.DefaultSelector() as selector:
            selector.register(self._exec_status_reader, selectors.EVENT_READ)
            ready = selector.select(timeout_seconds)
        if not ready:
            raise TimeoutError(
                f"process {self._process_id} did not publish exec status within "
                f"{timeout_seconds:.3f}s"
            )
        payload = os.read(self._exec_status_reader, _MAX_EXEC_STATUS_BYTES + 1)
        self._exec_status_consumed = True
        if len(payload) > _MAX_EXEC_STATUS_BYTES:
            raise RuntimeError("POSIX exec failure status exceeds size limit")
        if not payload:
            return _PosixExecSucceeded()
        try:
            record = _PosixExecRejectedRecord.model_validate_json(payload)
        except ValidationError as error:
            raise RuntimeError("malformed POSIX exec failure status") from error
        return _PosixExecRejected(record)

    def _close_activation_resources(self) -> None:
        errors: list[BaseException] = []
        if not self._activation_gate_writer_open:
            pass
        else:
            try:
                os.close(self._activation_gate_writer)
            except BaseException as error:
                errors.append(error)
            self._activation_gate_writer_open = False
        if self._exec_status_reader_open:
            try:
                os.close(self._exec_status_reader)
            except BaseException as error:
                errors.append(error)
            self._exec_status_reader_open = False
        if errors:
            raise BaseExceptionGroup(
                "POSIX process activation resource cleanup failed",
                errors,
            )


class _OwnedUnreleasedPosixProcess:
    """Exact child handle used only while its opaque activation gate is closed."""

    def __init__(self, process_id: int) -> None:
        if type(process_id) is not int or process_id <= 1:
            raise ValueError("unreleased POSIX process id must be above 1")
        self._process_id = process_id
        self._return_code: int | None = None

    @property
    def process_id(self) -> int:
        return self._process_id

    def poll(self) -> int | None:
        if self._return_code is not None:
            return self._return_code
        reaped_id, wait_status = os.waitpid(self._process_id, os.WNOHANG)
        if reaped_id == 0:
            return None
        if reaped_id != self._process_id:
            raise RuntimeError("waitpid returned a different unreleased child")
        self._return_code = os.waitstatus_to_exitcode(wait_status)
        return self._return_code

    def wait(self, timeout_seconds: float) -> int:
        if type(timeout_seconds) is not float or timeout_seconds <= 0:
            raise ValueError("unreleased child wait timeout must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            return_code = self.poll()
            if return_code is not None:
                return return_code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"unreleased process {self._process_id} did not exit within "
                    f"{timeout_seconds:.3f}s"
                )
            time.sleep(min(0.01, remaining))

    def kill(self) -> None:
        try:
            os.kill(self._process_id, signal.SIGKILL)
        except ProcessLookupError:
            return


class RetainedPosixProcessLauncher:
    """Wrap, spawn, and recover a process before exposing its exact handle."""

    def __init__(
        self,
        child_program: PosixProcessProgram,
        primitive: PosixSpawnPrimitive,
        process_group_supervisor: ProcessGroupSupervisor,
        activation_policy: PosixProcessActivationPolicy,
        recovery_policy: ExecutorProcessTerminationPolicy,
    ) -> None:
        if type(child_program) is not PosixProcessProgram:
            raise ValueError("retained launcher child_program must be typed")
        if type(recovery_policy) is not ExecutorProcessTerminationPolicy:
            raise ValueError("retained launcher recovery_policy must be typed")
        if type(activation_policy) is not PosixProcessActivationPolicy:
            raise ValueError("retained launcher activation_policy must be typed")
        if not callable(getattr(primitive, "start", None)):
            raise ValueError("retained launcher primitive must implement its port")
        if not callable(getattr(process_group_supervisor, "abort", None)):
            raise ValueError("retained launcher supervisor must implement its port")
        self._child_program = child_program
        self._primitive = primitive
        self._supervisor = process_group_supervisor
        self._activation_policy = activation_policy
        self._recovery_policy = recovery_policy

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        if type(specification) is not PosixProcessLaunchSpec:
            raise ValueError("retained process launcher requires a typed spec")
        try:
            gate = _ProcessActivationGate(
                tuple(
                    mapping.child_file_descriptor
                    for mapping in specification.descriptor_mappings
                )
            )
        except BaseException as error:
            return PosixProcessLaunchRejected(error)
        try:
            invocation = self._invocation(
                specification,
                gate.child_reader,
                gate.child_exec_status_writer,
            )
            wrapped_program = PosixProcessProgram(
                (
                    *self._child_program.arguments,
                    "--request-json",
                    invocation.model_dump_json(),
                )
            )
            primitive_request = PosixSpawnPrimitiveRequest(
                wrapped_program,
                specification.environment,
                specification.group_mode,
                (
                    *specification.descriptor_mappings,
                    *gate.descriptor_mappings,
                ),
            )
        except BaseException as preparation_error:
            return PosixProcessLaunchRejected(
                _error_with_cleanup(
                    "process activation preparation and gate cleanup failed",
                    preparation_error,
                    gate.close(),
                )
            )
        try:
            activation = self._primitive.start(primitive_request)
        except BaseException as primitive_error:
            return PosixProcessLaunchRejected(
                _error_with_cleanup(
                    "process activation primitive and gate cleanup failed",
                    primitive_error,
                    gate.close(),
                )
            )
        if type(activation) is PosixSpawnPrimitiveRejected:
            return PosixProcessLaunchRejected(
                _error_with_cleanup(
                    "process activation rejection and gate cleanup failed",
                    activation.error,
                    gate.close(),
                )
            )
        if type(activation) is PosixSpawnPrimitiveStarted:
            released = gate.release(activation.process_id)
            if type(released) is _ActivationGateReleased:
                return self._complete_exec_handshake(
                    released.process,
                    specification.group_mode,
                )
            if type(released) is not _ActivationGateReleaseFailed:
                raise AssertionError("activation gate release is a closed union")
            return self._recover_indeterminate(
                _OwnedUnreleasedPosixProcess(activation.process_id),
                specification.group_mode,
                released.error,
            )
        if type(activation) is not PosixSpawnPrimitiveIndeterminate:
            raise AssertionError("posix spawn primitive result is a closed union")
        activation_error = _error_with_cleanup(
            "process activation and gate cleanup failed",
            activation.error,
            gate.close(),
        )
        return self._recover_indeterminate(
            _OwnedUnreleasedPosixProcess(activation.process_id),
            specification.group_mode,
            activation_error,
        )

    @staticmethod
    def _invocation(
        specification: PosixProcessLaunchSpec,
        activation_gate_file_descriptor: int,
        exec_status_file_descriptor: int,
    ) -> _PosixProcessChildInvocation:
        terminal = specification.terminal
        if type(terminal) is PosixProcessWithoutTerminal:
            terminal_record: _TerminalRecord = _WithoutTerminalRecord()
        elif type(terminal) is PosixProcessControllingTerminal:
            terminal_record = _ControllingTerminalRecord(
                child_file_descriptor=terminal.child_file_descriptor
            )
        else:
            raise AssertionError("PosixProcessTerminal is a closed union")
        return _PosixProcessChildInvocation(
            arguments=specification.program.arguments,
            working_directory=str(specification.working_directory),
            inherited_file_descriptors=tuple(
                mapping.child_file_descriptor
                for mapping in specification.descriptor_mappings
                if mapping.child_file_descriptor > 2
            ),
            activation_gate_file_descriptor=activation_gate_file_descriptor,
            exec_status_file_descriptor=exec_status_file_descriptor,
            terminal=terminal_record,
        )

    def _complete_exec_handshake(
        self,
        process: OwnedPosixProcess,
        group_mode: PosixProcessGroup,
    ) -> PosixProcessLaunch:
        try:
            status = process.await_exec_status(
                self._activation_policy.exec_handshake_timeout_seconds
            )
        except BaseException as handshake_error:
            return self._recover_started_process(
                process,
                group_mode,
                handshake_error,
            )
        if type(status) is _PosixExecSucceeded:
            return PosixProcessLaunchStarted(process)
        if type(status) is not _PosixExecRejected:
            raise AssertionError("POSIX exec status is a closed union")
        try:
            exit_code = process.wait(
                self._recovery_policy.forceful_shutdown_seconds
            )
        except BaseException as recovery_error:
            return PosixProcessLaunchRecoveryFailed(
                process.process_id,
                RuntimeError("retained POSIX wrapper rejected exec"),
                recovery_error,
            )
        return PosixProcessExecRejected(
            process.process_id,
            exit_code,
            status.record.error_type,
            status.record.error_repr,
        )

    def _recover_started_process(
        self,
        process: OwnedPosixProcess,
        group_mode: PosixProcessGroup,
        activation_error: BaseException,
    ) -> PosixProcessLaunch:
        try:
            if group_mode in (
                PosixProcessGroupMode.NEW_SESSION,
                PosixProcessGroupMode.NEW_PROCESS_GROUP,
            ):
                termination = self._supervisor.abort(
                    OwnedProcessGroupLeader(process.process_id)
                )
                exit_code = termination.leader_exit_code
                try:
                    process.record_external_reap(exit_code)
                except BaseException as evidence_error:
                    activation_error = BaseExceptionGroup(
                        "process activation and contained-handle cleanup failed",
                        (activation_error, evidence_error),
                    )
            elif type(group_mode) is PosixProcessJoinGroup:
                process.kill()
                exit_code = process.wait(
                    self._recovery_policy.forceful_shutdown_seconds
                )
            else:
                raise AssertionError("PosixProcessGroup is a closed union")
        except BaseException as recovery_error:
            return PosixProcessLaunchRecoveryFailed(
                process.process_id,
                activation_error,
                recovery_error,
            )
        return PosixProcessLaunchRecovered(
            process.process_id,
            exit_code,
            activation_error,
        )

    def _recover_indeterminate(
        self,
        process: _OwnedUnreleasedPosixProcess,
        group_mode: PosixProcessGroup,
        activation_error: BaseException,
    ) -> PosixProcessLaunch:
        try:
            if group_mode in (
                PosixProcessGroupMode.NEW_SESSION,
                PosixProcessGroupMode.NEW_PROCESS_GROUP,
            ):
                termination = self._supervisor.abort(
                    OwnedProcessGroupLeader(process.process_id)
                )
                exit_code = termination.leader_exit_code
            elif type(group_mode) is PosixProcessJoinGroup:
                process.kill()
                exit_code = process.wait(
                    self._recovery_policy.forceful_shutdown_seconds
                )
            else:
                raise AssertionError("PosixProcessGroup is a closed union")
        except BaseException as recovery_error:
            return PosixProcessLaunchRecoveryFailed(
                process.process_id,
                activation_error,
                recovery_error,
            )
        return PosixProcessLaunchRecovered(
            process.process_id,
            exit_code,
            activation_error,
        )


def run_posix_process_child(raw_request: str) -> int:
    """Validate the wrapper contract and exec the requested process."""
    try:
        invocation = _PosixProcessChildInvocation.model_validate_json(raw_request)
    except ValidationError as error:
        raise ValueError("invalid retained POSIX child invocation") from error
    _await_activation_grant(invocation.activation_gate_file_descriptor)
    try:
        _mark_close_on_exec(invocation.exec_status_file_descriptor)
        terminal = invocation.terminal
        if type(terminal) is _ControllingTerminalRecord:
            fcntl.ioctl(terminal.child_file_descriptor, termios.TIOCSCTTY, 0)
            os.tcsetpgrp(terminal.child_file_descriptor, os.getpgrp())
        elif type(terminal) is not _WithoutTerminalRecord:
            raise AssertionError("terminal record is a closed union")
        os.chdir(invocation.working_directory)
        _close_unmapped_descriptors(
            (
                *invocation.inherited_file_descriptors,
                invocation.exec_status_file_descriptor,
            )
        )
        os.execvpe(invocation.arguments[0], invocation.arguments, os.environ)
    except BaseException as error:
        _publish_exec_rejection(
            invocation.exec_status_file_descriptor,
            invocation.arguments[0],
            error,
        )
        return 127
    raise AssertionError("retained POSIX child exec unexpectedly returned")


def _await_activation_grant(file_descriptor: int) -> None:
    try:
        grant = os.read(file_descriptor, 1)
    finally:
        os.close(file_descriptor)
    if grant != _ACTIVATION_GRANT:
        raise RuntimeError("retained POSIX child activation was not granted")


def _mark_close_on_exec(file_descriptor: int) -> None:
    flags = fcntl.fcntl(file_descriptor, fcntl.F_GETFD)
    fcntl.fcntl(file_descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def _publish_exec_rejection(
    file_descriptor: int,
    executable: str,
    error: BaseException,
) -> None:
    error_repr = f"{error!r}; executable={executable!r}"
    if len(error_repr) > _MAX_EXEC_ERROR_REPR:
        error_repr = error_repr[: _MAX_EXEC_ERROR_REPR - 3] + "..."
    record = _PosixExecRejectedRecord(
        error_type=type(error).__name__[:256],
        error_repr=error_repr,
    )
    payload = record.model_dump_json().encode("utf-8")
    if len(payload) > _MAX_EXEC_STATUS_BYTES:
        raise RuntimeError("POSIX exec rejection record exceeds size limit")
    try:
        if os.write(file_descriptor, payload) != len(payload):
            raise RuntimeError("POSIX exec status performed a short write")
    finally:
        os.close(file_descriptor)


def _close_unmapped_descriptors(inherited: tuple[int, ...]) -> None:
    maximum = min(1_048_576, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    preserved = tuple(sorted(set((0, 1, 2, *inherited))))
    for lower, upper in zip(preserved, (*preserved[1:], maximum), strict=True):
        os.closerange(lower + 1, upper)
