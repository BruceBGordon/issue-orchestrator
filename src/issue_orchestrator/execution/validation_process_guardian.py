# pyright: strict
"""Deep crash-containment owner for detached validation process groups."""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import selectors
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from ..domain.posix_pipe import PosixPipeClosed, PosixPipeCloseFailed
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessConfiguredActivationDeadline,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..domain.process_group import OwnedProcessGroupLeader
from ..domain.process_group_sentinel import (
    ProcessGroupSentinelParentLifetime,
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ..domain.validation_execution import ValidationGuardianClock
from ..ports.posix_pipe import (
    PosixPipe,
    PosixPipeFactory,
    PosixPipeReader,
    PosixPipeWriter,
)
from ..ports.posix_process import (
    PosixProcessLaunch,
    PosixProcessExecRejected,
    PosixProcessLauncher,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.validation_process_guardian import (
    ValidationProcessGuardianLaunch,
    ValidationProcessGuardianStarted,
)
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupSucceeded,
    IndependentCleanupPlan,
)
from .process_group_sentinel import (
    ProcessGroupSentinelController,
    ProcessGroupSentinelWithoutCancellation,
)
from .strict_wire_record import StrictWireRecord


_GUARDIAN_READY = b"R"
_MAX_EXEC_REJECTION_BYTES = 4096


class ValidationGuardianStartupPhase(StrEnum):
    READINESS = "readiness"
    EXEC_STATUS = "exec-status"


@dataclass(frozen=True, slots=True)
class ValidationGuardianStartupDeadlineOwner:
    """Own exact deadline observations around guardian select/read transitions."""

    clock: ValidationGuardianClock
    absolute_deadline: float

    def __post_init__(self) -> None:
        if type(self.clock) is not ValidationGuardianClock:
            raise ValueError("guardian startup deadline clock must be typed")
        if (
            type(self.absolute_deadline) is not float
            or not math.isfinite(self.absolute_deadline)
            or self.absolute_deadline <= 0
        ):
            raise ValueError(
                "guardian startup absolute deadline must be positive and finite"
            )

    def remaining_before_select(
        self,
        phase: ValidationGuardianStartupPhase,
    ) -> float:
        """Return a positive wait only after an immediate deadline observation."""
        return self._remaining(phase)

    def accept_after_read(self, phase: ValidationGuardianStartupPhase) -> None:
        """Reject readiness or decoded exec status observed at/after the deadline."""
        self._remaining(phase)

    def _remaining(self, phase: ValidationGuardianStartupPhase) -> float:
        if type(phase) is not ValidationGuardianStartupPhase:
            raise ValueError("guardian startup phase must be typed")
        observed_at = self.clock.monotonic_now()
        if (
            type(observed_at) is not float
            or not math.isfinite(observed_at)
            or observed_at <= 0
        ):
            raise ValueError(
                "guardian startup clock must return a positive finite float"
            )
        remaining = self.absolute_deadline - observed_at
        if remaining <= 0:
            raise TimeoutError(
                f"validation guardian {phase.value} exhausted its absolute "
                "startup deadline"
            )
        return remaining


def _require_error(value: object, field_name: str) -> None:
    if not isinstance(value, BaseException):
        raise ValueError(f"{field_name} must be a BaseException")


def _require_absolute_path(value: object, field_name: str) -> None:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{field_name} must be an absolute Path")


@dataclass(frozen=True, slots=True)
class ValidationProcessGuardianProgram:
    """Exact executable prefix for the validation guardian child role."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        PosixProcessProgram(self.arguments)


class _ValidationProcessGuardianInvocation(StrictWireRecord):
    schema_version: Literal[1] = 1
    arguments: tuple[str, ...]
    parent_lifetime_read_file_descriptor: int = Field(ge=0)
    guardian_ready_write_file_descriptor: int = Field(ge=0)
    command_exec_status_write_file_descriptor: int = Field(ge=0)
    sentinel_program: tuple[str, ...]
    sentinel_graceful_shutdown_seconds: float = Field(gt=0)
    sentinel_startup_timeout_seconds: float = Field(gt=0)

    def command_program(self) -> PosixProcessProgram:
        return PosixProcessProgram(self.arguments)

    def process_group_sentinel_program(self) -> ProcessGroupSentinelProgram:
        return ProcessGroupSentinelProgram(self.sentinel_program)

    def process_group_sentinel_policy(self) -> ProcessGroupSentinelPolicy:
        return ProcessGroupSentinelPolicy(
            self.sentinel_graceful_shutdown_seconds,
            self.sentinel_startup_timeout_seconds,
        )


class _ValidationCommandExecRejectedRecord(StrictWireRecord):
    error_type: str = Field(min_length=1, max_length=256)
    error_repr: str = Field(min_length=1, max_length=3072)


@dataclass(slots=True)
class _OwnedValidationParentLifetime:
    """Close exactly one parent writer that is never inherited by opaque work."""

    _writer: PosixPipeWriter
    _closed: bool = False

    def __post_init__(self) -> None:
        if not callable(getattr(self._writer, "close", None)):
            raise ValueError(
                "validation parent lifetime writer must implement its port"
            )

    def close(self) -> None:
        if self._closed:
            raise RuntimeError("validation parent lifetime was closed twice")
        self._closed = True
        self._writer.close()


@dataclass(frozen=True, slots=True)
class _TransferredGuardianActivation:
    lifetime_writer: PosixPipeWriter
    readiness_reader: PosixPipeReader
    exec_status_reader: PosixPipeReader

    def __post_init__(self) -> None:
        if not callable(getattr(self.lifetime_writer, "close", None)):
            raise ValueError("guardian lifetime writer must implement its port")
        if not callable(getattr(self.readiness_reader, "fileno", None)):
            raise ValueError("guardian readiness reader must implement its port")
        if not callable(getattr(self.exec_status_reader, "fileno", None)):
            raise ValueError("guardian exec-status reader must implement its port")


@dataclass(frozen=True, slots=True)
class _GuardianOpaqueExecStarted:
    """Close-on-exec proved that the opaque command replaced its guardian."""


@dataclass(frozen=True, slots=True)
class _GuardianOpaqueExecRejected:
    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        _ValidationCommandExecRejectedRecord(
            error_type=self.error_type,
            error_repr=self.error_repr,
        )


@dataclass(frozen=True, slots=True)
class _GuardianOpaqueExecStatusFailed:
    error: BaseException

    def __post_init__(self) -> None:
        _require_error(self.error, "guardian opaque exec status failure")


_GuardianOpaqueExecStatus = (
    _GuardianOpaqueExecStarted
    | _GuardianOpaqueExecRejected
    | _GuardianOpaqueExecStatusFailed
)


class _ValidationGuardianActivationResources:
    """Incrementally own the lifetime and readiness pipes through transfer."""

    def __init__(self, pipe_factory: PosixPipeFactory) -> None:
        if not callable(getattr(pipe_factory, "open", None)):
            raise ValueError("guardian activation pipe factory must implement its port")
        self._lifetime = pipe_factory.open()
        try:
            self._readiness = pipe_factory.open()
        except BaseException as error:
            raise _error_with_pipe_cleanup(
                "guardian readiness acquisition and lifetime cleanup failed",
                error,
                self._lifetime,
            )
        try:
            self._exec_status = pipe_factory.open()
        except BaseException as error:
            cleanup = IndependentCleanupPlan(
                (
                    CleanupAction("guardian-lifetime-pipe-close", self._close_lifetime),
                    CleanupAction(
                        "guardian-readiness-pipe-close", self._close_readiness
                    ),
                )
            ).run()
            if type(cleanup) is CleanupSucceeded:
                raise
            if type(cleanup) is not CleanupFailed:
                raise AssertionError("guardian activation cleanup is a closed union")
            raise BaseExceptionGroup(
                "guardian exec-status acquisition and cleanup failed",
                (error, *(failure.error for failure in cleanup.failures)),
            )
        self._owned = True

    @property
    def lifetime_read_descriptor(self) -> int:
        self._require_owned()
        return self._lifetime.read_descriptor

    @property
    def readiness_write_descriptor(self) -> int:
        self._require_owned()
        return self._readiness.write_descriptor

    @property
    def exec_status_write_descriptor(self) -> int:
        self._require_owned()
        return self._exec_status.write_descriptor

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        self._require_owned()
        return (
            PosixDescriptorMapping(
                self._lifetime.read_descriptor,
                self._lifetime.read_descriptor,
            ),
            PosixDescriptorMapping(
                self._readiness.write_descriptor,
                self._readiness.write_descriptor,
            ),
            PosixDescriptorMapping(
                self._exec_status.write_descriptor,
                self._exec_status.write_descriptor,
            ),
        )

    def transfer_after_launch(self) -> _TransferredGuardianActivation:
        self._require_owned()
        lifetime_writer: PosixPipeWriter | None = None
        readiness_reader: PosixPipeReader | None = None
        exec_status_reader: PosixPipeReader | None = None
        try:
            lifetime_writer = self._lifetime.transfer_writer_after_launch()
            readiness_reader = self._readiness.transfer_reader_after_launch()
            exec_status_reader = self._exec_status.transfer_reader_after_launch()
        except BaseException as error:
            cleanup = IndependentCleanupPlan(
                tuple(
                    action
                    for action in (
                        CleanupAction(
                            "guardian-lifetime-writer-close", lifetime_writer.close
                        )
                        if lifetime_writer is not None
                        else None,
                        CleanupAction(
                            "guardian-readiness-reader-close", readiness_reader.close
                        )
                        if readiness_reader is not None
                        else None,
                        CleanupAction(
                            "guardian-exec-status-reader-close",
                            exec_status_reader.close,
                        )
                        if exec_status_reader is not None
                        else None,
                        CleanupAction(
                            "guardian-lifetime-pipe-close", self._close_lifetime
                        ),
                        CleanupAction(
                            "guardian-readiness-pipe-close", self._close_readiness
                        ),
                        CleanupAction(
                            "guardian-exec-status-pipe-close", self._close_exec_status
                        ),
                    )
                    if action is not None
                )
            ).run()
            self._owned = False
            if type(cleanup) is CleanupSucceeded:
                raise
            if type(cleanup) is not CleanupFailed:
                raise AssertionError("guardian activation cleanup is a closed union")
            raise BaseExceptionGroup(
                "guardian activation transfer and cleanup failed",
                (error, *(failure.error for failure in cleanup.failures)),
            )
        self._owned = False
        return _TransferredGuardianActivation(
            lifetime_writer,
            readiness_reader,
            exec_status_reader,
        )

    def close(self) -> BaseException | None:
        if not self._owned:
            return None
        self._owned = False
        cleanup = IndependentCleanupPlan(
            (
                CleanupAction("guardian-lifetime-pipe-close", self._close_lifetime),
                CleanupAction("guardian-readiness-pipe-close", self._close_readiness),
                CleanupAction(
                    "guardian-exec-status-pipe-close", self._close_exec_status
                ),
            )
        ).run()
        if type(cleanup) is CleanupSucceeded:
            return None
        if type(cleanup) is not CleanupFailed:
            raise AssertionError("guardian activation cleanup is a closed union")
        return _one_or_group(
            "guardian activation resource cleanup failed",
            tuple(failure.error for failure in cleanup.failures),
        )

    def _close_lifetime(self) -> None:
        error = _pipe_cleanup_error(self._lifetime)
        if error is not None:
            raise error

    def _close_readiness(self) -> None:
        error = _pipe_cleanup_error(self._readiness)
        if error is not None:
            raise error

    def _close_exec_status(self) -> None:
        error = _pipe_cleanup_error(self._exec_status)
        if error is not None:
            raise error

    def _require_owned(self) -> None:
        if not self._owned:
            raise RuntimeError("guardian activation resources were already released")


class SentinelValidationProcessGuardian:
    """Launch validation behind an in-group sentinel tied to its parent."""

    def __init__(
        self,
        guardian_program: ValidationProcessGuardianProgram,
        sentinel_program: ProcessGroupSentinelProgram,
        sentinel_policy: ProcessGroupSentinelPolicy,
        process_launcher: PosixProcessLauncher,
        process_group_supervisor: ProcessGroupSupervisor,
        pipe_factory: PosixPipeFactory,
        clock: ValidationGuardianClock,
    ) -> None:
        if type(guardian_program) is not ValidationProcessGuardianProgram:
            raise ValueError("validation guardian program must be typed")
        if type(sentinel_program) is not ProcessGroupSentinelProgram:
            raise ValueError("validation sentinel program must be typed")
        if type(sentinel_policy) is not ProcessGroupSentinelPolicy:
            raise ValueError("validation sentinel policy must be typed")
        if not callable(getattr(process_launcher, "launch", None)):
            raise ValueError("validation process launcher must implement its port")
        if not callable(getattr(process_group_supervisor, "abort", None)):
            raise ValueError("validation supervisor must implement its port")
        if not callable(getattr(pipe_factory, "open", None)):
            raise ValueError("validation lifetime pipe factory must implement its port")
        if type(clock) is not ValidationGuardianClock:
            raise ValueError("validation guardian clock must be typed")
        self._guardian_program = guardian_program
        self._sentinel_program = sentinel_program
        self._sentinel_policy = sentinel_policy
        self._process_launcher = process_launcher
        self._supervisor = process_group_supervisor
        self._pipe_factory = pipe_factory
        self._clock = clock

    def launch(
        self,
        program: PosixProcessProgram,
        working_directory: Path,
        environment: PosixProcessEnvironment,
        descriptor_mappings: tuple[PosixDescriptorMapping, ...],
    ) -> ValidationProcessGuardianLaunch:
        self._validate_launch_contract(
            program,
            working_directory,
            environment,
            descriptor_mappings,
        )
        try:
            resources = _ValidationGuardianActivationResources(self._pipe_factory)
        except BaseException as error:
            return PosixProcessLaunchRejected(error)
        try:
            invocation = self._invocation(
                program,
                resources.lifetime_read_descriptor,
                resources.readiness_write_descriptor,
                resources.exec_status_write_descriptor,
            )
            launch_specification = PosixProcessLaunchSpec(
                program=PosixProcessProgram(
                    (
                        *self._guardian_program.arguments,
                        "--request-json",
                        invocation.model_dump_json(),
                    )
                ),
                working_directory=working_directory,
                environment=environment,
                group_mode=PosixProcessGroupMode.NEW_SESSION,
                descriptor_mappings=(
                    *descriptor_mappings,
                    *resources.descriptor_mappings,
                ),
                terminal=PosixProcessWithoutTerminal(),
                activation_deadline=PosixProcessConfiguredActivationDeadline(),
            )
        except BaseException as error:
            return PosixProcessLaunchRejected(
                _error_with_cleanup_error(
                    "validation guardian preparation and activation cleanup failed",
                    error,
                    resources.close(),
                )
            )
        try:
            launch = self._process_launcher.launch(launch_specification)
        except BaseException as error:
            return PosixProcessLaunchRejected(
                _error_with_cleanup_error(
                    "validation guardian launch and activation cleanup failed",
                    error,
                    resources.close(),
                )
            )
        if type(launch) is not PosixProcessLaunchStarted:
            return self._close_nonstarted_launch(launch, resources)
        return self._complete_started_launch(launch, resources)

    def _complete_started_launch(
        self,
        launch: PosixProcessLaunchStarted,
        resources: _ValidationGuardianActivationResources,
    ) -> ValidationProcessGuardianLaunch:
        deadline_owner = ValidationGuardianStartupDeadlineOwner(
            self._clock,
            self._clock.monotonic_now() + self._sentinel_policy.startup_timeout_seconds,
        )
        try:
            transferred = resources.transfer_after_launch()
        except BaseException as transfer_error:
            return self._recover_started(launch, transfer_error)
        readiness_error = self._await_guardian_ready(
            transferred.readiness_reader,
            deadline_owner,
        )
        if readiness_error is not None:
            readiness_error = self._close_transferred_after_activation_failure(
                transferred,
                readiness_error,
            )
            return self._recover_started(launch, readiness_error)
        exec_status = self._await_opaque_exec_status(
            transferred.exec_status_reader,
            deadline_owner,
        )
        if type(exec_status) is _GuardianOpaqueExecStatusFailed:
            activation_error = self._close_lifetime_after_activation_failure(
                transferred.lifetime_writer,
                exec_status.error,
            )
            return self._recover_started(launch, activation_error)
        if type(exec_status) is _GuardianOpaqueExecRejected:
            rejection_error = self._close_lifetime_after_activation_failure(
                transferred.lifetime_writer,
                RuntimeError(f"{exec_status.error_type}: {exec_status.error_repr}"),
            )
            return self._recover_exec_rejection(
                launch,
                exec_status,
                rejection_error,
            )
        if type(exec_status) is not _GuardianOpaqueExecStarted:
            raise AssertionError("guardian opaque exec status is a closed union")
        return ValidationProcessGuardianStarted(
            launch.process,
            _OwnedValidationParentLifetime(transferred.lifetime_writer),
        )

    @staticmethod
    def _close_nonstarted_launch(
        launch: PosixProcessLaunch,
        resources: _ValidationGuardianActivationResources,
    ) -> ValidationProcessGuardianLaunch:
        cleanup = resources.close()
        if type(launch) is PosixProcessLaunchRejected:
            return _launch_with_cleanup_error(launch, cleanup)
        if type(launch) is PosixProcessExecRejected:
            return _launch_with_cleanup_error(launch, cleanup)
        if type(launch) is PosixProcessLaunchRecovered:
            return _launch_with_cleanup_error(launch, cleanup)
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            return _launch_with_cleanup_error(launch, cleanup)
        raise AssertionError("validation process launch is a closed union")

    def _await_guardian_ready(
        self,
        readiness_reader: PosixPipeReader,
        deadline_owner: ValidationGuardianStartupDeadlineOwner,
    ) -> BaseException | None:
        error: BaseException | None = None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(readiness_reader.fileno(), selectors.EVENT_READ)
                ready = selector.select(
                    deadline_owner.remaining_before_select(
                        ValidationGuardianStartupPhase.READINESS
                    )
                )
            if not ready:
                raise TimeoutError(
                    "validation guardian did not become ready before its "
                    "absolute startup deadline"
                )
            if os.read(readiness_reader.fileno(), 1) != _GUARDIAN_READY:
                raise RuntimeError(
                    "validation guardian exited before publishing readiness"
                )
            deadline_owner.accept_after_read(ValidationGuardianStartupPhase.READINESS)
        except BaseException as readiness_error:
            error = readiness_error
        try:
            readiness_reader.close()
        except BaseException as close_error:
            error = _error_with_cleanup_error(
                "validation guardian readiness and reader cleanup failed",
                error,
                close_error,
            )
        return error

    def _await_opaque_exec_status(
        self,
        exec_status_reader: PosixPipeReader,
        deadline_owner: ValidationGuardianStartupDeadlineOwner,
    ) -> _GuardianOpaqueExecStatus:
        status: _GuardianOpaqueExecStatus
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(exec_status_reader.fileno(), selectors.EVENT_READ)
                ready = selector.select(
                    deadline_owner.remaining_before_select(
                        ValidationGuardianStartupPhase.EXEC_STATUS
                    )
                )
            if not ready:
                raise TimeoutError(
                    "validation command did not exec before its absolute "
                    "startup deadline"
                )
            payload = os.read(
                exec_status_reader.fileno(),
                _MAX_EXEC_REJECTION_BYTES + 1,
            )
            if len(payload) > _MAX_EXEC_REJECTION_BYTES:
                raise RuntimeError(
                    "validation command exec rejection exceeds size limit"
                )
            if not payload:
                deadline_owner.accept_after_read(
                    ValidationGuardianStartupPhase.EXEC_STATUS
                )
                status = _GuardianOpaqueExecStarted()
            else:
                record = _ValidationCommandExecRejectedRecord.model_validate_json(
                    payload
                )
                deadline_owner.accept_after_read(
                    ValidationGuardianStartupPhase.EXEC_STATUS
                )
                status = _GuardianOpaqueExecRejected(
                    record.error_type,
                    record.error_repr,
                )
        except BaseException as error:
            status = _GuardianOpaqueExecStatusFailed(error)
        try:
            exec_status_reader.close()
        except BaseException as close_error:
            if type(status) is _GuardianOpaqueExecStatusFailed:
                return _GuardianOpaqueExecStatusFailed(
                    BaseExceptionGroup(
                        "validation exec status and reader cleanup failed",
                        (status.error, close_error),
                    )
                )
            return _GuardianOpaqueExecStatusFailed(close_error)
        return status

    @staticmethod
    def _close_transferred_after_activation_failure(
        transferred: _TransferredGuardianActivation,
        primary: BaseException,
    ) -> BaseException:
        cleanup = IndependentCleanupPlan(
            (
                CleanupAction(
                    "validation-parent-lifetime-close",
                    transferred.lifetime_writer.close,
                ),
                CleanupAction(
                    "validation-exec-status-reader-close",
                    transferred.exec_status_reader.close,
                ),
            )
        ).run()
        if type(cleanup) is CleanupSucceeded:
            return primary
        if type(cleanup) is not CleanupFailed:
            raise AssertionError("validation activation cleanup is a closed union")
        return BaseExceptionGroup(
            "validation guardian activation and cleanup failed",
            (primary, *(failure.error for failure in cleanup.failures)),
        )

    @staticmethod
    def _close_lifetime_after_activation_failure(
        lifetime_writer: PosixPipeWriter,
        primary: BaseException,
    ) -> BaseException:
        try:
            lifetime_writer.close()
        except BaseException as close_error:
            return BaseExceptionGroup(
                "validation activation and parent lifetime cleanup failed",
                (primary, close_error),
            )
        return primary

    def _recover_started(
        self,
        launch: PosixProcessLaunchStarted,
        transfer_error: BaseException,
    ) -> PosixProcessLaunchRecovered | PosixProcessLaunchRecoveryFailed:
        try:
            termination = self._supervisor.abort(
                OwnedProcessGroupLeader(launch.process.process_id)
            )
            exit_code = termination.leader_exit_code
            failures = [transfer_error]
            courtesy = termination.courtesy_failure()
            if courtesy is not None:
                failures.append(courtesy.error)
            try:
                launch.process.record_external_reap(exit_code)
            except BaseException as evidence_error:
                failures.append(evidence_error)
            recovery_error = _one_or_group(
                "validation guardian activation recovery failed",
                tuple(failures),
            )
            return PosixProcessLaunchRecovered(
                launch.process.process_id,
                exit_code,
                recovery_error,
            )
        except BaseException as recovery_error:
            return PosixProcessLaunchRecoveryFailed(
                launch.process.process_id,
                transfer_error,
                recovery_error,
            )

    def _recover_exec_rejection(
        self,
        launch: PosixProcessLaunchStarted,
        rejection: _GuardianOpaqueExecRejected,
        rejection_error: BaseException,
    ) -> (
        PosixProcessExecRejected
        | PosixProcessLaunchRecovered
        | PosixProcessLaunchRecoveryFailed
    ):
        recovery = self._recover_started(launch, rejection_error)
        if type(recovery) is PosixProcessLaunchRecoveryFailed:
            return recovery
        if type(recovery) is not PosixProcessLaunchRecovered:
            raise AssertionError("validation exec rejection recovery is a closed union")
        if (
            type(rejection_error) is not RuntimeError
            or recovery.activation_error is not rejection_error
        ):
            return recovery
        return PosixProcessExecRejected(
            recovery.process_id,
            recovery.exit_code,
            rejection.error_type,
            rejection.error_repr,
        )

    def _invocation(
        self,
        program: PosixProcessProgram,
        lifetime_read_file_descriptor: int,
        readiness_write_file_descriptor: int,
        exec_status_write_file_descriptor: int,
    ) -> _ValidationProcessGuardianInvocation:
        return _ValidationProcessGuardianInvocation(
            arguments=program.arguments,
            parent_lifetime_read_file_descriptor=lifetime_read_file_descriptor,
            guardian_ready_write_file_descriptor=readiness_write_file_descriptor,
            command_exec_status_write_file_descriptor=(
                exec_status_write_file_descriptor
            ),
            sentinel_program=self._sentinel_program.arguments,
            sentinel_graceful_shutdown_seconds=(
                self._sentinel_policy.graceful_shutdown_seconds
            ),
            sentinel_startup_timeout_seconds=(
                self._sentinel_policy.startup_timeout_seconds
            ),
        )

    @staticmethod
    def _validate_launch_contract(
        program: PosixProcessProgram,
        working_directory: Path,
        environment: PosixProcessEnvironment,
        descriptor_mappings: tuple[PosixDescriptorMapping, ...],
    ) -> None:
        if type(program) is not PosixProcessProgram:
            raise ValueError("validation guarded program must be typed")
        _require_absolute_path(
            working_directory,
            "validation guarded working directory",
        )
        if type(environment) is not PosixProcessEnvironment:
            raise ValueError("validation guarded environment must be typed")
        if type(descriptor_mappings) is not tuple or any(
            type(mapping) is not PosixDescriptorMapping
            for mapping in descriptor_mappings
        ):
            raise ValueError("validation guarded descriptor mappings must be typed")


class _ValidationProcessGuardianChild:
    """Establish the sentinel before replacing this group leader with work."""

    def run(self, invocation: _ValidationProcessGuardianInvocation) -> int:
        if os.getpid() != os.getpgrp():
            raise RuntimeError("validation guardian must be its process-group leader")
        from ..entrypoints.bootstrap import build_posix_process_launcher

        self._mark_close_on_exec(invocation.command_exec_status_write_file_descriptor)
        controller = ProcessGroupSentinelController.start_with_parent_lifetime(
            invocation.process_group_sentinel_program(),
            ProcessGroupSentinelWithoutCancellation(),
            invocation.process_group_sentinel_policy(),
            (),
            ProcessGroupSentinelParentLifetime(
                invocation.parent_lifetime_read_file_descriptor
            ),
            build_posix_process_launcher(),
        )
        try:
            self._publish_ready(invocation.guardian_ready_write_file_descriptor)
            os.close(invocation.parent_lifetime_read_file_descriptor)
            controller.transfer_to_exec()
            command = invocation.command_program()
            os.execvpe(command.arguments[0], command.arguments, os.environ)
        except BaseException as error:
            publication_error = self._publish_exec_rejection(
                invocation.command_exec_status_write_file_descriptor,
                error,
            )
            cleanup = IndependentCleanupPlan(
                (
                    CleanupAction(
                        "validation-sentinel-abort",
                        controller.abort_before_opaque_work,
                    ),
                )
            ).run()
            if type(cleanup) is CleanupSucceeded and publication_error is None:
                raise
            if type(cleanup) is not CleanupFailed:
                if type(cleanup) is not CleanupSucceeded:
                    raise AssertionError(
                        "validation guardian cleanup is a closed union"
                    )
                if publication_error is None:
                    raise AssertionError(
                        "validation guardian publication failure is missing"
                    )
                raise BaseExceptionGroup(
                    "validation command exec and status publication failed",
                    (error, publication_error),
                )
            raise BaseExceptionGroup(
                "validation command exec and sentinel cleanup failed",
                (
                    error,
                    *((publication_error,) if publication_error is not None else ()),
                    *(failure.error for failure in cleanup.failures),
                ),
            )
        raise AssertionError("validation guardian exec unexpectedly returned")

    @staticmethod
    def _publish_ready(file_descriptor: int) -> None:
        try:
            if os.write(file_descriptor, _GUARDIAN_READY) != len(_GUARDIAN_READY):
                raise RuntimeError(
                    "validation guardian readiness channel performed a short write"
                )
        finally:
            os.close(file_descriptor)

    @staticmethod
    def _mark_close_on_exec(file_descriptor: int) -> None:
        flags = fcntl.fcntl(file_descriptor, fcntl.F_GETFD)
        fcntl.fcntl(file_descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

    @staticmethod
    def _publish_exec_rejection(
        file_descriptor: int,
        error: BaseException,
    ) -> BaseException | None:
        publication_error: BaseException | None = None
        try:
            record = _ValidationCommandExecRejectedRecord(
                error_type=type(error).__name__,
                error_repr=repr(error),
            )
            payload = record.model_dump_json().encode("utf-8")
            if len(payload) > _MAX_EXEC_REJECTION_BYTES:
                raise RuntimeError(
                    "validation command exec rejection exceeds size limit"
                )
            if os.write(file_descriptor, payload) != len(payload):
                raise RuntimeError(
                    "validation command exec rejection performed a short write"
                )
        except BaseException as caught_error:
            publication_error = caught_error
        try:
            os.close(file_descriptor)
        except BaseException as close_error:
            return _error_with_cleanup_error(
                "validation exec rejection publication and close failed",
                publication_error,
                close_error,
            )
        return publication_error


def _pipe_cleanup_error(pipe: PosixPipe) -> BaseException | None:
    outcome = pipe.close()
    if type(outcome) is PosixPipeClosed:
        return None
    if type(outcome) is not PosixPipeCloseFailed:
        raise AssertionError("POSIX pipe close is a closed union")
    return outcome.error


def _error_with_pipe_cleanup(
    message: str,
    primary: BaseException,
    pipe: PosixPipe,
) -> BaseException:
    cleanup = _pipe_cleanup_error(pipe)
    if cleanup is None:
        return primary
    return BaseExceptionGroup(message, (primary, cleanup))


def _launch_with_cleanup_error(
    launch: PosixProcessLaunchRejected
    | PosixProcessExecRejected
    | PosixProcessLaunchRecovered
    | PosixProcessLaunchRecoveryFailed,
    cleanup: BaseException | None,
) -> (
    PosixProcessLaunchRejected
    | PosixProcessExecRejected
    | PosixProcessLaunchRecovered
    | PosixProcessLaunchRecoveryFailed
):
    if cleanup is None:
        return launch
    if type(launch) is PosixProcessLaunchRejected:
        return PosixProcessLaunchRejected(
            BaseExceptionGroup(
                "validation launch rejection and lifetime cleanup failed",
                (launch.error, cleanup),
            )
        )
    if type(launch) is PosixProcessExecRejected:
        return PosixProcessLaunchRecovered(
            launch.process_id,
            launch.exit_code,
            BaseExceptionGroup(
                "validation exec rejection and lifetime cleanup failed",
                (launch.as_error(), cleanup),
            ),
        )
    if type(launch) is PosixProcessLaunchRecovered:
        return PosixProcessLaunchRecovered(
            launch.process_id,
            launch.exit_code,
            BaseExceptionGroup(
                "validation activation and lifetime cleanup failed",
                (launch.activation_error, cleanup),
            ),
        )
    if type(launch) is not PosixProcessLaunchRecoveryFailed:
        raise AssertionError("validation launch is a closed union")
    return PosixProcessLaunchRecoveryFailed(
        launch.process_id,
        launch.activation_error,
        BaseExceptionGroup(
            "validation activation recovery and lifetime cleanup failed",
            (launch.recovery_error, cleanup),
        ),
    )


def _error_with_cleanup_error(
    message: str,
    primary: BaseException | None,
    cleanup: BaseException | None,
) -> BaseException:
    if primary is None:
        if cleanup is None:
            raise ValueError("an error is required")
        return cleanup
    if cleanup is None:
        return primary
    return BaseExceptionGroup(message, (primary, cleanup))


def _one_or_group(message: str, errors: tuple[BaseException, ...]) -> BaseException:
    if not errors:
        raise ValueError("at least one error is required")
    if len(errors) == 1:
        return errors[0]
    return BaseExceptionGroup(message, errors)


def _parse_invocation(raw_request: str) -> _ValidationProcessGuardianInvocation:
    try:
        return _ValidationProcessGuardianInvocation.model_validate_json(raw_request)
    except ValidationError as error:
        raise ValueError("invalid validation process guardian invocation") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard one validation process group")
    parser.add_argument("--request-json", required=True)
    arguments = parser.parse_args()
    return _ValidationProcessGuardianChild().run(
        _parse_invocation(arguments.request_json)
    )


if __name__ == "__main__":
    raise SystemExit(main())
