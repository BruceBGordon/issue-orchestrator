"""Regression proof that simulated agent phases never use machine state."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

from platformdirs import user_state_path

from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseRunSpecification,
)
from issue_orchestrator.domain.executor import (
    ExecutorFairnessGroup,
    ExecutorInteractiveSessionCancellation,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.terminal_launch import TerminalInteractionIntent
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorPolicyChanged,
    ExecutorRecentEventsQuery,
)
from issue_orchestrator.entrypoints.bootstrap import build_executor_monitor
from tests.agent_phase_scheduler_helpers import host_agent_phase_command_scheduler
from tests.simulated_scenarios.conftest import SimulatedExecutorPool
from tests.unit.session_run_helpers import make_session_run_assets


def _run_git(repository: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_spawned_agent_phase_uses_worker_local_executor_pool(
    tmp_path: Path,
    simulated_executor_pool: SimulatedExecutorPool,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.name", "Executor Isolation Test")
    _run_git(repository, "config", "user.email", "executor@example.invalid")
    (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "-q", "-m", "fixture")

    destination = make_session_run_assets(
        repository,
        session_name="executor-isolation",
    ).terminal_destination
    specification = AgentPhaseRunSpecification.from_timeout_minutes(
        work_key=ExecutorWorkKey("simulated:executor-isolation"),
        fairness_group=ExecutorFairnessGroup("simulated:isolation-regression"),
        active_timeout_minutes=1,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell_command=shlex.join((sys.executable, "-c", "pass")),
        cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
            destination.run_dir
        ),
        destination=destination,
    )
    scheduled = host_agent_phase_command_scheduler().schedule(specification)
    completed = subprocess.run(
        (
            scheduled.terminal_launch.shell.value,
            "-lc",
            scheduled.terminal_launch.shell_command,
        ),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert os.environ["ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"] == str(
        simulated_executor_pool.path
    )
    machine_pool = user_state_path("issue-orchestrator") / "executor-pools" / "host-v2"
    assert simulated_executor_pool.path != machine_pool
    timeline = build_executor_monitor().recent_events(ExecutorRecentEventsQuery(20))
    assert any(
        not isinstance(event, ExecutorPolicyChanged)
        and event.work.work_key == specification.work_key
        for event in timeline.events
    )
