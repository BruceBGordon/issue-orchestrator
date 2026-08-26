# pyright: strict
"""Terminal command wrapper behind one behavior-level ownership interface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, ValidationError

from ..domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ..domain.terminal_session_owner import TerminalSessionOwnerPolicy
from ..domain.terminal_session_termination import TerminalSessionOwnerCancellation
from ..ports.atomic_record_store import AtomicRecordStoreFactory
from ..ports.posix_process import PosixProcessLauncher
from .independent_cleanup import (
    CleanupAction,
    IndependentCleanupPlan,
    raise_primary_with_cleanup,
)
from .process_cancellation_endpoint import (
    ProcessCancellationEndpointLeaseContract,
    ProcessCancellationEndpointLeaseFactory,
    ProcessCancellationEndpointReadiness,
    ProcessCancellationInheritedEndpointActivator,
    ProcessCancellationOwnerControls,
)
from .process_group_sentinel import RedundantProcessGroupSentinelController
from .strict_wire_record import StrictWireRecord


@dataclass(frozen=True, slots=True)
class TerminalSessionOwnerProgram:
    """Exact executable prefix for the terminal ownership wrapper."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError(
                "TerminalSessionOwnerProgram.arguments must be a non-empty tuple"
            )
        if any(
            type(argument) is not str or not argument or "\0" in argument
            for argument in self.arguments
        ):
            raise ValueError(
                "TerminalSessionOwnerProgram.arguments must be non-empty, "
                "NUL-free strings"
            )
        if not Path(self.arguments[0]).is_absolute():
            raise ValueError("TerminalSessionOwnerProgram executable must be absolute")


class _TerminalSessionOwnerInvocation(StrictWireRecord):
    schema_version: Literal[1] = 1
    command: tuple[str, ...]
    cancellation_record_path: str = Field(min_length=1)
    sentinel_program: tuple[str, ...]
    startup_timeout_seconds: float = Field(gt=0)
    graceful_shutdown_seconds: float = Field(gt=0)
    listener_file_descriptor: int = Field(ge=0)
    owner_lock_file_descriptor: int = Field(ge=0)


class PosixTerminalSessionLaunchLease:
    """Parent-side owner prepared before the PTY fork/exec boundary."""

    def __init__(
        self,
        command: tuple[str, ...],
        cancellation: TerminalSessionOwnerCancellation,
        lease: ProcessCancellationEndpointLeaseContract,
        readiness: ProcessCancellationEndpointReadiness,
    ) -> None:
        if type(command) is not tuple or not command:
            raise ValueError("terminal launch command must be a non-empty tuple")
        if type(cancellation) is not TerminalSessionOwnerCancellation:
            raise ValueError("terminal launch cancellation must be typed")
        if not isinstance(
            cast(object, lease),
            ProcessCancellationEndpointLeaseContract,
        ):
            raise ValueError(
                "terminal launch lease must implement the cancellation lease contract"
            )
        if type(readiness) is not ProcessCancellationEndpointReadiness:
            raise ValueError("terminal launch readiness must be typed")
        self._command = command
        self._cancellation = cancellation
        self._lease = lease
        self._readiness = readiness

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def inherited_file_descriptors(self) -> tuple[int, ...]:
        controls = self._lease.controls()
        return (
            controls.listener_file_descriptor,
            controls.owner_lock_file_descriptor,
        )

    def require_ready(self) -> None:
        self._readiness.await_active(self._cancellation.record_path)
        self._lease.transfer_after_inherited_activation()

    def abandon_after_spawn_uncertainty(self) -> None:
        """Leave SETTING_UP/ACTIVE evidence for exact durable recovery."""
        self._lease.release_parent_after_spawn_uncertainty()

    def retire_after_containment(self) -> None:
        self._lease.retire()


