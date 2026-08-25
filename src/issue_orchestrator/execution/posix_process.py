# pyright: strict
"""Deep POSIX process module with gap-free PID and process-group ownership."""

from __future__ import annotations

import fcntl
import os
import resource
import signal
import termios
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..domain.executor import ExecutorProcessTerminationPolicy
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessControllingTerminal,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..domain.process_group import OwnedProcessGroupLeader
from ..ports.posix_process import (
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
    schema_version: Literal[1] = 1
    arguments: tuple[str, ...]
    working_directory: str = Field(min_length=1)
    inherited_file_descriptors: tuple[int, ...]
    terminal: _TerminalRecord


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
        raise AssertionError("PosixProcessGroupMode is a closed enum")

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

    def __init__(self, process_id: int) -> None:
        if type(process_id) is not int or process_id <= 1:
            raise ValueError("OwnedPosixProcess.process_id must be above 1")
        self._process_id = process_id
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


class RetainedPosixProcessLauncher:
    """Wrap, spawn, and recover a process before exposing its exact handle."""

    def __init__(
        self,
        child_program: PosixProcessProgram,
        primitive: PosixSpawnPrimitive,
        process_group_supervisor: ProcessGroupSupervisor,
        recovery_policy: ExecutorProcessTerminationPolicy,
    ) -> None:
        if type(child_program) is not PosixProcessProgram:
            raise ValueError("retained launcher child_program must be typed")
        if type(recovery_policy) is not ExecutorProcessTerminationPolicy:
            raise ValueError("retained launcher recovery_policy must be typed")
        if not callable(getattr(primitive, "start", None)):
            raise ValueError("retained launcher primitive must implement its port")
        if not callable(getattr(process_group_supervisor, "abort", None)):
            raise ValueError("retained launcher supervisor must implement its port")
        self._child_program = child_program
        self._primitive = primitive
        self._supervisor = process_group_supervisor
        self._recovery_policy = recovery_policy

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        if type(specification) is not PosixProcessLaunchSpec:
            raise ValueError("retained process launcher requires a typed spec")
        invocation = self._invocation(specification)
        wrapped_program = PosixProcessProgram(
            (
                *self._child_program.arguments,
                "--request-json",
                invocation.model_dump_json(),
            )
        )
        activation = self._primitive.start(
            PosixSpawnPrimitiveRequest(
                wrapped_program,
                specification.environment,
                specification.group_mode,
                specification.descriptor_mappings,
            )
        )
        if type(activation) is PosixSpawnPrimitiveRejected:
            return PosixProcessLaunchRejected(activation.error)
        if type(activation) is PosixSpawnPrimitiveStarted:
            return PosixProcessLaunchStarted(OwnedPosixProcess(activation.process_id))
        if type(activation) is not PosixSpawnPrimitiveIndeterminate:
            raise AssertionError("posix spawn primitive result is a closed union")
        return self._recover_indeterminate(
            OwnedPosixProcess(activation.process_id),
            specification.group_mode,
            activation.error,
        )

    @staticmethod
    def _invocation(
        specification: PosixProcessLaunchSpec,
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
            terminal=terminal_record,
        )

    def _recover_indeterminate(
        self,
        process: OwnedPosixProcess,
        group_mode: PosixProcessGroupMode,
        activation_error: BaseException,
    ) -> PosixProcessLaunch:
        try:
            if group_mode not in (
                PosixProcessGroupMode.NEW_SESSION,
                PosixProcessGroupMode.NEW_PROCESS_GROUP,
            ):
                raise AssertionError("PosixProcessGroupMode is a closed enum")
            termination = self._supervisor.abort(
                OwnedProcessGroupLeader(process.process_id)
            )
            process.record_external_reap(termination.leader_exit_code)
            exit_code = termination.leader_exit_code
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
    terminal = invocation.terminal
    if type(terminal) is _ControllingTerminalRecord:
        fcntl.ioctl(terminal.child_file_descriptor, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(terminal.child_file_descriptor, os.getpgrp())
    elif type(terminal) is not _WithoutTerminalRecord:
        raise AssertionError("terminal record is a closed union")
    os.chdir(invocation.working_directory)
    _close_unmapped_descriptors(invocation.inherited_file_descriptors)
    os.execvpe(invocation.arguments[0], invocation.arguments, os.environ)
    raise AssertionError("retained POSIX child exec unexpectedly returned")


def _close_unmapped_descriptors(inherited: tuple[int, ...]) -> None:
    maximum = min(1_048_576, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    preserved = tuple(sorted(set((0, 1, 2, *inherited))))
    for lower, upper in zip(preserved, (*preserved[1:], maximum), strict=True):
        os.closerange(lower + 1, upper)
