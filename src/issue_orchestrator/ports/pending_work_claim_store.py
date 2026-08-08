"""The durable ledger of queued work that has left its queue (#6999 F4/F7/F8).

A request removed from a pending queue at launch exists nowhere else until the
session that took it reaches a terminal outcome. The pending queues themselves
are in-memory, so this store — not those lists — is the authoritative record.
Three durable states cover that whole span:

* **held** — a live run is doing this work.
* **deferred** — the run stopped for a provider reason; the work is untouched
  and waiting to be relaunched.
* **gone** — the row is deleted, and only a true terminal work outcome does that.

Why deferral is a state rather than a delete (#6999 F8): re-admitting the
request to an in-memory queue is not durable, so deleting the row at that moment
opens a window where a crash loses the only record. The row survives the
transition and startup re-admits from it; the relaunch that takes the work again
supersedes it by :meth:`PendingWorkClaim.work_key`.

Why implementations must not store any of this in the run directory (#6999 F7):
that directory lives inside the session worktree, which is handed to the
launched agent and is writable by it. A claim stored there would let an agent
rewrite what work the orchestrator believes it is doing — which queue, on which
PR, with which evidence hints — and restoration would accept it as truth. That
inverts "Agent Intent, Orchestrator Authority".

Rows are addressed by the ORCHESTRATOR-allocated run root and validated against
every field of the run identity recorded with them. Run ids are timestamps and
are not unique on their own; identities come from the worktree manifest and are
agent-writable. So neither is a safe address by itself, and a mismatch on any
recorded field fails closed rather than reading as "no claim".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.pending_work import PendingWorkClaim
from ..domain.session_run import SessionRunAssets


class ConflictingPendingWorkClaimError(RuntimeError):
    """A different claim already exists for this run."""


class ClaimState(Enum):
    """What the ledger says about one run's claim.

    ``DEFERRED`` is deliberately NOT collapsed into ``ABSENT`` (#6999 F8): a
    deferred run's work has been re-queued, so a terminal still discoverable
    for that run must never be admitted to ordinary completion processing as
    though it were carrying nothing. Losing that distinction is how a stale
    terminal settles work the queue already owns.
    """

    ABSENT = "absent"
    HELD = "held"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class ClaimLookup:
    """A typed answer to "what is this run holding?"."""

    state: ClaimState
    claim: PendingWorkClaim | None = None

    @property
    def held(self) -> PendingWorkClaim | None:
        return self.claim if self.state is ClaimState.HELD else None


@dataclass(frozen=True, slots=True)
class UnresolvedClaim:
    """A claim the ledger still holds, as seen by startup recovery.

    ``run_key`` is opaque to control: it identifies the run whose settlement
    never completed, and is only ever compared against other run keys the store
    produced.
    """

    run_key: str
    session_name: str
    deferred: bool
    # Recorded at hold time from the launching session, NOT derived from the
    # payload or the terminal name (#6999 F12): a review session is named for
    # its PR, and the payload is exactly what may have become unreadable.
    issue_number: int
    claim: PendingWorkClaim


@dataclass(frozen=True, slots=True)
class UnreadableClaim:
    """A stored row whose payload or identity could not be rebuilt."""

    run_key: str
    session_name: str
    issue_number: int
    error: str
    # Distinguishes run GENERATIONS. Run roots are named from a second-
    # resolution timestamp and created with exist_ok, so a replacement run of
    # one session can reuse the path; started_at has sub-second precision and
    # is what tells the two apart (#6999 F12).
    started_at: str


class PendingWorkClaimStore(Protocol):
    """The durable side of the launch-to-settlement lifecycle."""

    def hold_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim, *, issue_number: int
    ) -> None:
        """Record that ``run`` has taken ``claim`` off its queue.

        Supersedes any deferred row for the same work: relaunching it is what
        resolves the earlier deferral. Re-holding the identical claim for the
        same run is idempotent; a DIFFERENT claim for one run raises
        :class:`ConflictingPendingWorkClaimError` rather than overwriting,
        because overwriting destroys the only record of the first.
        """
        ...

    def defer_pending_work_claim(self, run: SessionRunAssets) -> None:
        """Mark ``run``'s claim as waiting to be relaunched.

        One durable transition. The row must survive it, so a crash on either
        side of the in-memory re-queue is recoverable at startup.
        """
        ...

    def consume_pending_work_claim(self, run: SessionRunAssets) -> None:
        """Delete ``run``'s claim. Only a true terminal work outcome may."""
        ...

    def look_up_pending_work_claim(self, run: SessionRunAssets) -> ClaimLookup:
        """What ``run`` is holding, as a typed state rather than a maybe-value.

        Raises rather than answering ABSENT when a record exists but cannot be
        trusted or rebuilt: "no claim" and "a claim I cannot read" are different
        facts, and conflating them drops work while looking like a clean start.
        """
        ...

    def list_unresolved_claims(self) -> tuple[UnresolvedClaim, ...]:
        """Every claim still held or deferred, for startup recovery.

        Deliberately enumerable (#6999 F8): a run whose terminal is long gone
        cannot be found by discovery, so without this its work would sit in the
        ledger forever. Rows whose payload cannot be rebuilt are reported by
        :meth:`list_unreadable_claims` instead of being skipped in silence.
        """
        ...

    def list_unreadable_claims(self) -> tuple[UnreadableClaim, ...]:
        """Stored rows that cannot be rebuilt, for the same recovery sweep."""
        ...

    def mark_deferred_by_run_key(self, run_key: str) -> None:
        """Move an enumerated row to deferred without deleting it.

        Recovery re-admits work to an IN-MEMORY queue, which is not a durable
        destination, so the row must stay authoritative until a relaunch takes
        the same work again and supersedes it (#6999 F8). Deleting here would
        lose the work to any crash before that relaunch.
        """
        ...

    def run_key_for(self, run: SessionRunAssets) -> str:
        """The opaque key this store addresses ``run`` by."""
        ...

    def quarantine_key_for(self, run: SessionRunAssets) -> str:
        """The opaque key a QUARANTINE against ``run`` is recorded under.

        Distinct from :meth:`run_key_for` because it must survive a replacement
        run reusing the same directory: an escalated marker from a previous
        generation would otherwise suppress the new one's comment and event
        (#6999 F12).
        """
        ...


class ClaimQuarantineStore(Protocol):
    """Durable record of runs whose claim could not be read (#6999 F12).

    Separate from the claim lifecycle on purpose: a quarantine outlives the
    claim it could not read, is keyed on the run rather than the work (the work
    is precisely what is unknown), and is cleared by a human rather than by any
    session outcome.
    """

    def record_quarantine(
        self,
        quarantine_key: str,
        *,
        run_key: str,
        session_name: str,
        issue_number: int,
        error: str,
    ) -> None:
        """Record (or refresh) that this run is quarantined."""
        ...

    def release_quarantine(self, quarantine_key: str) -> None:
        """Clear a quarantine once its cause is gone.

        The explicit clear seam: a quarantine ends when the run's claim can be
        read again or its row has been removed - a human having repaired or
        removed it - never because some other session happened to start.
        """
        ...

    def quarantined_run_keys(self) -> frozenset[str]:
        """Run keys with a live quarantine, for release reconciliation."""
        ...

    def quarantine_keys_for_run(self, run_key: str) -> tuple[str, ...]:
        """Every recorded quarantine key for one run root.

        A replacement run can reuse a directory, so one run key may carry more
        than one generation's marker (#6999 F12).
        """
        ...

    def quarantine_issue_number(self, quarantine_key: str) -> int | None:
        """The issue a quarantine is holding open, if it is still recorded."""
        ...

    def quarantined_issue_numbers(self) -> frozenset[int]:
        """Issues currently held open by a quarantine.

        Read by every owner that might otherwise remove the shared
        ``needs-human`` label (#6999 F12). A quarantined terminal is
        deliberately absent from ``active_sessions``, so "some session for this
        issue is running" is NOT evidence that the block can be lifted.
        """
        ...

    def mark_quarantine_escalated(self, quarantine_key: str) -> None:
        """Record that the durable operator surface committed for this run."""
        ...

    def is_quarantine_escalated(self, quarantine_key: str) -> bool:
        """Whether the durable operator surface already committed.

        Idempotency is the point: the orphan scan re-discovers an untracked
        terminal every 30 seconds, and each rediscovery must not re-comment.
        A quarantine recorded but NOT escalated is retried, so a failed label
        or comment does not silently become the final state.
        """
        ...


__all__ = [
    "ClaimLookup",
    "ClaimQuarantineStore",
    "ClaimState",
    "ConflictingPendingWorkClaimError",
    "PendingWorkClaimStore",
    "UnreadableClaim",
    "UnresolvedClaim",
]
