from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, call

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ReconcileHistoryEntryAction
from issue_orchestrator.control.awaiting_merge_reconciler import (
    POST_PUBLISH_VALIDATION_SOURCE,
    AwaitingMergeReconciler,
    classify_post_approval_state,
    classify_pr_set_drift,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    SessionHistoryEntry,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryEventSink
from issue_orchestrator.ports.pull_request_tracker import (
    MergeQueueEntry,
    MergeQueueRead,
    PRInfo,
    StatusCheckRollupCapability,
    StatusCheckRollupRead,
)
from issue_orchestrator.ports.repository_host import (
    DependencyIssueSnapshot,
    RepositoryHostError,
)


def _wire_pr(
    repository_host: MagicMock,
    pr: PRInfo,
    *,
    rollup_capability: StatusCheckRollupCapability = "ok",
) -> None:
    """Wire a mock repository_host the way the reconciler now reads a PR.

    The reconciler fetches PR state with REST ``get_pr`` (no rollup) and
    then, ONLY for decisive open PRs, reads the rollup separately via the
    gated ``read_pr_status_check_rollup``. This helper mirrors that split:
    ``get_pr`` returns the PR with the rollup stripped (as the real REST
    path does), and the rollup the test set on the PRInfo is surfaced
    through the second call with the given capability.
    """
    repository_host.get_pr.return_value = replace(pr, status_check_rollup=None)
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=pr.status_check_rollup,
        capability=rollup_capability,
    )


def _history_entry() -> SessionHistoryEntry:
    return SessionHistoryEntry(
        issue_number=228,
        title="Shared cache read misses",
        agent_type="agent:backend",
        status="completed",
        runtime_minutes=0,
        pr_url="https://github.com/owner/repo/pull/318",
        status_reason="Recovered awaiting merge state on startup",
    )


def _label_manager() -> LabelManager:
    return LabelManager(Config())


_MERGED_AT = "2026-08-03T13:52:09Z"


def _pr(
    state: str,
    *,
    number: int = 318,
    mergeable_state: str | None = None,
    labels: list[str] | None = None,
    status_check_rollup: str | None = None,
) -> PRInfo:
    return PRInfo(
        number=number,
        title="Add distributed coalescing for shared-cache read misses",
        url=f"https://github.com/owner/repo/pull/{number}",
        branch="228-cache-read-misses",
        body="",
        state=state,
        labels=labels or [],
        mergeable_state=mergeable_state,
        status_check_rollup=status_check_rollup,  # type: ignore[arg-type]
        merged_at=_MERGED_AT if state == "merged" else None,
    )


def _issue(state: str, *, number: int = 228) -> Issue:
    return Issue(
        number=number,
        title="Shared cache read misses",
        labels=["agent:backend", "pr-pending"],
        state=state,
    )


def test_pr_info_is_closed_unmerged_distinguishes_merged_from_closed() -> None:
    """is_closed_unmerged is the blocked:pr-closed gate; a merged PR (state
    "merged", as adapters normalize it) must never satisfy it."""
    assert _pr("merged").is_closed_unmerged is False
    assert _pr("closed").is_closed_unmerged is True
    assert _pr("open").is_closed_unmerged is False


# --------------------------------------------------------------------------- #
# Stacked-successor ordered-merge hold (ADR-0029 / #6596)
# --------------------------------------------------------------------------- #


def _stack_evaluator(predecessor_state: str, *, milestone: str = "M7"):
    """A DependencyEvaluator whose predecessor (#220) has the given state."""
    from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

    checker = MagicMock()
    checker.get_dependency_issue_snapshot.return_value = DependencyIssueSnapshot(
        state=predecessor_state,
        milestone=milestone,
    )
    return DependencyEvaluator(issue_checker=checker, events=InMemoryEventSink())


def _stack_successor_issue() -> Issue:
    return Issue(
        number=228,
        title="Stacked successor",
        labels=["agent:backend", "pr-pending"],
        state="open",
        body="Stack-after: #220",
        milestone="M7",
    )


def test_stacked_successor_held_from_merge_while_predecessor_open() -> None:
    """A stack successor with an unmerged predecessor is held entirely: no
    rework/escalation/enqueue is discovered even when its own PR looks ready."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    # PR is approved and dirty — the default path would normally discover rework.
    _wire_pr(
        repository_host,
        _pr("open", mergeable_state="dirty", labels=["code-reviewed"]),
    )
    repository_host.get_issue.return_value = _stack_successor_issue()
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
        dependency_evaluator=_stack_evaluator("open"),  # predecessor not merged
    ).discover(state)

    # Held: still observing, but nothing dispatched ahead of the predecessor.
    assert result.still_pending == 1
    assert result.rework_discovered == 0
    assert result.escalation_discovered == 0
    assert result.enqueue_discovered == 0


def test_stacked_successor_released_once_predecessor_merges() -> None:
    """Once the predecessor merges/closes, the successor rejoins the normal
    post-approval dispatch (here: a dirty PR discovers post-publish rework)."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr("open", mergeable_state="dirty", labels=["code-reviewed"]),
    )
    repository_host.get_issue.return_value = _stack_successor_issue()
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
        dependency_evaluator=_stack_evaluator("closed"),  # predecessor merged
    ).discover(state)

    # Not held: the default dispatch runs and discovers the dirty-PR rework.
    assert result.rework_discovered == 1


