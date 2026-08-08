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
from pathlib import Path
from typing import Protocol

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
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


def _quarantine_key(run_key: str, started_at: str) -> str:
    """The one format for a generation-anchored quarantine key (#6999 F12)."""
    return f"{run_key}@{started_at}"


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
    # The orchestrator's own recorded start instant for this run, which is what
    # quarantine keys are anchored on (#6999 F12).
    started_at: str
    # Recorded at hold time from the launching session, NOT derived from the
    # payload or the terminal name (#6999 F12): a review session is named for
    # its PR, and the payload is exactly what may have become unreadable.
    issue_number: int
    claim: PendingWorkClaim

    @property
    def quarantine_key(self) -> str:
        """The key a quarantine against THIS row is recorded under.

        Asked of the row rather than re-derived by each caller: the generation
        anchoring is the whole point (#6999 F12), and a caller that formats it
        by hand is a caller that can format it differently.
        """
        return _quarantine_key(self.run_key, self.started_at)


class QuarantineCause(Enum):
    """Why a run is quarantined — durable state, not a per-call argument.

    Lives beside :class:`QuarantineRecord` because it is part of the record
    (#6999 F6). Held only on the ephemeral subject, the cause could not be
    compared against the one an earlier pass announced, so a run whose
    observation CHANGED kept the operator story the first pass wrote — an
    "the terminal has already ended, re-queue by hand if necessary"
    instruction standing over a terminal that is in fact alive is precisely
    the duplicate execution this whole boundary exists to prevent.

    The four states are not one message with flags. Two families say opposite
    things about the queued work:

    * an UNREADABLE CLAIM means nobody can name what the run is carrying, so an
      operator has to work it out before re-queuing anything;
    * an UNRESTORABLE RUN means the claim is perfectly intact and the work IS
      named — it is deliberately not being requeued because a terminal is still
      running it. Telling an operator that this work is "unknown" invites the
      manual requeue, and therefore the duplicate execution, that protecting the
      run was meant to prevent.

    The fourth state is both at once and gets its own variant rather than an
    implicit branch: a live terminal that can be neither rebuilt nor identified
    (#6999 F2). It used to be protected from requeueing and reported to nobody.
    """

    #: A live terminal was rebuilt, but the claim it holds cannot be read.
    CLAIM_UNREADABLE_LIVE_RUN = "claim_unreadable_live_run"
    #: A ledger row nothing is running, whose payload cannot be rebuilt.
    CLAIM_UNREADABLE_ENDED_RUN = "claim_unreadable_ended_run"
    #: A live terminal whose session assets could not be rebuilt. Its claim
    #: reads cleanly, so the work it holds is known exactly.
    RUN_UNRESTORABLE = "run_unrestorable"
    #: A live terminal that can be neither rebuilt nor identified.
    RUN_UNRESTORABLE_CLAIM_UNREADABLE = "run_unrestorable_claim_unreadable"


