"""Public-boundary tests for cooperatively scheduled agent phases."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
)
from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseLaunchRequest,
    AgentPhaseRunSpecification,
    ProviderInvocationArguments,
)
from issue_orchestrator.control.agent_phase_launch_planner import (
    AgentPhaseLaunchPlanner,
)
from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorCommand,
    ExecutorCommandLifecycle,
    ExecutorConcurrencyRange,
    ExecutorDeadlineExceededError,
    ExecutorDeadlineReason,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
    ExecutorInteractiveSessionCancellation,
    ExecutorNoCommandCancellation,
    ExecutorProcessTerminationPolicy,
    ExecutorRunSpecification,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorAdmissionDeadlineExceeded,
    ExecutorCommandDeadlineExceeded,
    ExecutorRecentEventsQuery,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
)
from issue_orchestrator.execution.agent_phase_command_scheduler import (
    HostAgentPhaseCommandScheduler,
)
from issue_orchestrator.entrypoints.cli_tools.agent_phase_run import run_agent_phase
from issue_orchestrator.execution.atomic_record_store import OsAtomicPathReplacement
from issue_orchestrator.execution.host_executor import (
    ExecutorRequestIdentityFactory,
    HostExecutor,
    HostExecutorMonitor,
)
from issue_orchestrator.execution.executor_history_lock import (
    PosixExecutorHistoryRetentionLock,
)
from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseOuterWatchdogPolicy,
)
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)
from issue_orchestrator.domain.models import AgentConfig, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.domain.session_watchdog import ScheduledSessionWatchdog
from issue_orchestrator.ports.host_cpu_utilization import HostCpuUtilizationObserver
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ExitingTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.unit.session_run_helpers import make_session_run_assets
from tests.unit.executor_guardian_helpers import executor_command_guardian
from tests.process_completion_fixture import (
    ExecutorGuardianCancellationContainment,
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    TextProcessInvocation,
)
from tests.unit.executor_pool_dsl import (
    ExecutorPoolHeldCommand,
    ExecutorPoolRig,
    ExecutorPoolWork,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"


pytestmark = pytest.mark.timeout(180)


class _SaturatedHostCpuObserver:
    """Keep admission closed without relying on ambient machine load."""

    def reset(self) -> None:
        pass

    def observe(self) -> ExecutorHostCpuUtilization:
        return ExecutorHostCpuUtilization(95.0, 0.01)


class _IdleHostCpuObserver:
    def reset(self) -> None:
        return None

    def observe(self) -> ExecutorHostCpuUtilization:
        return ExecutorHostCpuUtilization(0.0, 0.01)


class _AdmissionThenExpiredClock:
    """Advance only between an admission grant and command budgeting."""

    def __init__(self) -> None:
        self._observations = 0

    def monotonic(self) -> float:
        self._observations += 1
        return 0.0 if self._observations <= 5 else 1.0


class _OpaqueProviderCommandWrapper:
    """Test seam that proves classification precedes opaque wrapping."""

    def __init__(self) -> None:
        self.received_commands: list[str] = []

    def wrap(
        self,
        base_command: str,
        agent_config: AgentConfig,
        run_dir: Path,
        *,
        extra_provider_args: Mapping[str, str],
    ) -> str:
        self.received_commands.append(base_command)
        return "opaque-provider-runner --command-token hidden"


class _RecordingScheduledWatchdogStore:
    """Port fake retaining the exact planner-owned watchdog write."""

    def __init__(self) -> None:
        self.records: list[tuple[SessionRunAssets, ScheduledSessionWatchdog]] = []

    def record_scheduled_watchdog(
        self,
        run: SessionRunAssets,
        watchdog: ScheduledSessionWatchdog,
    ) -> None:
        self.records.append((run, watchdog))


def _demand_estimator() -> ExecutorWorkDemandEstimator:
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


def _history_lock(pool_dir: Path) -> PosixExecutorHistoryRetentionLock:
    return PosixExecutorHistoryRetentionLock(
        (pool_dir / "work-history" / "retention.lock").resolve()
    )


def _deterministic_host_executor(
    pool_dir: Path,
    *,
    host_cpu_observer: HostCpuUtilizationObserver,
    request_nonce: str,
) -> HostExecutor:
    """Build a real executor whose admission does not depend on ambient load."""
    return HostExecutor(
        pool_dir=pool_dir,
        host_cpu_slots=1,
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_demand_estimator(),
        host_cpu_observer=host_cpu_observer,
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: request_nonce,
        ),
        command_guardian=executor_command_guardian(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            )
        ),
        atomic_path_replacement=OsAtomicPathReplacement(),
        history_retention_lock=_history_lock(pool_dir),
        history_retention_policy=ExecutorHistoryRetentionPolicy(2048, 24),
        queue_settle_seconds=0.01,
        queue_poll_seconds=0.01,
    )


def _phase_cli(
    pool_dir: Path,
    *,
    active_timeout_seconds: str,
    absolute_timeout_seconds: str,
    command: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    cancellation_record = pool_dir.resolve() / "executor-guardian-cancellation.json"
    return PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="agent phase CLI",
            arguments=(
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.agent_phase_run",
                "--work-key",
                "agent-phase:agent:web:code",
                "--group",
                "agent:run-1:coding-1",
                "--active-timeout-seconds",
                active_timeout_seconds,
                "--absolute-timeout-seconds",
                absolute_timeout_seconds,
                "--cancellation-record",
                str(cancellation_record),
                "--",
                *command,
            ),
            working_directory=REPO_ROOT,
            environment={**os.environ, POOL_DIR_ENV: str(pool_dir)},
            timeout_containment=ExecutorGuardianCancellationContainment(
                cancellation_record
            ),
        )
    )


def _executor_events(pool_dir: Path) -> subprocess.CompletedProcess[str]:
    return PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="executor events CLI",
            arguments=(
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli",
                "executor-events",
                "--limit",
                "20",
            ),
            working_directory=REPO_ROOT,
            environment={**os.environ, POOL_DIR_ENV: str(pool_dir)},
            timeout_containment=NoDescendantProcessContainment(),
        )
    )


def test_phase_specification_converts_active_timeout_to_fixed_absolute_bound(
    tmp_path: Path,
) -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command="run-agent --issue 42",
        cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            tmp_path.resolve()
        ),
        destination=make_session_run_assets(
            tmp_path,
            session_name="phase-specification",
        ).terminal_destination,
    )

    assert specification.deadline.active_timeout_seconds == 2700.0
    assert specification.deadline.absolute_timeout_seconds == 5400.0
    assert (
        specification.deadline.command_budget(
            submitted_at_monotonic=100.0,
            admitted_at_monotonic=3000.0,
        ).reason
        is ExecutorDeadlineReason.ABSOLUTE
    )


@pytest.mark.parametrize(
    ("phase_name", "task_kind"),
    [
        ("coding", TaskKind.CODE),
        ("validation-retry", TaskKind.CODE),
        ("rework", TaskKind.REWORK),
        ("review", TaskKind.REVIEW),
        ("retrospective-review", TaskKind.RETROSPECTIVE_REVIEW),
    ],
)
@pytest.mark.parametrize(
    ("provider_command", "expected_intent"),
    [
        (
            "claude --model sonnet 'do work'",
            TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
        ),
        (
            "codex --model gpt-5.4 'do work'",
            TerminalInteractionIntent.CODEX_TRUST_WORKTREE,
        ),
        ("custom-agent 'do work'", TerminalInteractionIntent.NONE),
    ],
)
def test_launch_owner_classifies_every_phase_provider_before_wrapping(
    tmp_path: Path,
    phase_name: str,
    task_kind: TaskKind,
    provider_command: str,
    expected_intent: TerminalInteractionIntent,
) -> None:
    wrapper = _OpaqueProviderCommandWrapper()
    scheduler = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            ),
            observer_margin_seconds=58.0,
        ),
    )
    watchdog_store = _RecordingScheduledWatchdogStore()
    planner = AgentPhaseLaunchPlanner(scheduler, wrapper, watchdog_store)
    run = make_session_run_assets(
        tmp_path / phase_name,
        session_name=phase_name,
    )

    launch, scheduled_config = planner.schedule(
        AgentPhaseLaunchRequest(
            provider_command=provider_command,
            environment_exports="export PHASE_TEST=1",
            agent_config=AgentConfig(
                prompt_path=tmp_path / "prompt.md",
                timeout_minutes=45,
            ),
            run=run,
            agent_label="agent:test",
            task_kind=task_kind,
            provider_arguments=ProviderInvocationArguments.from_mapping({}),
        )
    )

    arguments = shlex.split(launch.shell_command)
    assert wrapper.received_commands == [provider_command]
    assert launch.interaction_intent is expected_intent
    assert TerminalInteractionIntent.classify(launch.shell_command) is (
        TerminalInteractionIntent.NONE
    )
    assert arguments[arguments.index("--work-key") + 1] == (
        f"agent-phase:agent:test:{task_kind.value}"
    )
    assert scheduled_config.timeout_minutes == 92
    assert watchdog_store.records == [
        (run, ScheduledSessionWatchdog(timeout_minutes=92))
    ]


def test_launch_owner_preserves_unrestricted_agent_label_in_executor_identity(
    tmp_path: Path,
) -> None:
    scheduler = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            ),
            observer_margin_seconds=58.0,
        ),
    )
    planner = AgentPhaseLaunchPlanner(
        scheduler,
        _OpaqueProviderCommandWrapper(),
        _RecordingScheduledWatchdogStore(),
    )
    run = make_session_run_assets(tmp_path, session_name="unicode-label")

    launch, _scheduled_config = planner.schedule(
        AgentPhaseLaunchRequest(
            provider_command="custom-agent 'do work'",
            environment_exports="export PHASE_TEST=1",
            agent_config=AgentConfig(
                prompt_path=tmp_path / "prompt.md",
                timeout_minutes=45,
            ),
            run=run,
            agent_label="agent:backend team · β",
            task_kind=TaskKind.CODE,
            provider_arguments=ProviderInvocationArguments.from_mapping({}),
        )
    )

    arguments = shlex.split(launch.shell_command)
    assert arguments[arguments.index("--work-key") + 1] == (
        "agent-phase:agent:backend team · β:code"
    )


def test_scheduler_renders_one_shell_safe_internal_invocation(tmp_path: Path) -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command="printf '%s\\n' 'human readable'",
        cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            tmp_path.resolve()
        ),
        destination=make_session_run_assets(
            tmp_path,
            session_name="scheduler-render",
        ).terminal_destination,
    )
    scheduler = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            ),
            observer_margin_seconds=58.0,
        ),
    )

    scheduled = scheduler.schedule(specification)
    arguments = shlex.split(scheduled.terminal_launch.shell_command)

    assert scheduled.absolute_timeout_minutes == 92
    assert scheduled.absolute_timeout_minutes * 60 > (
        specification.deadline.absolute_timeout_seconds + 2.0 + 58.0
    )
    assert arguments[:4] == [
        "exec",
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.cli_tools.agent_phase_run",
    ]
    assert arguments[arguments.index("--") + 1 :] == [
        "/bin/bash",
        "-lc",
        specification.shell_command,
    ]
    assert arguments[arguments.index("--cancellation-record") + 1] == str(
        specification.cancellation.record_path
    )
    assert scheduled.terminal_launch.shell is TerminalShell.BASH


@pytest.mark.parametrize(
    ("command", "expected_intent"),
    [
        (
            "claude --model sonnet 'fix it'",
            TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
        ),
        (
            "codex --model gpt-5.4 'fix it'",
            TerminalInteractionIntent.CODEX_TRUST_WORKTREE,
        ),
    ],
)
def test_scheduler_preserves_interaction_intent_hidden_by_executor_wrapper(
    tmp_path: Path,
    command: str,
    expected_intent: TerminalInteractionIntent,
) -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=expected_intent,
        shell_command=command,
        cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            tmp_path.resolve()
        ),
        destination=make_session_run_assets(
            tmp_path,
            session_name="scheduler-intent",
        ).terminal_destination,
    )

    scheduled = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            ),
            observer_margin_seconds=58.0,
        ),
    ).schedule(specification)

    assert scheduled.terminal_launch.interaction_intent is expected_intent
    assert (
        TerminalInteractionIntent.classify(scheduled.terminal_launch.shell_command)
        is TerminalInteractionIntent.NONE
    )


def test_scheduled_phase_executes_bash_language_without_shell_drift(
    tmp_path: Path,
) -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:test:bash-language"),
        fairness_group=ExecutorFairnessGroup("agent:test:bash-language"),
        active_timeout_minutes=1,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command="values=(alpha beta); [[ ${values[1]} == beta ]]",
        cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            tmp_path.resolve()
        ),
        destination=make_session_run_assets(
            tmp_path,
            session_name="bash-language",
        ).terminal_destination,
    )
    scheduled = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            ),
            observer_margin_seconds=58.0,
        ),
    ).schedule(specification)
    scheduled_arguments = shlex.split(scheduled.terminal_launch.shell_command)
    executor = _deterministic_host_executor(
        tmp_path / "pool",
        host_cpu_observer=_IdleHostCpuObserver(),
        request_nonce="c" * 32,
    )

    assert scheduled_arguments[:4] == [
        "exec",
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.cli_tools.agent_phase_run",
    ]
    assert run_agent_phase(scheduled_arguments[4:], executor) == 0


def test_internal_phase_client_runs_a_plain_command_without_orchestrator(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    executor = _deterministic_host_executor(
        tmp_path / "pool",
        host_cpu_observer=_IdleHostCpuObserver(),
        request_nonce="d" * 32,
    )
    result = run_agent_phase(
        (
            "--work-key",
            "agent-phase:agent:web:code",
            "--group",
            "agent:run-1:coding-1",
            "--active-timeout-seconds",
            "2",
            "--absolute-timeout-seconds",
            "4",
            "--cancellation-record",
            str(tmp_path / "executor-guardian-cancellation.json"),
            "--",
            sys.executable,
            "-c",
            "print('PHASE-RAN')",
        ),
        executor,
    )
    captured = capfd.readouterr()

    assert result == 0, captured.err
    assert captured.out == "PHASE-RAN\n"


def test_internal_phase_client_terminates_at_active_deadline_and_releases_lease(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    executor = _deterministic_host_executor(
        pool_dir,
        host_cpu_observer=_IdleHostCpuObserver(),
        request_nonce="a" * 32,
    )
    specification = ExecutorRunSpecification(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        concurrency_range=ExecutorConcurrencyRange(1, 1),
        exclusive_resources=(),
    )
    timed_out = executor.run(
        specification,
        ExecutorCommand(
            (
                sys.executable,
                "-c",
                "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "signal.pause()",
            ),
            ExecutorBoundedDeadline(0.05, 1.0),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )
    recovered = executor.run(
        specification,
        ExecutorCommand(
            (sys.executable, "-c", "print('RECOVERED')"),
            ExecutorBoundedDeadline(2.0, 4.0),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )
    events = _executor_events(pool_dir)

    assert timed_out.exit_code == 124
    assert recovered.exit_code == 0
    assert tuple((pool_dir / "leases").glob("*.json")) == ()
    assert events.returncode == 0, events.stdout
    deadline_line = next(
        line for line in events.stdout.splitlines() if " deadline-exceeded " in line
    )
    assert "work=agent-phase:agent:web:code" in deadline_line
    assert "group=agent:run-1:coding-1" in deadline_line
    assert "phase=command reason=active" in deadline_line
    assert "active_timeout=0.050s" in deadline_line
    assert "absolute_timeout=1.000s" in deadline_line


def test_descendant_is_gone_before_timed_out_phase_releases_lease(
    tmp_path: Path,
) -> None:
    """A cooperative leader cannot release capacity around a resistant child."""
    pool_dir = tmp_path / "pool"
    descendant_pid_file = (tmp_path / "descendant.pid").resolve()
    leader_script = CooperativeTermResistantProcessTreeProgram(
        descendant_pid_file,
        300,
        (),
    ).python_source()

    executor = _deterministic_host_executor(
        pool_dir,
        host_cpu_observer=_IdleHostCpuObserver(),
        request_nonce="b" * 32,
    )
    specification = ExecutorRunSpecification(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        concurrency_range=ExecutorConcurrencyRange(1, 1),
        exclusive_resources=(),
    )
    timed_out = executor.run(
        specification,
        ExecutorCommand(
            (sys.executable, "-c", leader_script),
            ExecutorBoundedDeadline(1.0, 2.0),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )

    assert timed_out.exit_code == 124
    descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
    ProcessTreeMember(descendant_pid).assert_contained()
    recovered = executor.run(
        specification,
        ExecutorCommand(
            (sys.executable, "-c", "print('LEASE-RECOVERED')"),
            ExecutorBoundedDeadline(2.0, 4.0),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )
    assert recovered.exit_code == 0


def test_natural_phase_completion_contains_descendant_before_lease_release(
    tmp_path: Path,
) -> None:
    """A successful leader cannot leave a detached same-group child behind."""
    pool_dir = tmp_path / "pool"
    descendant_pid_file = (tmp_path / "natural-descendant.pid").resolve()
    leader_script = ExitingTermResistantProcessTreeProgram(
        descendant_pid_file,
        300,
        0,
    ).python_source()
    executor = _deterministic_host_executor(
        pool_dir,
        host_cpu_observer=_IdleHostCpuObserver(),
        request_nonce="e" * 32,
    )

    result = executor.run(
        ExecutorRunSpecification(
            work_key=ExecutorWorkKey("agent-phase:test:natural-descendant"),
            fairness_group=ExecutorFairnessGroup("agent:test:natural-descendant"),
            concurrency_range=ExecutorConcurrencyRange(1, 1),
            exclusive_resources=(),
        ),
        ExecutorCommand(
            (sys.executable, "-c", leader_script),
            ExecutorBoundedDeadline(5.0, 5.0),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )

    assert result.exit_code == 0
    descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
    ProcessTreeMember(descendant_pid).assert_contained()


def test_internal_phase_client_rejects_non_finite_deadlines(tmp_path: Path) -> None:
    result = _phase_cli(
        tmp_path / "pool",
        active_timeout_seconds="nan",
        absolute_timeout_seconds="4",
        command=(sys.executable, "-c", "raise AssertionError('must not run')"),
    )

    assert result.returncode == 2
    assert "must be a positive number" in result.stderr


def test_admission_deadline_fails_before_command_and_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = tmp_path / "pool"
    marker = tmp_path / "must-not-run"
    monkeypatch.chdir(REPO_ROOT)
    executor = HostExecutor(
        pool_dir=pool_dir,
        host_cpu_slots=1,
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_demand_estimator(),
        host_cpu_observer=_SaturatedHostCpuObserver(),
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: "c" * 32,
        ),
        command_guardian=executor_command_guardian(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            )
        ),
        atomic_path_replacement=OsAtomicPathReplacement(),
        history_retention_lock=_history_lock(pool_dir),
        history_retention_policy=ExecutorHistoryRetentionPolicy(2048, 24),
        queue_settle_seconds=0.01,
        queue_poll_seconds=0.01,
    )

    blocker = ExecutorPoolWork(
        work_key="agent-phase:lease-blocker",
        fairness_group="agent:blocking-run",
        requested_concurrency=1,
        host_cpu_slots=1,
        exclusive_resources=(),
        command=ExecutorPoolHeldCommand("BLOCKER_STARTED", 0),
    )
    with ExecutorPoolRig(pool_dir, working_directory=REPO_ROOT) as rig:
        held_lease = rig.admit(blocker)
        with pytest.raises(ExecutorDeadlineExceededError) as raised:
            executor.run(
                ExecutorRunSpecification(
                    work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
                    fairness_group=ExecutorFairnessGroup("agent:run-2:coding-2"),
                    concurrency_range=ExecutorConcurrencyRange(1, 1),
                    exclusive_resources=(),
                ),
                ExecutorCommand(
                    (
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ),
                    ExecutorBoundedDeadline(
                        active_timeout_seconds=0.01,
                        absolute_timeout_seconds=0.05,
                    ),
                    ExecutorCommandLifecycle.DETACHED,
                    ExecutorNoCommandCancellation(),
                ),
            )
        rig.release(held_lease)

    assert raised.value.reason is ExecutorDeadlineReason.ABSOLUTE
    assert not marker.exists()
    timeline = HostExecutorMonitor(
        pool_dir,
        1,
        _demand_estimator(),
        ExecutorHistoryRetentionPolicy(2048, 24),
        _history_lock(pool_dir),
        OsAtomicPathReplacement(),
    ).recent_events(ExecutorRecentEventsQuery(20))
    [deadline_event] = [
        event
        for event in timeline.events
        if isinstance(event, ExecutorAdmissionDeadlineExceeded)
    ]
    assert deadline_event.reason is ExecutorDeadlineReason.ABSOLUTE
    assert deadline_event.work.work_key.value == "agent-phase:agent:web:code"
    assert deadline_event.work.fairness_group.value == "agent:run-2:coding-2"
    assert deadline_event.active_timeout_seconds == 0.01
    assert deadline_event.absolute_timeout_seconds == 0.05


def test_deadline_expiring_after_admission_records_terminal_events_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = tmp_path / "pool"
    marker = tmp_path / "must-not-spawn"
    clock = _AdmissionThenExpiredClock()
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(
        "issue_orchestrator.execution.host_executor.adapter.time.monotonic",
        clock.monotonic,
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.host_executor.adapter.time.sleep",
        lambda _seconds: None,
    )
    executor = HostExecutor(
        pool_dir=pool_dir,
        host_cpu_slots=1,
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_demand_estimator(),
        host_cpu_observer=_IdleHostCpuObserver(),
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: "d" * 32,
        ),
        command_guardian=executor_command_guardian(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=2.0,
                forceful_shutdown_seconds=2.0,
            )
        ),
        atomic_path_replacement=OsAtomicPathReplacement(),
        history_retention_lock=_history_lock(pool_dir),
        history_retention_policy=ExecutorHistoryRetentionPolicy(2048, 24),
        queue_settle_seconds=0.01,
        queue_poll_seconds=0.01,
    )

    result = executor.run(
        ExecutorRunSpecification(
            work_key=ExecutorWorkKey("agent-phase:test:post-admission-deadline"),
            fairness_group=ExecutorFairnessGroup("agent:test:post-admission-deadline"),
            concurrency_range=ExecutorConcurrencyRange(1, 1),
            exclusive_resources=(),
        ),
        ExecutorCommand(
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ),
            ExecutorBoundedDeadline(0.5, 0.5),
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )

    assert result.exit_code == 124
    assert not marker.exists()
    assert tuple((pool_dir / "leases").glob("*.json")) == ()
    timeline = HostExecutorMonitor(
        pool_dir,
        1,
        _demand_estimator(),
        ExecutorHistoryRetentionPolicy(2048, 24),
        _history_lock(pool_dir),
        OsAtomicPathReplacement(),
    ).recent_events(ExecutorRecentEventsQuery(20))
    assert any(isinstance(event, ExecutorWorkAdmitted) for event in timeline.events)
    [deadline_event] = [
        event
        for event in timeline.events
        if isinstance(event, ExecutorCommandDeadlineExceeded)
    ]
    assert deadline_event.reason is ExecutorDeadlineReason.ABSOLUTE
    [completed_event] = [
        event for event in timeline.events if isinstance(event, ExecutorWorkCompleted)
    ]
    assert completed_event.exit_code == 124