def test_non_stack_issue_is_never_held() -> None:
    """An issue without a Stack-after: edge keeps its exact prior dispatch even
    when a stack evaluator is wired (cheap body short-circuit, no gate call)."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr("open", mergeable_state="dirty", labels=["code-reviewed"]),
    )
    repository_host.get_issue.return_value = _issue("open")  # no Stack-after: body
    repository_host.issue_comment_marker_present.return_value = False
    evaluator = _stack_evaluator("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
        dependency_evaluator=evaluator,
    ).discover(state)

    assert result.rework_discovered == 1


def test_recovered_awaiting_merge_entry_reconciles_when_pr_is_merged() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.return_value = _issue("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "merged"
    assert result.reconciliations[0].status_reason == "PR merged; awaiting merge reconciled"
    assert result.reconciliations[0].source == "pull_request"
    # GitHub's closing keyword auto-closed the issue — no fallback needed.
    assert result.reconciliations[0].issue_open is False
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    repository_host.get_pr.assert_called_once_with(318)
    # A terminal PR must NOT be status-rollup-polled — that GraphQL round-trip
    # (and its permission-wall exposure) is exactly the per-tick noise #6600
    # removes.
    repository_host.read_pr_status_check_rollup.assert_not_called()
    # The merged transition reads the issue exactly once, to decide the
    # close-on-merge fallback (porchpin #81). This is once per PR lifecycle at
    # the terminal transition — not the per-tick polling #6600 removed.
    repository_host.get_issue.assert_called_once_with(228)
    # A closed issue needs no transition evidence — the events read is skipped.
    repository_host.issue_closed_on_or_after.assert_not_called()


def test_merged_pr_with_open_issue_flags_close_on_merge_fallback() -> None:
    """A merged PR whose issue is open AND was never closed since the merge
    means GitHub's closing-keyword auto-close did not fire (word-boundary-
    defeated reference or none at all). The fact must carry issue_open=True so
    the planner orders the close-on-merge fallback (porchpin case file #81)."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_closed_on_or_after.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].status == "merged"
    assert result.reconciliations[0].issue_open is True
    repository_host.issue_closed_on_or_after.assert_called_once_with(
        228, _MERGED_AT,
    )


def test_merged_then_reopened_issue_is_never_reclosed() -> None:
    """F2 regression: merge → auto-close → human reopen → first reconcile.
    The issue is open but a close event exists at/after the merge, proving the
    auto-close DID fire — the open state is a deliberate reopen and must not
    be re-closed. state == open alone is not evidence."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_closed_on_or_after.return_value = True

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].status == "merged"
    assert result.reconciliations[0].issue_open is False


def test_merged_pr_events_read_error_leaves_entry_reconcilable() -> None:
    """Evidence unavailable is not either answer: no guessed finalization
    (fail-open recreates the relaunch bug) and no aborted gather tick."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_closed_on_or_after.side_effect = RepositoryHostError(
        "boom"
    )

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 0
    assert result.skipped == 1


def test_merged_pr_without_merged_at_never_closes() -> None:
    """No merge timestamp = no transition evidence; a destructive close is
    never inferred from open state alone."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = replace(_pr("merged"), merged_at=None)
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].issue_open is False
    repository_host.issue_closed_on_or_after.assert_not_called()


def test_merged_pr_issue_fetch_error_leaves_entry_reconcilable() -> None:
    """When the issue state cannot be read, the entry must NOT be finalized
    against a guessed state: fail-open recreates the relaunch bug and raising
    aborts the gather tick. Skip and retry next pass."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.side_effect = RepositoryHostError("boom")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 0
    assert result.skipped == 1
    assert result.reconciliations == ()


def test_merged_pr_with_missing_issue_reconciles_without_close() -> None:
    """A host that reports the issue missing has nothing to close; holding the
    entry hostage would strand it, so it finalizes without the fallback."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    repository_host.get_issue.return_value = None

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].status == "merged"
    assert result.reconciliations[0].issue_open is False


def test_recovered_entry_reconciles_when_linked_issue_is_closed() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.return_value = _issue("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "closed"
    assert result.reconciliations[0].status_reason == "Issue closed; awaiting merge reconciled"
    assert result.reconciliations[0].source == "issue"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_recovered_entry_reconciles_when_pr_is_closed() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert result.reconciliations[0].status == "closed"
    assert result.reconciliations[0].status_reason == "PR closed; awaiting merge reconciled"
    assert result.reconciliations[0].source == "pull_request"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    repository_host.read_pr_status_check_rollup.assert_not_called()
    repository_host.get_issue.assert_not_called()


def test_closed_pr_with_open_pr_pending_issue_discovers_label_drift() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("closed")
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 1
    assert result.drift_discovered == 1
    drift = result.drifts[0]
    assert drift.issue_number == 228
    assert drift.pr_number == 318
    assert drift.pr_url == "https://github.com/owner/repo/pull/318"
    assert drift.status_reason == "PR closed; issue remains open"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_closed_pr_with_closed_issue_does_not_discover_label_drift() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("closed")
    repository_host.get_issue.return_value = _issue("closed")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.drift_discovered == 0
    assert result.drifts == ()
    assert state.issue_refresh_timestamps[228] == 1234.5


def test_merged_pr_does_not_discover_blocked_pr_closed_drift() -> None:
    """#358: a PR that merged (reported by the adapter as state "merged") must
    never produce a blocked:pr-closed drift, even with a pr-pending label still
    present — this is the post-merge race the reconciler must tolerate."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.drift_discovered == 0
    assert result.drifts == ()
    assert result.reconciliations[0].status == "merged"
    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_label_only_pr_pending_issue_with_closed_pr_discovers_drift() -> None:
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [_pr("closed")]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
    ).discover(state)

    assert result.checked == 0
    assert result.drift_discovered == 1
    drift = result.drifts[0]
    assert drift.issue_number == 228
    assert drift.pr_number == 318
    assert drift.status_reason == "PR closed; issue remains open"
    assert state.awaiting_merge_drift_scan_timestamps[228] > 0
    repository_host.get_prs_for_issue.assert_called_once_with(228, state="all")


def test_open_latest_pr_with_older_merged_pr_does_not_discover_drift() -> None:
    """#364: an issue whose current PR is open must not be flagged
    blocked:pr-closed just because an earlier PR for the same issue merged."""
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [
        _pr("merged", number=428),
        _pr("open", number=437),
    ]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.drift_discovered == 0
    assert result.drifts == ()


def test_only_merged_pr_does_not_discover_drift() -> None:
    """A merged PR with no other PRs is terminal work that landed — it is
    never closed-unmerged, so no blocked:pr-closed drift is produced."""
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [_pr("merged", number=428)]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.drift_discovered == 0
    assert result.drifts == ()


def test_closed_unmerged_latest_pr_flags_despite_older_merged_pr() -> None:
    """A genuinely closed-without-merge current PR still flags, and the drift
    keys on that latest closed PR rather than the earlier merged one."""
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [
        _pr("merged", number=428),
        _pr("closed", number=437),
    ]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.drift_discovered == 1
    drift = result.drifts[0]
    assert drift.pr_number == 437
    assert drift.status_reason == "PR closed; issue remains open"


