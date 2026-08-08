"""The repository-wide ledger of live tech-lead runs, and its matrix (#6994).

A per-key claim cannot express the rule this feature exists to enforce. "Exactly
one engine owns ``issue:42``" is a statement about ONE key; "a whole-repository
review is exclusive of every other tech-lead run" is a statement about the
RELATIONSHIP between keys, and no composition of independent single-key
compare-and-swaps decides it: engine A can win ``global:health_review`` while
engine B independently wins ``issue:42``, and both are correct about their own
key while together violating the invariant (#6994 round 2 F1).

So the shared cell holds the whole picture. One ledger per repository, holding
every live run any engine has queued or running, mutated by compare-and-swap.
Because the ledger is the unit of atomicity, the conflict matrix is evaluated
against ALL of it — which is the only way "any live global conflicts with every
tech-lead run, while distinct issue scopes coexist" can be true across engines.

Two lifecycle states, because the matrix needs both:

* ``QUEUED`` — this run is admitted and waiting. Reserving a queued slot is
  NEVER refused by the barrier: waiting behind a global run is the designed
  behaviour, not a conflict, and refusing here would silently lose the request.
* ``RUNNING`` — a session is executing this run. The barrier applies HERE, at
  promotion, because that is the moment exclusivity actually means something.

Everything in this module is pure: the same :func:`resolve` decides the matrix
for the in-memory single-instance store and for the GitHub ref adapter, so the
two cannot drift, and the whole matrix is unit-testable without a network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional, TypeVar

from .tech_lead_run import TechLeadRunScopeKind, scope_kind_of_run_key

# Why a promotion was refused. Shares the launch gate's vocabulary
# (``domain.tech_lead_run.BARRIER_*``) so an operator reads the same phrase
# whether the run was held back by this engine's local gate or by a peer's.
BARRIER_GLOBAL_RUN_ACTIVE = "global_run_active"
BARRIER_GLOBAL_RUN_QUEUED = "global_run_queued"
BARRIER_GLOBAL_AWAITING_DRAIN = "global_run_awaiting_drain"


class RunLifecycle(str, Enum):
    """Where a run is in its life. The matrix reads both states."""

    QUEUED = "queued"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class RunLedgerEntry:
    """One engine's hold on one logical run."""

    run_key: str
    scope_kind: TechLeadRunScopeKind
    lifecycle: RunLifecycle
    claimant: str
    lease_id: str
    started_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """An unrenewed hold is not a hold: its owner died without releasing."""
        return now >= self.expires_at

    @property
    def is_global(self) -> bool:
        return self.scope_kind.is_global

    @property
    def is_running(self) -> bool:
        return self.lifecycle is RunLifecycle.RUNNING


@dataclass(frozen=True, slots=True)
class RunLedger:
    """Every live tech-lead run in the repository, from every engine."""

    entries: tuple[RunLedgerEntry, ...] = ()

    def live(self, now: datetime) -> tuple[RunLedgerEntry, ...]:
        """Entries whose lease has not lapsed."""
        return tuple(entry for entry in self.entries if not entry.is_expired(now))

    def find(self, run_key: str, now: datetime) -> Optional[RunLedgerEntry]:
        return next(
            (entry for entry in self.live(now) if entry.run_key == run_key), None
        )

    def upsert(self, entry: RunLedgerEntry, now: datetime) -> "RunLedger":
        """Replace ``entry``'s key and drop everything that has lapsed.

        Pruning on every write is what keeps a crashed engine's hold from
        accumulating forever in the shared cell.
        """
        kept = tuple(
            live for live in self.live(now) if live.run_key != entry.run_key
        )
        return RunLedger(tuple(sorted(kept + (entry,), key=_entry_order)))

    def without(self, run_key: str, now: datetime) -> "RunLedger":
        return RunLedger(
            tuple(entry for entry in self.live(now) if entry.run_key != run_key)
        )


