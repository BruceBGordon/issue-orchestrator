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
from typing import TYPE_CHECKING

from ..events import EventName
from ..ports import EventSink, make_trace_event
from ..ports.pending_work_claim_store import ClaimQuarantineStore, UnreadableClaim
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .in_flight_work import QuarantinedSession
from .label_manager import LabelManager
from .session_rework_launcher import ActionApplierFn

if TYPE_CHECKING:
    from .action_applier import ActionApplier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimQuarantineOwner:
    """The one place a run is quarantined for an unreadable claim."""

    store: ClaimQuarantineStore
    apply_actions: ActionApplierFn
    label_manager: LabelManager
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
        issue_number = unreadable.issue_number
        logger.error(
            "[WORK] Unreadable pending-work claim for run %s (session %s, "
            "issue #%d), and no live terminal is holding it: %s",
            unreadable.run_key,
            unreadable.session_name,
            issue_number,
            unreadable.error,
        )
        self._escalate(
            quarantine_key=f"{unreadable.run_key}@{unreadable.started_at}",
            run_key=unreadable.run_key,
            session_name=unreadable.session_name,
            issue_number=issue_number,
            error=unreadable.error,
            still_running=False,
        )

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
        if self.store.is_quarantine_escalated(quarantine_key):
            # Already told a human about THIS run: the orphan scan rediscovers
            # an untracked terminal every 30 seconds and must not re-comment.
            # The LABEL is still reasserted, because it is shared with owners
            # that remove it when any session for the issue looks active - and
            # a quarantined terminal is deliberately not one of those (F12).
            self._reassert_block(issue_number)
            return
        self.store.record_quarantine(
            quarantine_key,
            run_key=run_key,
            session_name=session_name,
            issue_number=issue_number,
            error=error,
        )
        if not self.apply_actions(
            self._actions(session_name, issue_number, error, still_running),
            context="pending_work_claim_quarantine",
        ):
            # Recorded but NOT escalated, so the next sweep retries. The event
            # is deliberately withheld: announcing a quarantine whose durable
            # half never landed would show a warning that vanishes on restart.
            logger.error(
                "[WORK] Durable quarantine escalation did not commit for %s; "
                "leaving it unescalated so the next sweep retries",
                session_name,
            )
            return
        self.store.mark_quarantine_escalated(quarantine_key)
        self.events.publish(make_trace_event(
            EventName.SESSION_CLAIM_UNREADABLE,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "run_key": run_key,
                "error": error,
            },
        ))

    def release(self, quarantine_key: str) -> None:
        """End a quarantine whose cause is gone, and clear what it owns.

        The explicit clear (#6999 F12): a quarantine ends when the run's claim
        can be read again or its row is gone - a human having repaired or
        removed it - never because some other session for the issue happened to
        start. Called by restoration and by the ledger sweep, so a repaired
        claim does not leave a marker holding its issue open forever.

        The blocking label is removed only when this owner put it there and no
        OTHER quarantine still holds the same issue. A ``needs-human`` applied
        by anything else keeps its own provenance and is left alone.
        """
        issue_number = self.store.quarantine_issue_number(quarantine_key)
        was_escalated = self.store.is_quarantine_escalated(quarantine_key)
        self.store.release_quarantine(quarantine_key)
        if issue_number is None or not was_escalated:
            return
        if issue_number in self.store.quarantined_issue_numbers():
            # Another run of the same issue is still quarantined; its block
            # stands on its own provenance.
            return
        self.apply_actions(
            [
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self.label_manager.needs_human,
                    reason="pending-work claim quarantine resolved",
                )
            ],
            context="pending_work_claim_quarantine_release",
        )

    def reconcile_released(self, live_quarantine_keys: frozenset[str]) -> None:
        """Release every quarantine whose run is no longer quarantined.

        Restoration and the ledger sweep report which runs are STILL in
        trouble; anything recorded but absent from that set has had its cause
        repaired or removed, so the marker and the block it owns come off.
        """
        for run_key in self.store.quarantined_run_keys():
            for key in self._keys_for_run(run_key):
                if key not in live_quarantine_keys:
                    self.release(key)

    def _keys_for_run(self, run_key: str) -> tuple[str, ...]:
        """Quarantine keys recorded against ``run_key`` (one per generation)."""
        return self.store.quarantine_keys_for_run(run_key)

    def _reassert_block(self, issue_number: int) -> None:
        """Re-apply the shared blocking label, idempotently.

        Adding a label that is already present is a no-op at the applier, so
        this costs nothing in the common case and repairs the case that
        matters: another owner removed it while this quarantine was still open.
        """
        self.apply_actions(
            [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self.label_manager.needs_human,
                    reason="pending-work claim still unreadable",
                )
            ],
            context="pending_work_claim_quarantine_reassert",
        )

    def _actions(
        self, session_name: str, issue_number: int, error: str, still_running: bool
    ) -> list[Action]:
        running_note = (
            "The terminal may still be running. "
            if still_running
            else "The terminal has already ended. "
        )
        return [
            AddLabelAction(
                issue_number=issue_number,
                label=self.label_manager.needs_human,
                reason="pending-work claim unreadable",
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    "🔒 **Session quarantined: its pending-work claim is "
                    "unreadable**\n\n"
                    f"`{session_name}` took a queued request off one of the "
                    "orchestrator's pending queues when it launched, and that "
                    "record can no longer be read — so which review, rework, "
                    "validation retry or tech-lead investigation it is carrying "
                    "is unknown.\n\n"
                    f"{running_note}It is deliberately NOT being tracked: "
                    "tracking it would let its completion be recorded as "
                    "holding no work at all, silently discarding that "
                    "request.\n\n"
                    f"Error: {error}\n\n"
                    "A human needs to work out what this session was doing, "
                    "re-queue it if necessary, and stop the terminal."
                ),
                reason="pending-work claim unreadable",
            ),
        ]


def build_claim_quarantine_owner(
    *,
    store: ClaimQuarantineStore,
    action_applier: "ActionApplier",
    label_manager: LabelManager,
    events: EventSink,
) -> ClaimQuarantineOwner:
    """Assemble the owner from composition-root collaborators.

    The ActionApplier-to-``ActionApplierFn`` adaptation lives here rather than
    at each composition site so both roots wire the same behaviour, including
    what "the durable surface committed" means.
    """

    def _apply(actions: list[Action], *, context: str) -> bool:
        del context  # the owner logs its own failure context
        return all(action_applier.apply(action).success for action in actions)

    return ClaimQuarantineOwner(
        store=store,
        apply_actions=_apply,
        label_manager=label_manager,
        events=events,
    )


__all__ = ["ClaimQuarantineOwner", "build_claim_quarantine_owner"]
