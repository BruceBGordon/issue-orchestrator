"""Typed issue-orchestrator contract for one cooperatively bounded agent phase."""

from __future__ import annotations

from dataclasses import dataclass

from .executor import (
    ExecutorBoundedDeadline,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)


@dataclass(frozen=True, slots=True)
class AgentPhaseRunSpecification:
    """A complete safe-boundary phase submitted to the host executor."""

    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup
    deadline: ExecutorBoundedDeadline
    shell_command: str

    def __post_init__(self) -> None:
        if type(self.work_key) is not ExecutorWorkKey:
            raise ValueError(
                "AgentPhaseRunSpecification.work_key must be ExecutorWorkKey"
            )
        if type(self.fairness_group) is not ExecutorFairnessGroup:
            raise ValueError(
                "AgentPhaseRunSpecification.fairness_group must be "
                "ExecutorFairnessGroup"
            )
        if type(self.deadline) is not ExecutorBoundedDeadline:
            raise ValueError(
                "AgentPhaseRunSpecification.deadline must be ExecutorBoundedDeadline"
            )
        if type(self.shell_command) is not str or not self.shell_command:
            raise ValueError(
                "AgentPhaseRunSpecification.shell_command must not be empty"
            )

    @classmethod
    def from_timeout_minutes(
        cls,
        *,
        work_key: ExecutorWorkKey,
        fairness_group: ExecutorFairnessGroup,
        active_timeout_minutes: int,
        shell_command: str,
    ) -> AgentPhaseRunSpecification:
        if type(active_timeout_minutes) is not int or active_timeout_minutes < 1:
            raise ValueError("active_timeout_minutes must be a positive integer")
        active_seconds = float(active_timeout_minutes * 60)
        return cls(
            work_key=work_key,
            fairness_group=fairness_group,
            deadline=ExecutorBoundedDeadline(
                active_timeout_seconds=active_seconds,
                absolute_timeout_seconds=active_seconds * 2.0,
            ),
            shell_command=shell_command,
        )

    @property
    def absolute_timeout_minutes(self) -> int:
        return int(self.deadline.absolute_timeout_seconds / 60)


@dataclass(frozen=True, slots=True)
class ScheduledAgentPhase:
    """Terminal command and outer safety timeout produced as one decision."""

    terminal_command: str
    absolute_timeout_minutes: int

    def __post_init__(self) -> None:
        if type(self.terminal_command) is not str or not self.terminal_command:
            raise ValueError("ScheduledAgentPhase.terminal_command must not be empty")
        if (
            type(self.absolute_timeout_minutes) is not int
            or self.absolute_timeout_minutes < 1
        ):
            raise ValueError(
                "ScheduledAgentPhase.absolute_timeout_minutes must be positive"
            )
