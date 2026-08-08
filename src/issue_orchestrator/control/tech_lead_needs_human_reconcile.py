"""Label-owned needs-human lifecycle for tech_lead launch exhaustion.

The provenance marker and the needs-human label live together on GitHub, the
repository's crash-safe source of truth.  No local clear-obligation store is
needed: every unpaused tick discovers marker-owned state, recovers interrupted
escalations, and idempotently removes an escalation that active work has
superseded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .needs_human_block import (
    NO_OTHER_NEEDS_HUMAN_CAUSES,
    NeedsHumanCause,
    SharedNeedsHumanBlock,
)
from .reconciliation import ExpectedState, ReconciliationRequired

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

ApplyActions = Callable[[list[Action], str], bool]
ReadLabels = Callable[[int], list[str]]
DiscoverMarkedIssueNumbers = Callable[[], Sequence[int]]

# Marker discovery is label-scoped. One maximum-size GitHub page is exhaustive
# in practice and, unlike the ordinary configurable work-fetch limit, cannot be
# reduced until an older recovery marker is silently excluded.
TECH_LEAD_NEEDS_HUMAN_DISCOVERY_LIMIT = 100


def discover_tech_lead_needs_human_issue_numbers(
    repository_host: "RepositoryHost", marker: str
) -> list[int]:
    """Target the durable marker label without a repository-wide scan."""
    issues = repository_host.list_issues(
        labels=[marker],
        state="open",
        limit=TECH_LEAD_NEEDS_HUMAN_DISCOVERY_LIMIT,
    )
    return [issue.number for issue in issues]


class TechLeadNeedsHumanLifecycle:
    """Own tech_lead needs-human escalation and stale-label reconciliation."""

    def __init__(
        self,
        *,
        labels: "LabelManager",
        events: EventSink,
        read_labels: ReadLabels,
        discover_marked_issue_numbers: DiscoverMarkedIssueNumbers,
        apply_actions: ApplyActions,
        needs_human_block: SharedNeedsHumanBlock = NO_OTHER_NEEDS_HUMAN_CAUSES,
    ) -> None:
        self._labels = labels
        self._events = events
        self._read_labels = read_labels
        self._discover_marked_issue_numbers = discover_marked_issue_numbers
        self._apply_actions = apply_actions
        # Every OTHER durable cause of the shared needs-human label (#6999
        # F12/F4). It answers two questions this lifecycle cannot answer from
        # its own marker: whether a bare label it did not apply is human intent
        # or another owner's block, and whether clearing its own escalation may
        # take the shared label with it. An unreadable-claim quarantine keeps
        # its terminal OUT of active_sessions on purpose, so "a session for this
        # issue is active" is not evidence its block can be lifted.
        self._block = needs_human_block

    def _expected(
        self,
        *,
        required: set[str] | None = None,
        forbidden: set[str] | None = None,
    ) -> ExpectedState:
        """Guard an owned mutation with marker-provenance invariants.

        Every lifecycle mutation is optimistic: the applier re-reads live
        labels and refuses the write (``ReconciliationRequired``) unless they
        still satisfy this expected state.  The reconcile pause label is always
        forbidden so a paused issue fails closed, and it is resolved through
        ``LabelManager`` rather than hard-coded so the configured prefix is
        honored.
        """
        forbidden_labels = {self._labels.needs_reconcile}
        if forbidden:
            forbidden_labels |= forbidden
        return ExpectedState.with_labels(
            required=required or set(),
            forbidden=forbidden_labels,
        )

    def _apply_guarded(self, actions: list[Action], context: str) -> bool:
        """Apply owned mutations, treating expected-state drift as retryable.

        A ``ReconciliationRequired`` means the labels changed between our read
        and the write — a human cleared needs-human, another path paused the
        issue, or the marker vanished.  We must not force the mutation past the
        stale assumption: skip it and let the next idempotent tick re-evaluate
        against fresh state.  Returning ``False`` matches the "did not apply"
        signal callers already handle, so the escalation withholds its event
        and the reconcile keeps its provenance marker.
        """
        try:
            return self._apply_actions(actions, context)
        except ReconciliationRequired as drift:
            logger.info(
                "[TECH_LEAD] %s skipped: labels drifted since read (%s); "
                "retrying next tick",
                context,
                drift.reason,
            )
            return False

    def escalate(
        self,
        *,
        issue_number: int,
        reason: str,
        comment: str,
        context: str,
        event_data: dict[str, object],
    ) -> bool:
        """Apply provenance, blocking state, and explanation in safe order.

        The marker commits first.  Therefore a needs-human label created by this
        workflow can never exist without durable ownership provenance.  A
        marker-only crash is recovered by :meth:`reconcile`, independently of
        the in-memory tech_lead queue.  A bare needs-human label that belongs to
        nobody else is respected as human/session-owned and is never
        retroactively claimed.

        "Nobody else's" is asked of the shared-block owner rather than inferred
        from the label alone (#6999 F4).  A label another ORCHESTRATOR cause
        applied — a quarantined pending-work claim — is not human intent, and
        reading it as such left this escalation with no provenance at all: when
        the quarantine resolved it took the shared label with it, and an issue a
        human had been told to look at went quietly back on the board with
        neither the block nor the marker :meth:`reconcile` recovers from.  So
        the marker is recorded in that case too; only genuine human/session
        intent suppresses it.

        Each step also carries an expected-state guard so a concurrent human or
        orchestrator path that clears or pauses the escalation between our steps
        stops the sequence instead of stamping needs-human or a comment onto
        state that no longer holds: the marker add refuses a paused issue, the
        needs-human add requires the marker to still be present, and the
        durable comment/event requires the blocking label and, for state this
        lifecycle owns, its marker.
        """
        try:
            current = set(self._read_labels(issue_number))
        except Exception:
            logger.exception(
                "[TECH_LEAD] Failed to read fresh labels before escalating issue #%d; "
                "will retry next tick",
                issue_number,
            )
            return False

        marker_present = self._labels.tech_lead_needs_human in current
        needs_human_present = self._labels.needs_human in current
        # Only a block nobody else accounts for is human intent (#6999 F4).
        human_owned_block = needs_human_present and not self._block.held_by_another_cause(
            issue_number, excluding=NeedsHumanCause.TECH_LEAD_ESCALATION
        )

        if not marker_present and not human_owned_block:
            # The label is forbidden only when this escalation is also the one
            # putting it there. When another orchestrator cause already holds
            # it, requiring its absence would refuse the very marker that keeps
            # this escalation recoverable after that cause resolves.
            forbidden = {self._labels.tech_lead_needs_human}
            if not needs_human_present:
                forbidden.add(self._labels.needs_human)
            marker = AddLabelAction(
                issue_number=issue_number,
                label=self._labels.tech_lead_needs_human,
                reason=reason,
                expected=self._expected(forbidden=forbidden),
            )
            if not self._apply_guarded([marker], context):
                return False
            marker_present = True

        if marker_present and not needs_human_present:
            needs_human = AddLabelAction(
                issue_number=issue_number,
                label=self._labels.needs_human,
                reason=reason,
                needs_human_cause=NeedsHumanCause.TECH_LEAD_ESCALATION,
                expected=self._expected(
                    required={self._labels.tech_lead_needs_human},
                    forbidden={self._labels.needs_human},
                ),
            )
            if not self._apply_guarded([needs_human], context):
                return False

        note_required = {self._labels.needs_human}
        note_forbidden = {self._labels.tech_lead_needs_human}
        if marker_present:
            note_required.add(self._labels.tech_lead_needs_human)
            note_forbidden.clear()
        note = AddCommentAction(
            number=issue_number,
            comment=comment,
            reason=reason,
            expected=self._expected(
                required=note_required,
                forbidden=note_forbidden,
            ),
        )
        if not self._apply_guarded([note], context):
            return False

        self._events.publish(
            make_trace_event(EventName.ISSUE_NEEDS_HUMAN, dict(event_data))
        )
        return True

    def reconcile(
        self,
        active_sessions: Sequence["Session"],
        *,
        discover_markers: bool = True,
    ) -> None:
        """Recover or clear marker-owned escalations from durable labels.

        Only the marker proves ownership.  A bare needs-human label is operator-
        or session-owned and is never touched.  Reads bypass caches because a
        stale label observation could remove legitimate human intent.  Marker
        discovery is targeted and lets a fresh process recover a marker-only
        escalation even when its queue and active-session state were lost.
        """
        active_issue_numbers = {session.issue.number for session in active_sessions}
        marked_issue_numbers: set[int] = set()
        if discover_markers:
            try:
                marked_issue_numbers = set(self._discover_marked_issue_numbers())
            except Exception:
                logger.exception(
                    "[TECH_LEAD] Failed to discover tech_lead needs-human markers; "
                    "will retry next tick"
                )

        issue_numbers = sorted(active_issue_numbers | marked_issue_numbers)
        for issue_number in issue_numbers:
            try:
                current = set(self._read_labels(issue_number))
            except Exception:
                logger.exception(
                    "[TECH_LEAD] Failed to read fresh labels while reconciling issue #%d; "
                    "will retry next tick",
                    issue_number,
                )
                continue

            marker_present = self._labels.tech_lead_needs_human in current
            if not marker_present:
                continue

            if issue_number not in active_issue_numbers:
                self._recover_interrupted_escalation(issue_number, current)
                continue

            if self._block.held_by_another_cause(
                issue_number, excluding=NeedsHumanCause.TECH_LEAD_ESCALATION
            ):
                logger.info(
                    "[TECH_LEAD] Leaving needs-human on issue #%d: another "
                    "durable cause still requires the shared block",
                    issue_number,
                )
                continue

            self._clear_superseded_escalation(issue_number, current)

    def _recover_interrupted_escalation(
        self, issue_number: int, current: set[str]
    ) -> None:
        """Make a stranded marker-only escalation visibly blocking again."""
        needs_human_present = self._labels.needs_human in current
        if needs_human_present:
            return

        recovered = self._apply_guarded(
            [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._labels.needs_human,
                    reason=(
                        "recover interrupted tech_lead needs-human escalation "
                        "from durable marker"
                    ),
                    needs_human_cause=NeedsHumanCause.TECH_LEAD_ESCALATION,
                    expected=self._expected(
                        required={self._labels.tech_lead_needs_human},
                        forbidden={self._labels.needs_human},
                    ),
                )
            ],
            "tech_lead_recover_needs_human",
        )
        if not recovered:
            return

        explained = self._apply_guarded(
            [
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "The orchestrator recovered an interrupted tech_lead "
                        "failure-investigation escalation from its durable "
                        "marker. Human review is required; the original "
                        "in-memory failure context was lost during restart."
                    ),
                    reason="explain recovered tech_lead needs-human escalation",
                    expected=self._expected(
                        required={
                            self._labels.tech_lead_needs_human,
                            self._labels.needs_human,
                        }
                    ),
                )
            ],
            "tech_lead_recover_needs_human_comment",
        )
        if not explained:
            return

        self._events.publish(
            make_trace_event(
                EventName.ISSUE_NEEDS_HUMAN,
                {
                    "issue_number": issue_number,
                    "reason": "recovered interrupted tech_lead escalation",
                },
            )
        )

    def _clear_superseded_escalation(
        self, issue_number: int, current: set[str]
    ) -> None:
        """Clear marker-owned state after active/restored work supersedes it."""

        # Keep provenance until the blocking label is definitely gone.  If
        # removal fails, the marker survives and the next tick retries.  Both
        # removals are guarded because the fresh read above is only a hint.
        needs_human_present = self._labels.needs_human in current
        if needs_human_present:
            cleared = self._apply_guarded(
                [
                    RemoveLabelAction(
                        issue_number=issue_number,
                        label=self._labels.needs_human,
                        reason=(
                            "running tech_lead investigation supersedes "
                            "orchestrator-owned needs-human escalation"
                        ),
                        needs_human_cause=NeedsHumanCause.TECH_LEAD_ESCALATION,
                        expected=self._expected(
                            required={
                                self._labels.tech_lead_needs_human,
                                self._labels.needs_human,
                            }
                        ),
                    )
                ],
                "tech_lead_reconcile_needs_human",
            )
            if not cleared:
                return

        self._apply_guarded(
            [
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._labels.tech_lead_needs_human,
                    reason="tech_lead needs-human escalation no longer active",
                    expected=self._expected(
                        required={self._labels.tech_lead_needs_human},
                        forbidden={self._labels.needs_human},
                    ),
                )
            ],
            "tech_lead_reconcile_needs_human_marker",
        )
