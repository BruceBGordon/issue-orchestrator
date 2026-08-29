# pyright: strict
"""Port for read-only inspection of the machine-wide executor pool.

Lanes execute through :mod:`issue_orchestrator.ports.lane_executor`;
this port answers the *other* operator question about that machinery —
not "run this lane" but "what is the pool doing right now, and why is my
lane waiting". It is strictly read-only: nothing here submits, removes,
or reprioritizes anything.

The vocabulary is deliberately the same backend-neutral vocabulary the
lane contracts use (work keys, cpus, priority, exclusive tokens). A
backend's own scheduling nouns are translated at its adapter, exactly as
they are on the execution side, so promoting this snapshot into the web
UI later never drags scheduler concepts into the UI layer.

Absence is not an error: a pool that is not installed, not running, or
not the configured backend at all is reported as
:class:`PoolOffline` with a human-readable reason, because "there is no
pool" is a fact an operator needs rather than an exception to swallow.
A pool that answers with something the adapter cannot translate *is* an
error and raises :class:`PoolInspectionError` — that means a broken
invariant, not a missing pool.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LaneWorkKey

_SUBMITTER_MAX_LENGTH = 256


class PoolJobState(StrEnum):
    """What one job in the pool is doing, in backend-neutral terms."""

    #: Admitted to the queue, not yet executing (waiting for capacity,
    #: for an exclusive token, or for its turn by priority).
    QUEUED = "queued"
    #: Executing now.
    RUNNING = "running"
    #: Started, then frozen by the backend's machine-load backoff. Time
    #: spent here is charged to nothing — see ``LaneDeadline``.
    SUSPENDED = "suspended"
    #: The backend refuses to run it until a human intervenes.
    HELD = "held"
    #: On its way out of the queue (finished, or being removed).
    FINISHING = "finishing"


@dataclass(frozen=True, slots=True)
class LaneJobOrigin:
    """A pool job this system submitted, and the worktree that asked.

    The pool is machine-wide: several worktrees of several repositories
    submit into it concurrently, which is normal rather than anomalous.
    Attribution is therefore part of the job's identity, not a detail to
    reconstruct afterwards.
    """

    work_key: LaneWorkKey
    submitter_worktree: str

    def __post_init__(self) -> None:
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneJobOrigin.work_key must be a LaneWorkKey")
        if (
            type(self.submitter_worktree) is not str
            or not self.submitter_worktree
            or len(self.submitter_worktree) > _SUBMITTER_MAX_LENGTH
        ):
            raise ValueError(
                "LaneJobOrigin.submitter_worktree must be a non-empty string "
                f"of at most {_SUBMITTER_MAX_LENGTH} characters"
            )


@dataclass(frozen=True, slots=True)
class ForeignJobOrigin:
    """A job sharing the pool that no lane of this system submitted.

    Foreign jobs are reported rather than filtered out: they consume the
    same machine capacity, so an operator asking why a lane is waiting
    needs to see them.
    """

    owner: str

    def __post_init__(self) -> None:
        if (
            type(self.owner) is not str
            or not self.owner
            or len(self.owner) > _SUBMITTER_MAX_LENGTH
        ):
            raise ValueError(
                "ForeignJobOrigin.owner must be a non-empty string of at "
                f"most {_SUBMITTER_MAX_LENGTH} characters"
            )


#: Who a pool job belongs to. A closed union so "we do not know the work
#: key" can never be confused with "the work key is empty".
PoolJobOrigin = LaneJobOrigin | ForeignJobOrigin


@dataclass(frozen=True, slots=True)
class PoolJob:
    """One job in the pool as an operator needs to see it.

    Deliberately omits everything that could carry a secret: no command
    line, no arguments, no environment, no output paths. A work key, a
    submitter, and timings answer the operator's question without
    exporting anything sensitive.
    """

    origin: PoolJobOrigin
    state: PoolJobState
    #: How long the job has been in :attr:`state`. For a queued job this
    #: is its wait; for a running one, how long it has been executing.
    seconds_in_state: float
    request_cpus: int
    priority: int
    exclusive: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.origin) is not LaneJobOrigin and (
            type(self.origin) is not ForeignJobOrigin
        ):
            raise ValueError("PoolJob.origin must be a PoolJobOrigin")
        if type(self.state) is not PoolJobState:
            raise ValueError("PoolJob.state must be a PoolJobState")
        if (
            type(self.seconds_in_state) is not float
            or not math.isfinite(self.seconds_in_state)
            or self.seconds_in_state < 0
        ):
            raise ValueError(
                "PoolJob.seconds_in_state must be finite and non-negative"
            )
        if type(self.request_cpus) is not int or self.request_cpus < 0:
            raise ValueError("PoolJob.request_cpus must be a non-negative integer")
        if type(self.priority) is not int:
            raise ValueError("PoolJob.priority must be an integer")
        if type(self.exclusive) is not tuple or any(
            type(token) is not str or not token for token in self.exclusive
        ):
            raise ValueError(
                "PoolJob.exclusive must be a tuple of non-empty strings"
            )


@dataclass(frozen=True, slots=True)
class PoolCapacity:
    """What the pool can run at once, machine-wide."""

    machines: int
    total_cpus: int

    def __post_init__(self) -> None:
        if type(self.machines) is not int or self.machines < 0:
            raise ValueError("PoolCapacity.machines must be a non-negative integer")
        if type(self.total_cpus) is not int or self.total_cpus < 0:
            raise ValueError("PoolCapacity.total_cpus must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AnsweredPool:
    """The facts a pool that answered reported about itself.

    Shared by every answered state so a consumer reads capacity and
    jobs the same way whether or not the pool proved healthy — the
    jobs in a stale pool's queue are still the jobs in its queue.
    """

    capacity: PoolCapacity
    jobs: tuple[PoolJob, ...]

    def __post_init__(self) -> None:
        if type(self.capacity) is not PoolCapacity:
            raise ValueError("capacity must be a PoolCapacity")
        if type(self.jobs) is not tuple or any(
            type(job) is not PoolJob for job in self.jobs
        ):
            raise ValueError("jobs must be a tuple of PoolJob")

    @property
    def claimed_cpus(self) -> int:
        """Cpus held by jobs that already started.

        Derived from the jobs themselves rather than read as a second
        capacity number, so the "how busy" answer can never contradict
        the job rows printed underneath it.
        """
        return sum(
            job.request_cpus
            for job in self.jobs
            if job.state in (PoolJobState.RUNNING, PoolJobState.SUSPENDED)
        )

    def in_state(self, state: PoolJobState) -> tuple[PoolJob, ...]:
        return tuple(job for job in self.jobs if job.state is state)


@dataclass(frozen=True, slots=True)
class PoolOnline(AnsweredPool):
    """The pool answered AND proved it can run work right now.

    "Online" is a claim about capability, not about a socket having
    accepted a connection. It is reported only when the pool has
    execute resources and their liveness is *established* — see
    :class:`PoolUnknownHealth` for why the weaker reading is dangerous.
    """


@dataclass(frozen=True, slots=True)
class PoolUnknownHealth(AnsweredPool):
    """The pool answered, but is not provably able to run work.

    Three situations that look identical from a naive query and must
    never be rendered as "online":

    - it has no execute resources at all;
    - its resource records are stale, because a scheduler's collector
      keeps serving a dead daemon's cached advertisement for minutes
      after the daemon stops;
    - liveness could not be established at all.

    Each is a different sentence in ``detail``. Capacity and jobs are
    still carried, because whatever the pool did say remains a fact an
    operator can use — it just may describe a machine that is gone.
    """

    detail: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.detail) is not str or not self.detail:
            raise ValueError("PoolUnknownHealth.detail must be a non-empty string")


@dataclass(frozen=True, slots=True)
class PoolOffline:
    """There is no pool to report on, and this is why.

    Not an error: the scheduling backend is opt-in, so "the configured
    backend has no pool" and "the pool is not running" are both ordinary
    states an operator is entitled to see spelled out. Distinct from
    :class:`PoolUnknownHealth`, where a pool *did* answer — there, the
    facts it gave are still worth printing.
    """

    detail: str

    def __post_init__(self) -> None:
        if type(self.detail) is not str or not self.detail:
            raise ValueError("PoolOffline.detail must be a non-empty string")


#: What an inspection produced. Closed union: every consumer must handle
#: the not-online cases explicitly instead of rendering a misleading
#: empty or stale pool as a healthy one.
PoolState = PoolOnline | PoolUnknownHealth | PoolOffline


class PoolInspectionError(RuntimeError):
    """The pool answered with something the adapter cannot translate.

    Distinct from :class:`PoolOffline`: a missing pool is a fact, but a
    pool reporting a job whose shape violates the adapter's contract is
    a defect that must be visible rather than rendered as an empty or
    partial snapshot.
    """


@runtime_checkable
class ExecutorPoolInspector(Protocol):
    """Read-only view of the machine-wide executor pool."""

    def inspect(self) -> PoolState:
        """Return the pool's current state; never mutate it.

        Raises :class:`PoolInspectionError` only when the pool answers
        with something untranslatable. An absent or unreachable pool is
        reported as :class:`PoolOffline`.
        """
        ...
