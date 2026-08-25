"""Composition support for the host executor and bounded agent phases."""

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
from ..execution.agent_phase_command_scheduler import HostAgentPhaseCommandScheduler
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler
from ..ports.executor import Executor
from ..ports.executor_monitor import ExecutorMonitor
from .bootstrap_executor_platform import (
    raise_missing_posix_executor_dependency,
    require_posix_executor,
)


_PROCESS_TERMINATION = ExecutorProcessTerminationPolicy(
    graceful_shutdown_seconds=2.0
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
    require_posix_executor()
    try:
        from ..adapters.host_cpu_utilization import (
            SystemHostCpuUtilizationObserver,
        )
        from ..execution.host_executor import (
            ExecutorRequestIdentityFactory,
            HostExecutor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        raise_missing_posix_executor_dependency(exc)
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
        process_termination_policy=_PROCESS_TERMINATION,
        history_retention_policy=_HISTORY_RETENTION,
        queue_settle_seconds=0.1,
        queue_poll_seconds=0.05,
    )


def build_executor_monitor() -> ExecutorMonitor:
    """Compose the read-only executor activity monitor."""
    require_posix_executor()
    try:
        from ..execution.host_executor import (
            HostExecutorMonitor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        raise_missing_posix_executor_dependency(exc)
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
