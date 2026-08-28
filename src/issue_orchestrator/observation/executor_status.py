# pyright: strict
"""Assemble the operator-facing executor snapshot from its fact sources.

Three independent sources answer one question — "why is validation work
running or waiting?" — and each can be absent or broken on its own:

- the machine-wide pool, which knows what is running and queued *now*;
- the dispatch journal, which knows what every recent lane cost;
- the runtime history, which knows the order the next dispatch will use.

This module is the single owner of joining them and of what happens when
one of them fails. Degradation is never silent: a source that has
nothing to say is reported as such, and a source that is *broken* is
reported as a fault, so a caller cannot render a confident-looking
snapshot over a source that blew up.

Read-only fact gathering only — no decisions, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from ..domain.lane_execution import LaneWorkKey
from ..ports.executor_pool import (
    ExecutorPoolInspector,
    PoolInspectionError,
    PoolOffline,
    PoolState,
)
from ..ports.lane_dispatch_journal import (
    LaneDispatchEntry,
    LaneDispatchJournalError,
    LaneDispatchJournalReader,
)
from ..ports.lane_runtime_history import (
    LaneRuntimeHistory,
    LaneRuntimeHistoryError,
)

DEFAULT_RECENT_DISPATCH_LIMIT = 400


class FactSource(StrEnum):
    """Which of the snapshot's inputs a fault came from."""

    POOL = "pool"
    DISPATCH_JOURNAL = "dispatch journal"
    RUNTIME_HISTORY = "runtime history"


@dataclass(frozen=True, slots=True)
class SnapshotFault:
    """One input that is broken rather than merely empty.

    Absence is not a fault: an unused journal and an uninstalled pool
    are ordinary. A fault means a source raised — corrupt records,
    untranslatable answers — and the snapshot is therefore incomplete in
    a way the operator must be told about.
    """

    source: FactSource
    detail: str

    def __post_init__(self) -> None:
        if type(self.source) is not FactSource:
            raise ValueError("SnapshotFault.source must be a FactSource")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("SnapshotFault.detail must be a non-empty string")


@dataclass(frozen=True, slots=True)
class LaneDispatchSummary:
    """What the journal and the learning loop know about one lane."""

    work_key: LaneWorkKey
    runs: int
    last_recorded_at: datetime
    last_backend: str
    last_runtime_seconds: float
    last_queue_wait_seconds: float
    last_exit_code: int
    #: The rank the next dispatch of this lane will carry. Not a
    #: promised duration — an ordering hint (longer lanes first).
    learned_priority: int

    def __post_init__(self) -> None:
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneDispatchSummary.work_key must be a LaneWorkKey")
        if type(self.runs) is not int or self.runs < 1:
            raise ValueError("LaneDispatchSummary.runs must be a positive integer")


@dataclass(frozen=True, slots=True)
class ExecutorStatusSnapshot:
    """Everything one ``executor-status`` invocation observed."""

    captured_at: datetime
    backend: str
    pool: PoolState
    journal_location: str
    #: Lanes seen in the scanned window, longest learned priority first
    #: — the order the next gate will dispatch them in.
    lanes: tuple[LaneDispatchSummary, ...]
    #: How many journal records were scanned to build ``lanes``.
    records_scanned: int
    faults: tuple[SnapshotFault, ...]

    def __post_init__(self) -> None:
        if type(self.captured_at) is not datetime or self.captured_at.tzinfo is None:
            raise ValueError(
                "ExecutorStatusSnapshot.captured_at must be timezone-aware"
            )
        if type(self.backend) is not str or not self.backend:
            raise ValueError(
                "ExecutorStatusSnapshot.backend must be a non-empty string"
            )

    @property
    def is_degraded(self) -> bool:
        """Whether any input is broken (as opposed to merely empty)."""
        return bool(self.faults)


