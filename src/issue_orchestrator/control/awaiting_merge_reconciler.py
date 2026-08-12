"""Reconcile history-backed awaiting-merge entries with repository state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal
from urllib.parse import urlparse

from ..domain.models import (
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeEscalation,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredRework,
    RECONCILABLE_HISTORY_STATUSES,
    TERMINAL_AWAITING_MERGE_HISTORY_STATUSES,
)
from ..history import latest_history_entries_by_issue
from ..ports.repository_host import RepositoryHostError
from .awaiting_merge_drift_policy import classify_pr_set_drift
from .close_on_merge import (
    pr_terminal_reason,
    reconciliation_fact,
    should_close_merged_issue,
)
from .awaiting_merge_post_publish_policy import (
    POST_PUBLISH_VALIDATION_COMMENT_MARKER,
    POST_PUBLISH_VALIDATION_SOURCE,
    REWORK_ACTIONS,
    PostApprovalAction,
    build_escalation,
    build_rework_feedback,
    classify_post_approval_state,
    next_rework_cycle,
    normalized_state,
)
from .queue_cache import record_issue_refreshes
from .status_rollup_gate import (
    STATUS_ROLLUP_PERMISSION_BACKOFF_SECONDS,
    StatusRollupGate,
    rollup_is_decisive,
)

if TYPE_CHECKING:
    from ..domain.models import (
        DiscoveredMergeQueueEnqueue,
        OrchestratorState,
        SessionHistoryEntry,
    )
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost
    from .dependency_evaluator import DependencyEvaluator
    from .label_manager import LabelManager
    from .merge_queue_coordinator import MergeQueueCoordinator


logger = logging.getLogger(__name__)

AWAITING_MERGE_HISTORY_LIMIT = 50
AWAITING_MERGE_LABEL_DRIFT_SCAN_INTERVAL_SECONDS = 300.0
AWAITING_MERGE_ROLLUP_SCAN_INTERVAL_SECONDS = 300.0

ReconciliationOutcome = Literal["terminal", "still_pending", "skipped"]


@dataclass(frozen=True)
class AwaitingMergeEntryDiscovery:
    """Discovery result for one awaiting-merge history entry."""

    outcome: ReconciliationOutcome
    reconciliation: DiscoveredAwaitingMergeReconciliation | None = None
    drift: DiscoveredAwaitingMergeDrift | None = None
    rework: DiscoveredRework | None = None
    escalation: DiscoveredAwaitingMergeEscalation | None = None
    enqueue: "DiscoveredMergeQueueEnqueue | None" = None


@dataclass(frozen=True)
class AwaitingMergeReconciliationResult:
    """Summary of awaiting-merge reconciliation discovery work."""

    checked: int = 0
    discovered: int = 0
    drift_discovered: int = 0
    rework_discovered: int = 0
    escalation_discovered: int = 0
    enqueue_discovered: int = 0
    still_pending: int = 0
    skipped: int = 0
    reconciliations: tuple[DiscoveredAwaitingMergeReconciliation, ...] = ()
    drifts: tuple[DiscoveredAwaitingMergeDrift, ...] = ()
    reworks: tuple[DiscoveredRework, ...] = ()
    escalations: tuple[DiscoveredAwaitingMergeEscalation, ...] = ()
    enqueues: tuple["DiscoveredMergeQueueEnqueue", ...] = ()


DEFAULT_POST_PUBLISH_CHECKS_PENDING_TIMEOUT_SECONDS = 1800.0


@dataclass
class AwaitingMergeReconciler:
    """Discovers lifecycle cleanup facts for history-backed awaiting-merge cards."""

    repository_host: RepositoryHost
    label_manager: LabelManager | None = None
    clock: Callable[[], float] = time.time
    history_limit: int = AWAITING_MERGE_HISTORY_LIMIT
    label_drift_scan_interval_seconds: float = (
        AWAITING_MERGE_LABEL_DRIFT_SCAN_INTERVAL_SECONDS
    )
    rollup_scan_interval_seconds: float = AWAITING_MERGE_ROLLUP_SCAN_INTERVAL_SECONDS
    # Wall-clock budget for WAIT_FOR_CHECKS before escalating. Default
    # mirrors Config.post_publish_checks_pending_timeout_seconds; callers
    # in production wire the configured value through.
    post_publish_checks_pending_timeout_seconds: float = (
        DEFAULT_POST_PUBLISH_CHECKS_PENDING_TIMEOUT_SECONDS
    )
    repo: str | None = None
    status_rollup_backoff_seconds: float = STATUS_ROLLUP_PERMISSION_BACKOFF_SECONDS
    # Optional merge queue owner. When set and enabled, it takes over the
    # post-approval merge-readiness decision (enqueue/observe/conflict-rework/
    # failure-routing) instead of the default rework/escalation dispatch.
    merge_queue: "MergeQueueCoordinator | None" = None
    # Optional stack-policy owner (ADR-0029 / #6596). When set, a stacked
    # successor whose merge gate is blocked (predecessors not merged in order)
    # is held out of the post-approval merge-readiness dispatch entirely, so it
    # is never enqueued or treated as silently mergeable ahead of its
    # predecessors. Non-stack issues are never affected.
    dependency_evaluator: "DependencyEvaluator | None" = None

    def _rollup_gate(self) -> StatusRollupGate:
        return StatusRollupGate(
            self.repository_host,
            repo=self.repo,
            clock=self.clock,
            backoff_seconds=self.status_rollup_backoff_seconds,
        )

    def discover(self, state: OrchestratorState) -> AwaitingMergeReconciliationResult:
        """Discover completed history entries that should become terminal."""
        checked = 0
        discovered = 0
        drift_discovered = 0
        rework_discovered = 0
        escalation_discovered = 0
        enqueue_discovered = 0
        still_pending = 0
        skipped = 0
        reconciliations: list[DiscoveredAwaitingMergeReconciliation] = []
        drifts: list[DiscoveredAwaitingMergeDrift] = []
        reworks: list[DiscoveredRework] = []
        escalations: list[DiscoveredAwaitingMergeEscalation] = []
        enqueues: list["DiscoveredMergeQueueEnqueue"] = []
        pending_issue_numbers: set[int] = set()
        terminal_issue_numbers: set[int] = set()

        candidates = self._awaiting_merge_entries(state)
        logger.debug(
            "Awaiting-merge scan: %d candidate entries (history_limit=%d)",
            len(candidates),
            self.history_limit,
        )
        for entry in candidates:
            checked += 1
            discovery = self._discover_entry(state, entry)
            if discovery.outcome == "terminal":
                discovered += 1
                # A terminally-reconciled issue (merged or closed) already has
                # its labels decided by the reconciliation; exclude it from the
                # label-drift scan so a stale cached `pr-pending` plus an older
                # closed-unmerged PR cannot manufacture a contradictory
                # `blocked:pr-closed` drift for the same issue in one pass.
                terminal_issue_numbers.add(entry.issue_number)
                if discovery.reconciliation is not None:
                    reconciliations.append(discovery.reconciliation)
            elif discovery.outcome == "still_pending":
                still_pending += 1
                pending_issue_numbers.add(entry.issue_number)
            else:
                skipped += 1
            if discovery.drift is not None:
                drift_discovered += 1
                drifts.append(discovery.drift)
            if discovery.rework is not None:
                rework_discovered += 1
                reworks.append(discovery.rework)
            if discovery.escalation is not None:
                escalation_discovered += 1
                escalations.append(discovery.escalation)
            if discovery.enqueue is not None:
                enqueue_discovered += 1
                enqueues.append(discovery.enqueue)

        label_drifts = self._discover_label_drifts(
            state,
            excluded_issue_numbers=pending_issue_numbers
            | terminal_issue_numbers
            | {drift.issue_number for drift in drifts},
        )
        drift_discovered += len(label_drifts)
        drifts.extend(label_drifts)

        logger.debug(
            "Awaiting-merge scan complete: checked=%d terminal=%d drift=%d "
            "still_pending=%d skipped=%d escalations=%d",
            checked, discovered, drift_discovered,
            still_pending, skipped, escalation_discovered,
        )
        return AwaitingMergeReconciliationResult(
            checked=checked,
            discovered=discovered,
            drift_discovered=drift_discovered,
            rework_discovered=rework_discovered,
            escalation_discovered=escalation_discovered,
            enqueue_discovered=enqueue_discovered,
            still_pending=still_pending,
            skipped=skipped,
            reconciliations=tuple(reconciliations),
            drifts=tuple(drifts),
            reworks=tuple(reworks),
            escalations=tuple(escalations),
            enqueues=tuple(enqueues),
        )

    def _awaiting_merge_entries(
        self, state: OrchestratorState
    ) -> list[SessionHistoryEntry]:
        return [
            entry
            for entry in latest_history_entries_by_issue(
                state.session_history,
                limit=self.history_limit,
            )
            if entry.status in RECONCILABLE_HISTORY_STATUSES and bool(entry.pr_url)
        ]

    def _discover_entry(
        self,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
    ) -> AwaitingMergeEntryDiscovery:
        pr_number = pr_number_from_url(entry.pr_url or "")
        if pr_number is None:
            logger.warning(
                "Cannot reconcile awaiting-merge history for issue #%d: invalid PR URL %r",
                entry.issue_number,
                entry.pr_url,
            )
            return AwaitingMergeEntryDiscovery("skipped")

        pr = self._get_pr(entry.issue_number, pr_number)
        if pr is not None:
            pr_state = normalized_state(pr.state)
            logger.debug(
                "Awaiting-merge entry issue=#%d pr=#%d state=%r terminal=%s pr_url=%s",
                entry.issue_number,
                pr_number,
                pr_state,
                pr_state in TERMINAL_AWAITING_MERGE_HISTORY_STATUSES,
                entry.pr_url,
            )
            if pr_state in TERMINAL_AWAITING_MERGE_HISTORY_STATUSES:
                logger.info(
                    "Awaiting-merge terminal via PR: issue=#%d pr=#%d state=%s",
                    entry.issue_number,
                    pr_number,
                    pr_state,
                )
                # PR is terminal — drop any pending-checks bookkeeping so
                # the dict doesn't leak across PR lifecycles.
                state.awaiting_merge_checks_pending_since.pop(
                    entry.issue_number, None,
                )
                state.awaiting_merge_rollup_scan_timestamps.pop(pr_number, None)
                drift = None
                issue_open = False
                if pr.is_closed_unmerged:
                    drift = self._discover_terminal_pr_issue_drift(
                        state=state,
                        entry=entry,
                        pr=pr,
                        pr_number=pr_number,
                    )
                else:
                    # PR merged: did GitHub's auto-close actually fire for
                    # this merge? See close_on_merge module (porchpin #81).
                    # None = evidence unreadable; leave the entry reconcilable.
                    close_check = should_close_merged_issue(
                        get_issue=self._get_issue,
                        closed_on_or_after=(
                            self.repository_host.issue_closed_on_or_after
                        ),
                        state=state, entry=entry,
                        merged_at=pr.merged_at, now=self.clock(),
                    )
                    if close_check is None:
                        return AwaitingMergeEntryDiscovery("skipped")
                    issue_open = close_check
                return AwaitingMergeEntryDiscovery(
                    "terminal",
                    reconciliation=reconciliation_fact(
                        entry=entry,
                        pr_number=pr_number,
                        status=pr_state,
                        reason=pr_terminal_reason(pr_state),
                        source="pull_request",
                        issue_open=issue_open,
                        merged_at=pr.merged_at,
                    ),
                    drift=drift,
                )
        else:
            logger.debug(
                "Awaiting-merge PR fetch returned None: issue=#%d pr=#%d pr_url=%s",
                entry.issue_number,
                pr_number,
                entry.pr_url,
            )
            state.awaiting_merge_rollup_scan_timestamps.pop(pr_number, None)

        issue = self._get_issue(entry.issue_number)
        if issue is None:
            # An open PR still means "awaiting merge"; only bump issue freshness
            # after a confirmed issue refresh.
            if pr is not None:
                return AwaitingMergeEntryDiscovery("still_pending")
            return AwaitingMergeEntryDiscovery("skipped")

        record_issue_refreshes(state, {entry.issue_number}, self.clock())
        if normalized_state(issue.state) == "closed":
            logger.info(
                "Awaiting-merge terminal via issue closure: issue=#%d pr=#%d",
                entry.issue_number,
                pr_number,
            )
            # Issue terminated — drop pending-checks bookkeeping.
            state.awaiting_merge_checks_pending_since.pop(
                entry.issue_number, None,
            )
            state.awaiting_merge_rollup_scan_timestamps.pop(pr_number, None)
            return AwaitingMergeEntryDiscovery(
                "terminal",
                reconciliation=reconciliation_fact(
                    entry=entry,
                    pr_number=pr_number,
                    status="closed",
                    reason="Issue closed; awaiting merge reconciled",
                    source="issue",
                ),
            )

        if pr is None:
            return AwaitingMergeEntryDiscovery("skipped")
        return self._discover_open_pr_followup(state, entry, pr, issue, pr_number)

    def _discover_open_pr_followup(
        self, state: OrchestratorState, entry: SessionHistoryEntry,
        pr: PRInfo, issue: Issue, pr_number: int,
    ) -> AwaitingMergeEntryDiscovery:
        if not self._post_publish_eligible(state, entry, pr):
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
            return AwaitingMergeEntryDiscovery("still_pending")
        rework, escalation, enqueue = self._discover_post_publish_followup(
            state=state,
            entry=entry,
            pr=pr,
            issue=issue,
            pr_number=pr_number,
        )
        return AwaitingMergeEntryDiscovery(
            "still_pending",
            rework=rework,
            escalation=escalation,
            enqueue=enqueue,
        )

    def _discover_terminal_pr_issue_drift(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeDrift | None:
        if self.label_manager is None:
            return None
        try:
            issue = self._get_issue(entry.issue_number)
        except RepositoryHostError:
            logger.warning(
                "Unable to check issue state for closed PR drift: issue=#%d pr=#%d",
                entry.issue_number,
                pr_number,
            )
            return None
        if issue is None:
            return None

        record_issue_refreshes(state, {entry.issue_number}, self.clock())
        if normalized_state(issue.state) == "closed":
            return None
        if not self.label_manager.is_pr_pending(issue.labels):
            return None

        return _drift_fact(
            issue_number=entry.issue_number,
            pr=pr,
            status_reason="PR closed; issue remains open",
        )

    def _discover_label_drifts(
        self,
        state: OrchestratorState,
        *,
        excluded_issue_numbers: set[int],
    ) -> list[DiscoveredAwaitingMergeDrift]:
        if self.label_manager is None:
            return []

        active_issue_numbers = {session.issue.number for session in state.active_sessions}
        now = self.clock()
        drifts: list[DiscoveredAwaitingMergeDrift] = []
        for issue in _unique_cached_issues(state):
            if not self._should_scan_label_drift_issue(
                state=state,
                issue=issue,
                active_issue_numbers=active_issue_numbers,
                excluded_issue_numbers=excluded_issue_numbers,
                now=now,
            ):
                continue

            drift = self._discover_label_drift_for_issue(
                state=state,
                issue=issue,
                scanned_at=now,
            )
            if drift is not None:
                drifts.append(drift)

        return drifts

    def _should_scan_label_drift_issue(
        self,
        *,
        state: OrchestratorState,
        issue: Issue,
        active_issue_numbers: set[int],
        excluded_issue_numbers: set[int],
        now: float,
    ) -> bool:
        if self.label_manager is None:
            return False
        if issue.number in excluded_issue_numbers or issue.number in active_issue_numbers:
            return False
        if normalized_state(issue.state) == "closed":
            return False
        if not self.label_manager.is_pr_pending(issue.labels):
            return False
        return not _recent_label_drift_scan(
            state=state,
            issue_number=issue.number,
            now=now,
            interval_seconds=self.label_drift_scan_interval_seconds,
        )

    def _discover_label_drift_for_issue(
        self,
        *,
        state: OrchestratorState,
        issue: Issue,
        scanned_at: float,
    ) -> DiscoveredAwaitingMergeDrift | None:
        state.awaiting_merge_drift_scan_timestamps[issue.number] = scanned_at
        try:
            prs = self._get_prs_for_issue(issue.number)
        except RepositoryHostError:
            return None

        # `classify_pr_set_drift` owns the open/merged/closed precedence so the
        # "latest terminal PR decides" rule lives in exactly one place.
        decision = classify_pr_set_drift(prs)
        if not decision.drifting:
            return None
        if decision.pr is None:
            return DiscoveredAwaitingMergeDrift(
                issue_number=issue.number,
                pr_number=0,
                pr_url="",
                status_reason="PR missing; issue remains open",
            )
        return _drift_fact(
            issue_number=issue.number,
            pr=decision.pr,
            status_reason="PR closed; issue remains open",
        )

    def _discover_post_publish_followup(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        issue: Issue,
        pr_number: int,
    ) -> tuple[
        DiscoveredRework | None,
        DiscoveredAwaitingMergeEscalation | None,
        "DiscoveredMergeQueueEnqueue | None",
    ]:
        """Decide what to do with an approved-but-not-merged PR.

        When merge queue mode is enabled the merge queue coordinator owns the
        decision (enqueue / observe / conflict-rework / failure-route) instead
        of the default rework/escalation dispatch below.
        """
        if not self._post_publish_eligible(state, entry, pr):
            # Eligibility loss resets the WAIT_FOR_CHECKS budget, so future
            # re-approval starts fresh.
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
            return None, None, None

        if self._stack_merge_held(issue):
            # A stacked successor stays out of the merge-readiness dispatch until
            # its predecessors merge in order (ADR-0029 / #6596). Holding here —
            # before the merge-queue and default paths — means it is never
            # enqueued, reworked, or escalated ahead of its predecessors, and so
            # never left silently mergeable. It keeps observing until unblocked.
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
            return None, None, None

        if self.merge_queue is not None and self.merge_queue.enabled:
            return self._discover_merge_queue_followup(
                state=state, entry=entry, pr=pr, issue=issue, pr_number=pr_number,
            )
        return self._discover_default_post_publish_followup(
            state=state, entry=entry, pr=pr, issue=issue, pr_number=pr_number,
        )

    def _discover_default_post_publish_followup(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        issue: Issue,
        pr_number: int,
    ) -> tuple[
        DiscoveredRework | None,
        DiscoveredAwaitingMergeEscalation | None,
        "DiscoveredMergeQueueEnqueue | None",
    ]:
        """Default (non-merge-queue) rework/escalation dispatch for an
        approved-but-not-merged PR.
        """
        # _post_publish_eligible returns False when label_manager is None.
        assert self.label_manager is not None
        # A stale post-publish human escalation may become reworkable later.
        already_escalated = self.label_manager.needs_human in pr.labels

        gate = self._rollup_gate()
        decisive = rollup_is_decisive(pr.mergeable_state)
        due = not decisive or gate.scan_due(
            state, pr_number, self.rollup_scan_interval_seconds
        )
        if not due:
            return None, None, None

        # The gate reads the status rollup only when it is decisive
        # (unstable/blocked), bounding both the GraphQL round-trip and
        # repeated token-permission failures. A permission denial means the
        # decision genuinely needs the rollup but the token can't provide
        # it — escalate loudly rather than hide it behind a PENDING default.
        resolution = gate.resolve_decisive(
            state.status_rollup_capability,
            pr=pr,
            issue_number=entry.issue_number,
            issue_key=issue.key.stable_id(),
        )
        if resolution.permission_denied:
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
            if already_escalated:
                return None, None, None
            return None, self._build_rollup_permission_escalation(
                pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
                pr_number=pr_number,
                reason=resolution.reason,
            ), None
        pr.status_check_rollup = resolution.rollup_state

        action = classify_post_approval_state(pr)
        logger.debug(
            "Awaiting-merge classify: issue=#%d pr=#%d state=%s rollup=%s "
            "action=%s already_escalated=%s",
            entry.issue_number,
            pr_number,
            normalized_state(pr.mergeable_state),
            pr.status_check_rollup,
            action,
            already_escalated,
        )
        if action != "WAIT_FOR_CHECKS" or already_escalated:
            state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)

        if action in REWORK_ACTIONS:
            return self._build_rework_discovery(
                pr=pr, action=action, entry=entry, issue=issue, pr_number=pr_number,
                clear_needs_human=already_escalated,
            ), None, None
        if already_escalated:
            return None, None, None
        if action == "BLOCKED_TERMINAL":
            return None, self._build_branch_protection_escalation(
                pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
                pr_number=pr_number,
            ), None
        if action == "WAIT_FOR_CHECKS":
            return None, self._maybe_escalate_pending_checks(
                state=state, pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
                pr_number=pr_number,
            ), None
        # READY / UNKNOWN — nothing to do.
        return None, None, None

    def _discover_merge_queue_followup(
        self,
        *,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
        issue: Issue,
        pr_number: int,
    ) -> tuple[
        DiscoveredRework | None,
        DiscoveredAwaitingMergeEscalation | None,
        "DiscoveredMergeQueueEnqueue | None",
    ]:
        """Run the merge queue coordinator for one eligible approved PR.

        Reads the queue entry first so an already-queued PR is observed without
        paying for (or escalating on) a status-rollup read. Only a not-yet-queued
        PR resolves the rollup, since the base eligibility classification needs
        it for ``unstable``/``blocked`` states.
        """
        assert self.merge_queue is not None
        read = self.merge_queue.read_entry(pr_number)
        if read.is_indeterminate:
            # The queue state could not be determined (transient read failure or
            # an unmodeled provider state). Treat it as non-actionable: do NOT
            # resolve the rollup or classify, so an unreadable queue can never
            # enqueue, rework, or escalate a PR off stale status. Re-observe next
            # tick.
            return None, None, None
        queue_entry = read.entry
        if queue_entry is None:
            decisive = rollup_is_decisive(pr.mergeable_state)
            due = not decisive or self._rollup_gate().scan_due(
                state, pr_number, self.rollup_scan_interval_seconds
            )
            if not due:
                return None, None, None
            resolution = self._rollup_gate().resolve_decisive(
                state.status_rollup_capability,
                pr=pr,
                issue_number=entry.issue_number,
                issue_key=issue.key.stable_id(),
            )
            if resolution.permission_denied:
                state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
                return None, self._build_rollup_permission_escalation(
                    pr=pr,
                    issue_number=entry.issue_number,
                    issue_key=issue.key.stable_id(),
                    pr_number=pr_number,
                    reason=resolution.reason,
                ), None
            pr.status_check_rollup = resolution.rollup_state

        # Merge-queue mode does not run the WAIT_FOR_CHECKS timeout machine —
        # GitHub re-runs required checks on the merge group — so clear any
        # pending-checks bookkeeping a prior non-queue tick may have left.
        state.awaiting_merge_checks_pending_since.pop(entry.issue_number, None)
        followup = self.merge_queue.classify(
            pr=pr,
            issue=issue,
            issue_number=entry.issue_number,
            pr_number=pr_number,
            entry=queue_entry,
        )
        return followup.rework, followup.escalation, followup.enqueue

    def _post_publish_eligible(
        self,
        state: OrchestratorState,
        entry: SessionHistoryEntry,
        pr: PRInfo,
    ) -> bool:
        """Pre-filter: only consider PRs that are reviewer-approved and not
        already in flight via another rework path."""
        if self.label_manager is None:
            return False
        if self.label_manager.code_reviewed not in pr.labels:
            return False
        if self.label_manager.needs_rework in pr.labels:
            return False
        if any(s.issue.number == entry.issue_number for s in state.active_sessions):
            return False
        if any(
            p.resolve_issue_number() == entry.issue_number
            for p in state.pending_reworks
        ):
            return False
        return True

    def _build_rework_discovery(
        self,
        *,
        pr: PRInfo,
        action: PostApprovalAction,
        entry: SessionHistoryEntry,
        issue: Issue,
        pr_number: int,
        clear_needs_human: bool = False,
    ) -> DiscoveredRework:
        assert self.label_manager is not None
        assert issue.agent_type is not None
        return DiscoveredRework(
            issue_number=entry.issue_number,
            pr_number=pr_number,
            branch_name=pr.branch,
            agent_type=issue.agent_type,
            rework_cycle=next_rework_cycle(pr.labels, self.label_manager),
            source=POST_PUBLISH_VALIDATION_SOURCE,
            feedback=build_rework_feedback(pr, action),
            clear_needs_human=clear_needs_human,
            feedback_comment_already_posted=self._post_publish_comment_present(
                pr_number
            ),
        )

    def _post_publish_comment_present(self, pr_number: int) -> bool:
        """Return True if the PR already carries the post-publish marker comment.

        Read-only dedupe guard: if a prior tick posted the feedback comment but
        failed to apply the ``needs_rework`` label (leaving the PR eligible for
        re-discovery), the planner would otherwise stack a duplicate comment.
        The marker scan covers every comment page (not just the first 100), so
        a marker sitting beyond the first page still suppresses the duplicate.
        Only reached on the rare rework-discovery path, so the extra read is
        bounded. Read failures propagate (fail loud) rather than risk a
        duplicate comment or a dropped feedback.
        """
        try:
            return self.repository_host.issue_comment_marker_present(
                pr_number, POST_PUBLISH_VALIDATION_COMMENT_MARKER
            )
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to read comments for awaiting-merge PR #%d: %s",
                pr_number,
                exc,
            )
            raise

    def _build_branch_protection_escalation(
        self,
        *,
        pr: PRInfo,
        issue_number: int,
        issue_key: str,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeEscalation:
        assert self.label_manager is not None
        return build_escalation(
            pr=pr,
            issue_number=issue_number,
            issue_key=issue_key,
            pr_number=pr_number,
            label_manager=self.label_manager,
            kind="branch_protection_blocked",
            reason=(
                "Branch protection blocks merge despite all required "
                "checks passing — likely missing approvals, CODEOWNERS "
                "sign-off, or required signatures. Code rework cannot "
                "unstick this."
            ),
        )

    def _build_rollup_permission_escalation(
        self,
        *,
        pr: PRInfo,
        issue_number: int,
        issue_key: str,
        pr_number: int,
        reason: str,
    ) -> DiscoveredAwaitingMergeEscalation:
        assert self.label_manager is not None
        return build_escalation(
            pr=pr,
            issue_number=issue_number,
            issue_key=issue_key,
            pr_number=pr_number,
            label_manager=self.label_manager,
            kind="status_rollup_permission_denied",
            reason=reason,
        )

    def _maybe_escalate_pending_checks(
        self,
        *,
        state: OrchestratorState,
        pr: PRInfo,
        issue_number: int,
        issue_key: str,
        pr_number: int,
    ) -> DiscoveredAwaitingMergeEscalation | None:
        """Run the WAIT_FOR_CHECKS timeout state machine for one PR."""
        assert self.label_manager is not None
        now = self.clock()
        first_seen = state.awaiting_merge_checks_pending_since.get(issue_number)
        if first_seen is None:
            state.awaiting_merge_checks_pending_since[issue_number] = now
            return None
        elapsed = now - first_seen
        if elapsed < self.post_publish_checks_pending_timeout_seconds:
            return None
        minutes = max(1, int(elapsed // 60))
        timeout_minutes = int(
            self.post_publish_checks_pending_timeout_seconds // 60
        )
        return build_escalation(
            pr=pr,
            issue_number=issue_number,
            issue_key=issue_key,
            pr_number=pr_number,
            label_manager=self.label_manager,
            kind="checks_pending_timeout",
            reason=(
                f"Required GitHub checks have been pending for "
                f"~{minutes} minute(s) since reviewer approval "
                f"(timeout: {timeout_minutes} minutes). The orchestrator "
                f"has stopped waiting and is handing the PR back for "
                f"human attention."
            ),
        )

    def _get_pr(self, issue_number: int, pr_number: int) -> PRInfo | None:
        # REST-only; decisive open PRs read check rollup lazily through the gate.
        try:
            return self.repository_host.get_pr(pr_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to refresh PR #%d for awaiting-merge issue #%d: %s",
                pr_number,
                issue_number,
                exc,
            )
            raise

    def _get_issue(self, issue_number: int) -> Issue | None:
        try:
            return self.repository_host.get_issue(issue_number)
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to refresh awaiting-merge issue #%d: %s",
                issue_number,
                exc,
            )
            raise

    def _stack_merge_held(self, issue: Issue) -> bool:
        """Whether a stacked successor must be held from merge readiness.

        Consults the single dependency-gate owner's *merge* gate (ADR-0029): a
        stack successor is not merge-ready until its predecessors have merged or
        closed in order. Non-stack issues are never held — the cheap body
        short-circuit means a slice with no ``Stack-after:`` edge keeps its exact
        prior merge-readiness behavior, and even among stack issues the gate
        only blocks while a predecessor remains unmerged.
        """
        if self.dependency_evaluator is None:
            return False
        body = issue.body or ""
        if "stack-after" not in body.lower():
            return False
        report = self.dependency_evaluator.evaluate_merge_gate(
            issue.number, body, issue.milestone,
        )
        if report.can_merge:
            return False
        logger.info(
            "Holding stacked successor issue #%d from merge readiness: %s",
            issue.number,
            report.merge.summary(),
        )
        return True

    def _get_prs_for_issue(self, issue_number: int) -> list[PRInfo]:
        try:
            return self.repository_host.get_prs_for_issue(issue_number, state="all")
        except RepositoryHostError as exc:
            logger.warning(
                "Failed to scan PRs for awaiting-merge label drift on issue #%d: %s",
                issue_number,
                exc,
            )
            raise


def pr_number_from_url(pr_url: str) -> int | None:
    """Extract a PR number from a GitHub-style pull request URL."""
    segments = [segment for segment in urlparse(pr_url).path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment == "pull":
            try:
                return int(segments[index + 1])
            except ValueError:
                continue
    return None


def _drift_fact(
    *,
    issue_number: int,
    pr: PRInfo,
    status_reason: str,
) -> DiscoveredAwaitingMergeDrift:
    return DiscoveredAwaitingMergeDrift(
        issue_number=issue_number,
        pr_number=pr.number,
        pr_url=pr.url,
        status_reason=status_reason,
    )


def _unique_cached_issues(state: OrchestratorState) -> list[Issue]:
    issues_by_number: dict[int, Issue] = {}
    for issue in [*state.cached_scope_issues, *state.cached_queue_issues]:
        issues_by_number[issue.number] = issue
    return list(issues_by_number.values())


def _recent_label_drift_scan(
    *,
    state: OrchestratorState,
    issue_number: int,
    now: float,
    interval_seconds: float,
) -> bool:
    last_scanned_at = state.awaiting_merge_drift_scan_timestamps.get(issue_number, 0.0)
    return last_scanned_at > 0 and (now - last_scanned_at) < interval_seconds

