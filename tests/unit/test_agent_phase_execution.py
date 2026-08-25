"""Public-boundary tests for cooperatively scheduled agent phases."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
)
from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseRunSpecification,
)
from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorCommand,
    ExecutorConcurrencyRange,
    ExecutorDeadlineExceededError,
    ExecutorDeadlineReason,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
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
from issue_orchestrator.execution.host_executor import (
    ExecutorRequestIdentityFactory,
    HostExecutor,
    HostExecutorMonitor,
)
from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseOuterWatchdogPolicy,
)
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"


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


def _demand_estimator() -> ExecutorWorkDemandEstimator:
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


def _phase_cli(
    pool_dir: Path,
    *,
    active_timeout_seconds: str,
    absolute_timeout_seconds: str,
    command: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
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
            "--",
            *command,
        ),
        cwd=REPO_ROOT,
        env={**os.environ, POOL_DIR_ENV: str(pool_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def _executor_events(pool_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli",
            "executor-events",
            "--limit",
            "20",
        ),
        cwd=REPO_ROOT,
        env={**os.environ, POOL_DIR_ENV: str(pool_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_phase_specification_converts_active_timeout_to_fixed_absolute_bound() -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command="run-agent --issue 42",
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


def test_scheduler_renders_one_shell_safe_internal_invocation() -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command="printf '%s\\n' 'human readable'",
    )
    scheduler = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(2.0),
            observer_margin_seconds=58.0,
        ),
    )

    scheduled = scheduler.schedule(specification)
    arguments = shlex.split(scheduled.terminal_launch.shell_command)

    assert scheduled.absolute_timeout_minutes == 92
    assert scheduled.absolute_timeout_minutes * 60 > (
        specification.deadline.absolute_timeout_seconds + 2.0 + 58.0
    )
    assert arguments[:3] == [
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.cli_tools.agent_phase_run",
    ]
    assert arguments[arguments.index("--") + 1 :] == [
        "/bin/bash",
        "-lc",
        specification.shell_command,
    ]
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
    command: str,
    expected_intent: TerminalInteractionIntent,
) -> None:
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("agent-phase:agent:web:code"),
        fairness_group=ExecutorFairnessGroup("agent:run-1:coding-1"),
        active_timeout_minutes=45,
        interaction_intent=expected_intent,
        shell_command=command,
    )

    scheduled = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(2.0),
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
    )
    scheduled = HostAgentPhaseCommandScheduler(
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(2.0),
            observer_margin_seconds=58.0,
        ),
    ).schedule(specification)

    result = subprocess.run(
        (
            scheduled.terminal_launch.shell.value,
            "-lc",
            scheduled.terminal_launch.shell_command,
        ),
        cwd=REPO_ROOT,
        env={**os.environ, POOL_DIR_ENV: str(tmp_path / "pool")},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_internal_phase_client_runs_a_plain_command_without_orchestrator(
    tmp_path: Path,
) -> None:
    result = _phase_cli(
        tmp_path / "pool",
        active_timeout_seconds="2",
        absolute_timeout_seconds="4",
        command=(sys.executable, "-c", "print('PHASE-RAN')"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "PHASE-RAN\n"


def test_internal_phase_client_terminates_at_active_deadline_and_releases_lease(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    timed_out = _phase_cli(
        pool_dir,
        active_timeout_seconds="0.05",
        absolute_timeout_seconds="1",
        command=(
            sys.executable,
            "-c",
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "signal.pause()",
        ),
    )
    recovered = _phase_cli(
        pool_dir,
        active_timeout_seconds="2",
        absolute_timeout_seconds="4",
        command=(sys.executable, "-c", "print('RECOVERED')"),
    )
    events = _executor_events(pool_dir)

    assert timed_out.returncode == 124
    assert "phase=command reason=active" in timed_out.stderr
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == "RECOVERED\n"
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
        process_termination_policy=ExecutorProcessTerminationPolicy(2.0),
        history_retention_policy=ExecutorHistoryRetentionPolicy(2048, 24),
        queue_settle_seconds=0.01,
        queue_poll_seconds=0.01,
    )

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
            ),
        )

    assert raised.value.reason is ExecutorDeadlineReason.ABSOLUTE
    assert not marker.exists()
    timeline = HostExecutorMonitor(
        pool_dir,
        1,
        _demand_estimator(),
        ExecutorHistoryRetentionPolicy(2048, 24),
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
        process_termination_policy=ExecutorProcessTerminationPolicy(2.0),
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
