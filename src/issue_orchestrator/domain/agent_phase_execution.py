"""Typed issue-orchestrator contract for one cooperatively bounded agent phase."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .executor import (
    ExecutorBoundedDeadline,
    ExecutorFairnessGroup,
    ExecutorProcessTerminationPolicy,
    ExecutorWorkKey,
)
from .terminal_launch import TerminalInteractionIntent, TerminalLaunch


@dataclass(frozen=True, slots=True)
class AgentPhaseOuterWatchdogPolicy:
    """Keep the terminal observer outside executor cleanup and scheduling."""

    executor_termination: ExecutorProcessTerminationPolicy
    observer_margin_seconds: float

    def __post_init__(self) -> None:
        if type(self.executor_termination) is not ExecutorProcessTerminationPolicy:
            raise ValueError(
                "AgentPhaseOuterWatchdogPolicy.executor_termination must be "
                "ExecutorProcessTerminationPolicy"
            )
        if (
            type(self.observer_margin_seconds) is not float
            or not math.isfinite(self.observer_margin_seconds)
            or self.observer_margin_seconds <= 0
        ):
            raise ValueError(
                "AgentPhaseOuterWatchdogPolicy.observer_margin_seconds must be "
                "finite and positive"
            )

    def timeout_minutes(self, deadline: ExecutorBoundedDeadline) -> int:
        """Round up an outer bound strictly beyond executor TERM/KILL/reap."""
        if type(deadline) is not ExecutorBoundedDeadline:
            raise ValueError(
                "AgentPhaseOuterWatchdogPolicy.timeout_minutes requires "
                "ExecutorBoundedDeadline"
            )
        cleanup_seconds = (
            self.executor_termination.graceful_shutdown_seconds
            + self.executor_termination.forceful_shutdown_seconds
            + self.observer_margin_seconds
        )
        return (
            math.floor((deadline.absolute_timeout_seconds + cleanup_seconds) / 60.0) + 1
        )


@dataclass(frozen=True, slots=True)
class AgentPhaseRunSpecification:
    """A complete safe-boundary phase submitted to the host executor."""

    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup
    deadline: ExecutorBoundedDeadline
    interaction_intent: TerminalInteractionIntent
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
        if type(self.interaction_intent) is not TerminalInteractionIntent:
            raise ValueError(
                "AgentPhaseRunSpecification.interaction_intent must be "
                "TerminalInteractionIntent"
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
        interaction_intent: TerminalInteractionIntent,
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
            interaction_intent=interaction_intent,
            shell_command=shell_command,
        )


@dataclass(frozen=True, slots=True)
class ScheduledAgentPhase:
    """Terminal command and outer safety timeout produced as one decision."""

    terminal_launch: TerminalLaunch
    absolute_timeout_minutes: int

    def __post_init__(self) -> None:
        if type(self.terminal_launch) is not TerminalLaunch:
            raise ValueError(
                "ScheduledAgentPhase.terminal_launch must be TerminalLaunch"
            )
        if (
            type(self.absolute_timeout_minutes) is not int
            or self.absolute_timeout_minutes < 1
        ):
            raise ValueError(
                "ScheduledAgentPhase.absolute_timeout_minutes must be positive"
            )
