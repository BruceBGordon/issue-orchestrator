"""What a terminal with an unreadable pending-work claim means (#6999 F6/F12).

A terminal whose stored claim cannot be read is the one restoration outcome that
must not be handled quietly. The session may still be alive and doing queued
work nobody can now name: admitting it would let its completion settle as
holding no claim and silently discard that request, and dropping it without a
word would leave a running agent nobody is watching.

This owner exists because that escalation is NOT the tech-lead launch-exhaustion
escalation it originally borrowed (#6999 F12). The two disagree on every rule
that matters:

* provenance — a quarantine can belong to review, rework, validation-retry or
  tech-lead work, so a comment written in the vocabulary of a failure
  investigation is simply wrong for most of them;
* clearing — the tech-lead lifecycle clears its marker as soon as ANY session
  for the issue is active, but a quarantined terminal is deliberately absent
  from ``active_sessions`` while still running, so a healthy sibling session on
  the same issue would silently retract the warning;
* identity — a quarantine belongs to one RUN, not to an issue. Two runs of the
  same issue can be quarantined independently, and re-discovering one every 30
  seconds must not re-comment.

So it keeps its own durable per-run marker, applies its own labels and comment,
and publishes the event only after that durable surface has committed. A failed
apply leaves the quarantine recorded-but-unescalated, which is what makes the
next orphan scan retry instead of treating the failure as final.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from ..events import EventName
from ..ports import EventSink, make_trace_event
from ..ports.pending_work_claim_store import (
    ClaimQuarantineStore,
    QuarantineLabelState,
    UnreadableClaim,
)
from .actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
    SupportsApplyAction,
)
from .in_flight_work import QuarantinedSession
from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class QuarantineLabelOps(Protocol):
    """The blocking-label operations a quarantine needs, with typed outcomes.

    A boolean "did the apply work" cannot express what release depends on
    (#6999 F12/A5): adding a label that is already present SUCCEEDS, so success
    is not evidence this quarantine put it there.
    """

    def acquire_block(self, issue_number: int) -> QuarantineLabelState:
        """Add the blocking label; say whether it was already present."""
        ...

    def release_block(self, issue_number: int) -> bool:
        """Remove the blocking label. False means it did not commit."""
        ...

    def announce(self, issue_number: int, comment: str) -> bool:
        """Post the operator-visible comment. False means it did not commit."""
        ...


@dataclass(frozen=True, slots=True)
class ClaimQuarantineOwner:
    """The one place a run is quarantined for an unreadable claim.

    Escalation and release are a durable state machine, not a pair of one-shot
    calls (#6999 F12). Three facts are persisted separately because they fail
    separately: whether this quarantine ACQUIRED the shared blocking label (as
    opposed to finding it already there), whether the operator comment
    committed, and whether a resolved cause still has cleanup outstanding. The
    row survives every one of those until its own step succeeds, so anything
    that failed is retried by the next sweep rather than becoming final.
    """

    store: ClaimQuarantineStore
    labels: QuarantineLabelOps
    events: EventSink

    def quarantine_session(self, quarantined: QuarantinedSession) -> None:
        """Quarantine a live terminal whose claim could not be read."""
        session = quarantined.session
        logger.error(
            "[WORK] Quarantined %s for issue #%d: its pending-work claim could "
            "not be read (%s). It is NOT being tracked, so its completion "
            "cannot settle as claimless and discard the queued work it holds.",
            session.terminal_id,
            session.issue.number,
            quarantined.error,
        )
        self._escalate(
            quarantine_key=quarantined.quarantine_key,
            run_key=quarantined.run_key,
            session_name=session.terminal_id,
            # Trusted: the launching session's own issue, never parsed out of
            # the terminal name (a review terminal is named for its PR).
            issue_number=session.issue.number,
            error=quarantined.error,
            still_running=True,
        )

    def quarantine_unresolved(self, unreadable: UnreadableClaim) -> None:
        """Quarantine a ledger row whose run is not live and cannot be rebuilt.

        The issue number comes from the ledger row, recorded at hold time from
        the launching session (#6999 F12). Deriving it from the terminal name
        would escalate the PR number for every ``review-*`` claim - and the
        payload, which is the other place it lives, is precisely what has
        become unreadable.
        """
        logger.error(
            "[WORK] Unreadable pending-work claim for run %s (session %s, "
            "issue #%d), and no live terminal is holding it: %s",
            unreadable.run_key,
            unreadable.session_name,
            unreadable.issue_number,
            unreadable.error,
        )
        self._escalate(
            quarantine_key=f"{unreadable.run_key}@{unreadable.started_at}",
            run_key=unreadable.run_key,
            session_name=unreadable.session_name,
            issue_number=unreadable.issue_number,
            error=unreadable.error,
            still_running=False,
        )

    def quarantine_unrestorable(
        self,
        *,
        quarantine_key: str,
        run_key: str,
        session_name: str,
        issue_number: int,
        error: str,
    ) -> None:
        """Quarantine a LIVE run whose session could not be rebuilt (#6999 F14).

        Its claim is perfectly readable; what failed is the run's own assets, so
        the orchestrator cannot track the terminal. Requeueing the work would
        launch a second session beside one that is still running, so the run is
        protected from recovery and surfaced instead.
        """
        logger.error(
            "[WORK] Discovered live run %s (session %s, issue #%d) could not be "
            "restored: %s. Its queued work is NOT being requeued - that would "
            "run it twice - and the terminal is not being tracked.",
            run_key,
            session_name,
            issue_number,
            error,
        )
        self._escalate(
            quarantine_key=quarantine_key,
            run_key=run_key,
            session_name=session_name,
            issue_number=issue_number,
            error=error,
            still_running=True,
        )

    def reconcile_released(self, live_quarantine_keys: frozenset[str]) -> None:
        """Advance every quarantine whose cause is gone, and retry what failed.

        Restoration and the ledger sweep report which quarantines are still
        justified; everything else has had its cause repaired or removed. Rows
        already mid-release are retried here too, which is the whole reason
        they were kept.
        """
        # Quarantines that do not own the label go first. When several share
        # one issue only the label's real owner can take it off, and it has to
        # wait until it is the last one out; releasing the others first lets
        # that happen in this sweep rather than the next one.
        ordered = sorted(
            self.store.list_quarantines(), key=lambda record: record.block_is_ours
        )
        for record in ordered:
            if record.quarantine_key in live_quarantine_keys and not record.releasing:
                continue
            self.release(record.quarantine_key)

    def release(self, quarantine_key: str) -> None:
        """End a quarantine whose cause is gone, and clear what it owns.

        Cleanup order is the point (#6999 F12). The row is first marked
        releasing and KEPT, so a failed label removal is retried by the next
        sweep instead of being lost. The blocking label comes off only when
        this quarantine acquired it and no other quarantine still holds the
        same issue; a ``needs-human`` applied by a human or another owner keeps
        its own provenance. Only after cleanup commits is the row deleted.
        """
        # Every early return below either deletes the row or leaves it
        # ``releasing``, which the sweep retries. There is no path that ends
        # with the obligation dropped and the label still on the issue.
        record = self.store.read_quarantine(quarantine_key)
        if record is None:
            return
        if not record.releasing:
            self.store.mark_quarantine_releasing(quarantine_key)
        if not record.block_is_ours:
            # Nothing of ours on the issue; the row is all there is to remove.
            self.store.release_quarantine(quarantine_key)
            return
        if record.issue_number in self.store.quarantined_issue_numbers():
            # Another quarantine on the same issue is still live, and it is
            # very likely standing on OUR label: the second one to escalate
            # found the label already present and recorded itself PREEXISTING,
            # so it will never take it off. Deleting our row here would strand
            # the block forever. The row stays in ``releasing`` - excluded from
            # the live set above, so it holds nothing open - and the next sweep
            # retries it until we are the last one out.
            return
        if not self.labels.release_block(record.issue_number):
            logger.error(
                "[WORK] Could not clear the quarantine block on issue #%d; "
                "keeping the record so the next sweep retries",
                record.issue_number,
            )
            return
        self.store.release_quarantine(quarantine_key)

    def _escalate(
        self,
        *,
        quarantine_key: str,
        run_key: str,
        session_name: str,
        issue_number: int,
        error: str,
        still_running: bool,
    ) -> None:
        record = self.store.read_quarantine(quarantine_key)
        if record is None:
            self.store.record_quarantine(
                quarantine_key,
                run_key=run_key,
                session_name=session_name,
                issue_number=issue_number,
                error=error,
            )
            record = self.store.read_quarantine(quarantine_key)
            assert record is not None
        elif record.releasing:
            # The cause came back before cleanup finished; it is live again.
            self.store.record_quarantine(
                quarantine_key,
                run_key=run_key,
                session_name=session_name,
                issue_number=issue_number,
                error=error,
            )
        # Applied on EVERY pass, idempotently. The block is shared with owners
        # that lift it when a session for the issue looks active, and a
        # quarantined terminal is deliberately not one of those - so a sweep
        # that found it missing must put it back, whoever it belonged to.
        # Adding a label already present is a no-op, so this costs nothing.
        outcome = self.labels.acquire_block(issue_number)
        if record.block_unrecorded:
            # First time: whatever this apply reports IS the provenance, and a
            # quarantine with no block at all is not escalated at all.
            if not outcome.applied:
                logger.error(
                    "[WORK] Could not apply the quarantine block on issue #%d; "
                    "the next sweep retries",
                    issue_number,
                )
                return
            self.store.record_quarantine_label_state(quarantine_key, outcome)
        if record.announced:
            return
        if not self.labels.announce(
            issue_number, _comment(session_name, error, still_running)
        ):
            # Recorded but NOT announced, so the next sweep retries. The event
            # is deliberately withheld: announcing a quarantine whose durable
            # half never landed would show a warning that vanishes on restart.
            logger.error(
                "[WORK] Durable quarantine escalation did not commit for %s; "
                "leaving it unannounced so the next sweep retries",
                session_name,
            )
            return
        self.store.mark_quarantine_announced(quarantine_key)
        self.events.publish(make_trace_event(
            EventName.SESSION_CLAIM_UNREADABLE,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "run_key": run_key,
                "error": error,
            },
        ))


def _comment(session_name: str, error: str, still_running: bool) -> str:
    running_note = (
        "The terminal may still be running. "
        if still_running
        else "The terminal has already ended. "
    )
    return (
        "🔒 **Session quarantined: its pending-work claim is unreadable**\n\n"
        f"`{session_name}` took a queued request off one of the "
        "orchestrator's pending queues when it launched, and that record can "
        "no longer be read — so which review, rework, validation retry or "
        "tech-lead investigation it is carrying is unknown.\n\n"
        f"{running_note}It is deliberately NOT being tracked: tracking it "
        "would let its completion be recorded as holding no work at all, "
        "silently discarding that request.\n\n"
        f"Error: {error}\n\n"
        "A human needs to work out what this session was doing, re-queue it "
        "if necessary, and stop the terminal."
    )


def build_claim_quarantine_owner(
    *,
    store: ClaimQuarantineStore,
    action_applier: SupportsApplyAction,
    label_manager: LabelManager,
    events: EventSink,
) -> ClaimQuarantineOwner:
    """Assemble the owner from composition-root collaborators.

    The ActionApplier-to-typed-outcome adaptation lives here so both roots wire
    the same behaviour - in particular that adding an already-present label is
    reported as PREEXISTING rather than as a successful acquisition (#6999
    F12).
    """

    class _Labels:
        def acquire_block(self, issue_number: int) -> QuarantineLabelState:
            result = action_applier.apply(
                AddLabelAction(
                    issue_number=issue_number,
                    label=label_manager.needs_human,
                    reason="pending-work claim unreadable",
                )
            )
            if not result.success:
                return QuarantineLabelState.UNKNOWN
            if result.details.get("no_op") or result.details.get("presence_unknown"):
                # Already there, or the applier could not check whether it was.
                # Both mean the same thing to release: not provably ours.
                return QuarantineLabelState.PREEXISTING
            return QuarantineLabelState.ACQUIRED

        def release_block(self, issue_number: int) -> bool:
            return action_applier.apply(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=label_manager.needs_human,
                    reason="pending-work claim quarantine resolved",
                )
            ).success

        def announce(self, issue_number: int, comment: str) -> bool:
            return action_applier.apply(
                AddCommentAction(
                    number=issue_number,
                    comment=comment,
                    reason="pending-work claim unreadable",
                )
            ).success

    return ClaimQuarantineOwner(store=store, labels=_Labels(), events=events)


__all__ = [
    "ClaimQuarantineOwner",
    "QuarantineLabelOps",
    "build_claim_quarantine_owner",
]
