# pyright: strict
"""Port for the lane dispatch journal.

Every completed lane reports its dispatch facts — the priority it ran
with, how long it queued, how long it executed, how it exited. The
journal is the behavior-level owner of persisting them: entrypoints
hand it a typed record and know nothing about storage transport or
where the journal lives. Like the runtime history, it is
backend-neutral — the direct backend's zero waits are records too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LaneWorkKey
from .machine_state import MachineState


@dataclass(frozen=True, slots=True)
class LaneDispatchRecord:
    """One completed lane's dispatch facts.

    Failed lanes are recorded too — a kill's dispatch facts are
    diagnosis, even though only successes feed the learning loop.

    ``machine_state`` is required, not optional: a runtime without the
    contention it ran under is the ambiguity this record exists to end
    (#7127). It is a measurement only — nothing may schedule, order or
    gate on it.
    """

    work_key: LaneWorkKey
    backend: str
    priority: int
    queue_wait_seconds: float
    observed_runtime_seconds: float
    exit_code: int
    machine_state: MachineState

    def __post_init__(self) -> None:
        if type(self.machine_state) is not MachineState:
            raise ValueError(
                "LaneDispatchRecord.machine_state must be a MachineState"
            )
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneDispatchRecord.work_key must be a LaneWorkKey")
        if type(self.backend) is not str or not self.backend:
            raise ValueError(
                "LaneDispatchRecord.backend must be a non-empty string"
            )
        if type(self.priority) is not int or self.priority < 0:
            raise ValueError(
                "LaneDispatchRecord.priority must be a non-negative integer"
            )
        for field_name, value in (
            ("queue_wait_seconds", self.queue_wait_seconds),
            ("observed_runtime_seconds", self.observed_runtime_seconds),
        ):
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    f"LaneDispatchRecord.{field_name} must be finite and "
                    "non-negative"
                )
        if type(self.exit_code) is not int:
            raise ValueError("LaneDispatchRecord.exit_code must be an integer")


class LaneDispatchJournalError(RuntimeError):
    """The journal could not persist a record — the one failure owner."""


@runtime_checkable
class LaneDispatchJournal(Protocol):
    """Persist one dispatch record per completed lane."""

    def record(self, record: LaneDispatchRecord) -> None:
        """Persist the record; raise LaneDispatchJournalError on failure."""
        ...