def _entry_order(entry: RunLedgerEntry) -> tuple[str, str]:
    """Deterministic serialization order, so two engines write the same bytes."""
    return (entry.run_key, entry.lease_id)


class RunLedgerRequestKind(str, Enum):
    """The four things an engine ever asks the shared ledger for."""

    RESERVE = "reserve"
    PROMOTE = "promote"
    RENEW = "renew"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class RunLedgerRequest:
    """One engine's ask, carrying everything the matrix needs to judge it."""

    kind: RunLedgerRequestKind
    run_key: str
    scope_kind: TechLeadRunScopeKind
    lease_id: str = ""

    def __post_init__(self) -> None:
        needs_lease = self.kind in (
            RunLedgerRequestKind.PROMOTE,
            RunLedgerRequestKind.RENEW,
            RunLedgerRequestKind.RELEASE,
        )
        if needs_lease and not self.lease_id:
            raise ValueError(
                f"A {self.kind.value} request must name the lease it holds"
                f" (run_key={self.run_key!r})"
            )


class RunLedgerStatus(str, Enum):
    """The discriminated verdict on one ledger request.

    ``HELD_BY_PEER`` and ``LOST`` are deliberately separate from ``UNAVAILABLE``
    and from each other, because the caller owes three different behaviours:

    * ``HELD_BY_PEER`` — contention. Another engine holds this run and we never
      did. Recovered queued work must be RETAINED and retried, not withdrawn:
      the holder's lease will lapse or it will release, and withdrawing on
      contention is how a restart strands its own recovered anchor (round 2 F4).
    * ``LOST`` — definitive loss. We held this lease and no longer do. Queued
      work is withdrawn and any active session must be stopped.
    * ``UNAVAILABLE`` — we could not tell. Never a reason to stop a running
      session; a transport failure is not evidence of anything.
    """

    GRANTED = "granted"
    ADOPTED = "adopted"
    BARRIER = "barrier"
    HELD_BY_PEER = "held_by_peer"
    LOST = "lost"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RunLedgerOutcome:
    """What the ledger decided, in the caller's vocabulary."""

    status: RunLedgerStatus
    run_key: str
    lease_id: str = ""
    holder: str = ""
    barrier_reason: str = ""
    detail: str = ""

    @property
    def granted(self) -> bool:
        """True when the engine may act on the run (fresh grant or adoption)."""
        return self.status in (RunLedgerStatus.GRANTED, RunLedgerStatus.ADOPTED)

    @classmethod
    def unavailable(cls, run_key: str, detail: str) -> "RunLedgerOutcome":
        return cls(RunLedgerStatus.UNAVAILABLE, run_key, detail=detail)


@dataclass(frozen=True, slots=True)
class RunLedgerResolution:
    """The verdict plus the ledger the caller must commit to make it true.

    ``ledger`` is ``None`` when nothing needs writing, so a read-only refusal
    never burns a compare-and-swap write on the shared cell.
    """

    outcome: RunLedgerOutcome
    ledger: Optional[RunLedger] = None


def resolve(
    ledger: RunLedger,
    request: RunLedgerRequest,
    *,
    claimant: str,
    now: datetime,
    lease_seconds: int,
) -> RunLedgerResolution:
    """Apply the whole conflict matrix to one request. Pure.

    THE single implementation. The in-memory store for a single-instance
    deployment and the GitHub ref adapter both call this, so "what conflicts
    with what" has one definition regardless of how many engines exist.
    """
    if request.kind is RunLedgerRequestKind.RESERVE:
        return _reserve(ledger, request, claimant, now, lease_seconds)
    if request.kind is RunLedgerRequestKind.PROMOTE:
        return _promote(ledger, request, now, lease_seconds)
    if request.kind is RunLedgerRequestKind.RENEW:
        return _renew(ledger, request, now, lease_seconds)
    return _release(ledger, request, now)


