"""Entry-point composition owner for host execution and agent phases."""

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
from ..domain.executor_guardian import ExecutorGuardianTerminationPolicy
from ..domain.terminal_launch import TerminalShell
from ..domain.terminal_session_lifecycle import TerminalSessionWatcherPolicy
from ..domain.terminal_session_owner import TerminalSessionOwnerPolicy
from ..domain.terminal_session_termination import TerminalSessionTerminationPolicy
from ..execution.agent_phase_command_scheduler import HostAgentPhaseCommandScheduler
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler
from ..ports.atomic_record_store import AtomicRecordStoreFactory
from ..ports.contained_command import ContainedCommandCapture
from ..ports.executor import Executor
from ..ports.executor_command_guardian import ExecutorCommandGuardian
from ..ports.executor_history_lock import ExecutorHistoryRetentionLock
from ..ports.executor_monitor import ExecutorMonitor
from ..ports.host_cpu_utilization import HostCpuUtilizationObserver
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.posix_process import PosixProcessLauncher
from ..ports.process_group_observer import ProcessGroupObserver
from ..ports.retained_thread import RetainedThreadFactory
from ..ports.terminal_session_terminator import TerminalSessionTerminator
from ..ports.terminal_session_owner import TerminalSessionOwner
from ..ports.terminal_session_registry import TerminalSessionRegistry
from ..ports.validation_command_runner import ValidationCommandRunner
from ..execution.terminal_session_lifecycle import (
    TerminalSessionWatcherFactory,
    ThreadTerminalSessionWatcherFactory,
)


_PROCESS_TERMINATION = ExecutorProcessTerminationPolicy(
    graceful_shutdown_seconds=2.0,
    forceful_shutdown_seconds=2.0,
)
_TERMINAL_SESSION_RELAY_MARGIN_SECONDS = 1.0
_TERMINAL_SESSION_GUARDIAN_RELAY_SECONDS = (
    _PROCESS_TERMINATION.graceful_shutdown_seconds
    + _PROCESS_TERMINATION.forceful_shutdown_seconds
    + _TERMINAL_SESSION_RELAY_MARGIN_SECONDS
)
_TERMINAL_SESSION_OWNER_CONTAINMENT_SECONDS = (
    _TERMINAL_SESSION_GUARDIAN_RELAY_SECONDS
    + _PROCESS_TERMINATION.graceful_shutdown_seconds
)
_TERMINAL_SESSION_TERMINATION = TerminalSessionTerminationPolicy(
    # The outer wrapper gets enough courtesy time to relay SIGTERM and let its
    # executor supervisor spend both inner TERM/KILL bounds, plus one margin.
    graceful_shutdown_seconds=_TERMINAL_SESSION_GUARDIAN_RELAY_SECONDS,
    forceful_shutdown_seconds=_TERMINAL_SESSION_GUARDIAN_RELAY_SECONDS,
)
_TERMINAL_SESSION_WATCHER = TerminalSessionWatcherPolicy(
    shutdown_timeout_seconds=_TERMINAL_SESSION_GUARDIAN_RELAY_SECONDS,
)
_TERMINAL_SESSION_OWNER = TerminalSessionOwnerPolicy(
    startup_timeout_seconds=30.0,
    graceful_shutdown_seconds=_PROCESS_TERMINATION.graceful_shutdown_seconds,
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


def build_atomic_record_store_factory() -> AtomicRecordStoreFactory:
    """Compose crash-safe JSON persistence for lifecycle owners."""
    from ..execution.atomic_record_store import OsAtomicRecordStoreFactory

    return OsAtomicRecordStoreFactory()


def build_process_group_observer() -> ProcessGroupObserver:
    """Compose the portable host process-table observer."""
    from ..adapters.kernel_process_identity import (
        build_kernel_process_identity_observer,
    )
    from ..adapters.ps_process_group_observer import (
        PsProcessGroupObserver,
        PsProcessObservationPolicy,
    )

    return PsProcessGroupObserver(
        Path("/bin/ps"),
        PsProcessObservationPolicy(command_timeout_seconds=2.0),
        build_kernel_process_identity_observer(),
    )


def compose_executor(host_cpu_observer: HostCpuUtilizationObserver) -> Executor:
    """Compose host execution after the root chooses its system observer."""
    _require_posix_executor()
    try:
        from ..execution.atomic_record_store import OsAtomicPathReplacement
        from ..execution.host_executor import (
            ExecutorRequestIdentityFactory,
            HostExecutor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        _raise_missing_posix_executor_dependency(exc)
        raise AssertionError("unreachable after missing executor dependency")
    pool_dir = default_executor_pool_dir()
    return HostExecutor(
        pool_dir=pool_dir,
        host_cpu_slots=detected_executor_cpu_count(),
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_build_demand_estimator(),
        host_cpu_observer=host_cpu_observer,
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: uuid4().hex,
        ),
        command_guardian=build_executor_command_guardian(),
        atomic_path_replacement=OsAtomicPathReplacement(),
        history_retention_lock=_build_history_retention_lock(pool_dir),
        history_retention_policy=_HISTORY_RETENTION,
        queue_settle_seconds=0.1,
        queue_poll_seconds=0.05,
    )


def build_process_group_supervisor() -> ProcessGroupSupervisor:
    """Compose wait, containment, and reaping behind one lifecycle owner."""
    _require_posix_process_groups()
    from ..execution.process_group_supervisor import PosixProcessGroupSupervisor
    from ..execution.process_group_terminator import PosixProcessGroupTerminator

    return PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            _PROCESS_TERMINATION,
            build_process_group_observer(),
        )
    )


