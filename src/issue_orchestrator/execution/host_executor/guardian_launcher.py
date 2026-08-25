# pyright: strict
"""Outer owner for launching and observing one crash-resilient guardian."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from pydantic import ValidationError

from ...domain.executor_guardian import (
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
)
from ...domain.executor import ExecutorCommandLifecycle
from ...domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ...domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupUnboundedWait,
)
from ...ports.executor_command_guardian import ExecutorGuardianRequest
from ...ports.atomic_record_store import AtomicRecordStoreFactory
from ...ports.process_group_supervisor import ProcessGroupSupervisor
from ..process_group_supervisor import NeverInterruptProcessGroup
from ..independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupFailure,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
)
from ..executor_guardian_cancellation import (
    ExecutorGuardianCancellationControls,
    ExecutorGuardianCancellationLease,
    NoExecutorGuardianCancellationControls,
    prepare_executor_guardian_cancellation,
)
from ..process_cancellation_endpoint import ProcessCancellationOwnerControls
from ._guardian_contracts import (
    GUARDIAN_TERMINAL_ADAPTER,
    GUARDIAN_START_SIGNAL,
    GuardianCancellationControlRecord,
    GuardianDetachedCancellationControlRecord,
    GuardianInteractiveCancellationControlRecord,
    GuardianInvocationRecord,
)


_MAX_RESULT_BYTES = 65536
_OWNER_READY_SIGNAL = b"R"


def _require_process_group_supervisor(value: object) -> None:
    if not isinstance(value, ProcessGroupSupervisor):
        raise ValueError(
            "PosixExecutorCommandGuardian.process_group_supervisor must implement "
            "ProcessGroupSupervisor"
        )


class ExecutorGuardianLaunchError(RuntimeError):
    """Raised when the outer process cannot start its guardian."""


class ExecutorGuardianProtocolError(RuntimeError):
    """Raised when a guardian exits without one exact terminal record."""


class _NoParentInterruption:
    """Explicit detached lifecycle that does not reinterpret parent signals."""

    def __init__(self) -> None:
        self._waiter = NeverInterruptProcessGroup()

    @property
    def requested(self) -> bool:
        return False

    def wait_for_request(self, timeout_seconds: float) -> bool:
        return self._waiter.wait_for_request(timeout_seconds)


class _SigtermParentInterruption:
    """Translate one deliberate parent SIGTERM into group interruption."""

    def __init__(self) -> None:
        self._requested = threading.Event()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(
        self,
        signal_number: int,
        frame: FrameType | None,
    ) -> None:
        del frame
        if signal_number != signal.SIGTERM:
            raise AssertionError("parent interruption only accepts SIGTERM")
        self._requested.set()

    def wait_for_request(self, timeout_seconds: float) -> bool:
        return self._requested.wait(timeout_seconds)


_ParentInterruption = _NoParentInterruption | _SigtermParentInterruption


@contextmanager
def _installed_parent_interruption(
    lifecycle: ExecutorCommandLifecycle,
) -> Generator[_ParentInterruption]:
    if lifecycle is ExecutorCommandLifecycle.DETACHED:
        yield _NoParentInterruption()
        return
    if lifecycle is not ExecutorCommandLifecycle.INTERACTIVE_SESSION:
        raise AssertionError("ExecutorCommandLifecycle is a closed enum")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "interactive executor sessions must run on the process main thread"
        )
    interruption = _SigtermParentInterruption()
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, interruption.request)
    try:
        yield interruption
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


class _NoControllingTerminal:
    def grant(self, process_group_id: int) -> None:
        del process_group_id

    def restore(self) -> None:
        pass


@dataclass(slots=True)
class _InheritedControllingTerminal:
    """Transfer one existing controlling terminal between process groups."""

    file_descriptor: int
    original_process_group_id: int
    _granted: bool = False

    def grant(self, process_group_id: int) -> None:
        if self._granted:
            raise RuntimeError("controlling terminal foreground is already granted")
        self._set_foreground_process_group(process_group_id)
        self._granted = True

    def restore(self) -> None:
        if not self._granted:
            return
        self._set_foreground_process_group(self.original_process_group_id)
        self._granted = False

    def _set_foreground_process_group(self, process_group_id: int) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGTTOU},
        )
        try:
            os.tcsetpgrp(self.file_descriptor, process_group_id)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


_ControllingTerminal = _NoControllingTerminal | _InheritedControllingTerminal


def _controlling_terminal(
    lifecycle: ExecutorCommandLifecycle,
) -> _ControllingTerminal:
    if lifecycle is ExecutorCommandLifecycle.DETACHED or not os.isatty(0):
        return _NoControllingTerminal()
    if lifecycle is not ExecutorCommandLifecycle.INTERACTIVE_SESSION:
        raise AssertionError("ExecutorCommandLifecycle is a closed enum")
    foreground_process_group = os.tcgetpgrp(0)
    process_group = os.getpgrp()
    if foreground_process_group != process_group:
        raise RuntimeError(
            "interactive executor parent must own its controlling terminal foreground"
        )
    return _InheritedControllingTerminal(0, process_group)


@dataclass(frozen=True, slots=True)
class ExecutorGuardianProgram:
    """Exact executable argument prefix for the guardian child."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError("guardian program arguments must be a non-empty tuple")
        if not self.arguments[0] or any(
            type(argument) is not str for argument in self.arguments
        ):
            raise ValueError(
                "guardian program arguments must contain strings and an executable"
            )
        if any("\0" in argument for argument in self.arguments):
            raise ValueError("guardian program arguments must not contain NUL bytes")
        executable = Path(self.arguments[0])
        if not executable.is_absolute():
            raise ValueError("guardian program executable must be absolute")