def _reserve(
    ledger: RunLedger,
    request: RunLedgerRequest,
    claimant: str,
    now: datetime,
    lease_seconds: int,
) -> RunLedgerResolution:
    """Take (or re-take) the QUEUED slot for one run identity.

    The barrier is NOT consulted here. A targeted run queued behind a global one
    is the designed outcome — the operator's request is recorded and waits —
    whereas refusing it would make the dashboard's "queued behind the board
    health review" answer impossible to produce.
    """
    held = ledger.find(request.run_key, now)
    if held is not None and held.claimant != claimant:
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.HELD_BY_PEER,
                request.run_key,
                holder=held.claimant,
                detail=(
                    f"Another orchestrator instance ({held.claimant}) already"
                    f" owns this tech-lead run."
                ),
            )
        )
    if held is not None:
        # Our OWN live hold — the restart case. Adopting it is what stops a
        # fresh engine from reporting its own unexpired pre-crash lease as a
        # peer's and stranding the recovered run (round 2 F4). The lifecycle is
        # preserved: adopting a RUNNING entry must not silently demote it.
        adopted = replace(held, expires_at=now + timedelta(seconds=lease_seconds))
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.ADOPTED,
                request.run_key,
                lease_id=adopted.lease_id,
                holder=claimant,
                detail="Re-adopted this engine's own live hold on the run.",
            ),
            ledger.upsert(adopted, now),
        )
    entry = RunLedgerEntry(
        run_key=request.run_key,
        scope_kind=request.scope_kind,
        lifecycle=RunLifecycle.QUEUED,
        claimant=claimant,
        lease_id=request.lease_id or _mint_lease_id(request.run_key, now),
        started_at=now,
        expires_at=now + timedelta(seconds=lease_seconds),
    )
    return RunLedgerResolution(
        RunLedgerOutcome(
            RunLedgerStatus.GRANTED, request.run_key, lease_id=entry.lease_id
        ),
        ledger.upsert(entry, now),
    )


def _promote(
    ledger: RunLedger,
    request: RunLedgerRequest,
    now: datetime,
    lease_seconds: int,
) -> RunLedgerResolution:
    """Move a reserved run to RUNNING — the atomic exclusivity decision.

    This is where the matrix bites, and it is evaluated against every engine's
    entries at once:

    * a GLOBAL run waits for a full DRAIN (no other run may be executing) and,
      when a second global flavor is also queued, the deterministically FIRST of
      them goes first, so two engines cannot each conclude they are next;
    * a TARGETED run waits while any global run is live at all — queued or
      running — which is what makes a queued global a barrier rather than merely
      first in line;
    * two distinct targeted runs never conflict.
    """
    ours = ledger.find(request.run_key, now)
    if ours is None or ours.lease_id != request.lease_id:
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.LOST,
                request.run_key,
                holder=ours.claimant if ours is not None else "",
                detail=(
                    "This engine no longer holds the run it is trying to start."
                ),
            )
        )
    others = tuple(
        entry for entry in ledger.live(now) if entry.run_key != request.run_key
    )
    barrier = (
        _global_promotion_barrier(ours, others)
        if ours.is_global
        else _targeted_promotion_barrier(others)
    )
    if barrier is not None:
        reason, holder = barrier
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.BARRIER,
                request.run_key,
                lease_id=ours.lease_id,
                holder=holder,
                barrier_reason=reason,
                detail=_barrier_detail(reason, holder),
            )
        )
    running = replace(
        ours,
        lifecycle=RunLifecycle.RUNNING,
        expires_at=now + timedelta(seconds=lease_seconds),
    )
    return RunLedgerResolution(
        RunLedgerOutcome(
            RunLedgerStatus.GRANTED, request.run_key, lease_id=running.lease_id
        ),
        ledger.upsert(running, now),
    )


