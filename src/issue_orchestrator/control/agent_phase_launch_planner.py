"""Control owner for turning one agent phase into a bounded terminal launch."""

from __future__ import annotations

from dataclasses import replace

from ..domain.agent_phase_execution import AgentPhaseRunSpecification
from ..domain.executor import ExecutorFairnessGroup, ExecutorWorkKey
from ..domain.models import AgentConfig, TaskKind
from ..domain.session_run import SessionRunAssets
from ..domain.terminal_launch import TerminalInteractionIntent, TerminalLaunch
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler


class AgentPhaseLaunchPlanner:
    """Own phase identity, bounded scheduling, and outer timeout projection."""

    def __init__(self, scheduler: AgentPhaseCommandScheduler) -> None:
        self._scheduler = scheduler

    def schedule(
        self,
        *,
        shell_command: str,
        interaction_intent: TerminalInteractionIntent,
        agent_config: AgentConfig,
        run: SessionRunAssets,
        agent_label: str,
        task_kind: TaskKind,
    ) -> tuple[TerminalLaunch, AgentConfig]:
        specification = AgentPhaseRunSpecification.from_timeout_minutes(
            work_key=ExecutorWorkKey(
                f"agent-phase:{agent_label}:{task_kind.value}"
            ),
            fairness_group=ExecutorFairnessGroup(
                f"agent:{run.run_id}:{run.session_name}"
            ),
            active_timeout_minutes=agent_config.timeout_minutes,
            interaction_intent=interaction_intent,
            shell_command=shell_command,
        )
        scheduled = self._scheduler.schedule(specification)
        return (
            scheduled.terminal_launch,
            replace(
                agent_config,
                timeout_minutes=scheduled.absolute_timeout_minutes,
            ),
        )
