# pyright: strict
"""Outer owner for launching and observing one crash-resilient guardian."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ...domain.executor_guardian import (
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
)
from ...domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupUnboundedWait,
)
from ...ports.executor_command_guardian import ExecutorGuardianRequest
from ...ports.process_group_supervisor import ProcessGroupSupervisor
from ..process_group_supervisor import NeverInterruptProcessGroup
from ._guardian_contracts import (
    GUARDIAN_TERMINAL_ADAPTER,
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
        result_read_fd, result_write_fd = os.pipe()
        try:
            try:
                invocation = GuardianInvocationRecord.create(
                    arguments=request.arguments,
                    result_file_descriptor=result_write_fd,
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
                )
                try:
                    guardian = subprocess.Popen(
                        guardian_arguments,
                        env=dict(request.environment),
                        pass_fds=inherited_descriptors,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise ExecutorGuardianLaunchError(
                        f"could not start executor guardian: {error!r}"
                    ) from error
            finally:
                os.close(result_write_fd)

            supervision = self._process_group_supervisor.supervise(
                OwnedProcessGroupLeader(guardian.pid),
                ProcessGroupUnboundedWait(),
                NeverInterruptProcessGroup(),
            )
            guardian.returncode = supervision.termination.leader_exit_code
            if type(supervision) is not ProcessGroupCompleted:
                raise AssertionError("an unbounded guardian wait cannot time out")
            terminal = self._read_terminal(result_read_fd)
            self._require_expected_guardian_exit(guardian.returncode, terminal)
        finally:
            os.close(result_read_fd)
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