def _global_promotion_barrier(
    ours: RunLedgerEntry, others: Iterable[RunLedgerEntry]
) -> Optional[tuple[str, str]]:
    """Why an exclusive whole-repository run may not start yet."""
    peers = tuple(others)
    executing = next((entry for entry in peers if entry.is_running), None)
    if executing is not None:
        return (BARRIER_GLOBAL_AWAITING_DRAIN, executing.claimant)
    ahead = next(
        (
            entry
            for entry in sorted(peers, key=_queue_order)
            if entry.is_global and _queue_order(entry) < _queue_order(ours)
        ),
        None,
    )
    if ahead is not None:
        return (BARRIER_GLOBAL_RUN_QUEUED, ahead.claimant)
    return None


def _targeted_promotion_barrier(
    others: Iterable[RunLedgerEntry],
) -> Optional[tuple[str, str]]:
    """Why a focused investigation may not start yet."""
    globals_live = tuple(entry for entry in others if entry.is_global)
    executing = next((entry for entry in globals_live if entry.is_running), None)
    if executing is not None:
        return (BARRIER_GLOBAL_RUN_ACTIVE, executing.claimant)
    if globals_live:
        return (BARRIER_GLOBAL_RUN_QUEUED, globals_live[0].claimant)
    return None


def _queue_order(entry: RunLedgerEntry) -> tuple[datetime, str]:
    """Total order over queued runs: oldest first, run key breaks exact ties."""
    return (entry.started_at, entry.run_key)


def _barrier_detail(reason: str, holder: str) -> str:
    who = f" held by {holder}" if holder else ""
    if reason == BARRIER_GLOBAL_AWAITING_DRAIN:
        return f"Waiting for every tech-lead run{who} to finish."
    if reason == BARRIER_GLOBAL_RUN_ACTIVE:
        return f"A whole-repository tech-lead review{who} is running."
    return f"A whole-repository tech-lead review{who} is queued ahead of this run."


def _renew(
    ledger: RunLedger,
    request: RunLedgerRequest,
    now: datetime,
    lease_seconds: int,
) -> RunLedgerResolution:
    """Extend our hold. A missing or reassigned entry is DEFINITIVE loss."""
    ours = ledger.find(request.run_key, now)
    if ours is None:
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.LOST,
                request.run_key,
                detail="This engine's hold on the run has lapsed.",
            )
        )
    if ours.lease_id != request.lease_id:
        return RunLedgerResolution(
            RunLedgerOutcome(
                RunLedgerStatus.LOST,
                request.run_key,
                holder=ours.claimant,
                detail=f"The run is now owned by {ours.claimant}.",
            )
        )
    renewed = replace(ours, expires_at=now + timedelta(seconds=lease_seconds))
    return RunLedgerResolution(
        RunLedgerOutcome(
            RunLedgerStatus.GRANTED, request.run_key, lease_id=renewed.lease_id
        ),
        ledger.upsert(renewed, now),
    )


def _release(
    ledger: RunLedger, request: RunLedgerRequest, now: datetime
) -> RunLedgerResolution:
    """Hand the run back so a peer need not wait out the lease."""
    ours = ledger.find(request.run_key, now)
    if ours is None or ours.lease_id != request.lease_id:
        return RunLedgerResolution(
            RunLedgerOutcome(RunLedgerStatus.GRANTED, request.run_key)
        )
    return RunLedgerResolution(
        RunLedgerOutcome(RunLedgerStatus.GRANTED, request.run_key),
        ledger.without(request.run_key, now),
    )


def _mint_lease_id(run_key: str, now: datetime) -> str:
    """A lease id for stores that do not mint their own (the in-memory one)."""
    return f"{run_key}-{int(now.timestamp() * 1000)}"