class QuarantineLabelState(Enum):
    """Whether a quarantine owns the shared blocking label it needed.

    The distinction release depends on (#6999 F12): adding a label that is
    already present succeeds, so "the apply worked" is not evidence the
    quarantine put it there. Removing a label a human or another owner applied
    would silently retract their block.
    """

    #: No apply has been recorded yet, so the next sweep tries again.
    UNKNOWN = "unknown"
    #: This quarantine demonstrably put the label on the issue, so it is the
    #: one that takes it off.
    ACQUIRED = "acquired"
    #: NOT provably ours: the label was already present, or the adapter could
    #: not determine whether it was. Release leaves it where it is.
    PREEXISTING = "preexisting"

    @property
    def applied(self) -> bool:
        """Whether the apply that produced this state actually committed."""
        return self is not QuarantineLabelState.UNKNOWN


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    """The durable state of one quarantine.

    The two predicates below are the whole provenance rule, asked of the record
    rather than pattern-matched on the enum by every caller (#6999 F12/A5):
    only the quarantine that put the shared block there may take it off, and
    only one that has never recorded an outcome still owes an apply.
    """

    quarantine_key: str
    run_key: str
    session_name: str
    issue_number: int
    error: str
    label_state: QuarantineLabelState
    announced: bool
    releasing: bool
    #: The observation this row's announcement was written for (#6999 F6).
    #: ``None`` only for a row recorded before the cause became durable; it is
    #: deliberately not collapsed into a default, because "no cause recorded"
    #: must read as *different from whatever is observed next* so the story is
    #: rewritten rather than silently kept.
    cause: QuarantineCause | None = None
    #: The queued work this run is carrying, when the claim reads cleanly. It
    #: is what lets the unrestorable-run story name what it is protecting, so
    #: it is durable for the same reason the cause is.
    work_kind: PendingWorkKind | None = None

    def announces(self, cause: QuarantineCause) -> bool:
        """Whether this row's committed announcement already tells ``cause``'s story.

        The one predicate the escalation's idempotency rests on (#6999 F6):
        re-observing the SAME cause must not re-comment, and observing a
        DIFFERENT one must, because the message and event a cause produces are
        chosen from the cause alone.
        """
        return self.announced and self.cause is cause

    @property
    def block_is_ours(self) -> bool:
        """Whether THIS quarantine demonstrably applied the shared block."""
        return self.label_state is QuarantineLabelState.ACQUIRED

    @property
    def block_unrecorded(self) -> bool:
        """Whether no apply outcome has been recorded for it yet."""
        return self.label_state is QuarantineLabelState.UNKNOWN

    def records_ownership(self, outcome: QuarantineLabelState) -> bool:
        """Whether a fresh apply's ``outcome`` supersedes the stored provenance.

        The block is re-applied on EVERY pass, so provenance is not a one-shot
        fact and cannot be recorded only the first time (#6999 F3). Two
        transitions are real:

        * nothing recorded yet — the first apply that commits IS the provenance;
        * ``PREEXISTING`` -> ``ACQUIRED`` — the label this quarantine once found
          already present has since been removed, and THIS pass demonstrably put
          it back. It is now ours to take off, and leaving the row saying
          ``PREEXISTING`` strands the issue in ``needs-human`` forever: release
          would delete the row without removing the label it re-added.

        ``ACQUIRED`` never degrades to ``PREEXISTING``: a later pass finding the
        label present is finding the one we applied.
        """
        if not outcome.applied:
            return False
        if self.block_unrecorded:
            return True
        return outcome is QuarantineLabelState.ACQUIRED and not self.block_is_ours


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

    @property
    def quarantine_key(self) -> str:
        """The key a quarantine against THIS row is recorded under."""
        return _quarantine_key(self.run_key, self.started_at)


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

    def retire_deferred_claim(self, work_key: str) -> None:
        """Delete the deferred row for work a launch has now DROPPED (#6999 F4).

        The other half of the unspawned-launch compensation. Deferring the row
        says "untouched, waiting to be relaunched", which stops being true the
        moment the launch transaction commits a drop — the queue item is gone
        and, for a tech-lead investigation, an exhausted budget has already been
        escalated to a human. Leaving the row behind lets the startup sweep
        re-admit work that was deliberately dropped, so "permanent" would not be.

        Addressed by ``work_key`` rather than by run: the row that has to go is
        the one holding THIS work, whichever run last deferred it.
        """
        ...

    def refresh_deferred_claim(self, work_key: str, claim: PendingWorkClaim) -> bool:
        """Re-persist a deferred row's payload from the current request (#6999 F4).

        The payload is written when the claim is HELD, before the launch fails,
        so state the queue owner mutates while settling that failure — notably a
        tech-lead item's spent retry budget — is not in it. Without this a
        restart re-admits the request with a refunded budget and relaunches an
        investigation whose retries are already exhausted.

        Returns whether a deferred row was actually rewritten (#6999 F1 round
        2). A launch that never held the claim — the provider refused before the
        hold, or the hold itself failed — has no row here, and the write then
        matches nothing. That is a legitimate outcome, but it is NOT a commit,
        and a caller that treats it as one spends a retry budget nothing
        durable will remember. Unlike :meth:`retire_deferred_claim`, whose
        postcondition ("no deferred row for this work") holds either way, this
        method's postcondition ("the deferred row now says THIS") cannot be
        satisfied by an absent row, so the difference is reported rather than
        swallowed.
        """
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

    def run_key_for_path(self, run_dir: Path) -> str:
        """The same key, from a raw discovered run root.

        Discovery hands back a directory long before typed run assets can be
        rebuilt from it, and a run whose assets fail to parse must still be
        recognised as live (#6999 F14).
        """
        ...

    def quarantine_key_for(self, run: SessionRunAssets) -> str:
        """The opaque key a QUARANTINE against ``run`` is recorded under.

        Distinct from :meth:`run_key_for` because it must survive a replacement
        run reusing the same directory: an escalated marker from a previous
        generation would otherwise suppress the new one's comment and event
        (#6999 F12).
        """
        ...