def test_older_closed_pr_with_newer_merged_pr_does_not_discover_drift() -> None:
    """#6628 F1: a label-only `pr-pending` issue whose latest PR merged must not
    be flagged blocked:pr-closed just because an earlier attempt's PR closed
    unmerged. The latest terminal PR (merged) wins over the older closed one."""
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [
        _pr("closed", number=428),
        _pr("merged", number=437),
    ]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.drift_discovered == 0
    assert result.drifts == ()


def test_merged_reconciliation_excludes_issue_from_label_drift_scan() -> None:
    """#6628 F1: a history entry that reconciles as merged must suppress the
    label-drift scan for the same issue. Otherwise a stale cached `pr-pending`
    plus an older closed-unmerged PR would manufacture a contradictory
    blocked:pr-closed drift alongside the merged reconciliation in one
    discover() call."""
    entry = _history_entry()  # issue 228, PR #318
    issue = _issue("open")  # same issue 228, still labelled pr-pending
    state = OrchestratorState(
        session_history=[entry],
        cached_queue_issues=[issue],
    )
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    # Had the scan run, this older closed-unmerged PR would have flagged drift.
    repository_host.get_prs_for_issue.return_value = [
        _pr("closed", number=300),
        _pr("merged", number=318),
    ]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].status == "merged"
    assert result.drift_discovered == 0
    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert result.drifts == ()
    repository_host.get_prs_for_issue.assert_not_called()


def test_label_only_pr_pending_issue_without_pr_discovers_missing_pr_drift() -> None:
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = []

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
    ).discover(state)

    assert result.checked == 0
    assert result.drift_discovered == 1
    drift = result.drifts[0]
    assert drift.issue_number == 228
    assert drift.pr_number == 0
    assert drift.pr_url == ""
    assert drift.status_reason == "PR missing; issue remains open"
    assert state.awaiting_merge_drift_scan_timestamps[228] > 0


def test_recent_label_only_pr_pending_issue_scan_is_throttled() -> None:
    issue = _issue("open")
    state = OrchestratorState(
        cached_queue_issues=[issue],
        awaiting_merge_drift_scan_timestamps={228: 1000.0},
    )
    repository_host = MagicMock()

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1200.0,
        label_drift_scan_interval_seconds=300.0,
    ).discover(state)

    assert result.checked == 0
    assert result.drift_discovered == 0
    repository_host.get_prs_for_issue.assert_not_called()


def test_stale_label_only_pr_pending_issue_scan_runs_and_updates_timestamp() -> None:
    issue = _issue("open")
    state = OrchestratorState(
        cached_queue_issues=[issue],
        awaiting_merge_drift_scan_timestamps={228: 1000.0},
    )
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.return_value = [_pr("closed")]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1400.0,
        label_drift_scan_interval_seconds=300.0,
    ).discover(state)

    assert result.drift_discovered == 1
    assert state.awaiting_merge_drift_scan_timestamps[228] == 1400.0
    repository_host.get_prs_for_issue.assert_called_once_with(228, state="all")


def test_label_only_pr_scan_error_skips_issue_and_continues() -> None:
    state = OrchestratorState(
        cached_queue_issues=[
            _issue("open", number=228),
            _issue("open", number=229),
        ]
    )
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.side_effect = [
        RepositoryHostError("github unavailable"),
        [_pr("closed", number=319)],
    ]

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.drift_discovered == 1
    assert result.drifts[0].issue_number == 229
    assert result.drifts[0].pr_number == 319
    assert state.awaiting_merge_drift_scan_timestamps == {228: 1234.5, 229: 1234.5}
    assert repository_host.get_prs_for_issue.call_count == 2


def test_failed_label_only_pr_scan_is_throttled() -> None:
    issue = _issue("open")
    state = OrchestratorState(cached_queue_issues=[issue])
    repository_host = MagicMock()
    repository_host.get_prs_for_issue.side_effect = RepositoryHostError("github unavailable")
    reconciler = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
        label_drift_scan_interval_seconds=300.0,
    )

    first_result = reconciler.discover(state)

    assert first_result.drift_discovered == 0
    assert state.awaiting_merge_drift_scan_timestamps == {228: 1234.5}
    repository_host.get_prs_for_issue.assert_called_once_with(228, state="all")

    second_result = reconciler.discover(state)

    assert second_result.drift_discovered == 0
    repository_host.get_prs_for_issue.assert_called_once_with(228, state="all")


def test_open_pr_and_failed_issue_refresh_propagates_without_freshness() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.side_effect = RepositoryHostError("github unavailable")

    with pytest.raises(RepositoryHostError, match="github unavailable"):
        AwaitingMergeReconciler(
            repository_host,
            clock=lambda: 1234.5,
        ).discover(state)

    assert entry.status == "completed"
    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}


def test_unexpected_issue_refresh_bug_propagates() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.side_effect = TypeError("programming bug")

    with pytest.raises(TypeError, match="programming bug"):
        AwaitingMergeReconciler(repository_host).discover(state)


def test_pr_fetch_failure_propagates_without_issue_fallback() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.side_effect = RepositoryHostError("github unavailable")
    repository_host.get_issue.return_value = _issue("closed")

    with pytest.raises(RepositoryHostError, match="github unavailable"):
        AwaitingMergeReconciler(
            repository_host,
            clock=lambda: 1234.5,
        ).discover(state)

    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert state.issue_refresh_timestamps == {}
    assert state.issue_last_refreshed_at == {}
    repository_host.get_issue.assert_not_called()
    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_missing_pr_clears_stale_rollup_bookkeeping() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_rollup_scan_timestamps={318: 1000.0},
    )
    repository_host = MagicMock()
    repository_host.get_pr.return_value = None
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(repository_host).discover(state)

    assert result.skipped == 1
    assert state.awaiting_merge_rollup_scan_timestamps == {}
    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_invalid_pr_url_is_skipped_without_repository_fetches() -> None:
    entry = _history_entry()
    entry.pr_url = "https://github.com/owner/repo/pull/not-a-number"
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()

    result = AwaitingMergeReconciler(repository_host).discover(state)

    assert result.checked == 1
    assert result.skipped == 1
    assert entry.status == "completed"
    repository_host.get_pr.assert_not_called()
    repository_host.read_pr_status_check_rollup.assert_not_called()
    repository_host.get_issue.assert_not_called()


