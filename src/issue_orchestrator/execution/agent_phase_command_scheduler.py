"""Terminal command adapter for typed agent-phase executor submissions."""

from __future__ import annotations

import shlex
from pathlib import Path

from ..domain.agent_phase_execution import (
    AgentPhaseRunSpecification,
    ScheduledAgentPhase,
)
from ..ports.agent_phase_command_scheduler import AgentPhaseCommandScheduler


class HostAgentPhaseCommandScheduler(AgentPhaseCommandScheduler):
    """Render the internal phase runner without leaking its CLI into control."""

    def __init__(self, *, python_executable: Path, shell_executable: Path) -> None:
        for name, path in (
            ("python_executable", python_executable),
            ("shell_executable", shell_executable),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(
                    f"HostAgentPhaseCommandScheduler.{name} must be an absolute Path"
                )
        self._python_executable = python_executable
        self._shell_executable = shell_executable

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
            terminal_command=shlex.join(
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
                "--",
                str(self._shell_executable),
                "-lc",
                specification.shell_command,
                )
            ),
            absolute_timeout_minutes=specification.absolute_timeout_minutes,
        )
