"""Shared strongly typed guardian composition for host-executor tests."""

from __future__ import annotations

import sys
from pathlib import Path

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.executor_guardian import (
    ExecutorGuardianTerminationPolicy,
)
from issue_orchestrator.domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from issue_orchestrator.execution.host_executor.guardian_launcher import (
    ExecutorGuardianProgram,
    PosixExecutorCommandGuardian,
)
from issue_orchestrator.execution.atomic_record_store import (
    OsAtomicRecordStoreFactory,
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
from tests.process_completion_fixture import build_test_process_group_observer


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
        ProcessGroupSentinelProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.process_group_sentinel",
            )
        ),
        ProcessGroupSentinelPolicy(
            termination_policy.graceful_shutdown_seconds,
            1.0,
        ),
        OsAtomicRecordStoreFactory(),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination_policy,
                build_test_process_group_observer(),
            )
        ),
        ExecutorGuardianTerminationPolicy(
            termination_policy.graceful_shutdown_seconds
        ),
    )