def test_non_completed_history_entry_is_ignored() -> None:
    entry = _history_entry()
    entry.status = "merged"
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()

    result = AwaitingMergeReconciler(repository_host).discover(state)

    assert result.checked == 0
    assert result.discovered == 0
    repository_host.get_pr.assert_not_called()
    repository_host.read_pr_status_check_rollup.assert_not_called()
    repository_host.get_issue.assert_not_called()


def test_second_reconcile_pass_on_terminal_entry_is_noop() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")
    reconciler = AwaitingMergeReconciler(repository_host)

    first_result = reconciler.discover(state)
    action = ReconcileHistoryEntryAction(
        issue_number=228,
        pr_number=318,
        pr_url=entry.pr_url or "",
        status=first_result.reconciliations[0].status,
        source=first_result.reconciliations[0].source,
        reason=first_result.reconciliations[0].status_reason,
    )
    events = InMemoryEventSink()
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=events,
        history_owner=SessionHistoryOwner(state.session_history),
    )
    applier.apply(action)
    second_result = reconciler.discover(state)

    assert first_result.checked == 1
    assert first_result.discovered == 1
    assert second_result.checked == 0
    assert second_result.discovered == 0
    # First pass fetches the PR once (REST) and reconciles it terminal; the
    # second pass sees the now-terminal history entry and never re-fetches.
    # The terminal PR is never status-rollup-polled across either pass.
    repository_host.get_pr.assert_called_once_with(318)
    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert entry.status == "merged"
    assert events.last_event(EventName.HISTORY_RECONCILED.value) is not None


def test_open_pr_and_open_issue_remain_awaiting_merge_with_freshness_updated() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("open")
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.still_pending == 1
    assert result.discovered == 0
    assert entry.status == "completed"
    assert entry.status_reason == "Recovered awaiting merge state on startup"
    assert entry.pr_url == "https://github.com/owner/repo/pull/318"
    assert state.issue_refresh_timestamps[228] == 1234.5
    assert state.issue_last_refreshed_at[228] == 1234.5


def test_merge_conflict_after_review_discovers_post_publish_validation_rework() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="dirty",
            labels=["code-reviewed"],
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.discovered == 0
    assert result.rework_discovered == 1
    assert result.still_pending == 1
    rework = result.reworks[0]
    assert rework.issue_number == 228
    assert rework.pr_number == 318
    assert rework.branch_name == "228-cache-read-misses"
    assert rework.agent_type == "agent:backend"
    assert rework.rework_cycle == 1
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    assert "Mergeability: dirty" in (rework.feedback or "")
    # No existing marker comment → planner is free to post one.
    assert rework.feedback_comment_already_posted is False


def test_post_publish_rework_flags_existing_marker_comment_for_dedupe() -> None:
    """If the PR already carries the post-publish marker comment, the rework
    is still discovered (labels/queue idempotency is unchanged) but the
    discovery records that the feedback comment is already posted so the
    planner skips a duplicate."""
    from issue_orchestrator.control.awaiting_merge_reconciler import (
        POST_PUBLISH_VALIDATION_COMMENT_MARKER,
    )

    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open",
        mergeable_state="dirty",
        labels=["code-reviewed"],
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = True

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 1
    rework = result.reworks[0]
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    # Feedback is still carried (the rework agent needs it).
    assert "Mergeability: dirty" in (rework.feedback or "")
    # But the marker was already present, so flag it for the planner.
    assert rework.feedback_comment_already_posted is True
    # The dedupe scan must cover every comment page, not just the first.
    repository_host.issue_comment_marker_present.assert_called_once_with(
        318, POST_PUBLISH_VALIDATION_COMMENT_MARKER
    )