def build_posix_process_launcher() -> PosixProcessLauncher:
    """Compose the gap-free retained child-process activation owner."""
    _require_posix_process_groups()
    from ..domain.posix_process import PosixProcessProgram
    from ..execution.posix_process import (
        MaskedPosixSpawnPrimitive,
        RetainedPosixProcessLauncher,
    )

    return RetainedPosixProcessLauncher(
        PosixProcessProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.entrypoints.posix_process_child",
            )
        ),
        MaskedPosixSpawnPrimitive(),
        build_process_group_supervisor(),
        _PROCESS_TERMINATION,
    )


def terminal_session_watcher_policy() -> TerminalSessionWatcherPolicy:
    """Return the composition-root-owned PTY watcher shutdown policy."""
    return _TERMINAL_SESSION_WATCHER


def build_terminal_session_watcher_factory() -> TerminalSessionWatcherFactory:
    """Compose the thread-backed PTY completion-owner factory."""
    return ThreadTerminalSessionWatcherFactory(build_retained_thread_factory())


def build_retained_thread_factory() -> RetainedThreadFactory:
    """Compose the single owner for retained background-thread lifecycles."""
    from ..execution.retained_thread import (
        MaskedThreadStartPrimitive,
        ThreadingRetainedThreadFactory,
    )

    return ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())


def build_terminal_session_owner() -> TerminalSessionOwner:
    """Compose the pre-registry terminal process-group ownership wrapper."""
    _require_posix_process_groups()
    from ..execution.terminal_session_owner import (
        PosixTerminalSessionOwner,
        TerminalSessionOwnerProgram,
    )
    from ..domain.process_group_sentinel import ProcessGroupSentinelProgram

    owner_program = (
        str(Path(sys.executable)),
        "-m",
        "issue_orchestrator.entrypoints.terminal_session_owner_child",
    )
    sentinel_program = (
        str(Path(sys.executable)),
        "-m",
        "issue_orchestrator.execution.process_group_sentinel",
    )
    return PosixTerminalSessionOwner(
        TerminalSessionOwnerProgram(owner_program),
        ProcessGroupSentinelProgram(sentinel_program),
        _TERMINAL_SESSION_OWNER,
        build_atomic_record_store_factory(),
    )


def build_terminal_session_registry(repo_root: Path) -> TerminalSessionRegistry:
    """Compose durable pending-to-identified terminal launch ownership."""
    from ..execution.terminal_session_registry import (
        SqliteTerminalSessionRegistry,
    )

    return SqliteTerminalSessionRegistry(repo_root.resolve())


def compose_terminal_session_terminator(
    process_group_observer: ProcessGroupObserver,
) -> TerminalSessionTerminator:
    """Compose containment policy around the root-supplied host observer."""
    _require_posix_process_groups()
    if not isinstance(process_group_observer, ProcessGroupObserver):
        raise ValueError(
            "compose_terminal_session_terminator.process_group_observer must "
            "implement ProcessGroupObserver"
        )
    from ..execution.executor_guardian_cancellation import (
        ExecutorSessionGuardianCanceller,
    )
    from ..execution.process_cancellation_endpoint import (
        ProcessCancellationEndpointRequester,
    )
    from ..execution.session_process_group_terminator import (
        PosixTerminalSessionProcessGroupTerminator,
    )
    from ..execution.terminal_session_containment import (
        OwnerMediatedTerminalSessionContainment,
    )

    return PosixTerminalSessionProcessGroupTerminator(
        _TERMINAL_SESSION_TERMINATION,
        OwnerMediatedTerminalSessionContainment(
            ProcessCancellationEndpointRequester(
                _TERMINAL_SESSION_OWNER_CONTAINMENT_SECONDS,
                build_atomic_record_store_factory(),
            ),
            ExecutorSessionGuardianCanceller(
                containment_timeout_seconds=(
                    _TERMINAL_SESSION_OWNER_CONTAINMENT_SECONDS
                ),
                record_stores=build_atomic_record_store_factory(),
            ),
            _TERMINAL_SESSION_OWNER_CONTAINMENT_SECONDS,
        ),
        process_group_observer,
    )


