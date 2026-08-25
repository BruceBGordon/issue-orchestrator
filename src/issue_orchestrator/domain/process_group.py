"""Typed ownership and outcome contracts for process-group containment."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class OwnedProcessGroupLeader:
    """A live or unreaped leader whose pid still reserves its process group."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "OwnedProcessGroupLeader.process_id must be an integer above 1"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupTermination:
    """The reaped leader result after its whole process group was contained."""

    leader_exit_code: int

    def __post_init__(self) -> None:
        if type(self.leader_exit_code) is not int:
            raise ValueError(
                "ProcessGroupTermination.leader_exit_code must be an integer"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupUnboundedWait:
    """Wait for natural leader completion without a wall-clock deadline."""


@dataclass(frozen=True, slots=True)
class ProcessGroupBoundedWait:
    """Wait at most the remaining command budget before containment."""

    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "ProcessGroupBoundedWait.timeout_seconds must be finite and positive"
            )


ProcessGroupWait = ProcessGroupUnboundedWait | ProcessGroupBoundedWait


@dataclass(frozen=True, slots=True)
class ProcessGroupCompleted:
    """Natural leader exit followed by whole-group containment and reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupCompleted.termination must be ProcessGroupTermination"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupTimedOut:
    """Deadline-driven whole-group containment and leader reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupTimedOut.termination must be ProcessGroupTermination"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupInterrupted:
    """Caller-requested whole-group containment and leader reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupInterrupted.termination must be "
                "ProcessGroupTermination"
            )


ProcessGroupSupervision = (
    ProcessGroupCompleted | ProcessGroupTimedOut | ProcessGroupInterrupted
)