def test_post_publish_rework_comment_read_failure_propagates() -> None:
    """A comment-read failure during dedupe propagates like the other
    awaiting-merge reads, aborting the tick rather than silently risking a
    duplicate or dropping the feedback."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open",
        mergeable_state="dirty",
        labels=["code-reviewed"],
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.side_effect = RepositoryHostError(
        "boom"
    )

    with pytest.raises(RepositoryHostError):
        AwaitingMergeReconciler(
            repository_host,
            label_manager=_label_manager(),
            clock=lambda: 1234.5,
        ).discover(state)


def test_recent_rollup_poll_suppresses_post_publish_rework_until_interval_expires() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_rollup_scan_timestamps={318: 1000.0},
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="FAILURE",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1100.0,
        rollup_scan_interval_seconds=300.0,
    ).discover(state)

    assert result.checked == 1
    assert result.still_pending == 1
    assert result.rework_discovered == 0
    repository_host.get_pr.assert_called_once_with(318)
    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert state.awaiting_merge_rollup_scan_timestamps == {318: 1000.0}


def test_rollup_poll_after_interval_discovers_post_publish_rework() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_rollup_scan_timestamps={318: 1000.0},
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="FAILURE",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1400.0,
        rollup_scan_interval_seconds=300.0,
    ).discover(state)

    assert result.rework_discovered == 1
    repository_host.get_pr.assert_called_once_with(318)
    repository_host.read_pr_status_check_rollup.assert_called_once_with(318)
    assert state.awaiting_merge_rollup_scan_timestamps == {318: 1400.0}


def test_terminal_pr_detection_bypasses_recent_rollup_throttle() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_rollup_scan_timestamps={318: 1000.0},
    )
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")

    result = AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 1100.0,
        rollup_scan_interval_seconds=300.0,
    ).discover(state)

    assert result.discovered == 1
    assert result.reconciliations[0].status == "merged"
    repository_host.get_pr.assert_called_once_with(318)
    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert state.awaiting_merge_rollup_scan_timestamps == {}


def test_post_publish_validation_rework_is_suppressed_when_rework_already_pending() -> None:
    entry = _history_entry()
    pending_rework = MagicMock()
    pending_rework.resolve_issue_number.return_value = 228
    state = OrchestratorState(
        session_history=[entry],
        pending_reworks=[pending_rework],
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="behind",
            labels=["code-reviewed"],
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.checked == 1
    assert result.rework_discovered == 0
    assert result.still_pending == 1
    assert state.issue_last_refreshed_at[228] == 1234.5


# ---------------------------------------------------------------------------
# classify_post_approval_state — pure dispatch table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mergeable_state,rollup,expected",
    [
        # Happy path
        ("clean", None, "READY"),
        ("clean", "SUCCESS", "READY"),
        # Code-action causes
        ("dirty", None, "REWORK_CONFLICT"),
        ("dirty", "PENDING", "REWORK_CONFLICT"),  # conflict trumps checks
        ("behind", None, "REWORK_BEHIND"),
        ("behind", "SUCCESS", "REWORK_BEHIND"),
        # The big disambiguation: unstable + check status
        ("unstable", "PENDING", "WAIT_FOR_CHECKS"),
        ("unstable", "EXPECTED", "WAIT_FOR_CHECKS"),
        ("unstable", None, "WAIT_FOR_CHECKS"),
        ("unstable", "SUCCESS", "WAIT_FOR_CHECKS"),  # unusual; resolves to clean
        ("unstable", "FAILURE", "REWORK_CHECK_FAILED"),
        ("unstable", "ERROR", "REWORK_CHECK_FAILED"),
        # blocked is similar but blocked+SUCCESS means branch protection
        ("blocked", "PENDING", "WAIT_FOR_CHECKS"),
        ("blocked", "FAILURE", "REWORK_CHECK_FAILED"),
        ("blocked", "ERROR", "REWORK_CHECK_FAILED"),
        ("blocked", "SUCCESS", "BLOCKED_TERMINAL"),
        ("blocked", None, "WAIT_FOR_CHECKS"),
        # Unknown / unhandled GitHub states fall through
        ("has_hooks", None, "UNKNOWN"),
        ("draft", None, "UNKNOWN"),
        ("", None, "UNKNOWN"),
        (None, None, "UNKNOWN"),
    ],
)
def test_classify_post_approval_state(
    mergeable_state: str | None, rollup: str | None, expected: str
) -> None:
    pr = _pr(
        "open", mergeable_state=mergeable_state, status_check_rollup=rollup
    )
    assert classify_post_approval_state(pr) == expected


# ---------------------------------------------------------------------------
# classify_pr_set_drift — owner of the blocked:pr-closed precedence policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prs,expected_drifting,expected_pr_number",
    [
        # No associated PR at all → "PR missing" drift with no PR reference.
        ([], True, None),
        # Single terminal PR: the leaf predicate decides.
        ([("closed", 318)], True, 318),
        ([("merged", 318)], False, None),
        # Any open PR suppresses drift regardless of older terminal PRs.
        ([("merged", 428), ("open", 437)], False, None),
        ([("closed", 428), ("open", 437)], False, None),
        # Latest terminal PR decides: a newer merge beats an older close.
        ([("closed", 428), ("merged", 437)], False, None),
        # ...and a newer close beats an older merge, keying on the newer PR.
        ([("merged", 428), ("closed", 437)], True, 437),
        # Multiple closed PRs → the latest closed one.
        ([("closed", 428), ("closed", 437)], True, 437),
    ],
)
def test_classify_pr_set_drift(
    prs: list[tuple[str, int]],
    expected_drifting: bool,
    expected_pr_number: int | None,
) -> None:
    decision = classify_pr_set_drift(
        [_pr(state, number=number) for state, number in prs]
    )
    assert decision.drifting is expected_drifting
    if expected_pr_number is None:
        assert decision.pr is None
    else:
        assert decision.pr is not None
        assert decision.pr.number == expected_pr_number


# ---------------------------------------------------------------------------
# Reconciler integration: classifier outputs gate rework
# ---------------------------------------------------------------------------


def test_unstable_pr_with_checks_pending_does_not_trigger_rework() -> None:
    """Regression: this is the spurious-rework case the user observed.

    Reviewer approved (code-reviewed label) and GitHub reports
    `mergeable_state=unstable` because required CI checks are still
    in progress. The orchestrator must wait, not start rework.
    """
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="PENDING",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 0
    assert result.still_pending == 1


def test_unstable_pr_with_check_failure_triggers_check_failed_rework() -> None:
    """Inverse case: a required check actually failed → rework with check-failed copy."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="FAILURE",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 1
    rework = result.reworks[0]
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    feedback = rework.feedback or ""
    assert "Required check failed" in feedback
    assert "Status checks: FAILURE" in feedback
    # Legacy header is gone
    assert "POST-PUBLISH VALIDATION FAILURE" not in feedback