def build_contained_command_capture() -> ContainedCommandCapture:
    """Compose streamed command capture behind one process-lifecycle owner."""
    _require_posix_process_groups()
    from ..domain.contained_command import ContainedCommandOutputPolicy
    from ..execution.contained_command_capture import (
        OsContainedCommandOutputPipeFactory,
        PosixContainedCommandCapture,
    )

    return PosixContainedCommandCapture(
        build_posix_process_launcher(),
        build_process_group_supervisor(),
        ContainedCommandOutputPolicy(
            poll_interval_seconds=0.05,
            shutdown_timeout_seconds=2.0,
            final_drain_byte_limit=1_048_576,
        ),
        OsContainedCommandOutputPipeFactory(),
    )


def build_validation_command_runner() -> ValidationCommandRunner:
    """Compose validation spawn, capture, timeout, containment, and reaping."""
    _require_posix_process_groups()
    from ..domain.contained_command import ContainedCommandOutputPolicy
    from ..execution.contained_validation_command import (
        PosixContainedValidationCommandRunner,
        PosixValidationPipeCaptureFactory,
    )
    from ..execution.validation_pipe_resources import (
        default_validation_pipe_selector,
    )
    from ..execution.posix_pipe import OsPosixPipeFactory
    from ..execution.validation_launch_pipes import (
        PosixValidationLaunchPipesFactory,
    )

    return PosixContainedValidationCommandRunner(
        build_posix_process_launcher(),
        build_process_group_supervisor(),
        ContainedCommandOutputPolicy(
            poll_interval_seconds=0.05,
            shutdown_timeout_seconds=2.0,
            final_drain_byte_limit=4_194_304,
        ),
        PosixValidationPipeCaptureFactory(default_validation_pipe_selector),
        PosixValidationLaunchPipesFactory(OsPosixPipeFactory()),
    )


def build_executor_command_guardian() -> ExecutorCommandGuardian:
    """Compose the child-side lease, deadline, and process-group owner."""
    _require_posix_process_groups()
    from ..execution.host_executor.guardian_launcher import (
        ExecutorGuardianProgram,
        PosixExecutorCommandGuardian,
    )
    from ..domain.process_group_sentinel import (
        ProcessGroupSentinelPolicy,
        ProcessGroupSentinelProgram,
    )

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
            graceful_shutdown_seconds=(_PROCESS_TERMINATION.graceful_shutdown_seconds),
            startup_timeout_seconds=_TERMINAL_SESSION_OWNER.startup_timeout_seconds,
        ),
        build_atomic_record_store_factory(),
        build_process_group_supervisor(),
        ExecutorGuardianTerminationPolicy(
            _PROCESS_TERMINATION.graceful_shutdown_seconds
        ),
    )


def build_executor_monitor() -> ExecutorMonitor:
    """Compose the read-only executor activity monitor."""
    _require_posix_executor()
    try:
        from ..execution.atomic_record_store import OsAtomicPathReplacement
        from ..execution.host_executor import (
            HostExecutorMonitor,
            default_executor_pool_dir,
            detected_executor_cpu_count,
        )
    except ModuleNotFoundError as exc:
        _raise_missing_posix_executor_dependency(exc)
        raise AssertionError("unreachable after missing executor dependency")
    pool_dir = default_executor_pool_dir()
    return HostExecutorMonitor(
        pool_dir,
        detected_executor_cpu_count(),
        _build_demand_estimator(),
        _HISTORY_RETENTION,
        _build_history_retention_lock(pool_dir),
        OsAtomicPathReplacement(),
    )


def _build_history_retention_lock(
    pool_dir: Path,
) -> ExecutorHistoryRetentionLock:
    """Own the one lock identity shared by executor writers and monitors."""
    try:
        from ..execution.executor_history_lock import (
            PosixExecutorHistoryRetentionLock,
        )
    except ModuleNotFoundError as exc:
        _raise_missing_posix_executor_dependency(exc)
        raise AssertionError("unreachable after missing executor dependency")
    return PosixExecutorHistoryRetentionLock(
        (pool_dir / "work-history" / "retention.lock").resolve()
    )


def _build_demand_estimator() -> ExecutorWorkDemandEstimator:
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


def _require_posix_executor() -> None:
    if os.name != "posix":
        raise RuntimeError(
            "the pooled host executor requires POSIX advisory locks; "
            "use executor-run-direct explicitly for unpooled execution"
        )


def _require_posix_process_groups() -> None:
    if os.name != "posix" or not hasattr(os, "killpg") or not hasattr(os, "waitid"):
        raise RuntimeError(
            "process-tree containment requires POSIX os.killpg and os.waitid"
        )


def _raise_missing_posix_executor_dependency(exc: ModuleNotFoundError) -> None:
    if exc.name not in {"fcntl", "resource"}:
        raise exc
    raise RuntimeError(
        "the pooled host executor requires POSIX fcntl and resource support; "
        "use executor-run-direct explicitly for unpooled execution"
    ) from exc
