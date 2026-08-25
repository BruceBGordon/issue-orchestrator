# pyright: strict
"""Outer owner for launching and observing one crash-resilient guardian."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
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
from ...domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupUnboundedWait,
)
from ...ports.executor_command_guardian import ExecutorGuardianRequest
from ...ports.process_group_supervisor import ProcessGroupSupervisor
from ..process_group_supervisor import NeverInterruptProcessGroup
from ..executor_guardian_cancellation import (
    ExecutorGuardianCancellationLease,
    prepare_executor_guardian_cancellation,
)
from ._guardian_contracts import (
    GUARDIAN_TERMINAL_ADAPTER,
    GUARDIAN_START_SIGNAL,
    GuardianInvocationRecord,
)


_MAX_RESULT_BYTES = 65536


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
        cancellation_lease = self._prepare_cancellation(
            request,
            (result_read_fd, result_write_fd, start_read_fd, start_write_fd),
        )
        guardian: subprocess.Popen[bytes] | None = None
        group_contained = False
        result_write_open = True
        start_read_open = True
        start_write_open = True
        try:
            invocation = GuardianInvocationRecord.create(
                arguments=request.arguments,
                result_file_descriptor=result_write_fd,
                start_file_descriptor=start_read_fd,
                lifecycle=request.lifecycle,
                budget=request.budget,
                termination_policy=self._termination_policy,
            )
            guardian_arguments = (
                *self._program.arguments,
                "--request-json",
                invocation.model_dump_json(),
            )
            inherited_descriptors = (
                *request.lease_file_descriptors,
                result_write_fd,
                start_read_fd,
                *cancellation_lease.inherited_file_descriptors(),
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
                os.close(result_write_fd)
                result_write_open = False
                os.close(start_read_fd)
                start_read_open = False
                cancellation_lease.publish(guardian.pid)
                cancellation_lease.transfer_to_guardian()
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
            try:
                if guardian is not None and not group_contained:
                    self._process_group_supervisor.abort(
                        OwnedProcessGroupLeader(guardian.pid)
                    )
            finally:
                try:
                    cancellation_lease.retire()
                finally:
                    terminal_foreground.restore()
                    if result_write_open:
                        os.close(result_write_fd)
                    if start_read_open:
                        os.close(start_read_fd)
                    if start_write_open:
                        os.close(start_write_fd)
                    os.close(result_read_fd)

    @staticmethod
    def _prepare_cancellation(
        request: ExecutorGuardianRequest,
        open_pipe_descriptors: tuple[int, int, int, int],
    ) -> ExecutorGuardianCancellationLease:
        """Acquire cancellation ownership or roll back all pre-spawn pipes."""
        try:
            return prepare_executor_guardian_cancellation(request.cancellation)
        except BaseException:
            for descriptor in open_pipe_descriptors:
                os.close(descriptor)
            raise

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
        if type(supervision) is ProcessGroupInterrupted or interruption.requested:
            return ExecutorGuardianCommandCompleted(-signal.SIGTERM)
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("an unbounded guardian wait cannot time out")
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
