"""Typed ownership and outcome contracts for process-group containment."""

from __future__ import annotations

from dataclasses import dataclass


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