# ----------------------------------------------------------------------
# Wire format — STRICT and versioned (#6994 round 2 F7/A4)
#
# An exclusivity ledger must never be read approximately. A row this build does
# not understand — a forward-version scope written by a newer peer during a
# rolling upgrade, a truncated line, a duplicate key — could be the very global
# run that makes the repository busy, and silently dropping it makes the
# repository look FREE. So decoding is all-or-nothing: any defect raises, and
# the adapter turns that into ``UNAVAILABLE`` with no write, exactly as it
# treats an unreachable store.
#
# The payload is JSON rather than delimited tokens, because token splitting
# cannot round-trip a claimant id (or any field) that happens to contain the
# delimiter — a format that can silently mis-parse valid data is the same class
# of bug one layer down.
# ----------------------------------------------------------------------

RUN_LEDGER_VERSION = 1
_LEDGER_OPEN = "<io-run-ledger>"
_LEDGER_CLOSE = "</io-run-ledger>"
_TOP_LEVEL_FIELDS = frozenset({"version", "entries"})
_ENTRY_FIELDS = frozenset(
    {
        "run_key",
        "scope_kind",
        "lifecycle",
        "claimant",
        "lease_id",
        "started_at",
        "expires_at",
    }
)


class RunLedgerDecodeError(ValueError):
    """The shared ledger could not be decoded EXACTLY.

    Raised rather than returning a partial ledger, because a partial
    exclusivity ledger is indistinguishable from a free repository and is the
    one answer this type must never be able to give.
    """