class PosixExecutorCommandGuardian:
    """Transfer lease FDs to a child owner and validate its terminal channel."""

    def __init__(
        self,
        program: ExecutorGuardianProgram,
        sentinel_program: ProcessGroupSentinelProgram,
        sentinel_policy: ProcessGroupSentinelPolicy,
        record_stores: AtomicRecordStoreFactory,
        process_group_supervisor: ProcessGroupSupervisor,
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> None:
        if type(program) is not ExecutorGuardianProgram:
            raise ValueError(
                "PosixExecutorCommandGuardian.program must be ExecutorGuardianProgram"
            )
        _require_process_group_supervisor(process_group_supervisor)
        if type(termination_policy) is not ExecutorGuardianTerminationPolicy:
            raise ValueError(
                "PosixExecutorCommandGuardian.termination_policy must be an "
                "ExecutorGuardianTerminationPolicy"
            )
        self._program = program
        if type(sentinel_program) is not ProcessGroupSentinelProgram:
            raise ValueError(
                "PosixExecutorCommandGuardian.sentinel_program must be a "
                "ProcessGroupSentinelProgram"
            )
        if type(sentinel_policy) is not ProcessGroupSentinelPolicy:
            raise ValueError(
                "PosixExecutorCommandGuardian.sentinel_policy must be a "
                "ProcessGroupSentinelPolicy"
            )
        self._sentinel_program = sentinel_program
        self._sentinel_policy = sentinel_policy
        self._record_stores = record_stores
        self._process_group_supervisor = process_group_supervisor
        self._termination_policy = termination_policy

    def run(self, request: ExecutorGuardianRequest) -> ExecutorGuardianTerminal:
        if type(request) is not ExecutorGuardianRequest:
            raise ValueError(
                "PosixExecutorCommandGuardian.run requires ExecutorGuardianRequest"
            )
        terminal_foreground = _controlling_terminal(request.lifecycle)
        result_read_fd, result_write_fd = os.pipe()
        start_read_fd, start_write_fd = os.pipe()
        owner_ready_read_fd, owner_ready_write_fd = os.pipe()
        cancellation_lease = self._prepare_cancellation(
            request,
            (
                result_read_fd,
                result_write_fd,
                start_read_fd,
                start_write_fd,
                owner_ready_read_fd,
                owner_ready_write_fd,
            ),
        )
        cancellation_controls = cancellation_lease.controls()
        guardian: subprocess.Popen[bytes] | None = None
        group_contained = False
        result_write_open = True
        start_read_open = True
        start_write_open = True
        owner_ready_read_open = True
        owner_ready_write_open = True
        try:
            lease_file_descriptors = request.lease.inherited_file_descriptors()
            invocation = GuardianInvocationRecord.create(
                arguments=request.arguments,
                result_file_descriptor=result_write_fd,
                start_file_descriptor=start_read_fd,
                owner_ready_file_descriptor=owner_ready_write_fd,
                lifecycle=request.lifecycle,
                budget=request.budget,
                cancellation=self._guardian_cancellation_record(
                    cancellation_controls
                ),
                termination_policy=self._termination_policy,
                sentinel_program=self._sentinel_program,
                sentinel_policy=self._sentinel_policy,
                lease_file_descriptors=lease_file_descriptors,
            )
            guardian_arguments = (
                *self._program.arguments,
                "--request-json",
                invocation.model_dump_json(),
            )
            inherited_descriptors = (
                *lease_file_descriptors,
                result_write_fd,
                start_read_fd,
                owner_ready_write_fd,
                *self._guardian_cancellation_descriptors(
                    cancellation_controls
                ),
            )
            with _installed_parent_interruption(request.lifecycle) as interruption:
                try:
                    guardian = self._spawn_guardian(
                        guardian_arguments,
                        request,
                        inherited_descriptors,
                    )
                except OSError as error:
                    raise ExecutorGuardianLaunchError(
                        f"could not start executor guardian: {error!r}"
                    ) from error
                # The guardian now owns inherited copies. Close the outer
                # executor's references before publishing or starting work so
                # a stopped outer process cannot retain machine capacity.
                os.close(result_write_fd)
                result_write_open = False
                os.close(start_read_fd)
                start_read_open = False
                os.close(owner_ready_write_fd)
                owner_ready_write_open = False
                self._await_guardian_owner_ready(
                    owner_ready_read_fd,
                    guardian,
                )
                os.close(owner_ready_read_fd)
                owner_ready_read_open = False
                request.lease.transfer_to_guardian()
                cancellation_lease.activate()
                cancellation_lease.transfer_to_owner()
                if interruption.requested:
                    return ExecutorGuardianCommandCompleted(-signal.SIGTERM)
                terminal_foreground.grant(guardian.pid)
                if os.write(start_write_fd, GUARDIAN_START_SIGNAL) != len(
                    GUARDIAN_START_SIGNAL
                ):
                    raise ExecutorGuardianLaunchError(
                        "executor guardian start gate performed a short write"
                    )
                os.close(start_write_fd)
                start_write_open = False

                supervision = self._process_group_supervisor.supervise(
                    OwnedProcessGroupLeader(guardian.pid),
                    ProcessGroupUnboundedWait(),
                    interruption,
                )
                group_contained = True
                return self._terminal_after_supervision(
                    supervision,
                    interruption,
                    guardian,
                    result_read_fd,
                )
        finally:
            primary_error = sys.exception()
            containment_cleanup: CleanupOutcome = CleanupSucceeded()
            safe_to_retire_cancellation = guardian is None or group_contained
            if guardian is not None and not group_contained:
                try:
                    self._process_group_supervisor.abort(
                        OwnedProcessGroupLeader(guardian.pid)
                    )
                    safe_to_retire_cancellation = True
                except BaseException as cleanup_error:
                    cleanup_error.add_note(
                        "guardian group abort failed; cancellation identity retained"
                    )
                    containment_cleanup = CleanupFailed(
                        (CleanupFailure("guardian-group-abort", cleanup_error),)
                    )
            cleanup_actions: list[CleanupAction] = []
            if safe_to_retire_cancellation:
                cleanup_actions.append(
                    CleanupAction(
                        "cancellation-endpoint-retirement",
                        cancellation_lease.retire,
                    )
                )
            cleanup_actions.append(
                CleanupAction("terminal-foreground-restore", terminal_foreground.restore)
            )
            if result_write_open:
                cleanup_actions.append(
                    CleanupAction(
                        "result-writer-close",
                        lambda: os.close(result_write_fd),
                    )
                )
            if start_read_open:
                cleanup_actions.append(
                    CleanupAction(
                        "start-reader-close",
                        lambda: os.close(start_read_fd),
                    )
                )
            if start_write_open:
                cleanup_actions.append(
                    CleanupAction(
                        "start-writer-close",
                        lambda: os.close(start_write_fd),
                    )
                )
            if owner_ready_read_open:
                cleanup_actions.append(
                    CleanupAction(
                        "owner-readiness-reader-close",
                        lambda: os.close(owner_ready_read_fd),
                    )
                )
            if owner_ready_write_open:
                cleanup_actions.append(
                    CleanupAction(
                        "owner-readiness-writer-close",
                        lambda: os.close(owner_ready_write_fd),
                    )
                )
            cleanup_actions.append(
                CleanupAction(
                    "result-reader-close",
                    lambda: os.close(result_read_fd),
                )
            )
            resource_cleanup = IndependentCleanupPlan(
                tuple(cleanup_actions)
            ).run()
            cleanup_errors = self._cleanup_errors(
                containment_cleanup,
                resource_cleanup,
            )
            if cleanup_errors:
                if primary_error is not None:
                    raise BaseExceptionGroup(
                        "guardian execution and cleanup failed",
                        (primary_error, *cleanup_errors),
                    )
                raise BaseExceptionGroup(
                    "guardian cleanup failed",
                    cleanup_errors,
                )

    @staticmethod
    def _cleanup_errors(
        *outcomes: CleanupOutcome,
    ) -> tuple[BaseException, ...]:
        errors: list[BaseException] = []
        for outcome in outcomes:
            if type(outcome) is CleanupSucceeded:
                continue
            if type(outcome) is not CleanupFailed:
                raise AssertionError("cleanup outcome is a closed union")
            errors.extend(failure.error for failure in outcome.failures)
        return tuple(errors)

    def _prepare_cancellation(
        self,
        request: ExecutorGuardianRequest,
        open_pipe_descriptors: tuple[int, ...],
    ) -> ExecutorGuardianCancellationLease:
        """Acquire cancellation ownership or roll back all pre-spawn pipes."""
        try:
            return prepare_executor_guardian_cancellation(
                request.cancellation,
                self._record_stores,
            )
        except BaseException:
            for descriptor in open_pipe_descriptors:
                os.close(descriptor)
            raise

    def _await_guardian_owner_ready(
        self,
        ready_read_file_descriptor: int,
        guardian: subprocess.Popen[bytes],
    ) -> None:
        deadline = time.monotonic() + self._sentinel_policy.startup_timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(ready_read_file_descriptor, selectors.EVENT_READ)
            remaining = deadline - time.monotonic()
            ready = selector.select(max(0.0, remaining))
        if not ready or time.monotonic() >= deadline:
            raise ExecutorGuardianLaunchError(
                "guardian owner did not become ready before its absolute deadline"
            )
        if os.read(ready_read_file_descriptor, 1) != _OWNER_READY_SIGNAL:
            guardian.poll()
            raise ExecutorGuardianLaunchError(
                "guardian exited before publishing exact owner readiness"
            )

    @staticmethod
    def _guardian_cancellation_record(
        controls: ExecutorGuardianCancellationControls,
    ) -> GuardianCancellationControlRecord:
        if type(controls) is NoExecutorGuardianCancellationControls:
            return GuardianDetachedCancellationControlRecord()
        if type(controls) is ProcessCancellationOwnerControls:
            return GuardianInteractiveCancellationControlRecord(
                listener_file_descriptor=controls.listener_file_descriptor,
                owner_lock_file_descriptor=controls.owner_lock_file_descriptor,
            )
        raise AssertionError("guardian cancellation controls are a closed union")

    @staticmethod
    def _guardian_cancellation_descriptors(
        controls: ExecutorGuardianCancellationControls,
    ) -> tuple[int, ...]:
        if type(controls) is NoExecutorGuardianCancellationControls:
            return ()
        if type(controls) is ProcessCancellationOwnerControls:
            return (
                controls.listener_file_descriptor,
                controls.owner_lock_file_descriptor,
            )
        raise AssertionError("guardian cancellation controls are a closed union")

    @staticmethod
    def _spawn_guardian(
        guardian_arguments: tuple[str, ...],
        request: ExecutorGuardianRequest,
        inherited_descriptors: tuple[int, ...],
    ) -> subprocess.Popen[bytes]:
        if request.lifecycle is ExecutorCommandLifecycle.DETACHED:
            return subprocess.Popen(
                guardian_arguments,
                env=dict(request.environment),
                pass_fds=inherited_descriptors,
                start_new_session=True,
            )
        if request.lifecycle is ExecutorCommandLifecycle.INTERACTIVE_SESSION:
            return subprocess.Popen(
                guardian_arguments,
                env=dict(request.environment),
                pass_fds=inherited_descriptors,
                process_group=0,
            )
        raise AssertionError("ExecutorCommandLifecycle is a closed enum")

    @classmethod
    def _terminal_after_supervision(
        cls,
        supervision: ProcessGroupSupervision,
        interruption: _ParentInterruption,
        guardian: subprocess.Popen[bytes],
        result_read_fd: int,
    ) -> ExecutorGuardianTerminal:
        """Interpret one fully contained guardian supervision outcome."""
        guardian.returncode = supervision.termination.leader_exit_code
        if type(supervision) is ProcessGroupInterrupted:
            return ExecutorGuardianCommandCompleted(-signal.SIGTERM)
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("an unbounded guardian wait cannot time out")
        # A signal may arrive after the guardian has already completed and
        # published its terminal record.  Completion evidence is authoritative;
        # interruption is synthesized only when supervision actually observed
        # an interrupted group.
        terminal = cls._read_terminal(result_read_fd)
        cls._require_expected_guardian_exit(guardian.returncode, terminal)
        return terminal

    @staticmethod
    def _require_expected_guardian_exit(
        guardian_exit_code: int,
        terminal: ExecutorGuardianTerminal,
    ) -> None:
        if type(terminal) is ExecutorGuardianCommandStartFailed:
            if guardian_exit_code != 0:
                raise ExecutorGuardianProtocolError(
                    "executor guardian command-start record requires exit code 0"
                )
            return
        if type(terminal) is ExecutorGuardianInternalFailed:
            if guardian_exit_code not in (1, -signal.SIGKILL):
                raise ExecutorGuardianProtocolError(
                    "executor guardian internal-failure record requires exit "
                    "code 1 or a contained SIGKILL exit"
                )
            return
        if guardian_exit_code != -signal.SIGKILL:
            raise ExecutorGuardianProtocolError(
                "executor guardian started-command record requires a contained "
                "SIGKILL exit"
            )

    @staticmethod
    def _read_terminal(result_read_fd: int) -> ExecutorGuardianTerminal:
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = os.read(result_read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESULT_BYTES:
                raise ExecutorGuardianProtocolError(
                    "executor guardian terminal record exceeds size limit"
                )
        payload = b"".join(chunks)
        if not payload:
            raise ExecutorGuardianProtocolError(
                "executor guardian exited without a terminal record"
            )
        try:
            record = GUARDIAN_TERMINAL_ADAPTER.validate_json(payload, strict=True)
        except ValidationError as error:
            raise ExecutorGuardianProtocolError(
                "executor guardian emitted a malformed terminal record"
            ) from error
        return record.to_domain()