def test_blocked_pr_with_all_checks_passing_escalates_immediately() -> None:
    """blocked + SUCCESS rollup → branch protection (approvals/CODEOWNERS),
    not a code problem. Code rework can't unstick this so we escalate
    immediately rather than waiting on the checks-pending timeout."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="blocked",
            labels=["code-reviewed"],
            status_check_rollup="SUCCESS",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 0
    assert result.escalation_discovered == 1
    esc = result.escalations[0]
    assert esc.kind == "branch_protection_blocked"
    assert "Branch protection" in esc.reason


def test_post_publish_escalation_is_suppressed_when_pr_already_needs_human() -> None:
    entry = _history_entry()
    label_manager = _label_manager()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="blocked",
            labels=[label_manager.code_reviewed, label_manager.needs_human],
            status_check_rollup="SUCCESS",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=label_manager,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 0
    assert result.escalation_discovered == 0
    assert 228 not in state.awaiting_merge_checks_pending_since


def test_needs_human_pr_with_now_readable_failure_recovers_to_rework() -> None:
    entry = _history_entry()
    label_manager = _label_manager()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=[label_manager.code_reviewed, label_manager.needs_human],
            status_check_rollup="FAILURE",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=label_manager,
        clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 1
    assert result.escalation_discovered == 0
    rework = result.reworks[0]
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    assert rework.clear_needs_human is True


def test_dirty_pr_feedback_uses_conflict_copy() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="dirty",
            labels=["code-reviewed"],
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    feedback = (result.reworks[0].feedback or "")
    assert "Merge conflict against base branch" in feedback
    assert "Mergeability: dirty" in feedback


def test_behind_pr_feedback_uses_rebase_copy() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="behind",
            labels=["code-reviewed"],
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    feedback = (result.reworks[0].feedback or "")
    assert "Branch is behind base branch" in feedback
    assert "Rebase" in feedback


# ---------------------------------------------------------------------------
# WAIT_FOR_CHECKS timeout state machine
# ---------------------------------------------------------------------------


def _wait_for_checks_repo() -> MagicMock:
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="PENDING",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    return repository_host


def test_wait_for_checks_first_seen_records_timestamp_and_does_not_escalate() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])

    result = AwaitingMergeReconciler(
        _wait_for_checks_repo(),
        label_manager=_label_manager(),
        clock=lambda: 1000.0,
        post_publish_checks_pending_timeout_seconds=1800.0,
    ).discover(state)

    assert result.escalation_discovered == 0
    assert result.rework_discovered == 0
    assert state.awaiting_merge_checks_pending_since[228] == 1000.0


def test_wait_for_checks_within_timeout_holds_steady() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )

    # 5 minutes after first-seen, well below the 30-minute default.
    result = AwaitingMergeReconciler(
        _wait_for_checks_repo(),
        label_manager=_label_manager(),
        clock=lambda: 1300.0,
        post_publish_checks_pending_timeout_seconds=1800.0,
    ).discover(state)

    assert result.escalation_discovered == 0
    assert state.awaiting_merge_checks_pending_since[228] == 1000.0


def test_wait_for_checks_past_timeout_escalates_with_explanation() -> None:
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )

    # 31 minutes after first-seen — past the 30-minute default.
    result = AwaitingMergeReconciler(
        _wait_for_checks_repo(),
        label_manager=_label_manager(),
        clock=lambda: 1000.0 + 31 * 60,
        post_publish_checks_pending_timeout_seconds=1800.0,
    ).discover(state)

    assert result.escalation_discovered == 1
    esc = result.escalations[0]
    assert esc.issue_number == 228
    assert esc.pr_number == 318
    assert esc.kind == "checks_pending_timeout"
    assert "31 minute" in esc.reason
    assert "30 minutes" in esc.reason  # configured timeout displayed


def test_wait_for_checks_resolved_clears_pending_since() -> None:
    """When the PR moves out of WAIT_FOR_CHECKS (e.g., checks finished),
    the pending-since timestamp is cleared so a future stall starts a
    fresh budget instead of inheriting an old one."""
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="clean",
            labels=["code-reviewed"],
            status_check_rollup="SUCCESS",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 2000.0,
    ).discover(state)

    assert 228 not in state.awaiting_merge_checks_pending_since


def test_wait_for_checks_then_label_dropped_then_reapproval_does_not_immediately_escalate() -> None:
    """Regression: when a PR loses its post-approval eligibility (e.g.
    a new commit drops the `code-reviewed` label), the WAIT_FOR_CHECKS
    bookkeeping must be cleared. Otherwise a much later re-approval
    inherits the stale timestamp, computes elapsed >> timeout, and
    escalates immediately — defeating the timeout entirely.
    """
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        # Stale timestamp from a much earlier observation.
        awaiting_merge_checks_pending_since={228: 1000.0},
    )

    # Tick 1: PR has lost the `code-reviewed` label (new commit landed,
    # reviewer hasn't re-approved yet). Eligibility gate fails.
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=[],  # No code-reviewed label
            status_check_rollup="PENDING",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1000.0 + 60 * 60,  # an hour later
    ).discover(state)

    # Stale timestamp must be cleared even though we never reached
    # `_discover_post_publish_followup`'s WAIT_FOR_CHECKS branch.
    assert 228 not in state.awaiting_merge_checks_pending_since

    # Tick 2: reviewer re-approves; PR is back in WAIT_FOR_CHECKS.
    # Because tick 1 cleared the dict, this run records `now` afresh
    # and does not escalate (elapsed = 0).
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="PENDING",
        ),
    )

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1000.0 + 60 * 60 + 60,  # one minute after tick 1
        post_publish_checks_pending_timeout_seconds=1800.0,
    ).discover(state)

    assert result.escalation_discovered == 0
    # Fresh timestamp, not the stale 1000.0.
    assert state.awaiting_merge_checks_pending_since[228] == 1000.0 + 60 * 60 + 60


def test_terminal_pr_clears_pending_checks_bookkeeping() -> None:
    """When the PR moves to a terminal state (merged/closed), drop any
    WAIT_FOR_CHECKS bookkeeping for that issue so the dict doesn't
    leak across PR lifecycles."""
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("merged")

    AwaitingMergeReconciler(
        repository_host,
        clock=lambda: 5000.0,
    ).discover(state)

    assert 228 not in state.awaiting_merge_checks_pending_since


def test_wait_for_checks_resolved_into_failure_clears_pending_and_reworks() -> None:
    """If checks finish and one fails, we clear pending_since AND emit
    rework on the same tick — no extra ticks of latency."""
    entry = _history_entry()
    state = OrchestratorState(
        session_history=[entry],
        awaiting_merge_checks_pending_since={228: 1000.0},
    )
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="FAILURE",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1500.0,
    ).discover(state)

    assert 228 not in state.awaiting_merge_checks_pending_since
    assert result.rework_discovered == 1
    assert result.escalation_discovered == 0


# ---------------------------------------------------------------------------
# Status-rollup eligibility: bound the rollup read to decisive PRs only (#6600)
# ---------------------------------------------------------------------------


def test_terminal_pr_is_never_status_rollup_polled_across_repeated_ticks() -> None:
    """The core #6600 fix: a closed/merged PR that gets revisited on
    successive ticks must never pay the status-rollup GraphQL round-trip
    (or hit its permission wall). Only the cheap REST fetch runs."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr("closed")
    reconciler = AwaitingMergeReconciler(repository_host, clock=lambda: 1.0)

    # The history entry stays "completed" here (no action applier), so the
    # entry is re-examined on every tick — exactly the revisit pattern the
    # issue reported.
    reconciler.discover(state)
    reconciler.discover(state)
    reconciler.discover(state)

    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert repository_host.get_pr.call_count == 3


@pytest.mark.parametrize("mergeable_state", ["clean", "dirty", "behind"])
def test_non_decisive_open_pr_is_not_status_rollup_polled(mergeable_state: str) -> None:
    """The rollup only changes the decision for unstable/blocked PRs. For
    clean (READY), dirty (REWORK_CONFLICT), and behind (REWORK_BEHIND) the
    classifier ignores it, so no rollup read should ever fire."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state=mergeable_state, labels=["code-reviewed"]
    )
    repository_host.get_issue.return_value = _issue("open")

    AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_decisive_unstable_pr_reads_rollup_exactly_once() -> None:
    """An open, reviewer-approved, unstable PR is the one shape that needs
    the rollup — and it pays for exactly one GraphQL read."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr(
            "open",
            mergeable_state="unstable",
            labels=["code-reviewed"],
            status_check_rollup="PENDING",
        ),
    )
    repository_host.get_issue.return_value = _issue("open")

    AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
    ).discover(state)

    repository_host.read_pr_status_check_rollup.assert_called_once_with(318)