def format_run_ledger(ledger: RunLedger) -> str:
    """Serialize the ledger deterministically for a compare-and-swap cell.

    Deterministic because two engines racing on the SAME logical content must
    not produce two different bytes and thereby two different compare-and-swap
    outcomes: entries are sorted, and ``json.dumps`` is given sorted keys and a
    fixed separator.
    """
    payload = {
        "version": RUN_LEDGER_VERSION,
        "entries": [
            {
                "run_key": entry.run_key,
                "scope_kind": entry.scope_kind.value,
                "lifecycle": entry.lifecycle.value,
                "claimant": entry.claimant,
                "lease_id": entry.lease_id,
                "started_at": entry.started_at.isoformat(),
                "expires_at": entry.expires_at.isoformat(),
            }
            for entry in sorted(ledger.entries, key=_entry_order)
        ],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{_LEDGER_OPEN}\n{body}\n{_LEDGER_CLOSE}"


def parse_run_ledger(text: str) -> RunLedger:
    """Read a ledger back, or refuse. Never partially.

    Raises :class:`RunLedgerDecodeError` on a missing/duplicated block, invalid
    JSON, a DUPLICATE JSON member at any depth, an unexpected top-level or entry
    field set, an unknown schema version, an unknown scope kind or lifecycle, an
    unparseable timestamp, a run key that disagrees with its declared scope, or a
    duplicate run key.
    """
    payload = _ledger_payload(text)
    unexpected = set(payload).symmetric_difference(_TOP_LEVEL_FIELDS)
    if unexpected:
        raise RunLedgerDecodeError(
            f"run-ledger payload has an unexpected field set: {sorted(unexpected)}"
        )
    version = payload.get("version")
    if version != RUN_LEDGER_VERSION:
        raise RunLedgerDecodeError(
            f"unsupported run-ledger schema version {version!r};"
            f" this build reads version {RUN_LEDGER_VERSION}"
        )
    rows = payload.get("entries")
    if not isinstance(rows, list):
        raise RunLedgerDecodeError("run-ledger 'entries' must be a list")
    entries = [_decode_entry(row) for row in rows]
    keys = [entry.run_key for entry in entries]
    if len(set(keys)) != len(keys):
        raise RunLedgerDecodeError(f"run-ledger has duplicate run keys: {keys}")
    return RunLedger(tuple(sorted(entries, key=_entry_order)))


def _ledger_payload(text: str) -> dict[str, object]:
    """The single JSON object between the ledger markers."""
    if text.count(_LEDGER_OPEN) != 1 or text.count(_LEDGER_CLOSE) != 1:
        raise RunLedgerDecodeError(
            "run-ledger block must appear exactly once in the record"
        )
    opened = text.index(_LEDGER_OPEN) + len(_LEDGER_OPEN)
    closed = text.index(_LEDGER_CLOSE)
    if closed < opened:
        raise RunLedgerDecodeError("run-ledger block markers are out of order")
    try:
        payload = json.loads(text[opened:closed], object_pairs_hook=_exact_object)
    except ValueError as exc:
        raise RunLedgerDecodeError(
            f"run-ledger payload could not be decoded: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RunLedgerDecodeError("run-ledger payload must be a JSON object")
    return payload


def _exact_object(pairs: "list[tuple[str, object]]") -> dict[str, object]:
    """Build a JSON object, refusing duplicate member names at ANY depth.

    ``json.loads`` is last-value-wins by default, so a record carrying a live
    global run in one ``entries`` member and ``[]`` in a second would decode as
    an EMPTY ledger — the fail-open read the strict codec exists to prevent
    (#6994 round 2 F13). One malformed record is not worth guessing about, so a
    duplicate member is a decode failure rather than a resolution rule.
    """
    seen: set[str] = set()
    for name, _value in pairs:
        if name in seen:
            raise ValueError(f"duplicate member name {name!r}")
        seen.add(name)
    return dict(pairs)


def _decode_entry(row: object) -> RunLedgerEntry:
    if not isinstance(row, dict):
        raise RunLedgerDecodeError(f"run-ledger entry must be an object: {row!r}")
    names: set[str] = {str(name) for name in row}
    unexpected = names.symmetric_difference(_ENTRY_FIELDS)
    if unexpected:
        raise RunLedgerDecodeError(
            f"run-ledger entry has an unexpected field set: {sorted(unexpected)}"
        )
    values = {name: row[name] for name in _ENTRY_FIELDS}
    if not all(isinstance(value, str) for value in values.values()):
        raise RunLedgerDecodeError(
            f"every run-ledger entry field must be a string: {row!r}"
        )
    run_key = str(values["run_key"])
    scope_kind = _decode_enum(TechLeadRunScopeKind, values["scope_kind"], "scope kind")
    try:
        declared = scope_kind_of_run_key(run_key)
    except ValueError as exc:
        raise RunLedgerDecodeError(str(exc)) from exc
    if declared is not scope_kind:
        raise RunLedgerDecodeError(
            f"run key {run_key!r} does not name a {scope_kind.value} run"
        )
    return RunLedgerEntry(
        run_key=run_key,
        scope_kind=scope_kind,
        lifecycle=_decode_enum(RunLifecycle, values["lifecycle"], "lifecycle"),
        claimant=str(values["claimant"]),
        lease_id=str(values["lease_id"]),
        started_at=_decode_timestamp(values["started_at"]),
        expires_at=_decode_timestamp(values["expires_at"]),
    )


_EnumT = TypeVar("_EnumT", bound=Enum)


def _decode_enum(enum_type: type[_EnumT], raw: object, what: str) -> _EnumT:
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise RunLedgerDecodeError(f"unknown run-ledger {what}: {raw!r}") from exc


def _decode_timestamp(raw: object) -> datetime:
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise RunLedgerDecodeError(f"unparseable run-ledger timestamp: {raw!r}") from exc


__all__ = [
    "BARRIER_GLOBAL_AWAITING_DRAIN",
    "BARRIER_GLOBAL_RUN_ACTIVE",
    "BARRIER_GLOBAL_RUN_QUEUED",
    "RunLedger",
    "RunLedgerEntry",
    "RunLedgerOutcome",
    "RunLedgerRequest",
    "RunLedgerRequestKind",
    "RunLedgerResolution",
    "RunLedgerDecodeError",
    "RunLedgerStatus",
    "RunLifecycle",
    "RUN_LEDGER_VERSION",
    "format_run_ledger",
    "parse_run_ledger",
    "resolve",
]
