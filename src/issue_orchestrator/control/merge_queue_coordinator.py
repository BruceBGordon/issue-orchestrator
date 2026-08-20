"""Single owner of GitHub Merge Queue policy.

When ``merge_queue.enabled`` is true, this coordinator owns every final
merge-readiness decision for a reviewer-approved PR: eligibility, the
stale/conflict classification, the enqueue decision, and failure routing.
Keeping that policy in one place is the point of the feature — stale checks,
review gates, and queue outcomes must not drift across planner/review/tech_lead
code paths.

Layering note: the coordinator runs inside the awaiting-merge *discovery*
phase, so it only ever *reads* from the repository host (the queue entry) and
*emits observational events*. It never mutates the queue here — the actual
``enqueuePullRequest`` mutation is carried as a ``DiscoveredMergeQueueEnqueue``
fact through the planner into the ActionApplier, honoring the
Observer -> Planner -> ActionApplier contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..domain.models import (
    DiscoveredAwaitingMergeEscalation,
    DiscoveredMergeQueueEnqueue,
    DiscoveredRework,
)
from ..events import EventName
from ..ports import make_trace_event
from ..ports.pull_request_tracker import MergeQueueRead
from ..ports.repository_host import (
    RepositoryHostError,
    RepositoryScanIncompleteError,
)
from .awaiting_merge_post_publish_policy import (
    POST_PUBLISH_VALIDATION_COMMENT_MARKER,
    POST_PUBLISH_VALIDATION_SOURCE,
    build_escalation,
    build_rework_feedback,
    classify_post_approval_state,
    next_rework_cycle,
)

if TYPE_CHECKING:
    from ..events import EventContext
    from ..infra.config_models import MergeQueueConfig
    from ..ports import EventSink
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import MergeQueueEntry, PRInfo
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager


logger = logging.getLogger(__name__)


# What the coordinator wants to do with an approved PR in merge-queue mode.
# Pure output of `decide_merge_queue_action`, so the dispatch matrix is
# exhaustively unit-testable without any I/O.
MergeQueueDecision = Literal[
    "ENQUEUE",              # eligible & not yet queued → enqueue (behind-base IS eligible)
    "WAIT",                 # already in the queue, or mergeability not yet known
    "REWORK_CONFLICT",      # merge conflict; the queue cannot resolve this
    "REWORK_CHECK_FAILED",  # a required PR-head check failed
    "ROUTE_FAILURE",        # the queue rejected the PR (UNMERGEABLE)
]


def decide_merge_queue_action(
    pr: "PRInfo", entry: "MergeQueueEntry | None"
) -> MergeQueueDecision:
    """Decide the merge-queue action for an approved PR (pure).

    ``entry`` is the PR's current merge queue entry (``None`` when not queued).
    The PR's ``status_check_rollup`` must already be resolved when ``entry`` is
    ``None`` (the caller reads it only for not-yet-queued PRs).
    """
    if entry is not None:
        if entry.is_failed:
            return "ROUTE_FAILURE"
        # QUEUED / AWAITING_CHECKS / MERGEABLE / PENDING / LOCKED → observe.
        return "WAIT"

    base = classify_post_approval_state(pr)
    # Behind-base and the merge-queue-required "blocked + checks green" state are
    # both enqueue-eligible: GitHub validates the merge group, so we do NOT send
    # them to rework just for being behind.
    if base in ("READY", "REWORK_BEHIND", "BLOCKED_TERMINAL"):
        return "ENQUEUE"
    if base == "REWORK_CONFLICT":
        return "REWORK_CONFLICT"
    if base == "REWORK_CHECK_FAILED":
        return "REWORK_CHECK_FAILED"
    # WAIT_FOR_CHECKS / UNKNOWN → mergeability not yet known: wait/retry.
    return "WAIT"


@dataclass(frozen=True)
class MergeQueueFollowup:
    """Facts the coordinator produced for one approved PR.

    At most one of the three is set; all ``None`` means "wait / observe next
    tick". The reconciler folds these into the existing discovery result so the
    planner and applier handle them through the normal pipeline.
    """

    enqueue: DiscoveredMergeQueueEnqueue | None = None
    rework: DiscoveredRework | None = None
    escalation: DiscoveredAwaitingMergeEscalation | None = None


@dataclass
class MergeQueueCoordinator:
    """Owns merge-queue eligibility, decisions, and failure routing."""

    config: "MergeQueueConfig"
    repository_host: "RepositoryHost"
    events: "EventSink"
    event_context: "EventContext"
    label_manager: "LabelManager"

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def gate_label(self) -> str:
        """The PR label required before a PR may be enqueued.

        ``enqueue_after`` selects the approval gate, and each gate resolves —
        via the label owner — to its own PR-readiness label. ``code-reviewed``
        (reviewer approval) and ``tech-lead-reviewed`` (batch tech_lead) are distinct
        lifecycle gates and must not collapse onto one label: a repo configured
        to wait for tech_lead must not enqueue on code review alone. The tech_lead
        gate intentionally uses the unprefixed label because the tech_lead
        subsystem applies it raw (see ``LabelManager.tech_lead_reviewed``).
        """
        if self.config.enqueue_after == "tech-lead-reviewed":
            return self.label_manager.tech_lead_reviewed
        assert self.config.enqueue_after == "code-reviewed"
        return self.label_manager.code_reviewed

    def read_entry(self, pr_number: int) -> "MergeQueueRead":
        """Read the PR's merge queue entry as a typed three-valued result.

        A transient read failure (``RepositoryHostError``) becomes
        ``INDETERMINATE`` — never ``ABSENT`` — so an unreadable queue cannot be
        mistaken for "not enqueued" and drive an enqueue/rework/escalation
        decision off stale PR status. An unmodeled provider state is already
        ``INDETERMINATE`` from the adapter.
        """
        try:
            return self.repository_host.read_merge_queue_entry(pr_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Merge queue entry unreadable for PR #%d (treating as "
                "indeterminate, no action): %s",
                pr_number,
                exc,
            )
            return MergeQueueRead.indeterminate()

    def classify(
        self,
        *,
        pr: "PRInfo",
        issue: "Issue",
        issue_number: int,
        pr_number: int,
        entry: "MergeQueueEntry | None",
    ) -> MergeQueueFollowup:
        """Decide and build the follow-up fact for one approved PR."""
        # The coordinator owns eligibility end to end: a PR that has not cleared
        # the configured gate is never touched, even if a caller reaches here.
        if entry is None and self.gate_label() not in pr.labels:
            return MergeQueueFollowup()
        decision = decide_merge_queue_action(pr, entry)
        logger.debug(
            "Merge-queue classify: issue=#%d pr=#%d state=%s rollup=%s "
            "entry=%s decision=%s",
            issue_number,
            pr_number,
            pr.mergeable_state,
            pr.status_check_rollup,
            entry.state if entry is not None else None,
            decision,
        )
        if decision == "ENQUEUE":
            return MergeQueueFollowup(
                enqueue=DiscoveredMergeQueueEnqueue(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    pr_url=pr.url,
                    issue_key=issue.key.stable_id(),
                )
            )
        if decision in ("REWORK_CONFLICT", "REWORK_CHECK_FAILED"):
            return MergeQueueFollowup(
                rework=self._build_rework(
                    pr=pr,
                    issue=issue,
                    issue_number=issue_number,
                    pr_number=pr_number,
                    feedback=build_rework_feedback(pr, decision),
                )
            )
        if decision == "ROUTE_FAILURE":
            return self._route_failure(
                pr=pr, issue=issue, issue_number=issue_number, pr_number=pr_number
            )
        # WAIT
        return MergeQueueFollowup()

    # ------------------------------------------------------------------ #

    def _route_failure(
        self,
        *,
        pr: "PRInfo",
        issue: "Issue",
        issue_number: int,
        pr_number: int,
    ) -> MergeQueueFollowup:
        """Route a queue-rejected PR per the configured failure policy."""
        reason = (
            "GitHub's merge queue could not merge this PR (entry became "
            "UNMERGEABLE). GitHub removes failed entries from the queue; the "
            "orchestrator is taking it off the merge-queue happy path."
        )
        self.events.publish(make_trace_event(
            EventName.MERGE_QUEUE_FAILED,
            self.event_context.enrich({
                "issue_number": issue_number,
                "issue_key": issue.key.stable_id(),
                "pr_number": pr_number,
                "pr_url": pr.url,
                "failure_action": self.config.failure_action,
            }),
        ))
        if self.config.failure_action == "needs_human":
            return MergeQueueFollowup(
                escalation=build_escalation(
                    pr=pr,
                    issue_number=issue_number,
                    issue_key=issue.key.stable_id(),
                    pr_number=pr_number,
                    label_manager=self.label_manager,
                    kind="merge_queue_failed",
                    reason=reason,
                )
            )
        # failure_action == "rework"
        feedback = self._merge_queue_failure_feedback(pr, reason)
        return MergeQueueFollowup(
            rework=self._build_rework(
                pr=pr,
                issue=issue,
                issue_number=issue_number,
                pr_number=pr_number,
                feedback=feedback,
            )
        )

    def _build_rework(
        self,
        *,
        pr: "PRInfo",
        issue: "Issue",
        issue_number: int,
        pr_number: int,
        feedback: str,
    ) -> DiscoveredRework:
        assert issue.agent_type is not None
        return DiscoveredRework(
            issue_number=issue_number,
            pr_number=pr_number,
            branch_name=pr.branch,
            agent_type=issue.agent_type,
            rework_cycle=next_rework_cycle(pr.labels, self.label_manager),
            source=POST_PUBLISH_VALIDATION_SOURCE,
            feedback=feedback,
            feedback_comment_already_posted=self._comment_marker_present(pr_number),
        )

    def _comment_marker_present(self, pr_number: int) -> bool:
        """Read-only dedupe guard, mirroring the post-publish rework path.

        A scan that could not prove completeness must NOT become "marker
        absent". This is a negative-existence question, so an unprovable answer
        is indistinguishable from "I saw everything and it wasn't there" — and
        answering False posts a duplicate comment, exactly what the adapter
        fails loud to prevent. Assume PRESENT instead: the cost of a missed
        comment is a delay, the cost of a wrong one is spam a human must clean
        up. A reachability failure (outage) stays False — no scan was even
        attempted, and the next tick retries.
        """
        try:
            return self.repository_host.issue_comment_marker_present(
                pr_number, POST_PUBLISH_VALIDATION_COMMENT_MARKER
            )
        except RepositoryScanIncompleteError as exc:
            logger.warning(
                "Comment scan for merge-queue PR #%d could not prove "
                "completeness (%s); assuming the marker IS present so this "
                "tick cannot post a duplicate.",
                pr_number,
                exc,
            )
            return True
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to read comments for merge-queue PR #%d: %s", pr_number, exc
            )
            return False

    @staticmethod
    def _merge_queue_failure_feedback(pr: "PRInfo", reason: str) -> str:
        return "\n".join([
            "Merge queue rejected this PR (handled by the merge queue "
            "coordinator, not the reviewer):",
            "",
            f"PR #{pr.number} was approved and entered GitHub's merge queue, "
            "but the merge group could not be merged.",
            f"- URL: {pr.url}",
            f"- Branch: {pr.branch}",
            f"- Diagnosis: {reason}",
            "",
            "Re-validate against the latest base branch, fix whatever broke in "
            "the merge group (conflicts, failing required checks), and push so "
            "the PR can re-enter the queue.",
        ])