# ---------------------------------------------------------------------------
# Status-rollup permission failures: loud, actionable, and bounded (#6600)
# ---------------------------------------------------------------------------


def _permission_denied_repo() -> MagicMock:
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="unstable", labels=["code-reviewed"]
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="permission_denied", primary_source_denied=True
    )
    return repository_host


def test_decisive_pr_with_rollup_permission_denied_escalates_loudly() -> None:
    """A decision genuinely needs the rollup but the token can't read it.
    The orchestrator must surface a clear, actionable escalation rather
    than silently waiting forever behind a PENDING default."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = _permission_denied_repo()

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1000.0,
        repo="owner/repo",
    ).discover(state)

    assert result.rework_discovered == 0
    assert result.escalation_discovered == 1
    esc = result.escalations[0]
    assert esc.kind == "status_rollup_permission_denied"
    assert esc.issue_number == 228
    assert esc.pr_number == 318
    # Names the token capability and a concrete next action.
    assert "statusCheckRollup" in esc.reason
    assert "scope" in esc.reason
    # Not a timing wait — no WAIT_FOR_CHECKS bookkeeping is left behind.
    assert 228 not in state.awaiting_merge_checks_pending_since


def test_rollup_permission_denial_bounds_graphql_but_keeps_fallback() -> None:
    """Once the GraphQL source is observed to lack rollup-read capability, the
    next tick must NOT re-issue the wasted GraphQL probe (nor re-log the same
    permission error). The gate still reads the REST fallback each tick
    (``skip_primary_source=True``) so a now-readable failure is never masked,
    and the per-PR diagnostic still surfaces — bounded repo-wide, not dropped."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = _permission_denied_repo()
    reconciler = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1000.0,
        repo="owner/repo",
    )

    first = reconciler.discover(state)
    second = reconciler.discover(state)

    # First tick probes GraphQL; the second is inside the backoff window and
    # reads ONLY the REST fallback — the GraphQL probe is not re-issued.
    assert repository_host.read_pr_status_check_rollup.call_args_list == [
        call(318),
        call(318, skip_primary_source=True),
    ]
    # The PR is still genuinely blocked, so the diagnostic still fires —
    # bounded downstream by the needs_human label, not hidden.
    assert first.escalation_discovered == 1
    assert second.escalation_discovered == 1
    assert state.status_rollup_capability.permission_denied_since == 1000.0


def test_needs_human_pr_recovers_to_rework_during_graphql_backoff() -> None:
    """Repo-wide GraphQL backoff must NOT mask a now-readable failure: a stale
    needs-human PR whose REST fallback now reports FAILURE recovers to rework
    even while the GraphQL backoff window is active (issue #6589 F1/A1)."""
    entry = _history_entry()
    label_manager = _label_manager()
    state = OrchestratorState(session_history=[entry])
    # GraphQL was denied earlier this hour; the backoff window is still active.
    state.status_rollup_capability.permission_denied_since = 1000.0
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open",
        mergeable_state="unstable",
        labels=[label_manager.code_reviewed, label_manager.needs_human],
    )
    repository_host.get_issue.return_value = _issue("open")
    # The gate reads the REST fallback (skip_primary_source=True) during the
    # backoff window; it now classifies a failure GraphQL could not read.
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="FAILURE", capability="ok", primary_source_denied=True
    )

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=label_manager,
        clock=lambda: 1500.0,  # within the 3600s window
        repo="owner/repo",
    ).discover(state)

    # The fallback was read despite the active backoff.
    repository_host.read_pr_status_check_rollup.assert_called_once_with(
        318, skip_primary_source=True
    )
    assert result.escalation_discovered == 0
    assert result.rework_discovered == 1
    rework = result.reworks[0]
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    assert rework.clear_needs_human is True
    # A fallback-only read cannot prove GraphQL recovered — window preserved.
    assert state.status_rollup_capability.permission_denied_since == 1000.0


def test_decisive_pr_reworks_via_rest_fallback_during_graphql_backoff() -> None:
    """A repo-wide GraphQL backoff (armed by an earlier PR) must not mask a
    DIFFERENT decisive PR whose REST fallback reports FAILURE — it still
    reworks rather than waiting forever behind the backoff (#6589 F1/A1)."""
    entry = _history_entry()
    label_manager = _label_manager()
    state = OrchestratorState(session_history=[entry])
    state.status_rollup_capability.permission_denied_since = 1000.0
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="unstable", labels=[label_manager.code_reviewed]
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="FAILURE", capability="ok", primary_source_denied=True
    )

    result = AwaitingMergeReconciler(
        repository_host,
        label_manager=label_manager,
        clock=lambda: 1500.0,
        repo="owner/repo",
    ).discover(state)

    repository_host.read_pr_status_check_rollup.assert_called_once_with(
        318, skip_primary_source=True
    )
    assert result.rework_discovered == 1
    assert result.escalation_discovered == 0
    rework = result.reworks[0]
    assert rework.source == POST_PUBLISH_VALIDATION_SOURCE
    # No stale needs-human on this PR, so nothing to clear.
    assert rework.clear_needs_human is False
    assert state.status_rollup_capability.permission_denied_since == 1000.0