class PosixTerminalSessionOwner:
    """Wrap commands and await crash-safe owner publication before registry use."""

    def __init__(
        self,
        owner_program: TerminalSessionOwnerProgram,
        sentinel_program: ProcessGroupSentinelProgram,
        policy: TerminalSessionOwnerPolicy,
        record_stores: AtomicRecordStoreFactory,
        cancellation_endpoint_leases: ProcessCancellationEndpointLeaseFactory,
    ) -> None:
        if type(owner_program) is not TerminalSessionOwnerProgram:
            raise ValueError("owner_program must be TerminalSessionOwnerProgram")
        if type(sentinel_program) is not ProcessGroupSentinelProgram:
            raise ValueError("sentinel_program must be ProcessGroupSentinelProgram")
        if type(policy) is not TerminalSessionOwnerPolicy:
            raise ValueError("policy must be TerminalSessionOwnerPolicy")
        if not isinstance(
            cast(object, cancellation_endpoint_leases),
            ProcessCancellationEndpointLeaseFactory,
        ):
            raise ValueError(
                "cancellation_endpoint_leases must implement "
                "ProcessCancellationEndpointLeaseFactory"
            )
        self._owner_program = owner_program
        self._sentinel_program = sentinel_program
        self._policy = policy
        self._record_stores = record_stores
        self._cancellation_endpoint_leases = cancellation_endpoint_leases

    def prepare(
        self,
        command: tuple[str, ...],
        cancellation: TerminalSessionOwnerCancellation,
    ) -> PosixTerminalSessionLaunchLease:
        if type(command) is not tuple or not command:
            raise ValueError("terminal owner command must be a non-empty tuple")
        if any(
            type(argument) is not str or not argument or "\0" in argument
            for argument in command
        ):
            raise ValueError(
                "terminal owner command must contain non-empty, NUL-free strings"
            )
        if type(cancellation) is not TerminalSessionOwnerCancellation:
            raise ValueError(
                "terminal owner cancellation must be TerminalSessionOwnerCancellation"
            )
        lease = self._cancellation_endpoint_leases.create(
            cancellation.record_path,
            self._record_stores,
        )
        try:
            controls = lease.controls()
            invocation = _TerminalSessionOwnerInvocation(
                command=command,
                cancellation_record_path=str(cancellation.record_path),
                sentinel_program=self._sentinel_program.arguments,
                startup_timeout_seconds=self._policy.startup_timeout_seconds,
                graceful_shutdown_seconds=self._policy.graceful_shutdown_seconds,
                listener_file_descriptor=controls.listener_file_descriptor,
                owner_lock_file_descriptor=controls.owner_lock_file_descriptor,
            )
            readiness = ProcessCancellationEndpointReadiness(
                self._policy.startup_timeout_seconds,
                self._record_stores,
            )
            return PosixTerminalSessionLaunchLease(
                (
                    *self._owner_program.arguments,
                    "--owner-request-json",
                    invocation.model_dump_json(),
                ),
                cancellation,
                lease,
                readiness,
            )
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "terminal launch preparation and endpoint retirement failed",
                primary_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "cancellation-endpoint-retirement",
                            lease.retire,
                        ),
                    )
                ).run(),
            )


class TerminalSessionOwnerChild:
    """Publish an in-group sentinel, then exec the real terminal command."""

    def __init__(
        self,
        record_stores: AtomicRecordStoreFactory,
        process_launcher: PosixProcessLauncher,
    ) -> None:
        self._record_stores = record_stores
        if not callable(getattr(process_launcher, "launch", None)):
            raise ValueError(
                "TerminalSessionOwnerChild.process_launcher must implement "
                "PosixProcessLauncher"
            )
        self._process_launcher = process_launcher

    def run(self, invocation: _TerminalSessionOwnerInvocation) -> int:
        cancellation = TerminalSessionOwnerCancellation(
            Path(invocation.cancellation_record_path)
        )
        controls = ProcessCancellationOwnerControls(
            invocation.listener_file_descriptor,
            invocation.owner_lock_file_descriptor,
        )
        activation = ProcessCancellationInheritedEndpointActivator(
            cancellation.record_path,
            controls,
            self._record_stores,
        )
        controller: RedundantProcessGroupSentinelController | None = None
        try:
            controller = RedundantProcessGroupSentinelController.start(
                ProcessGroupSentinelProgram(invocation.sentinel_program),
                controls,
                ProcessGroupSentinelPolicy(
                    graceful_shutdown_seconds=(
                        invocation.graceful_shutdown_seconds
                    ),
                    startup_timeout_seconds=invocation.startup_timeout_seconds,
                ),
                (),
                self._process_launcher,
            )
            activation.activate_for_owner()
            controller.transfer_to_exec()
            os.execvpe(invocation.command[0], invocation.command, os.environ)
        except BaseException as primary_error:
            cleanup_errors: list[BaseException] = []
            if controller is not None:
                try:
                    controller.abort_before_opaque_work()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "terminal owner setup and cleanup failed",
                    (primary_error, *cleanup_errors),
                )
            raise
        raise AssertionError("terminal owner exec unexpectedly returned")


def run_terminal_session_owner_child(
    raw_request: str,
    record_stores: AtomicRecordStoreFactory,
    process_launcher: PosixProcessLauncher,
) -> int:
    """Validate one child invocation and run the terminal ownership wrapper."""
    try:
        invocation = _TerminalSessionOwnerInvocation.model_validate_json(raw_request)
    except ValidationError as error:
        raise ValueError("invalid terminal owner invocation") from error
    return TerminalSessionOwnerChild(record_stores, process_launcher).run(invocation)