class NeedsHumanCauseStore(Protocol):
    """Durable provenance for the shared ``needs-human`` block (#6999 F2 r2).

    Two of the block's causes already record themselves durably and elsewhere:
    a tech-lead escalation owns a marker label, and a quarantine owns a row in
    :class:`ClaimQuarantineStore`. Every OTHER orchestrator cause — a session
    that ended without a completion record, publish failures past their bound,
    an invalid completion record, a stuck sweep — simply added the shared label
    and left no trace of having needed it. A remover could then see a label with
    no discoverable owner and conclude it was its own to take off.

    These rows are that missing trace. They are deliberately NOT authoritative
    over the label: the label is, and a human who removes it ends every cause at
    once. A row therefore only ever means "while this label is present, THIS
    lifecycle is one of the reasons for it", and rows are dropped as soon as the
    label goes — by the remover that took it off, or by the first reader that
    finds it already gone. That is what stops a stale row stranding an issue in
    ``needs-human`` forever.

    Kept in the orchestrator-owned database for the same reason the quarantine
    is: it shares that trust boundary and its lifetime, and it must not live
    anywhere an agent can write.
    """

    def record_needs_human_cause(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        """Record that ``cause`` requires the shared block on ``issue_number``.

        Idempotent: a lifecycle that re-asserts the same cause on every tick
        must not accumulate rows.
        """
        ...

    def restart_needs_human_causes(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        """Make ``cause`` the ONLY cause, atomically (#6999 F4 round 5).

        Used when the shared label is absent and is about to be applied again:
        every row from the previous generation of that label is stale by
        definition, and a new cause must not inherit it. Replacing them in ONE
        transaction is what makes that safe under interruption - a separate
        clear-then-record could die in between and leave the new cause recorded
        beside a stale one, which is the very state this prevents.
        """
        ...

    def needs_human_causes(self, issue_number: int) -> frozenset[str]:
        """Every cause currently recorded against ``issue_number``."""
        ...

    def withdraw_needs_human_cause(self, issue_number: int, cause: str) -> None:
        """Drop one cause's row, leaving any others in place."""
        ...

    def clear_needs_human_causes(self, issue_number: int) -> None:
        """Drop every cause for an issue whose shared label is gone."""
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
        cause: QuarantineCause,
        work_kind: PendingWorkKind | None,
    ) -> None:
        """Record this pass's observation of a quarantined run.

        One durable transition, and the ONLY place the cause-change rule lives
        (#6999 F6). Recording a DIFFERENT cause than the row carries clears its
        announcement, because the comment and event a quarantine produces are
        chosen from the cause alone: keeping the flag would leave an operator
        reading a story the orchestrator no longer believes. Recording the SAME
        cause preserves it, so a run re-observed on every 30-second scan is
        announced exactly once.

        A resolved-but-uncleaned row (``releasing``) is revived by this call:
        the cause it was being released for has come back.
        """
        ...

    def read_quarantine(self, quarantine_key: str) -> QuarantineRecord | None:
        """The durable state of one quarantine, or None when there is none."""
        ...

    def record_quarantine_label_state(
        self, quarantine_key: str, label_state: QuarantineLabelState
    ) -> None:
        """Record whether THIS quarantine acquired the shared blocking label."""
        ...

    def mark_quarantine_announced(self, quarantine_key: str) -> None:
        """Record that the operator-visible comment committed."""
        ...

    def mark_quarantine_releasing(self, quarantine_key: str) -> None:
        """Record that the cause is gone but cleanup has not committed yet.

        The row SURVIVES this (#6999 F12): deleting it first would leave a
        failed label removal with nothing to retry from, so the block could
        stay forever.
        """
        ...

    def release_quarantine(self, quarantine_key: str) -> None:
        """Delete a quarantine whose cleanup has committed."""
        ...

    def list_quarantines(self) -> tuple[QuarantineRecord, ...]:
        """Every recorded quarantine, for release reconciliation and retry."""
        ...

    def quarantined_issue_numbers(self) -> frozenset[int]:
        """Issues currently held open by a quarantine whose cause remains.

        Read by every owner that might otherwise remove the shared
        ``needs-human`` label (#6999 F12). A quarantined terminal is
        deliberately absent from ``active_sessions``, so "some session for this
        issue is running" is NOT evidence that the block can be lifted.
        """
        ...


__all__ = [
    "ClaimLookup",
    "ClaimQuarantineStore",
    "ClaimState",
    "QuarantineCause",
    "QuarantineLabelState",
    "QuarantineRecord",
    "ConflictingPendingWorkClaimError",
    "NeedsHumanCauseStore",
    "PendingWorkClaimStore",
    "UnreadableClaim",
    "UnresolvedClaim",
]