def test_rollup_permission_backoff_expires_and_re_probes() -> None:
    """After the backoff window the gate re-probes once, so a token that
    has been fixed self-heals without an orchestrator restart."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = _permission_denied_repo()
    now = {"t": 1000.0}

    reconciler = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: now["t"],
        repo="owner/repo",
        status_rollup_backoff_seconds=3600.0,
    )

    reconciler.discover(state)  # observes denial, starts backoff
    now["t"] = 1000.0 + 3600.0 + 1  # just past the window
    reconciler.discover(state)  # re-probes

    assert repository_host.read_pr_status_check_rollup.call_count == 2


def test_decisive_pr_with_transient_rollup_error_waits_and_retries() -> None:
    """A transient rollup failure is NOT a permission problem: treat it as
    PENDING-equivalent (wait), do not escalate, and retry after the
    per-PR rollup cadence expires."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="unstable", labels=["code-reviewed"]
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="transient_error"
    )
    now = {"t": 1000.0}
    reconciler = AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: now["t"],
        rollup_scan_interval_seconds=300.0,
    )

    result = reconciler.discover(state)

    assert result.escalation_discovered == 0
    assert result.rework_discovered == 0
    # PENDING-equivalent → WAIT_FOR_CHECKS bookkeeping recorded, not escalated.
    assert state.awaiting_merge_checks_pending_since[228] == 1000.0
    assert state.status_rollup_capability.permission_denied_since is None
    assert state.awaiting_merge_rollup_scan_timestamps == {318: 1000.0}

    reconciler.discover(state)
    # Transient does not trip the permission backoff, but the PR cadence
    # still suppresses the expensive retry.
    assert repository_host.read_pr_status_check_rollup.call_count == 1

    now["t"] = 1301.0
    reconciler.discover(state)
    assert repository_host.read_pr_status_check_rollup.call_count == 2


# --------------------------------------------------------------------------- #
# Merge queue mode (merge_queue.enabled)
# --------------------------------------------------------------------------- #


def _merge_queue(repository_host: MagicMock, *, failure_action: str = "rework"):
    from issue_orchestrator.control.merge_queue_coordinator import MergeQueueCoordinator
    from issue_orchestrator.events import EventContext
    from issue_orchestrator.infra.config_models import MergeQueueConfig

    return MergeQueueCoordinator(
        config=MergeQueueConfig(enabled=True, failure_action=failure_action),
        repository_host=repository_host,
        events=InMemoryEventSink(),
        event_context=EventContext(),
        label_manager=_label_manager(),
    )


def _reconciler_with_merge_queue(
    repository_host: MagicMock, *, failure_action: str = "rework"
) -> AwaitingMergeReconciler:
    return AwaitingMergeReconciler(
        repository_host,
        label_manager=_label_manager(),
        clock=lambda: 1234.5,
        merge_queue=_merge_queue(repository_host, failure_action=failure_action),
    )


def test_merge_queue_behind_base_pr_is_enqueued_not_reworked() -> None:
    """The headline behavior: with merge queue enabled, an approved PR that is
    only *behind base* is enqueued, NOT sent to rework."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="behind", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = MergeQueueRead.absent()
    repository_host.get_issue.return_value = _issue("open")

    result = _reconciler_with_merge_queue(repository_host).discover(state)

    assert result.enqueue_discovered == 1
    assert result.rework_discovered == 0
    assert result.enqueues[0].pr_number == 318
    assert result.enqueues[0].issue_number == 228


def test_merge_queue_disabled_still_reworks_behind_base_pr() -> None:
    """Without merge queue, the existing behind-base rework behavior is intact."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    _wire_pr(
        repository_host,
        _pr("open", mergeable_state="behind", labels=["code-reviewed"]),
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = AwaitingMergeReconciler(
        repository_host, label_manager=_label_manager(), clock=lambda: 1234.5,
    ).discover(state)

    assert result.rework_discovered == 1
    assert result.enqueue_discovered == 0


def test_merge_queue_conflict_still_routes_to_rework() -> None:
    """Merge conflicts cannot be fixed by the queue, so they still go to rework."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="dirty", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = MergeQueueRead.absent()
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = _reconciler_with_merge_queue(repository_host).discover(state)

    assert result.rework_discovered == 1
    assert result.enqueue_discovered == 0


def test_merge_queue_already_queued_pr_waits() -> None:
    """A PR already in the queue is observed, not re-enqueued or reworked."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="blocked", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = MergeQueueRead.present(
        MergeQueueEntry("QUEUED", position=1)
    )
    repository_host.get_issue.return_value = _issue("open")

    result = _reconciler_with_merge_queue(repository_host).discover(state)

    assert result.enqueue_discovered == 0
    assert result.rework_discovered == 0
    assert result.escalation_discovered == 0
    # An already-queued PR must not pay for a status-rollup read.
    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_merge_queue_indeterminate_read_takes_no_action() -> None:
    """A queue-read failure / unmodeled state must NOT enqueue, rework, or
    escalate, and must not even pay for a status-rollup read. It is observed
    again next tick instead of acting on the PR's (possibly stale) status."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    # A behind-base PR that, if the read were mistaken for ABSENT, would enqueue.
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="behind", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = (
        MergeQueueRead.indeterminate()
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = _reconciler_with_merge_queue(repository_host).discover(state)

    assert result.enqueue_discovered == 0
    assert result.rework_discovered == 0
    assert result.escalation_discovered == 0
    repository_host.read_pr_status_check_rollup.assert_not_called()


def test_merge_queue_unreadable_entry_does_not_enqueue() -> None:
    """End-to-end through the coordinator's own read path: a RepositoryHostError
    from the provider resolves to INDETERMINATE, so no fact is produced."""
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="behind", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.side_effect = RepositoryHostError("boom")
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = _reconciler_with_merge_queue(repository_host).discover(state)

    assert result.enqueue_discovered == 0
    assert result.rework_discovered == 0
    assert result.escalation_discovered == 0


def test_merge_queue_failure_routes_to_rework() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="blocked", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = MergeQueueRead.present(
        MergeQueueEntry("UNMERGEABLE")
    )
    repository_host.get_issue.return_value = _issue("open")
    repository_host.issue_comment_marker_present.return_value = False

    result = _reconciler_with_merge_queue(
        repository_host, failure_action="rework"
    ).discover(state)

    assert result.rework_discovered == 1
    assert result.enqueue_discovered == 0


def test_merge_queue_failure_routes_to_needs_human() -> None:
    entry = _history_entry()
    state = OrchestratorState(session_history=[entry])
    repository_host = MagicMock()
    repository_host.get_pr.return_value = _pr(
        "open", mergeable_state="blocked", labels=["code-reviewed"]
    )
    repository_host.read_merge_queue_entry.return_value = MergeQueueRead.present(
        MergeQueueEntry("UNMERGEABLE")
    )
    repository_host.get_issue.return_value = _issue("open")

    result = _reconciler_with_merge_queue(
        repository_host, failure_action="needs_human"
    ).discover(state)

    assert result.escalation_discovered == 1
    assert result.escalations[0].kind == "merge_queue_failed"
    assert result.rework_discovered == 0
