"""Terminal command adapter for typed agent-phase executor submissions."""

from __future__ import annotations

import shlex
from pathlib import Path

from ..domain.agent_phase_execution import (
    AgentPhaseOuterWatchdogPolicy,
    AgentPhaseRunSpecification,
    ScheduledAgentPhase,
)
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler
from ..domain.terminal_launch import TerminalLaunch, TerminalShell


class HostAgentPhaseCommandScheduler(AgentPhaseCommandScheduler):
    """Render the internal phase runner without leaking its CLI into control."""

    def __init__(
        self,
        *,
        python_executable: Path,
        application_shell: TerminalShell,
        outer_watchdog_policy: AgentPhaseOuterWatchdogPolicy,
    ) -> None:
        if (
            not isinstance(python_executable, Path)
            or not python_executable.is_absolute()
        ):
            raise ValueError(
                "HostAgentPhaseCommandScheduler.python_executable must be an "
                "absolute Path"
            )
        self._python_executable = python_executable
        if type(application_shell) is not TerminalShell:
            raise ValueError(
                "HostAgentPhaseCommandScheduler.application_shell must be TerminalShell"
            )
        self._application_shell = application_shell
        if type(outer_watchdog_policy) is not AgentPhaseOuterWatchdogPolicy:
            raise ValueError(
                "HostAgentPhaseCommandScheduler.outer_watchdog_policy must be "
                "AgentPhaseOuterWatchdogPolicy"
            )
        self._outer_watchdog_policy = outer_watchdog_policy

    def schedule(
        self,
        specification: AgentPhaseRunSpecification,
    ) -> ScheduledAgentPhase:
        if type(specification) is not AgentPhaseRunSpecification:
            raise ValueError(
                "HostAgentPhaseCommandScheduler.schedule requires "
                "AgentPhaseRunSpecification"
            )
        return ScheduledAgentPhase(
            terminal_launch=TerminalLaunch(
                shell_command=shlex.join(
                    (
                        str(self._python_executable),
                        "-m",
                        "issue_orchestrator.entrypoints.cli_tools.agent_phase_run",
                        "--work-key",
                        specification.work_key.value,
                        "--group",
                        specification.fairness_group.value,
                        "--active-timeout-seconds",
                        str(specification.deadline.active_timeout_seconds),
                        "--absolute-timeout-seconds",
                        str(specification.deadline.absolute_timeout_seconds),
                        "--cancellation-record",
                        str(specification.cancellation.record_path),
                        "--",
                        self._application_shell.value,
                        "-lc",
                        specification.shell_command,
                    )
                ),
                shell=self._application_shell,
                interaction_intent=specification.interaction_intent,
            ),
            absolute_timeout_minutes=self._outer_watchdog_policy.timeout_minutes(
                specification.deadline
            ),
        )
