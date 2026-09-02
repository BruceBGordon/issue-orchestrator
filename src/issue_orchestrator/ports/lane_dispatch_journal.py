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
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..domain.lane_cpu_request import LaneCpuRequest
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

    ``cpu_request`` carries the whole sizing decision (declared,
    learned, submitted) rather than just the winning number, and
    ``observed_busy_cores`` carries what the run then actually used.
    Together they make the measured-vs-declared divergence readable
    from the journal alone: a lane whose evidence is being capped, or
    whose declaration is far above what it ever uses, is visible
    without re-deriving either side. The two envelopes answer
    different questions about the same run and neither substitutes for
    the other: machine_state is the contention the lane MET, cpu_request
    is the capacity it ASKED FOR.
    """

    work_key: LaneWorkKey
    backend: str
    priority: int
    queue_wait_seconds: float
    observed_runtime_seconds: float
    exit_code: int
    machine_state: MachineState
    cpu_request: LaneCpuRequest
    observed_busy_cores: float | None

    def __post_init__(self) -> None:
        if type(self.machine_state) is not MachineState:
            raise ValueError(
                "LaneDispatchRecord.machine_state must be a MachineState"
            )
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneDispatchRecord.work_key must be a LaneWorkKey")
        if type(self.cpu_request) is not LaneCpuRequest:
            raise ValueError(
                "LaneDispatchRecord.cpu_request must be a LaneCpuRequest"
            )
        if self.observed_busy_cores is not None and (
            type(self.observed_busy_cores) is not float
            or not math.isfinite(self.observed_busy_cores)
            or self.observed_busy_cores < 0
        ):
            raise ValueError(
                "LaneDispatchRecord.observed_busy_cores must be None or a "
                "finite, non-negative float"
            )
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


@dataclass(frozen=True, slots=True)
class LaneDispatchEntry:
    """One persisted record, plus the facts persistence itself added.

    ``recorded_at`` and ``worktree`` are not part of what a lane
    *reports* — they are what the journal *observes* about the report —
    so they belong to the read side rather than to
    :class:`LaneDispatchRecord`.
    """

    recorded_at: datetime
    worktree: str
    record: LaneDispatchRecord

    def __post_init__(self) -> None:
        if (
            type(self.recorded_at) is not datetime
            or self.recorded_at.tzinfo is None
        ):
            raise ValueError(
                "LaneDispatchEntry.recorded_at must be a timezone-aware datetime"
            )
        if type(self.worktree) is not str or not self.worktree:
            raise ValueError(
                "LaneDispatchEntry.worktree must be a non-empty string"
            )
        if type(self.record) is not LaneDispatchRecord:
            raise ValueError(
                "LaneDispatchEntry.record must be a LaneDispatchRecord"
            )

    def age_seconds(self, now: datetime) -> float:
        """Seconds between this record landing and ``now`` (never negative)."""
        if type(now) is not datetime or now.tzinfo is None:
            raise ValueError("age_seconds requires a timezone-aware datetime")
        return max(
            0.0,
            (now.astimezone(timezone.utc) - self.recorded_at).total_seconds(),
        )


@dataclass(frozen=True, slots=True)
class LaneDispatchHistory:
    """What the journal holds, and where the reader looked for it.

    ``location`` is a human-readable description owned by the adapter:
    the operator's first question about an empty history is "which file
    did you read?", and answering it must not require the caller to know
    the storage layout.
    """

    location: str
    entries: tuple[LaneDispatchEntry, ...]
    #: Rows inside the scanned window that predate some dimension this
    #: record now requires, and so cannot be represented. Counted rather
    #: than dropped in silence: the journal is shared by every worktree
    #: on the machine, so a worktree on older code is still appending
    #: such rows, and a reader that hid them would quietly under-report
    #: how much history it actually looked at.
    #:
    #: Deliberately ONE count across every schema epoch, not one per
    #: epoch (#7135's machine-state envelope, #7136's cpu request, and
    #: whatever comes next). The operator's question is "how much of
    #: this window was too old to read", and the answer to it does not
    #: get better by being split by cause — while a counter per epoch
    #: would repeat this whole mechanism through the port, the adapter,
    #: the snapshot and the CLI every time a dimension is added.
    predating_schema: int = 0

    def __post_init__(self) -> None:
        if type(self.location) is not str or not self.location:
            raise ValueError(
                "LaneDispatchHistory.location must be a non-empty string"
            )
        if type(self.predating_schema) is not int or self.predating_schema < 0:
            raise ValueError(
                "LaneDispatchHistory.predating_schema must be non-negative"
            )
        if type(self.entries) is not tuple or any(
            type(entry) is not LaneDispatchEntry for entry in self.entries
        ):
            raise ValueError(
                "LaneDispatchHistory.entries must be a tuple of LaneDispatchEntry"
            )


class LaneDispatchJournalError(RuntimeError):
    """The journal could not persist or read back a record.

    The one failure owner for both directions. Absence is never an
    error — an unwritten journal is simply an empty history — but a
    record that cannot be parsed is, because something wrote garbage.
    """


@runtime_checkable
class LaneDispatchJournal(Protocol):
    """Persist one dispatch record per completed lane."""

    def record(self, record: LaneDispatchRecord) -> None:
        """Persist the record; raise LaneDispatchJournalError on failure."""
        ...


@runtime_checkable
class LaneDispatchJournalReader(Protocol):
    """Read back the most recent dispatch records.

    Separate from :class:`LaneDispatchJournal` so a read-only consumer —
    an operator snapshot, a future UI panel — depends on reading alone
    and cannot accidentally acquire the ability to write.
    """

    def read_recent(self, limit: int) -> LaneDispatchHistory:
        """Return at most ``limit`` most-recent entries, oldest first.

        Raises LaneDispatchJournalError when the stored records cannot
        be parsed. An absent journal yields an empty history, not an
        error.
        """
        ...
