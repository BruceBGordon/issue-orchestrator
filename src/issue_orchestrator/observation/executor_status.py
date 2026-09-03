# pyright: strict
"""Assemble the operator-facing executor snapshot from its fact sources.

Three independent sources answer one question — "why is validation work
running or waiting?" — and each can be absent or broken on its own:

- the machine-wide pool, which knows what is running and queued *now*;
- the lane declarations, which know every lane that EXISTS and how each
  one is routed — the only source that can mention a lane which has
  never run;
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
from typing import Callable, Mapping

from ..domain.lane_execution import LaneWorkKey
from ..execution.lane_backends import (
    BackendSelection,
    SelectedBackend,
    UnknownBackend,
)
from ..infra.lane_declarations import (
    LaneDeclaration,
    LaneDeclarationError,
    LaneDeclarations,
)
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
from ..ports.machine_state import MachineState
from ..ports.lane_runtime_history import (
    LaneRuntimeHistory,
    LaneRuntimeHistoryError,
)

DEFAULT_RECENT_DISPATCH_LIMIT = 400


class FactSource(StrEnum):
    """Which of the snapshot's inputs a fault came from."""

    POOL = "pool"
    LANE_DECLARATIONS = "lane declarations"
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
    #: The host contention the last run was measured under (#7127/#7135).
    #: Carried rather than dropped because this row's headline number is
    #: a RUNTIME: a contention-inflated sample beside a bare duration is
    #: exactly the ambiguity the envelope exists to end, and discarding
    #: it here would rebuild that ambiguity in the operator's view. It
    #: is displayed only — nothing here schedules, orders or gates on it.
    last_machine_state: MachineState
    #: The rank the next dispatch of this lane will carry. Not a
    #: promised duration — an ordering hint (longer lanes first).
    learned_priority: int

    def __post_init__(self) -> None:
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneDispatchSummary.work_key must be a LaneWorkKey")
        if type(self.runs) is not int or self.runs < 1:
            raise ValueError("LaneDispatchSummary.runs must be a positive integer")


@dataclass(frozen=True, slots=True)
class LaneRouting:
    """How the declarations say one lane is scheduled.

    The facts that decide whether a lane fits the machine and what it
    must not run beside — read from the same declarations `lane-run`
    resolves, never re-derived.
    """

    request_cpus: int
    memory_mb: int
    #: The declared three-valued freeze classification (#7134), carried
    #: verbatim: "cooperative" is a materially different promise from
    #: "anywhere", and collapsing them to a boolean here would hide
    #: which lanes can only be frozen at advertised safe points.
    suspendability: str
    exclusive: tuple[str, ...]

    @classmethod
    def of(cls, declaration: LaneDeclaration) -> LaneRouting:
        return cls(
            request_cpus=declaration.request_cpus,
            memory_mb=declaration.memory_mb,
            suspendability=declaration.suspendability,
            exclusive=tuple(declaration.exclusive),
        )


@dataclass(frozen=True, slots=True)
class LaneRow:
    """One lane, from whichever sources know about it.

    Either side may be absent, and which one is absent is the finding:

    - no ``history`` — the lane is declared but has never run in the
      scanned window, which no journal-only view could ever mention;
    - no ``routing`` — the lane ran but the declarations do not describe
      it, so (if the declarations were readable at all) it cannot run
      again: `lane-run` refuses an undeclared lane.

    Both absent is not a lane at all and is rejected here.
    """

    work_key: LaneWorkKey
    routing: LaneRouting | None
    history: LaneDispatchSummary | None

    def __post_init__(self) -> None:
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneRow.work_key must be a LaneWorkKey")
        if self.routing is None and self.history is None:
            raise ValueError(
                "LaneRow needs a declaration or a dispatch record to exist"
            )


@dataclass(frozen=True, slots=True)
class DeclarationsRead:
    """The declarations parsed; ``routing=None`` means truly undeclared."""

    path: str


@dataclass(frozen=True, slots=True)
class DeclarationsUnavailable:
    """The declarations could not be read, so absent routing proves nothing.

    Kept distinct from :class:`DeclarationsRead` precisely so a missing
    or malformed declarations file can never be mistaken for a repo
    whose lanes are simply undeclared (finding 2, #7138).
    """

    detail: str


DeclarationsState = DeclarationsRead | DeclarationsUnavailable


@dataclass(frozen=True, slots=True)
class ExecutorStatusSnapshot:
    """Everything one ``executor-status`` invocation observed."""

    captured_at: datetime
    #: Which backend is in play and what established it — or an explicit
    #: "nothing did". Never a guess.
    backend: BackendSelection
    pool: PoolState
    declarations: DeclarationsState
    journal_location: str
    #: Every lane either source knows about, longest learned priority
    #: first — the order the next gate will dispatch them in.
    lanes: tuple[LaneRow, ...]
    #: How many journal records were scanned to build ``lanes``.
    records_scanned: int
    #: Of those, how many predate a dimension the record now requires
    #: (the machine-state envelope #7135, the cpu request #7136) and so
    #: could not be read back. Reported, never hidden: a reader that
    #: quietly dropped them would understate how thin the history it
    #: summarized actually is. One count across every epoch — see
    #: LaneDispatchHistory.predating_schema for why it is not split.
    records_predating_schema: int
    faults: tuple[SnapshotFault, ...]

    def __post_init__(self) -> None:
        if type(self.captured_at) is not datetime or self.captured_at.tzinfo is None:
            raise ValueError(
                "ExecutorStatusSnapshot.captured_at must be timezone-aware"
            )

    @property
    def is_degraded(self) -> bool:
        """Whether any input is broken (as opposed to merely empty)."""
        return bool(self.faults)