def build_executor_status_snapshot(
    *,
    inspector: ExecutorPoolInspector,
    journal_reader: LaneDispatchJournalReader,
    runtime_history: LaneRuntimeHistory,
    backend: str,
    captured_at: datetime,
    recent_limit: int = DEFAULT_RECENT_DISPATCH_LIMIT,
) -> ExecutorStatusSnapshot:
    """Gather the pool and the dispatch record into one snapshot.

    Never raises for a missing or broken source: each is reduced to an
    offline reason or a :class:`SnapshotFault` so the surviving sources
    still reach the operator. That is the whole point — a machine with
    no pool must still be able to see what its lanes have been costing.
    """
    if type(captured_at) is not datetime or captured_at.tzinfo is None:
        raise ValueError("build_executor_status_snapshot needs an aware captured_at")
    if type(recent_limit) is not int or recent_limit < 1:
        raise ValueError("build_executor_status_snapshot recent_limit must be >= 1")
    faults: list[SnapshotFault] = []
    pool = _inspect_pool(inspector, faults)
    location, entries = _read_journal(journal_reader, recent_limit, faults)
    lanes = _summarize_lanes(entries, runtime_history, faults)
    return ExecutorStatusSnapshot(
        captured_at=captured_at.astimezone(timezone.utc),
        backend=backend,
        pool=pool,
        journal_location=location,
        lanes=lanes,
        records_scanned=len(entries),
        faults=tuple(faults),
    )


def _inspect_pool(
    inspector: ExecutorPoolInspector, faults: list[SnapshotFault]
) -> PoolState:
    try:
        return inspector.inspect()
    except PoolInspectionError as error:
        faults.append(SnapshotFault(source=FactSource.POOL, detail=str(error)))
        return PoolOffline(f"the pool answered, but unintelligibly: {error}")


def _read_journal(
    journal_reader: LaneDispatchJournalReader,
    recent_limit: int,
    faults: list[SnapshotFault],
) -> tuple[str, tuple[LaneDispatchEntry, ...]]:
    try:
        history = journal_reader.read_recent(recent_limit)
    except LaneDispatchJournalError as error:
        faults.append(
            SnapshotFault(source=FactSource.DISPATCH_JOURNAL, detail=str(error))
        )
        return (f"unreadable: {error}", ())
    return (history.location, history.entries)


def _summarize_lanes(
    entries: tuple[LaneDispatchEntry, ...],
    runtime_history: LaneRuntimeHistory,
    faults: list[SnapshotFault],
) -> tuple[LaneDispatchSummary, ...]:
    """Reduce the scanned window to one row per lane.

    Entries arrive oldest first, so the last one seen for a work key is
    that lane's most recent dispatch.
    """
    latest: dict[str, LaneDispatchEntry] = {}
    runs: dict[str, int] = {}
    for entry in entries:
        key = entry.record.work_key.value
        latest[key] = entry
        runs[key] = runs.get(key, 0) + 1
    try:
        summaries = [
            _summarize_lane(entry, runs[key], runtime_history)
            for key, entry in latest.items()
        ]
    except LaneRuntimeHistoryError as error:
        # A broken learning store cannot be papered over with a neutral
        # priority: that would print an ordering the next gate will not
        # actually use. Drop the rows and say why, loudly.
        faults.append(
            SnapshotFault(source=FactSource.RUNTIME_HISTORY, detail=str(error))
        )
        return ()
    # Highest learned priority first: the order the next gate dispatches
    # in, with a stable tiebreak so repeated snapshots do not shuffle.
    summaries.sort(
        key=lambda summary: (-summary.learned_priority, summary.work_key.value)
    )
    return tuple(summaries)


def _summarize_lane(
    entry: LaneDispatchEntry, runs: int, runtime_history: LaneRuntimeHistory
) -> LaneDispatchSummary:
    record = entry.record
    return LaneDispatchSummary(
        work_key=record.work_key,
        runs=runs,
        last_recorded_at=entry.recorded_at,
        last_backend=record.backend,
        last_runtime_seconds=record.observed_runtime_seconds,
        last_queue_wait_seconds=record.queue_wait_seconds,
        last_exit_code=record.exit_code,
        learned_priority=runtime_history.learned_priority(record.work_key),
    )
