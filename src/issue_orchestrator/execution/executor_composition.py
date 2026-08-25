"""Composition provider for the host executor and bounded agent phases."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from ..control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
)
from ..domain.agent_phase_execution import AgentPhaseOuterWatchdogPolicy
from ..domain.executor import (
    ExecutorHistoryRetentionPolicy,
    ExecutorProcessTerminationPolicy,
)
from ..domain.terminal_launch import TerminalShell
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler
from ..ports.executor import Executor
from ..ports.executor_monitor import ExecutorMonitor
from ..ports.process_group_terminator import ProcessGroupTerminator
from .agent_phase_command_scheduler import HostAgentPhaseCommandScheduler
from .process_group_terminator import PosixProcessGroupTerminator


_PROCESS_TERMINATION = ExecutorProcessTerminationPolicy(
    graceful_shutdown_seconds=2.0,
    forceful_shutdown_seconds=2.0,
)
_HISTORY_RETENTION = ExecutorHistoryRetentionPolicy(
    maximum_profiles=2048,
    maximum_observations_per_profile=24,
)
_OUTER_WATCHDOG = AgentPhaseOuterWatchdogPolicy(
    executor_termination=_PROCESS_TERMINATION,
    observer_margin_seconds=58.0,
)


def build_agent_phase_command_scheduler() -> AgentPhaseCommandScheduler:
    """Compose the scheduler that preserves Bash and outer cleanup margin."""
    return HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=_OUTER_WATCHDOG,
    )


def build_executor() -> Executor:
    """Compose the machine-wide command executor behind its public port."""
    _require_posix_executor()
    try:
        from ..adapters.host_cpu_utilization import (
            SystemHostCpuUtilizationObserver,
        )
        from .host_executor import (
            ExecutorRequestIdentityFactory,
            HostExecutor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        _raise_missing_posix_executor_dependency(exc)
        raise AssertionError("unreachable after missing executor dependency")
    return HostExecutor(
        pool_dir=default_executor_pool_dir(),
        host_cpu_slots=detected_executor_cpu_count(),
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_build_demand_estimator(),
        host_cpu_observer=SystemHostCpuUtilizationObserver(),
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: uuid4().hex,
        ),
        process_group_terminator=build_process_group_terminator(),
        history_retention_policy=_HISTORY_RETENTION,
        queue_settle_seconds=0.1,
        queue_poll_seconds=0.05,
    )


def build_process_group_terminator() -> ProcessGroupTerminator:
    """Compose fail-fast containment for a caller-owned process group."""
    _require_posix_process_groups()
    return PosixProcessGroupTerminator(_PROCESS_TERMINATION)


def build_executor_monitor() -> ExecutorMonitor:
    """Compose the read-only executor activity monitor."""
    _require_posix_executor()
    try:
        from .host_executor import (
            HostExecutorMonitor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        _raise_missing_posix_executor_dependency(exc)
        raise AssertionError("unreachable after missing executor dependency")
    return HostExecutorMonitor(
        default_executor_pool_dir(),
        detected_executor_cpu_count(),
        _build_demand_estimator(),
        _HISTORY_RETENTION,
    )


def _build_demand_estimator() -> ExecutorWorkDemandEstimator:
    """Construct the one learning policy shared by executor and monitor."""
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


def _require_posix_executor() -> None:
    """Reject pooled execution where POSIX advisory locks are unavailable."""
    if os.name != "posix":
        raise RuntimeError(
            "the pooled host executor requires POSIX advisory locks; "
            "use executor-run-direct explicitly for unpooled execution"
        )


def _require_posix_process_groups() -> None:
    """Reject process containment where the required kernel contract is absent."""
    if os.name != "posix" or not hasattr(os, "killpg") or not hasattr(os, "waitid"):
        raise RuntimeError(
            "process-tree containment requires POSIX os.killpg and os.waitid"
        )


def _raise_missing_posix_executor_dependency(exc: ModuleNotFoundError) -> None:
    """Translate known missing POSIX modules without hiding import defects."""
    if exc.name not in {"fcntl", "resource"}:
        raise exc
    raise RuntimeError(
        "the pooled host executor requires POSIX fcntl and resource support; "
        "use executor-run-direct explicitly for unpooled execution"
    ) from exc
