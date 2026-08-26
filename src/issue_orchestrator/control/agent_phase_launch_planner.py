"""Control owner for turning one agent phase into a bounded terminal launch."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ..domain.agent_phase_execution import (
    AgentPhaseLaunchRequest,
    AgentPhaseRunSpecification,
)
from ..domain.executor import (
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from ..domain.models import AgentConfig
from ..domain.session_watchdog import ScheduledSessionWatchdog
from ..domain.terminal_launch import TerminalInteractionIntent, TerminalLaunch
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler
from ..ports.scheduled_session_watchdog_store import ScheduledSessionWatchdogStore


class AgentProviderCommandWrapper(Protocol):
    """Provider retry wrapper needed by the phase launch owner."""

    def wrap(
        self,
        base_command: str,
        agent_config: AgentConfig,
        run_dir: Path,
        *,
        extra_provider_args: Mapping[str, str],
    ) -> str: ...


class AgentPhaseLaunchPlanner:
    """Own phase identity, bounded scheduling, and outer timeout projection."""

    def __init__(
        self,
        scheduler: AgentPhaseCommandScheduler,
        provider_command_wrapper: AgentProviderCommandWrapper,
        watchdog_store: ScheduledSessionWatchdogStore,
    ) -> None:
        self._scheduler = scheduler
        self._provider_command_wrapper = provider_command_wrapper
        self._watchdog_store = watchdog_store

    def schedule(
        self,
        request: AgentPhaseLaunchRequest,
    ) -> tuple[TerminalLaunch, AgentConfig]:
        """Classify the unwrapped provider command, then wrap and schedule it."""
        interaction_intent = TerminalInteractionIntent.classify(
            request.provider_command
        )
        wrapped_provider_command = self._provider_command_wrapper.wrap(
            request.provider_command,
            request.agent_config,
            request.run.run_dir,
            extra_provider_args=request.provider_arguments.as_mapping(),
        )
        specification = AgentPhaseRunSpecification.from_timeout_minutes(
            work_key=ExecutorWorkKey(
                f"agent-phase:{request.agent_label}:{request.task_kind.value}"
            ),
            fairness_group=ExecutorFairnessGroup(
                f"agent:{request.run.run_id}:{request.run.session_name}"
            ),
            active_timeout_minutes=request.agent_config.timeout_minutes,
            interaction_intent=interaction_intent,
            shell_command=(
                f"{request.environment_exports} && {wrapped_provider_command}"
            ),
            destination=request.run.terminal_destination,
        )
        scheduled = self._scheduler.schedule(specification)
        scheduled_watchdog = ScheduledSessionWatchdog(
            scheduled.absolute_timeout_minutes
        )
        self._watchdog_store.record_scheduled_watchdog(
            request.run,
            scheduled_watchdog,
        )
        return (
            scheduled.terminal_launch,
            replace(
                request.agent_config,
                timeout_minutes=scheduled_watchdog.timeout_minutes,
            ),
        )
