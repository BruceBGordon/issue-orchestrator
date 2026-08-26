"""Port for rendering one agent phase through the host executor."""

from __future__ import annotations

from typing import Protocol

from ..domain.agent_phase_execution import (
    AgentPhaseRunSpecification,
    ScheduledAgentPhase,
)


class AgentPhaseCommandScheduler(Protocol):
    """Translate a typed phase into the terminal's one command string."""

    def schedule(
        self,
        specification: AgentPhaseRunSpecification,
    ) -> ScheduledAgentPhase:
        """Return an exact shell-safe command that executes the phase."""
        ...