def build_executor_status_snapshot(
    *,
    backend: BackendSelection,
    inspector_for: Callable[[str], ExecutorPoolInspector],
    declarations_reader: Callable[[], LaneDeclarations],
    declarations_location: str,
    journal_reader: LaneDispatchJournalReader,
    runtime_history: LaneRuntimeHistory,
    captured_at: datetime,
    recent_limit: int = DEFAULT_RECENT_DISPATCH_LIMIT,
) -> ExecutorStatusSnapshot:
    """Gather every source that knows something into one snapshot.

    Never raises for a missing or broken source: each is reduced to an
    offline reason or a :class:`SnapshotFault` so the surviving sources
    still reach the operator. That is the whole point — a machine with
    no pool must still be able to see what its lanes have been costing,
    and a repository whose declarations will not parse must be TOLD so
    rather than shown a plausible-looking short list.

    The pool inspector arrives as a factory rather than an instance
    because there may be no backend to build one for: an unestablished
    backend is a reportable state, not a caller's problem to pre-solve.
    """
    if type(captured_at) is not datetime or captured_at.tzinfo is None:
        raise ValueError("build_executor_status_snapshot needs an aware captured_at")
    if type(recent_limit) is not int or recent_limit < 1:
        raise ValueError("build_executor_status_snapshot recent_limit must be >= 1")
    faults: list[SnapshotFault] = []
    pool = _inspect_backend_pool(backend, inspector_for, faults)
    declarations, declared = _read_declarations(
        declarations_reader, declarations_location, faults
    )
    location, entries, predating = _read_journal(
        journal_reader, recent_limit, faults
    )
    lanes = _lane_rows(declared, entries, runtime_history, faults)
    return ExecutorStatusSnapshot(
        captured_at=captured_at.astimezone(timezone.utc),
        backend=backend,
        pool=pool,
        declarations=declarations,
        journal_location=location,
        lanes=lanes,
        records_scanned=len(entries) + predating,
        records_predating_schema=predating,
        faults=tuple(faults),
    )


def _inspect_backend_pool(
    backend: BackendSelection,
    inspector_for: Callable[[str], ExecutorPoolInspector],
    faults: list[SnapshotFault],
) -> PoolState:
    if type(backend) is SelectedBackend:
        return _inspect_pool(inspector_for(backend.name), faults)
    if type(backend) is UnknownBackend:
        # No backend, no pool to look at. Reported as the same offline
        # state a missing pool uses, carrying the reason, so the reader
        # gets a sentence instead of an empty section.
        return PoolOffline(
            f"which backend is unknown, so is its pool: {backend.reason}"
        )
    raise AssertionError("backend selection is a closed union")


def _read_declarations(
    declarations_reader: Callable[[], LaneDeclarations],
    location: str,
    faults: list[SnapshotFault],
) -> tuple[DeclarationsState, Mapping[str, LaneDeclaration]]:
    """Read the canonical declarations, loudly.

    A declarations file that is missing or will not validate is not an
    empty repository: every lane in it is unrunnable until it is fixed
    (`lane-run` exits 78), so it is a fault, not a quiet zero.
    """
    try:
        declarations = declarations_reader()
    except LaneDeclarationError as error:
        faults.append(
            SnapshotFault(source=FactSource.LANE_DECLARATIONS, detail=str(error))
        )
        return (DeclarationsUnavailable(detail=str(error)), {})
    return (DeclarationsRead(path=location), declarations.lanes)


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
) -> tuple[str, tuple[LaneDispatchEntry, ...], int]:
    try:
        history = journal_reader.read_recent(recent_limit)
    except LaneDispatchJournalError as error:
        faults.append(
            SnapshotFault(source=FactSource.DISPATCH_JOURNAL, detail=str(error))
        )
        return (f"unreadable: {error}", (), 0)
    return (history.location, history.entries, history.predating_schema)


def _lane_rows(
    declared: Mapping[str, LaneDeclaration],
    entries: tuple[LaneDispatchEntry, ...],
    runtime_history: LaneRuntimeHistory,
    faults: list[SnapshotFault],
) -> tuple[LaneRow, ...]:
    """One row per lane either source knows about.

    Entries arrive oldest first, so the last one seen for a work key is
    that lane's most recent dispatch. A declared lane with no entry is
    still a row: "declared and never run" is exactly the fact a
    journal-only view could not express.
    """
    latest: dict[str, LaneDispatchEntry] = {}
    runs: dict[str, int] = {}
    for entry in entries:
        key = entry.record.work_key.value
        latest[key] = entry
        runs[key] = runs.get(key, 0) + 1
    try:
        histories = {
            key: _summarize_lane(entry, runs[key], runtime_history)
            for key, entry in latest.items()
        }
    except LaneRuntimeHistoryError as error:
        # A broken learning store cannot be papered over with a neutral
        # priority: that would print an ordering the next gate will not
        # actually use. Drop the history side and say why, loudly.
        faults.append(
            SnapshotFault(source=FactSource.RUNTIME_HISTORY, detail=str(error))
        )
        histories = {}
    rows = [
        LaneRow(
            work_key=LaneWorkKey(key),
            routing=(
                LaneRouting.of(declared[key]) if key in declared else None
            ),
            history=histories.get(key),
        )
        for key in sorted(set(declared) | set(histories))
    ]
    # Highest learned priority first: the order the next gate dispatches
    # in. Lanes with no history sort last — nothing is known about how
    # long they take — with a stable tiebreak so repeated snapshots do
    # not shuffle.
    rows.sort(
        key=lambda row: (
            0 if row.history is not None else 1,
            -(row.history.learned_priority if row.history is not None else 0),
            row.work_key.value,
        )
    )
    return tuple(rows)


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
        last_machine_state=record.machine_state,
        learned_priority=runtime_history.learned_priority(record.work_key),
    )
