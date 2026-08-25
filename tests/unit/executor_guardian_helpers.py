"""Shared strongly typed guardian composition for host-executor tests."""

from __future__ import annotations

import sys
from pathlib import Path

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.executor_guardian import (
    ExecutorGuardianTerminationPolicy,
)
from issue_orchestrator.execution.host_executor.guardian_launcher import (
    ExecutorGuardianProgram,
    PosixExecutorCommandGuardian,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.ports.executor_command_guardian import (
    ExecutorCommandGuardian,
)


def executor_command_guardian(
    termination_policy: ExecutorProcessTerminationPolicy,
) -> ExecutorCommandGuardian:
    """Compose the production guardian with a test-selected termination policy."""
    if type(termination_policy) is not ExecutorProcessTerminationPolicy:
        raise ValueError("test guardian requires ExecutorProcessTerminationPolicy")
    return PosixExecutorCommandGuardian(
        ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(termination_policy)
        ),
        ExecutorGuardianTerminationPolicy(
            termination_policy.graceful_shutdown_seconds
        ),
    )
