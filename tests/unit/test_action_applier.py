"""Unit tests for ActionApplier."""

import logging
import pytest
from unittest.mock import MagicMock, Mock, patch
from pathlib import Path

from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError
from issue_orchestrator.control.actions import (
    ActionType,
    ActionResultType,
    AddLabelAction,
    RemoveLabelAction,
    AddCommentAction,
    SyncLabelsAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    StopSessionAction,
    QueueReviewAction,
    EnqueueToMergeQueueAction,
    EscalateToHumanAction,
    CreateTechLeadIssueAction,
    SurfaceTechLeadProposalAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
    ReconcileHistoryEntryAction,
    RecoverTerminalIssueAction,
    ShedRecoveredWorkflowLabelsAction,
    SupersedePullRequestAction,
    CloseIssueAction,
    SetIssueStateAction,
)
from issue_orchestrator.control.orchestrator_support import (
    init_orchestrator_components,
)
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import (
    AgentConfig,
    DiscoveredFailure,
    Issue,
    Session,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.claim_manager import ClaimManager
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore


@pytest.fixture
def mock_labels():
    """Create a mock LabelSet."""
    labels = MagicMock()
    labels.has_label.return_value = False
    return labels


@pytest.fixture
def mock_sessions():
    """Create a mock SessionManager."""
    sessions = MagicMock()
    sessions.exists.return_value = False
    sessions.start.return_value = True
    return sessions


@pytest.fixture
def mock_events():
    """Create a mock EventSink."""
    events = MagicMock()
    return events


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    repo = MagicMock()
    repo.create_issue.return_value = 123
    return repo


@pytest.fixture
def mock_fresh_issue_reader():
    """Create a mock FreshIssueReader."""
    reader = MagicMock()
    reader.read_issue_labels.return_value = []
    return reader


@pytest.fixture
def mock_worktree_manager():
    """Create a mock WorktreeManager."""
    wm = MagicMock()
    return wm


@pytest.fixture
def applier(
    mock_labels,
    mock_sessions,
    mock_events,
    mock_repository_host,
    mock_worktree_manager,
    mock_fresh_issue_reader,
):
    """Create an ActionApplier with mocks."""
    from issue_orchestrator.control.tech_lead_run_ownership import (
        single_instance_run_ownership,
    )

    return ActionApplier(
        labels=mock_labels,
        sessions=mock_sessions,
        events=mock_events,
        repository_host=mock_repository_host,
        worktree_manager=mock_worktree_manager,
        fresh_issue_reader=mock_fresh_issue_reader,
        reconcile=False,
        # A whole-repository anchor is RESERVED before it is created (#6994
        # round 2 F3), so the applier that owns the create needs the run owner.
        run_ownership=single_instance_run_ownership(),
    )


class TestAddLabelAction:
    """Tests for ADD_LABEL action."""

    def test_add_label_success(self, applier, mock_labels):
        """Test successful label addition."""
        action = AddLabelAction(
            issue_number=123,
            label="in-progress",
            reason="launch session",
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "in-progress")

    def test_add_label_logs_reason_on_success(self, applier, caplog):
        """Successful label mutations should include the triggering reason."""
        action = AddLabelAction(
            issue_number=123,
            label="publish-failed",
            reason="publish blocked: branch missing",
        )

        with caplog.at_level(logging.INFO):
            result = applier.apply(action)

        assert result.success
        assert any(
            "Label mutation: op=add outcome=applied label=publish-failed reason=publish blocked: branch missing"
            in message
            for message in caplog.messages
        )

    def test_add_label_failure(self, applier, mock_labels):
        """Test label addition failure."""
        mock_labels.add_label.side_effect = Exception("API error")
        action = AddLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error

    def test_add_label_noop_when_already_present(self, applier, mock_labels):
        """Skip add_label mutation when label is already present."""
        mock_labels.has_label.return_value = True
        action = AddLabelAction(
            issue_number=123,
            label="in-progress",
            reason="launch session",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        mock_labels.add_label.assert_not_called()

    def test_add_label_raises_when_claim_lost(self, applier, mock_labels, mock_events):
        """Claim verification blocks external mutation when ownership is lost."""
        claim_manager = MagicMock(spec=ClaimManager)
        claim_manager.check_winner.return_value = False

        applier.claim_gate = ClaimGate(claim_manager, mock_events)
        applier.lease_id_lookup = lambda issue_number: "lease-123" if issue_number == 123 else None
        action = AddLabelAction(issue_number=123, label="in-progress")

        with pytest.raises(ClaimLostError, match="Claim lost for issue #123 before add_label"):
            applier.apply(action)

        claim_manager.check_winner.assert_called_once_with(123, "lease-123")
        mock_labels.add_label.assert_not_called()


class TestRemoveLabelAction:
    """Tests for REMOVE_LABEL action."""

    def test_remove_label_success(self, applier, mock_labels):
        """Test successful label removal."""
        mock_labels.has_label.return_value = True
        action = RemoveLabelAction(
            issue_number=123,
            label="in-progress",
            reason="session completed",
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.remove_label.assert_called_once_with(123, "in-progress")

    def test_remove_label_logs_reason_on_failure(self, applier, mock_labels, caplog):
        """Failed label removals should log the reason and error detail."""
        mock_labels.has_label.return_value = True
        mock_labels.remove_label.side_effect = Exception("API error")
        action = RemoveLabelAction(
            issue_number=123,
            label="pr-pending",
            reason="rework needed for PR #77",
        )

        with caplog.at_level(logging.INFO):
            result = applier.apply(action)

        assert not result.success
        assert any(
            "Label mutation: op=remove outcome=failed label=pr-pending reason=rework needed for PR #77 detail=API error"
            in message
            for message in caplog.messages
        )

    def test_remove_label_failure(self, applier, mock_labels):
        """Test label removal failure."""
        mock_labels.has_label.return_value = True
        mock_labels.remove_label.side_effect = Exception("API error")
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert not result.success
        assert "API error" in result.error

    def test_remove_label_noop_when_already_absent(self, applier, mock_labels):
        """Skip remove_label mutation when label is already absent."""
        applier.reconcile = True
        applier.fresh_issue_reader.read_issue_labels.return_value = []
        mock_labels.has_label.return_value = False
        action = RemoveLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        mock_labels.remove_label.assert_not_called()


class TestSetIssueStateAction:
    """Tests for SET_ISSUE_STATE action."""

    def test_set_issue_state_opens_issue(self, applier, mock_repository_host):
        action = SetIssueStateAction(
            issue_number=365,
            state="open",
            reason="retrospective review requested coder rework",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["issue_number"] == 365
        assert result.details["state"] == "open"
        mock_repository_host.update_issue_state.assert_called_once_with(365, "open")

    def test_set_issue_state_failure_returns_action_result(
        self,
        applier,
        mock_repository_host,
    ):
        mock_repository_host.update_issue_state.side_effect = RuntimeError("api error")
        action = SetIssueStateAction(issue_number=365, state="open")

        result = applier.apply(action)

        assert not result.success
        assert "api error" in result.error

    def test_set_issue_state_rejects_invalid_state(self):
        with pytest.raises(ValueError, match="state must be 'open' or 'closed'"):
            SetIssueStateAction(issue_number=365, state="reopened")


class TestReconcileHistoryEntryAction:
    """Tests for RECONCILE_HISTORY_ENTRY action."""

    def test_reconcile_history_entry_mutates_history_and_emits_event(self, applier, mock_events):
        """History reconciliation is applied through the history owner."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            issue_key="M1-228",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        assert entry.status == "merged"
        assert entry.status_reason == "PR merged; awaiting merge reconciled"
        published = [call.args[0] for call in mock_events.publish.call_args_list]
        history_events = [
            evt for evt in published
            if evt.name == EventName.HISTORY_RECONCILED.value
        ]
        assert len(history_events) == 1
        event = history_events[0]
        assert event.data["issue_number"] == 228
        assert event.data["issue_key"] == "M1-228"
        assert event.data["previous_status"] == "completed"
        assert event.data["status"] == "merged"

        # When the awaiting-merge reconciliation lands on the "merged"
        # terminal state, the orchestrator must also publish REVIEW_MERGED
        # so the user-facing timeline carries a "PR merged" event. Without
        # this, the dashboard sees only a debug-only HISTORY_RECONCILED
        # record after a successful merge.
        merged_events = [
            evt for evt in published
            if evt.name == EventName.REVIEW_MERGED.value
        ]
        assert len(merged_events) == 1, (
            "Expected exactly one REVIEW_MERGED event for a merged-status "
            f"reconciliation; saw events: {[e.name for e in published]}"
        )
        merged = merged_events[0]
        assert merged.data["issue_number"] == 228
        assert merged.data["pr_number"] == 318
        assert merged.data["pr_url"] == "https://github.com/test/repo/pull/318"
        assert merged.data["issue_key"] == "M1-228"
        assert merged.data["source"] == "pull_request"

    def test_merged_area_tagged_issue_records_durable_shipped_fix(
        self, applier
    ) -> None:
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Repair DB seam",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=10,
            pr_url="https://github.com/test/repo/pull/318",
            issue_labels=("Area:db",),
        )
        store = InMemoryTechLeadAuthorityStore()
        applier.history_owner = SessionHistoryOwner([entry])
        applier.tech_lead_ops = store

        result = applier.apply(ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        ))

        assert result.success
        [fix] = store.list_recent_shipped_fixes(limit=10)
        assert (fix.issue_number, fix.title, fix.area) == (
            228,
            "Repair DB seam",
            "db",
        )

    def test_shipped_fix_persistence_failure_leaves_history_retryable(
        self, applier
    ) -> None:
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Repair DB seam",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=10,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
            issue_labels=("area:db",),
        )
        store = MagicMock()
        store.record_shipped_fix.side_effect = RuntimeError("disk full")
        applier.history_owner = SessionHistoryOwner([entry])
        applier.tech_lead_ops = store

        result = applier.apply(ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        ))

        assert not result.success
        assert result.error == "disk full"
        assert entry.status == "completed"
        assert entry.status_reason == "Recovered awaiting merge state on startup"

    def test_area_tagged_merge_requires_durable_store(self, applier) -> None:
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Repair DB seam",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=10,
            pr_url="https://github.com/test/repo/pull/318",
            issue_labels=("area:db",),
        )
        applier.history_owner = SessionHistoryOwner([entry])

        result = applier.apply(ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        ))

        assert not result.success
        assert "required to record" in result.error
        assert entry.status == "completed"

    def test_closed_area_tagged_issue_is_not_a_shipped_fix(self, applier) -> None:
        entry = SessionHistoryEntry(
            issue_number=229,
            title="Abandoned DB repair",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=10,
            pr_url="https://github.com/test/repo/pull/319",
            issue_labels=("area:db",),
        )
        store = MagicMock()
        applier.history_owner = SessionHistoryOwner([entry])
        applier.tech_lead_ops = store

        result = applier.apply(ReconcileHistoryEntryAction(
            issue_number=229,
            pr_number=319,
            pr_url="https://github.com/test/repo/pull/319",
            status="closed",
            source="pull_request",
            reason="PR closed without merge",
        ))

        assert result.success
        store.record_shipped_fix.assert_not_called()

    def test_reconcile_history_entry_closed_does_not_emit_review_merged(
        self,
        applier,
        mock_events,
    ):
        """Closed (not merged) PRs must not produce a REVIEW_MERGED event.

        Closed-without-merge is a different end state: the user-facing
        timeline should not show a "PR merged" event when the PR was
        actually abandoned. HISTORY_RECONCILED still fires (so the
        orchestrator records the reconciliation) but REVIEW_MERGED must
        not.
        """
        entry = SessionHistoryEntry(
            issue_number=229,
            title="Abandoned PR",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/319",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=229,
            pr_number=319,
            pr_url="https://github.com/test/repo/pull/319",
            status="closed",
            source="pull_request",
            issue_key="M1-229",
            reason="PR closed without merge; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        published = [call.args[0] for call in mock_events.publish.call_args_list]
        names = [evt.name for evt in published]
        assert EventName.HISTORY_RECONCILED.value in names
        assert EventName.REVIEW_MERGED.value not in names, (
            "REVIEW_MERGED leaked from a closed-without-merge reconciliation; "
            f"events published: {names}"
        )

    def test_reconcile_history_entry_requires_history_owner(self, applier):
        """Applying a history reconciliation without an owner fails loudly."""
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert not result.success
        assert result.error == "Session history owner is not configured"

    def test_reconcile_history_entry_noops_without_event_when_latest_match_terminal(
        self,
        applier,
        mock_events,
    ):
        """An already-terminal latest history entry is idempotent and quiet."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="merged",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR merged; awaiting merge reconciled",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        assert result.details["noop_reason"] == "not_reconcilable"
        assert result.details["current_status"] == "merged"
        event_names = [call.args[0].name for call in mock_events.publish.call_args_list]
        assert EventName.HISTORY_RECONCILED.value not in event_names

    def test_reconcile_history_entry_warns_when_matching_history_entry_missing(
        self,
        applier,
        caplog,
    ):
        """A missing history entry is a visible no-op, not a silent idempotency path."""
        applier.history_owner = SessionHistoryOwner([])
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed; awaiting merge reconciled",
        )

        with caplog.at_level(logging.WARNING):
            result = applier.apply(action)

        assert result.success
        assert result.details["no_op"] is True
        assert result.details["noop_reason"] == "missing"
        assert "history reconciliation missing entry" in caplog.text

    def test_reconcile_history_entry_releases_pair_on_merged(
        self, applier, mock_events,
    ):
        """PR-merge release boundary (ADR 0026 / B2 review feedback,
        PR #6212 finding 1). When awaiting-merge reconciliation flips
        the entry to ``merged``, the persistent exchange pair must be
        released — otherwise a successfully merged issue keeps its
        coder/reviewer subprocesses alive until orchestrator shutdown
        even though no more exchanges can occur for it.
        """
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        applier.pair_registry.release.assert_called_once_with(
            228, reason="issue-completed",
        )

    def test_reconcile_history_entry_cancels_supervised_exchange_on_merged(
        self, applier,
    ):
        """Terminal merge reconciliation must cancel the pair and job."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = [
            "review-exchange:228:coding-1"
        ]
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        pair_registry.release.assert_called_once_with(
            228, reason="issue-completed",
        )
        job_supervisor.cancel_matching.assert_called_once()
        predicate = job_supervisor.cancel_matching.call_args.args[0]
        assert predicate("review-exchange:228:coding-1")
        assert not predicate("review-exchange:229:coding-1")

    def test_reconcile_history_entry_stops_visible_issue_runtime_on_merged(
        self, applier, mock_sessions,
    ):
        """Merged issue reconciliation is terminal for issue/rework sessions."""
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        mock_sessions.exists.side_effect = (
            lambda ref: ref.name in {"issue-228", "rework-228"}
        )
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)

        assert result.success
        assert [call.args[0].name for call in mock_sessions.stop.call_args_list] == [
            "issue-228",
            "rework-228",
        ]

    def test_reconcile_history_entry_releases_pair_on_closed(
        self, applier,
    ):
        """An issue closed without merge (e.g. abandoned PR) is also
        terminal — the pair has nothing left to do, so the same
        ``issue-completed`` release fires."""
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            source="pull_request",
            reason="PR closed without merge",
        )

        applier.apply(action)

        applier.pair_registry.release.assert_called_once_with(
            228, reason="issue-completed",
        )

    def test_reconcile_history_entry_does_not_release_on_noop_path(
        self, applier,
    ):
        """If the history entry is already terminal (idempotent
        no-op), the reconcile action returns early without firing the
        HISTORY_RECONCILED event — and must NOT call release a second
        time. Otherwise an already-released pair would receive a
        second release call (idempotent, but noisy in logs)."""
        from unittest.mock import MagicMock

        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="merged",  # already terminal
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR merged; awaiting merge reconciled",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        applier.apply(action)

        applier.pair_registry.release.assert_not_called()


class TestSyncLabelsAction:
    """Tests for SYNC_LABELS action."""

    def test_sync_labels_add_and_remove(self, applier, mock_labels):
        """Test syncing labels - adding and removing."""
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "in-progress")
        mock_labels.remove_label.assert_called_once_with(123, "ready")

    def test_sync_labels_partial_failure(self, applier, mock_labels):
        """Test sync labels with partial failure."""
        mock_labels.remove_label.side_effect = Exception("API error")
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert not result.success
        assert "remove ready" in result.error

    def test_sync_labels_contributes_to_mutation_summary(self, applier):
        """SYNC_LABELS should increment add/remove counters in batch summary."""
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        applier.apply_all([action])

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert len(summary_events) == 1
        payload = summary_events[0].data
        assert payload["label_add_attempted"] == 1
        assert payload["label_remove_attempted"] == 1
        assert payload["label_mutation_applied"] == 2
        assert payload["label_mutation_failed"] == 0


class TestLaunchSessionAction:
    """Tests for LAUNCH_SESSION action."""

    def test_launch_session_with_callback(self, applier, mock_sessions, tmp_path):
        """Test launching session via callback."""
        # Create a mock session launcher callback
        mock_session = MagicMock()
        mock_session.terminal_id = "issue-123"
        mock_session.issue.number = 123

        callback = MagicMock(return_value=mock_session)
        applier.session_launcher = callback

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        callback.assert_called_once_with(SessionType.ISSUE, 123)
        assert result.details["session_name"] == "issue-123"
        assert result.details["issue_number"] == 123

    def test_launch_session_callback_fails(self, applier):
        """Test launch session when callback returns None."""
        callback = MagicMock(return_value=None)
        applier.session_launcher = callback

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "Failed to launch" in result.error

    def test_launch_session_no_callback_no_command(self, applier):
        """Test launch session without callback or command fails."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert not result.success
        assert "No session_launcher callback" in result.error

    def test_launch_session_fallback_with_command(self, applier, mock_sessions, tmp_path):
        """Test launch session fallback when command provided."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude work",
            working_dir=str(tmp_path),
            title="Issue #123",
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.start.assert_called_once()

    def test_launch_session_already_running(self, applier, mock_sessions, tmp_path):
        """Test launch session when already running."""
        mock_sessions.exists.return_value = True

        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude work",
            working_dir=str(tmp_path),
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED
        assert "already running" in result.details.get("skip_reason", "")


class TestLaunchValidationRetryAction:
    """Tests for LAUNCH_VALIDATION_RETRY action."""

    def test_launch_validation_retry_with_callback(self, applier):
        """Test launching validation retry via callback."""
        mock_session = MagicMock()
        mock_session.terminal_id = "issue-123"
        mock_session.issue.number = 123
        callback = MagicMock(return_value=mock_session)
        applier.validation_retry_launcher = callback

        result = applier.apply(LaunchValidationRetryAction(issue_number=123, retry_count=1))

        assert result.success
        callback.assert_called_once_with(123)
        assert result.details["session_name"] == "issue-123"

    def test_launch_validation_retry_without_callback_fails(self, applier):
        """Validation retry launch requires an orchestrator callback."""
        result = applier.apply(LaunchValidationRetryAction(issue_number=123, retry_count=1))

        assert not result.success
        assert "No validation_retry_launcher callback configured" in result.error


class TestStopSessionAction:
    """Tests for STOP_SESSION action."""

    def test_stop_session_success(self, applier, mock_sessions):
        """Test successful session stop."""
        mock_sessions.exists.return_value = True

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()

    def test_stop_issue_session_releases_review_exchange_lifecycle(
        self, applier, mock_sessions
    ):
        """Stopping an issue must also cancel hidden review-exchange work."""
        mock_sessions.exists.return_value = True
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = ["review-exchange:123:issue-123"]
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["review_exchange_lifecycle_checked"] is True
        assert result.details["cancelled_review_exchange_jobs"] == [
            "review-exchange:123:issue-123"
        ]
        pair_registry.release.assert_called_once_with(123, reason="session-stopped")
        job_supervisor.cancel_matching.assert_called_once()
        predicate = job_supervisor.cancel_matching.call_args.args[0]
        assert predicate("review-exchange:123:issue-123")
        assert not predicate("review-exchange:124:issue-124")

    def test_stop_session_not_running(self, applier, mock_sessions):
        """Test stopping non-existent session."""
        mock_sessions.exists.return_value = False

        action = StopSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED
        assert result.details["review_exchange_lifecycle_checked"] is True
        assert result.details["cancelled_review_exchange_jobs"] == []


class TestQueueReviewAction:
    """Tests for QUEUE_REVIEW action."""

    def test_queue_review_success(self, applier, mock_events):
        """Test successful review queue."""
        action = QueueReviewAction(
            issue_number=123,
            pr_number=456,
            pr_url="https://github.com/owner/repo/pull/456",
            branch_name="123-feature",
        )

        result = applier.apply(action)

        assert result.success
        mock_events.publish.assert_called()


class TestAddCommentAction:
    """Tests for ADD_COMMENT action."""

    def test_pr_comment_event_keeps_full_excerpt(self, applier, mock_repository_host, mock_events):
        """Review comment event should not hard-truncate excerpt content."""
        mock_repository_host.add_comment.return_value = "https://github.com/owner/repo/pull/1#issuecomment-1"
        long_comment = "A" * 300
        action = AddCommentAction(number=1, comment=long_comment, is_pr=True)

        result = applier.apply(action)

        assert result.success
        assert mock_events.publish.call_count >= 1
        review_events = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.REVIEW_COMMENT_ADDED)
        ]
        assert review_events
        payload = review_events[-1].data
        assert payload.get("comment_excerpt") == long_comment


class TestSupersedePullRequestAction:
    """Tests for SUPERSEDE_PR action."""

    def test_supersede_pr_comments_then_closes_pr(self, applier, mock_repository_host):
        """Superseding a PR is an ActionApplier-owned GitHub mutation."""
        mock_repository_host.add_comment.return_value = "https://github.com/owner/repo/pull/376#issuecomment-1"
        action = SupersedePullRequestAction(
            issue_number=559,
            pr_number=376,
            comment="Superseded by reset and retry from scratch.",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["pr_number"] == 376
        mock_repository_host.add_comment.assert_called_once_with(376, action.comment)
        mock_repository_host.close_pr.assert_called_once_with(376)

    def test_supersede_pr_fails_when_close_fails(self, applier, mock_repository_host):
        """Scratch reset callers must see PR supersession failures."""
        mock_repository_host.close_pr.side_effect = RuntimeError("GitHub refused")
        action = SupersedePullRequestAction(
            issue_number=559,
            pr_number=376,
            comment="Superseded by reset and retry from scratch.",
        )

        result = applier.apply(action)

        assert not result.success
        assert "GitHub refused" in (result.error or "")


class TestCloseIssueAction:
    """Tests for CLOSE_ISSUE action."""

    def test_close_issue_updates_repository_state(self, applier, mock_repository_host):
        """Closing an issue is an ActionApplier-owned GitHub mutation."""
        action = CloseIssueAction(issue_number=559)

        result = applier.apply(action)

        assert result.success
        assert result.details["issue_number"] == 559
        assert result.details["state"] == "closed"
        mock_repository_host.update_issue_state.assert_called_once_with(559, "closed")

    def test_close_issue_fails_when_repository_update_fails(
        self,
        applier,
        mock_repository_host,
    ):
        """Close failures are returned to callers."""
        mock_repository_host.update_issue_state.side_effect = RuntimeError("GitHub refused")
        action = CloseIssueAction(issue_number=559)

        result = applier.apply(action)

        assert not result.success
        assert "GitHub refused" in (result.error or "")


class _FakePublishRetryAbandoner:
    def __init__(self) -> None:
        self.abandoned: list[int] = []

    def abandon_issue(self, issue_number: int) -> None:
        self.abandoned.append(issue_number)


class TestPublishRetryAbandonmentAtLifecycleBoundaries:
    """Every issue terminal boundary abandons an in-flight publish retry.

    The abandonment is owned by the shared runtime terminator, not each caller,
    so escalation and issue-completed/closed reconciliation both drop a stored /
    in-flight retry that could otherwise repopulate a terminated issue.
    """

    def test_escalation_abandons_publish_retry(self, applier, mock_labels):
        publish_recovery = _FakePublishRetryAbandoner()
        applier.publish_recovery = publish_recovery
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        assert applier.apply(action).success
        assert publish_recovery.abandoned == [123]

    def test_issue_completed_reconciliation_abandons_publish_retry(self, applier):
        publish_recovery = _FakePublishRetryAbandoner()
        applier.publish_recovery = publish_recovery
        entry = SessionHistoryEntry(
            issue_number=228,
            title="Shared cache read misses",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )
        applier.history_owner = SessionHistoryOwner([entry])
        applier.pair_registry = MagicMock(name="pair_registry")
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        assert applier.apply(action).success
        assert publish_recovery.abandoned == [228]


class TestEscalateToHumanAction:
    """Tests for ESCALATE_TO_HUMAN action."""

    def test_escalate_success(self, applier, mock_labels, mock_events):
        """Test successful escalation adds label to PR number."""
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        # Should add needs-human to PR number (456), not issue number
        mock_labels.add_label.assert_called_with(456, "needs-human")
        # Should try to remove needs-rework
        mock_labels.remove_label.assert_called_with(456, "needs-rework")

    def test_escalate_posts_comment(self, applier, mock_labels, mock_events, mock_repository_host):
        """Test escalation posts explanatory comment."""
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        mock_repository_host.add_comment.assert_called_once()
        call_args = mock_repository_host.add_comment.call_args
        assert call_args[0][0] == 456  # PR number
        assert "Escalated to Human Review" in call_args[0][1]

    def test_escalate_failure(self, applier, mock_labels):
        """Test escalation failure when add_label fails."""
        mock_labels.add_label.side_effect = Exception("API error")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
        )

        result = applier.apply(action)

        assert not result.success

    def test_escalate_releases_pair_with_correct_reason(
        self, applier, mock_labels,
    ):
        """ADR 0026 / B2: escalation kills the pair, full stop.

        ``_apply_escalate_to_human`` must call
        ``pair_registry.release(issue_number, reason="escalated-to-human")``
        when a registry is wired. Without this, an escalated issue's
        agent processes leak until orchestrator shutdown — defeating
        the lifecycle contract that escalation is a terminal boundary.
        """
        from unittest.mock import MagicMock

        applier.pair_registry = MagicMock(name="pair_registry")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        applier.pair_registry.release.assert_called_once_with(
            123, reason="escalated-to-human",
        )

    def test_escalate_cancels_supervised_review_exchange_job(
        self, applier, mock_labels,
    ):
        """Escalation is terminal for both the pair and supervisor job."""
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = [
            "review-exchange:123:coding-1"
        ]
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        pair_registry.release.assert_called_once_with(
            123, reason="escalated-to-human",
        )
        job_supervisor.cancel_matching.assert_called_once()
        predicate = job_supervisor.cancel_matching.call_args.args[0]
        assert predicate("review-exchange:123:coding-1")
        assert not predicate("review-exchange:124:coding-1")

    def test_escalate_stops_visible_issue_runtime_sessions(
        self, applier, mock_labels, mock_sessions,
    ):
        """Escalation stops visible issue/rework terminals before mutation."""
        mock_sessions.exists.side_effect = (
            lambda ref: ref.name in {"issue-123", "rework-123"}
        )
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        assert [call.args[0].name for call in mock_sessions.stop.call_args_list] == [
            "issue-123",
            "rework-123",
        ]

    def test_escalate_releases_pair_before_label_mutations(
        self, applier, mock_labels,
    ):
        """The release must run BEFORE label mutations.

        If a label mutation fails partway, the contract still holds
        ("escalation kills the pair, full stop"). This test pins
        ordering: ``release`` is called before ``labels.add_label``.
        """
        from unittest.mock import MagicMock, call

        # Use one parent mock so we can compare call ordering across
        # the two collaborators.
        parent = MagicMock()
        applier.pair_registry = parent.pair_registry
        applier.labels = parent.labels  # type: ignore[assignment]

        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        applier.apply(action)

        ordered_calls = parent.method_calls
        release_index = next(
            (i for i, c in enumerate(ordered_calls)
             if c == call.pair_registry.release(123, reason="escalated-to-human")),
            None,
        )
        first_label_index = next(
            (i for i, c in enumerate(ordered_calls)
             if c[0].startswith("labels.add_label")),
            None,
        )
        assert release_index is not None, (
            "pair_registry.release was never called on escalation"
        )
        assert first_label_index is not None, (
            "labels.add_label was never called on escalation"
        )
        assert release_index < first_label_index, (
            "pair_registry.release must run BEFORE label mutations on "
            "escalation; otherwise a partial-failure escalation can "
            "leave the agent processes alive after the lifecycle has "
            f"moved on. Call order was: {ordered_calls}"
        )

    def test_escalate_releases_pair_even_when_label_add_fails(
        self, applier, mock_labels,
    ):
        """Release-before-label means release fires on the failure
        path too: ``add_label`` raising must not prevent the pair
        from being terminated."""
        from unittest.mock import MagicMock

        applier.pair_registry = MagicMock(name="pair_registry")
        mock_labels.add_label.side_effect = Exception("API error")
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        # Action itself fails because label mutation failed — but the
        # release call must still have happened first.
        assert not result.success
        applier.pair_registry.release.assert_called_once_with(
            123, reason="escalated-to-human",
        )

    def test_escalate_skips_release_when_no_pair_registry(
        self, applier, mock_labels,
    ):
        """When ``pair_registry`` is None (not wired), escalation
        proceeds without raising. Sanity guard for environments that
        run without the persistent-pair feature configured."""
        # The default applier fixture leaves pair_registry unset.
        # Confirm it's not present so the test below is meaningful.
        assert getattr(applier, "pair_registry", None) is None

        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)
        assert result.success


class TestCreateTechLeadIssueAction:
    """Tests for CREATE_TECH_LEAD_ISSUE action."""

    def test_create_tech_lead_issue_success(self, applier, mock_repository_host, mock_events):
        """Test successful tech_lead issue creation."""
        mock_repository_host.create_issue.return_value = {"number": 100, "html_url": "https://github.com/owner/repo/issues/100"}

        action = CreateTechLeadIssueAction(
            title="Batch Review: 5 PRs",
            body="Review these PRs...",
            labels=("agent:tech-lead",),
            pr_count=5,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["issue_number"] == 100
        mock_repository_host.create_issue.assert_called_once()

    def test_storm_health_review_creation_emits_flavor_and_trigger(
        self, applier, mock_repository_host, mock_events
    ):
        """Storm escalation is machine-observable without parsing log text."""
        mock_repository_host.create_issue.return_value = {"number": 100}
        trigger = "problem storm: 3 issues inside settle window"
        action = CreateTechLeadIssueAction(
            title="Repository health review",
            body="Review this problem cohort",
            labels=(HEALTH_REVIEW_MARKER_LABEL,),
            storm_problems=(
                DiscoveredFailure(41, "Problem 41", "failed"),
                DiscoveredFailure(42, "Problem 42", "failed"),
                DiscoveredFailure(43, "Problem 43", "failed"),
            ),
            reason=trigger,
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        result = applier.apply(action)

        assert result.success
        [created] = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.TECH_LEAD_ISSUE_CREATED.value
        ]
        assert created.data == {
            "issue_number": 100,
            "pr_count": 0,
            "trigger": trigger,
            "storm_problem_count": 3,
            "flavor": TechLeadSessionFlavor.HEALTH_REVIEW.value,
        }

    @pytest.mark.parametrize(
        "flavor",
        [TechLeadSessionFlavor.BATCH_REVIEW, TechLeadSessionFlavor.HEALTH_REVIEW],
    )
    def test_creation_reports_the_authored_flavor_without_reading_labels(
        self, applier, mock_repository_host, mock_events, flavor
    ):
        """The creation boundary reports the owner's decision verbatim.

        Marker labels are the recovery/intake restatement of the variant, not
        this boundary's input: classifying here would put health-review
        lifecycle policy in the generic applier (#6780).
        """
        mock_repository_host.create_issue.return_value = {"number": 100}
        action = CreateTechLeadIssueAction(
            title="Anchor",
            body="Body",
            # Deliberately contradicts the flavor for BATCH_REVIEW: a label the
            # applier must not interpret.
            labels=(HEALTH_REVIEW_MARKER_LABEL,),
            reason="trigger",
            flavor=flavor,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        assert applier.apply(action).success
        [created] = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.TECH_LEAD_ISSUE_CREATED.value
        ]
        assert created.data["flavor"] == flavor.value

    def test_create_tech_lead_issue_no_repo_host(self, applier):
        """Test tech_lead issue creation without repository host."""
        applier.repository_host = None

        action = CreateTechLeadIssueAction(
            title="Batch Review: 5 PRs",
            body="Review these PRs...",
            labels=("agent:tech-lead",),
            pr_count=5,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        result = applier.apply(action)

        assert not result.success
        assert "No repository_host" in result.error

    def test_create_tech_lead_issue_without_milestone_makes_no_milestone_read(
        self, applier, mock_repository_host
    ):
        """Empty/number intents never touch the milestones API (#6769 F4)."""
        mock_repository_host.create_issue.return_value = {"number": 100}

        result = applier.apply(
            CreateTechLeadIssueAction(title="T", body="B", labels=(), pr_count=1, origin=TechLeadCreationOrigin.authors_anchor())
        )

        assert result.success
        mock_repository_host.list_milestones.assert_not_called()
        assert mock_repository_host.create_issue.call_args.kwargs["milestone"] is None

    def test_create_tech_lead_issue_resolves_explicit_name_at_creation(
        self, applier, mock_repository_host
    ):
        """THE resolution boundary: the planned name becomes a number here,
        with exactly one list_milestones read (#6769 finding 4)."""
        from issue_orchestrator.control.actions import TechLeadMilestoneIntent

        mock_repository_host.list_milestones.return_value = [
            {"number": 3, "title": "M3"},
            {"number": 5, "title": "M5"},
        ]
        mock_repository_host.create_issue.return_value = {"number": 100}

        result = applier.apply(
            CreateTechLeadIssueAction(
                title="T",
                body="B",
                labels=(),
                pr_count=1,
                milestone=TechLeadMilestoneIntent(explicit_name="M5"),
                origin=TechLeadCreationOrigin.authors_anchor(),
            )
        )

        assert result.success
        mock_repository_host.list_milestones.assert_called_once()
        assert mock_repository_host.create_issue.call_args.kwargs["milestone"] == 5

    def test_create_tech_lead_issue_unresolvable_name_fails_loudly_without_creating(
        self, applier, mock_repository_host
    ):
        """execute + unresolvable configured name -> loud applier failure;
        the issue is never created (#6769 finding 4)."""
        from issue_orchestrator.control.actions import TechLeadMilestoneIntent

        mock_repository_host.list_milestones.return_value = [
            {"number": 3, "title": "M3"},
        ]

        result = applier.apply(
            CreateTechLeadIssueAction(
                title="T",
                body="B",
                labels=(),
                pr_count=1,
                milestone=TechLeadMilestoneIntent(explicit_name="Nope"),
                origin=TechLeadCreationOrigin.authors_anchor(),
            )
        )

        assert not result.success
        assert "does not match any" in result.error
        mock_repository_host.create_issue.assert_not_called()


class TestSurfaceTechLeadProposalAction:
    """Tests for SURFACE_TECH_LEAD_PROPOSAL action (ADR-0031)."""

    def _proposal(self, **overrides):
        defaults = dict(
            issue_number=42,
            action_id="A1",
            proposal_type="reset_retry",
            target_number=17,
            target_is_pr=False,
            title="",
            body_preview="Reset issue #17 from scratch",
            finding_ids=("T1", "T2"),
            mode="shadow",
            reason="tech_lead proposal A1 (reset_retry) surfaced as shadow",
        )
        defaults.update(overrides)
        return SurfaceTechLeadProposalAction(**defaults)

    def test_shadow_proposal_emits_action_proposed_with_full_payload(
        self, applier, mock_events, mock_repository_host
    ):
        result = applier.apply(self._proposal())

        assert result.success
        published = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.TECH_LEAD_ACTION_PROPOSED.value
        ]
        assert len(published) == 1
        assert published[0].data == {
            "issue_number": 42,
            "action_id": "A1",
            "proposal_type": "reset_retry",
            "target_number": 17,
            "target_is_pr": False,
            "title": "",
            "body_preview": "Reset issue #17 from scratch",
            "finding_ids": ["T1", "T2"],
            "mode": "shadow",
        }
        # Surfacing must never touch GitHub.
        assert not mock_repository_host.method_calls

    def test_pattern_proposal_emits_action_proposed(self, applier, mock_events):
        result = applier.apply(
            self._proposal(proposal_type="flag_pattern", mode="pattern", target_number=0)
        )

        assert result.success
        names = [call.args[0].name for call in mock_events.publish.call_args_list]
        assert EventName.TECH_LEAD_ACTION_PROPOSED.value in names
        assert EventName.TECH_LEAD_DECISION_REJECTED.value not in names

    def test_rejected_mode_emits_decision_rejected(
        self, applier, mock_events, mock_repository_host, mock_labels
    ):
        result = applier.apply(
            self._proposal(
                action_id="",
                proposal_type="decision",
                target_number=0,
                finding_ids=(),
                mode="rejected",
                body_preview="tech_lead decision missing or empty: x",
                reason="tech_lead decision rejected (missing_decision)",
            )
        )

        assert result.success
        published = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.TECH_LEAD_DECISION_REJECTED.value
        ]
        assert len(published) == 1
        assert published[0].data["proposal_type"] == "decision"
        assert published[0].data["mode"] == "rejected"
        assert published[0].data["body_preview"] == (
            "tech_lead decision missing or empty: x"
        )
        assert not mock_repository_host.method_calls
        assert not mock_labels.method_calls


class TestCleanupSessionAction:
    """Tests for CLEANUP_SESSION action."""

    def test_cleanup_full(self, applier, mock_sessions, mock_worktree_manager, tmp_path):
        """Test full cleanup - close tab and remove worktree."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_cleanup_tabs_only(self, applier, mock_sessions, mock_worktree_manager, tmp_path):
        """Test cleanup with only tab closing."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=False,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_called_once()
        mock_worktree_manager.remove_checkout.assert_not_called()

    def test_cleanup_worktree_only(
        self, applier, mock_sessions, mock_worktree_manager, tmp_path
    ):
        """Worktree removal does not implicitly close the session tab."""
        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=False,
            remove_worktrees=True,
        )

        result = applier.apply(action)

        assert result.success
        mock_sessions.stop.assert_not_called()
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_disposable_worktree_cleanup_forces_removal(
        self, applier, mock_sessions, mock_worktree_manager, tmp_path
    ):
        """F8: a disposable scratch worktree is FORCE-removed (untracked agent
        artifacts must not fail its cleanup and leak it)."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=0,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
            disposable_worktree=True,
        )

        result = applier.apply(action)

        assert result.success
        mock_worktree_manager.remove_checkout_and_branch.assert_called_once_with(
            Path(str(tmp_path)), force=True
        )

    def test_disposable_cleanup_succeeds_when_removed_callback_fails(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        mock_worktree_manager, mock_fresh_issue_reader, tmp_path
    ):
        """R3 (#6824): the worktree was removed but the 'worktree gone' callback
        raised — the action still SUCCEEDS so the disposable cleanup is not
        retained and retried forever against an already-removed worktree."""
        mock_sessions.exists.return_value = True

        def _boom(_path):
            raise RuntimeError("async notify failed")

        applier = ActionApplier(
            labels=mock_labels, sessions=mock_sessions, events=mock_events,
            repository_host=mock_repository_host, worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader, reconcile=False,
            on_worktree_removed=_boom,
        )
        action = CleanupSessionAction(
            issue_number=123, pr_number=0, terminal_id="issue-123",
            worktree_path=str(tmp_path), close_tabs=True, remove_worktrees=True,
            disposable_worktree=True,
        )

        result = applier.apply(action)

        assert result.success  # removal completed; callback failure doesn't re-fail it
        mock_worktree_manager.remove_checkout_and_branch.assert_called_once_with(
            Path(str(tmp_path)), force=True
        )

    def test_non_disposable_worktree_cleanup_is_not_forced(
        self, applier, mock_sessions, mock_worktree_manager, tmp_path
    ):
        """F8: a normal coding worktree stays non-forced so user work is never
        discarded by a forced removal."""
        mock_sessions.exists.return_value = True

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
            disposable_worktree=False,
        )

        result = applier.apply(action)

        assert result.success
        mock_worktree_manager.remove_checkout.assert_called_once_with(
            Path(str(tmp_path)), force=False
        )

    def test_cleanup_issue_session_releases_review_exchange_lifecycle(
        self, applier, mock_sessions, mock_worktree_manager, tmp_path
    ):
        """Cleanup must also terminate hidden issue-scoped exchange work."""
        mock_sessions.exists.return_value = True
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = ["review-exchange:123:coding-1"]
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="issue-123",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["review_exchange_lifecycle_checked"] is True
        assert result.details["cancelled_review_exchange_jobs"] == [
            "review-exchange:123:coding-1"
        ]
        pair_registry.release.assert_called_once_with(123, reason="session-cleanup")
        job_supervisor.cancel_matching.assert_called_once()
        predicate = job_supervisor.cancel_matching.call_args.args[0]
        assert predicate("review-exchange:123:coding-1")
        assert not predicate("review-exchange:124:coding-1")
        mock_sessions.stop.assert_called_once()
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_cleanup_without_terminal_id_logs_issue_lifecycle_default(
        self,
        applier,
        mock_sessions,
        mock_worktree_manager,
        tmp_path,
        caplog,
    ):
        """No-terminal cleanup still checks issue exchange lifecycle explicitly."""
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = []
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
        )

        with caplog.at_level(logging.WARNING):
            result = applier.apply(action)

        assert result.success
        assert result.details["review_exchange_lifecycle_checked"] is True
        assert "missing terminal_id; assuming issue session" in caplog.text
        pair_registry.release.assert_called_once_with(123, reason="session-cleanup")
        mock_sessions.stop.assert_not_called()
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_cleanup_review_session_does_not_release_issue_exchange(
        self, applier, mock_sessions, mock_worktree_manager, tmp_path
    ):
        """Review-only cleanup must not tear down an issue exchange pair."""
        mock_sessions.exists.return_value = True
        pair_registry = Mock()
        job_supervisor = Mock()
        applier.pair_registry = pair_registry
        applier.background_job_supervisor = job_supervisor

        action = CleanupSessionAction(
            issue_number=123,
            pr_number=456,
            terminal_id="review-456",
            worktree_path=str(tmp_path),
            close_tabs=True,
            remove_worktrees=True,
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["review_exchange_lifecycle_checked"] is False
        assert result.details["cancelled_review_exchange_jobs"] == []
        pair_registry.release.assert_not_called()
        job_supervisor.cancel_matching.assert_not_called()


class TestRemoveWorktreeAction:
    """Tests for REMOVE_WORKTREE action."""

    def test_remove_worktree_success(self, applier, mock_worktree_manager, tmp_path):
        """Test successful worktree removal."""
        action = RemoveWorktreeAction(worktree_path=str(tmp_path))

        result = applier.apply(action)

        assert result.success
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_remove_worktree_no_manager(self, applier, tmp_path):
        """Test worktree removal without manager."""
        applier.worktree_manager = None
        action = RemoveWorktreeAction(worktree_path=str(tmp_path))

        result = applier.apply(action)

        assert not result.success
        assert "No worktree_manager" in result.error


class TestApplyAll:
    """Tests for apply_all method."""

    def test_apply_all_success(self, applier, mock_labels):
        """Test applying multiple actions."""
        actions = [
            AddLabelAction(issue_number=1, label="a"),
            AddLabelAction(issue_number=2, label="b"),
        ]

        results = applier.apply_all(actions)

        assert len(results) == 2
        assert all(r.success for r in results)

    def test_apply_all_partial_failure(self, applier, mock_labels):
        """Test applying multiple actions with partial failure."""
        mock_labels.add_label.side_effect = [None, Exception("fail"), None]

        actions = [
            AddLabelAction(issue_number=1, label="a"),
            AddLabelAction(issue_number=2, label="b"),
            AddLabelAction(issue_number=3, label="c"),
        ]

        results = applier.apply_all(actions)

        assert len(results) == 3
        assert results[0].success
        assert not results[1].success
        assert results[2].success

    def test_apply_all_emits_label_mutation_summary(self, applier, mock_labels, caplog):
        """Emit summary event/log with attempted/applied/noop/failed mutation counters."""
        # add #1 => no-op, remove #2 => applied, add #3 => failure, remove #4 => applied
        mock_labels.has_label.side_effect = [True, False, False, True]
        mock_labels.add_label.side_effect = Exception("boom")
        actions = [
            AddLabelAction(issue_number=1, label="already-present"),
            RemoveLabelAction(issue_number=1, label="already-absent"),
            AddLabelAction(issue_number=2, label="fails"),
            RemoveLabelAction(issue_number=2, label="removed"),
        ]

        with caplog.at_level(logging.INFO):
            applier.apply_all(actions)

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert len(summary_events) == 1
        payload = summary_events[0].data
        assert payload["label_add_attempted"] == 2
        assert payload["label_remove_attempted"] == 2
        assert payload["label_mutation_attempted"] == 4
        assert payload["label_mutation_applied"] == 2
        assert payload["label_mutation_noop"] == 1
        assert payload["label_mutation_failed"] == 1
        assert payload["noop_ratio"] == 0.25
        assert payload["failure_ratio"] == 0.25
        assert len(payload["per_issue"]) == 2

        assert any(
            "label_mutations attempted=4 applied=2 noop=1 failed=1" in message
            for message in caplog.messages
        )

    def test_apply_all_skips_label_mutation_summary_when_no_label_actions(self, applier):
        """Avoid summary event noise when batch does no label mutations."""
        actions = [StopSessionAction(session_type=SessionType.ISSUE, number=99)]

        applier.apply_all(actions)

        summary_events = [
            call.args[0]
            for call in applier.events.publish.call_args_list
            if getattr(call.args[0], "name", None) == str(EventName.LABEL_MUTATION_SUMMARY)
        ]
        assert summary_events == []


class TestShedRecoveredWorkflowLabelsNotDispatchable:
    """SHED_RECOVERED_WORKFLOW_LABELS is a private sub-step of the
    RECOVER_TERMINAL_ISSUE owner command, not an independently dispatchable
    action. Applying it directly must be a no-op skip so it can never bypass
    the reconciliation pause gate the owner command enforces (#6431 F1).

    The shed's label-selection behavior is covered through the owner command in
    TestRecoverTerminalIssueAction — its only reachable entry point.
    """

    def test_standalone_shed_is_not_dispatched_and_mutates_nothing(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        mock_worktree_manager, mock_fresh_issue_reader,
    ):
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.control.label_manager import LabelManager

        # Wire everything for a shed that WOULD succeed (live transient labels,
        # a real label_manager). Applying the shed directly must still do
        # nothing, proving the gate cannot be bypassed by issuing the sub-step
        # as a top-level action.
        mock_fresh_issue_reader.read_issue_labels.return_value = [
            "pr-pending", "publish-failed",
        ]
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader,
            label_manager=LabelManager(Config(repo="o/r")),
            reconcile=False,
        )
        action = ShedRecoveredWorkflowLabelsAction(
            issue_number=228, issue_key="M1-228",
        )

        result = applier.apply(action)

        assert not result.success
        mock_labels.remove_label.assert_not_called()


class TestRecoverTerminalIssueAction:
    """Tests for RECOVER_TERMINAL_ISSUE — shed labels, then finalize history.

    This action owns the terminal-recovery ordering invariant: history must
    only terminalize after the label cleanup has succeeded, so a transient
    label-removal failure leaves the history entry reconcilable for retry
    instead of stranding pr-pending / publish-failed / publish-fail-count-*
    labels (#6431 F1).
    """

    @pytest.fixture
    def real_label_manager(self):
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.control.label_manager import LabelManager
        return LabelManager(Config(repo="o/r"))

    @pytest.fixture
    def real_label_store(self, tmp_path):
        from issue_orchestrator.execution.label_store import LabelStore
        return LabelStore(tmp_path / "label_store.sqlite")

    def _make_applier(
        self,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        real_label_manager,
        github_labels,
        history_entry,
        *,
        reconcile=False,
        label_store=None,
    ):
        reader = MagicMock()
        reader.read_issue_labels.return_value = list(github_labels)
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            label_manager=real_label_manager,
            label_store=label_store,
            reconcile=reconcile,
        )
        applier.history_owner = SessionHistoryOwner([history_entry])
        return applier

    @staticmethod
    def _awaiting_merge_entry(issue_number=228):
        return SessionHistoryEntry(
            issue_number=issue_number,
            title="Recovered work",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=0,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="Recovered awaiting merge state on startup",
        )

    @patch("issue_orchestrator.observation.observer.SessionObserver")
    def test_init_wiring_owner_follows_session_history_replacement(
        self,
        _mock_observer,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        real_label_manager,
    ):
        """#6692 composition-boundary regression.

        The owner-level unit tests would stay green if the production wiring in
        ``init_orchestrator_components`` were reverted to
        ``SessionHistoryOwner(orch.state.session_history)`` (capturing the list
        at startup). This drives the *real* wiring: it installs the owner on the
        long-lived ``ActionApplier``, then a recovery/retry path replaces
        ``state.session_history`` wholesale, and a terminal awaiting-merge action
        applied through the applier must still terminalize the entry in the
        replacement list. A captured-list owner would read the stale (empty)
        initial list and no-op, leaving the entry stuck in Awaiting Merge.
        """
        reader = MagicMock()
        reader.read_issue_labels.return_value = [
            "pr-pending", "publish-failed", "agent:backend",
        ]
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            label_manager=real_label_manager,
            label_store=None,
            reconcile=False,
        )

        orch = MagicMock()
        orch.deps.action_applier = applier
        orch.deps.claim_gate = None
        orch.state.active_sessions = []
        # Owner is wired against the initial (empty) list at startup, exactly as
        # production does before any recovery/retry replacement.
        orch.state.session_history = []

        init_orchestrator_components(orch)

        # A recovery / publish-retry finalization path replaces the list
        # wholesale with a new object carrying the reconcilable entry.
        entry = self._awaiting_merge_entry(issue_number=228)
        orch.state.session_history = [entry]

        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        # The entry in the REPLACEMENT list is terminalized, proving the wired
        # owner resolves state.session_history at mutation time.
        assert entry.status == "merged"
        assert entry.status_reason == "PR merged; awaiting merge reconciled"

    def test_sheds_then_finalizes_history(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "publish-failed", "agent:backend"],
            history_entry=entry,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        removed = {call.args[1] for call in mock_labels.remove_label.call_args_list}
        assert {"pr-pending", "publish-failed"} <= removed
        assert "agent:backend" not in removed
        # History finalized only after the shed succeeded.
        assert entry.status == "merged"
        assert entry.status_reason == "PR merged; awaiting merge reconciled"

    _CLOSE_MERGED_AT = "2026-08-03T13:52:09Z"

    def _close_on_merge_action(self):
        return RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
            close_issue=True,
            merged_at=self._CLOSE_MERGED_AT,
        )

    @staticmethod
    def _stub_close_evidence(
        mock_repository_host, *, issue_state="open", auto_close_fired=False,
    ):
        """Wire the apply-time revalidation reads (F4/A2): the live issue
        state and the close-event evidence since the merge."""
        mock_repository_host.get_issue.return_value = MagicMock(
            state=issue_state,
        )
        mock_repository_host.issue_closed_on_or_after.return_value = (
            auto_close_fired
        )

    def test_close_on_merge_close_is_first_mutation_then_comment_then_shed(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """F1 ordered-call regression (porchpin #81): the close is the FIRST
        mutation after the owner gates — before the label shed, so a shed/
        history failure after a successful close is safe (a closed issue
        cannot re-enter the queue). The audit comment posts only AFTER the
        successful close, and history terminalizes last."""
        order: list[str] = []
        mock_repository_host.update_issue_state.side_effect = (
            lambda *a, **k: order.append("close")
        )
        mock_repository_host.add_comment.side_effect = (
            lambda *a, **k: order.append("comment")
        )
        mock_labels.remove_label.side_effect = (
            lambda *a, **k: order.append("shed")
        )
        self._stub_close_evidence(mock_repository_host)
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert result.success
        assert result.details["closed_issue"] is True
        # Revalidation consulted the live evidence at the owner boundary.
        mock_repository_host.issue_closed_on_or_after.assert_called_once_with(
            228, self._CLOSE_MERGED_AT,
        )
        mock_repository_host.update_issue_state.assert_called_once_with(
            228, "closed",
        )
        assert order[:2] == ["close", "comment"]
        assert "shed" in order and order.index("shed") > 1
        comment_body = mock_repository_host.add_comment.call_args.args[1]
        assert "https://github.com/test/repo/pull/318" in comment_body
        assert "no closing reference" in comment_body
        assert entry.status == "merged"

    def test_close_on_merge_failure_leaves_labels_and_history_untouched(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """A failed close must NOT shed labels, post the audit comment, or
        terminalize history: labels-shed-but-open across a restart is exactly
        the relaunch window this fallback exists to eliminate, and a comment
        claiming the close happened would be a false audit trail repeated on
        every retry. The entry stays reconcilable for the next pass."""
        mock_repository_host.update_issue_state.side_effect = Exception(
            "GitHub 502 closing issue"
        )
        self._stub_close_evidence(mock_repository_host)
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert not result.success
        assert "close-on-merge" in (result.error or "")
        mock_labels.remove_label.assert_not_called()
        mock_repository_host.add_comment.assert_not_called()
        # Entry stays in its reconcilable status for the next discovery pass.
        assert entry.status == "completed"

    def test_apply_time_revalidation_preserves_human_reopen(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """F4/A2 regression: discovery planned close_issue=True, then a human
        closed AND reopened the issue before apply. The owner command re-reads
        the live evidence, sees a close event since the merge, and must NOT
        close the intentional reopen — while shed + history still proceed."""
        self._stub_close_evidence(mock_repository_host, auto_close_fired=True)
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert result.success
        assert result.details["closed_issue"] is False
        mock_repository_host.update_issue_state.assert_not_called()
        mock_repository_host.add_comment.assert_not_called()
        # Recovery itself still completes: labels shed, history terminalized.
        assert entry.status == "merged"

    def test_apply_time_revalidation_skips_close_when_already_closed(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """A human closed the issue in the discovery→apply gap: nothing to
        close; shed + history proceed."""
        self._stub_close_evidence(mock_repository_host, issue_state="closed")
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert result.success
        assert result.details["closed_issue"] is False
        mock_repository_host.update_issue_state.assert_not_called()
        assert entry.status == "merged"

    def test_apply_time_revalidation_unreadable_fails_without_mutation(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """Unreadable revalidation is not either answer: no close, no shed,
        no history — the entry stays reconcilable for retry."""
        from issue_orchestrator.ports.repository_host import (
            RepositoryHostError,
        )
        mock_repository_host.get_issue.side_effect = RepositoryHostError(
            "boom"
        )
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert not result.success
        assert "revalidation unreadable" in (result.error or "")
        mock_repository_host.update_issue_state.assert_not_called()
        mock_labels.remove_label.assert_not_called()
        assert entry.status == "completed"

    def test_close_on_merge_comment_failure_still_closes(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """The comment is best-effort; the close is the safety-critical write."""
        mock_repository_host.add_comment.side_effect = Exception("comment 502")
        self._stub_close_evidence(mock_repository_host)
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )

        result = applier.apply(self._close_on_merge_action())

        assert result.success
        mock_repository_host.update_issue_state.assert_called_once_with(
            228, "closed",
        )
        assert entry.status == "merged"

    def test_no_close_without_close_issue_flag(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """Default recovery (issue already closed, or closed-unmerged PR)
        must never touch the issue's open/closed state."""
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "agent:backend"],
            history_entry=entry,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        assert result.details["closed_issue"] is False
        mock_repository_host.update_issue_state.assert_not_called()
        assert entry.status == "merged"

    def test_shed_failure_leaves_history_reconcilable(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """F1 regression: a label-removal failure must NOT terminalize history.

        The entry must stay in its reconcilable awaiting-merge status so a
        later awaiting-merge discovery pass re-finds and retries the cleanup.
        """
        mock_labels.remove_label.side_effect = Exception("GitHub 502 removing label")
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "publish-failed", "agent:backend"],
            history_entry=entry,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        # Shed was attempted first...
        assert mock_labels.remove_label.called
        # ...it failed, so the action fails and history is left untouched.
        assert not result.success
        assert "reconcilable" in (result.error or "")
        assert entry.status == "completed"
        assert entry.status_reason == "Recovered awaiting merge state on startup"

    def test_sheds_every_transient_label_and_keeps_durable_ones(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """The shed sub-step removes every transient workflow label (pr-pending,
        publish-failed, publish-fail-count-N, blocking labels) and never touches
        durable labels — reached only through the owner command."""
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=[
                "pr-pending", "publish-failed", "publish-fail-count-2",
                "blocked:pr-closed", "agent:backend", "bug",
            ],
            history_entry=entry,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        removed = {call.args[1] for call in mock_labels.remove_label.call_args_list}
        assert removed == {
            "pr-pending", "publish-failed", "publish-fail-count-2", "blocked:pr-closed",
        }
        # Non-transient labels must never be touched.
        assert "agent:backend" not in removed
        assert "bug" not in removed
        assert entry.status == "merged"

    def test_cleans_label_store_mirror(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager, real_label_store,
    ):
        """A label_store row stranded by past drift (publish-fail-count-1) is
        shed from the mirror even when the fresh GitHub read no longer surfaces
        it."""
        for label in ("pr-pending", "publish-failed", "publish-fail-count-1"):
            real_label_store.add_label(228, label)
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "publish-failed"],
            history_entry=entry,
            label_store=real_label_store,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        assert real_label_store.load_labels(228) == set()
        removed = {call.args[1] for call in mock_labels.remove_label.call_args_list}
        assert "publish-fail-count-1" in removed

    def test_noop_shed_still_finalizes_history(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """When the issue carries no transient workflow labels there is nothing
        to shed, but the awaiting-merge history is still finalized."""
        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["agent:backend", "bug", "code-reviewed"],
            history_entry=entry,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.remove_label.assert_not_called()
        assert entry.status == "merged"

    def test_fails_without_label_manager(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
    ):
        """The shed sub-step requires a LabelManager; without one the owner
        command fails before finalizing history."""
        entry = self._awaiting_merge_entry()
        reader = MagicMock()
        reader.read_issue_labels.return_value = ["pr-pending"]
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            reconcile=False,
        )
        applier.history_owner = SessionHistoryOwner([entry])
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
        )

        result = applier.apply(action)

        assert not result.success
        # History is not finalized when the shed sub-step cannot run.
        assert entry.status == "completed"

    def test_reconcile_pause_label_blocks_recovery(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host,
        real_label_manager,
    ):
        """F1: an issue paused for reconciliation (io:needs-reconcile) must not
        be shed or finalized. With reconcile enabled and the pause label live,
        the planner-issued expected guard makes the owner command raise
        ReconciliationRequired before any label write, leaving the
        awaiting-merge history entry reconcilable for a later discovery pass.
        """
        from issue_orchestrator.control.reconciliation import (
            ReconciliationRequired,
            build_expected_for_mutation,
        )

        entry = self._awaiting_merge_entry()
        applier = self._make_applier(
            mock_labels, mock_sessions, mock_events, mock_repository_host,
            real_label_manager,
            github_labels=["pr-pending", "publish-failed", "io:needs-reconcile"],
            history_entry=entry,
            reconcile=True,
        )
        action = RecoverTerminalIssueAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            status_reason="PR merged; awaiting merge reconciled",
            issue_key="M1-228",
            reason="awaiting-merge terminal: merged",
            expected=build_expected_for_mutation(),
        )

        with pytest.raises(ReconciliationRequired):
            applier.apply(action)

        # No label write happened, and the history entry stays reconcilable.
        mock_labels.remove_label.assert_not_called()
        assert entry.status == "completed"
        assert entry.status_reason == "Recovered awaiting merge state on startup"


class TestReconciliation:
    """Tests for reconciliation behavior."""

    def test_reconciliation_disabled(self, applier, mock_labels):
        """Test that reconciliation is skipped when disabled."""
        applier.reconcile = False

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        # Should not call fresh_issue_reader for reconciliation
        applier.fresh_issue_reader.read_issue_labels.assert_not_called()

    def test_reconciliation_enabled(self, applier, mock_labels):
        """Test that reconciliation checks labels when enabled."""
        applier.reconcile = True
        applier.fresh_issue_reader.read_issue_labels.return_value = ["ready", "other"]

        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("ready",),
        )

        result = applier.apply(action)

        assert result.success
        applier.fresh_issue_reader.read_issue_labels.assert_called_once_with(123)


class TestExpectedStateEnforcement:
    """Tests for ExpectedState enforcement via _require_expected."""

    @pytest.fixture
    def applier_with_reconcile(
        self,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        mock_worktree_manager,
        mock_fresh_issue_reader,
    ):
        """Create an ActionApplier with reconciliation enabled."""
        return ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader,
            reconcile=True,
        )

    def test_add_label_with_expected_passes_when_satisfied(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction proceeds when ExpectedState is satisfied."""
        from issue_orchestrator.control.reconciliation import ExpectedState

        # Current labels satisfy the expected state
        mock_fresh_issue_reader.read_issue_labels.return_value = ["agent:web", "in-progress"]

        action = AddLabelAction(
            issue_number=123,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                required={"in-progress"},
                forbidden={"io:needs-reconcile"},
            ),
        )

        result = applier_with_reconcile.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once_with(123, "pr-pending")

    def test_add_label_with_expected_raises_when_missing_required(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction raises ReconciliationRequired when required label missing."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Current labels don't have required "in-progress"
        mock_fresh_issue_reader.read_issue_labels.return_value = ["agent:web"]

        action = AddLabelAction(
            issue_number=42,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                required={"in-progress"},
            ),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 42
        assert "in-progress" in exc_info.value.reason
        mock_labels.add_label.assert_not_called()

    def test_add_label_with_expected_raises_when_has_forbidden(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction raises ReconciliationRequired when forbidden label present."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Current labels have forbidden "io:needs-reconcile"
        mock_fresh_issue_reader.read_issue_labels.return_value = ["in-progress", "io:needs-reconcile"]

        action = AddLabelAction(
            issue_number=99,
            label="pr-pending",
            reason="Session completed",
            expected=ExpectedState.with_labels(
                forbidden={"io:needs-reconcile"},
            ),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 99
        assert "forbidden" in exc_info.value.reason.lower()
        mock_labels.add_label.assert_not_called()

    def test_add_label_without_expected_proceeds_normally(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test AddLabelAction without ExpectedState doesn't check constraints."""
        # Even if labels would fail constraints, no ExpectedState means no check
        mock_fresh_issue_reader.read_issue_labels.return_value = ["blocked"]

        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=None,  # No ExpectedState
        )

        result = applier_with_reconcile.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once()

    def test_expected_not_enforced_when_reconcile_disabled(
        self,
        mock_labels,
        mock_sessions,
        mock_events,
        mock_repository_host,
        mock_worktree_manager,
        mock_fresh_issue_reader,
    ):
        """Test ExpectedState is not enforced when reconcile=False."""
        from issue_orchestrator.control.reconciliation import ExpectedState

        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            fresh_issue_reader=mock_fresh_issue_reader,
            reconcile=False,  # Disabled
        )

        # ExpectedState would fail if checked, but reconcile=False
        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=ExpectedState.with_labels(required={"nonexistent-label"}),
        )

        result = applier.apply(action)

        assert result.success
        mock_labels.add_label.assert_called_once()

    def test_expected_raises_when_labels_cannot_be_fetched(
        self,
        applier_with_reconcile,
        mock_labels,
        mock_fresh_issue_reader,
    ):
        """Test raises ReconciliationRequired when labels cannot be fetched."""
        from issue_orchestrator.control.reconciliation import ExpectedState, ReconciliationRequired

        # Simulate API failure
        mock_fresh_issue_reader.read_issue_labels.return_value = None

        action = AddLabelAction(
            issue_number=123,
            label="test-label",
            reason="Test",
            expected=ExpectedState.with_labels(required={"in-progress"}),
        )

        with pytest.raises(ReconciliationRequired) as exc_info:
            applier_with_reconcile.apply(action)

        assert exc_info.value.entity_id == 123
        assert "fetch" in exc_info.value.reason.lower()
        mock_labels.add_label.assert_not_called()


class TestEnqueueToMergeQueueAction:
    """The applier performs the protected enqueue and emits an event."""

    def test_enqueue_calls_repository_and_emits_event(
        self, applier, mock_repository_host, mock_events
    ):
        action = EnqueueToMergeQueueAction(
            issue_number=228, pr_number=318, pr_url="https://x/pull/318",
            issue_key="M1-228",
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SUCCESS
        mock_repository_host.enqueue_to_merge_queue.assert_called_once_with(318)
        published = [
            c.args[0] for c in mock_events.publish.call_args_list
            if getattr(c.args[0], "name", None) == EventName.MERGE_QUEUE_ENQUEUED.value
        ]
        assert len(published) == 1
        assert published[0].data["pr_number"] == 318

    def test_enqueue_failure_is_reported_not_raised(
        self, applier, mock_repository_host
    ):
        mock_repository_host.enqueue_to_merge_queue.side_effect = RuntimeError("boom")
        action = EnqueueToMergeQueueAction(issue_number=228, pr_number=318)

        result = applier.apply(action)

        assert result.result_type == ActionResultType.FAILURE


class TestClaimGateAudit:
    """Structural test: all GitHub-write action types must verify claim ownership.

    This test ensures that no new action type that writes to GitHub can be added
    without also adding ClaimGate verification. If this test fails, it means a
    new action type was added that mutates GitHub state without checking whether
    the orchestrator still owns the claim.
    """

    # Action types that write to GitHub on a CLAIMED issue and must verify ownership
    GITHUB_WRITE_ACTIONS = {
        ActionType.ADD_LABEL,
        ActionType.REMOVE_LABEL,
        ActionType.SYNC_LABELS,
        ActionType.SHED_RECOVERED_WORKFLOW_LABELS,
        ActionType.RECOVER_TERMINAL_ISSUE,
        ActionType.ADD_COMMENT,
        ActionType.SUPERSEDE_PR,
        ActionType.CLOSE_ISSUE,
        ActionType.SET_ISSUE_STATE,
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.QUEUE_REVIEW,
        ActionType.ENQUEUE_TO_MERGE_QUEUE,
    }

    # Action types that legitimately skip claim verification:
    # - LAUNCH_SESSION: does its own claim acquisition in session_launcher
    # - LAUNCH_VALIDATION_RETRY: does its own claim acquisition in session_launcher
    # - STOP_SESSION: local terminal operation (killing sessions)
    # - CREATE_WORKTREE / REMOVE_WORKTREE: local filesystem only
    # - QUEUE_RETROSPECTIVE_REVIEW / QUEUE_REWORK / QUEUE_TECH_LEAD: local state operations
    # - CREATE_TECH_LEAD_ISSUE: creates a NEW issue, not modifying a claimed one
    # - CREATE_TECH_LEAD_PROPOSAL_ISSUE: creates a NEW gated issue + records the
    #   stored op (#6778); the anchor link comment targets the tech_lead
    #   session's own anchor issue, never a claimed coding issue
    # - CREATE_TECH_LEAD_CASE_FILE_ISSUE: creates a NEW observation-labeled case
    #   file + records the pattern ledger row (#6781); the repeat-observation
    #   evidence comment targets that orchestrator-owned case file, never a
    #   claimed coding issue
    # - SURFACE_TECH_LEAD_PROPOSAL: emits a trace event only, no GitHub calls
    # - RESET_RETRY_ISSUE: owner command (#6764) - every GitHub write it
    #   triggers is delegated to the reset owner, which routes label/PR
    #   mutations back through this applier's claim-verified handlers
    #   (AddLabel/RemoveLabel/SupersedePR), same as the dashboard reset.
    # - KILL_HUNG_SESSION: owner command (#6778) - applies the local
    #   issue-runtime termination boundary (sessions/jobs/pair); its only
    #   GitHub writes are the proposal-issue outcome comment + close, on the
    #   unclaimed gated proposal issue the operator just approved.
    # - DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS: orchestrator-owned ledger cleanup
    #   (#6779 R7/R10) - confirms each absent proposal with a targeted READ
    #   (get_issue_state) and discards only the local authority-store op row;
    #   it never writes GitHub state and never touches a claimed coding issue.
    # - APPLY_PROVIDER_IMPACT: owner command (#5980) - its only GitHub write is
    #   the provider-blocked label, dispatched back through this applier's
    #   claim-verified AddLabel/RemoveLabel handlers (same delegation shape as
    #   RESET_RETRY_ISSUE); the extra half of the command is an event emit.
    # - APPEND_PATTERN_OBSERVATION: comments on an orchestrator-owned pattern
    #   case file and increments its durable observation count (#6781/#6957);
    #   case files are observation-labeled evidence ledgers that are never
    #   agent work items, so they are never claimed.
    # - PROMOTE_TECH_LEAD_FINDING: creates a NEW gated issue in the ROUTED repo
    #   (#6957) and records the promotion ledger row; it modifies nothing that
    #   exists, let alone a claimed coding issue.
    # - REPORT_PROMOTED_FINDING_EVIDENCE: comments on the routed promotion issue
    #   and advances its local evidence watermark (#6957); that target is an
    #   orchestrator-authored issue in the promotion lane, not a claimed coding
    #   issue in this repository.
    # - SETTLE_TECH_LEAD_PROMOTION: comments on / closes the orchestrator-owned
    #   case file and writes the local shipped-fix + promotion ledger rows
    #   (#6957); like APPEND_PATTERN_OBSERVATION the only GitHub target is a
    #   case file, which is never claimed.
    # - CLEANUP_SESSION: post-completion cleanup
    # - RECONCILE_HISTORY_ENTRY: local session history mutation + event only
    # - CREATE_PR: not implemented in action_applier
    EXEMPT_ACTIONS = {
        ActionType.APPLY_PROVIDER_IMPACT,
        ActionType.APPEND_PATTERN_OBSERVATION,
        ActionType.PROMOTE_TECH_LEAD_FINDING,
        ActionType.REPORT_PROMOTED_FINDING_EVIDENCE,
        ActionType.SETTLE_TECH_LEAD_PROMOTION,
        ActionType.LAUNCH_SESSION,
        ActionType.LAUNCH_VALIDATION_RETRY,
        ActionType.STOP_SESSION,
        ActionType.CREATE_WORKTREE,
        ActionType.REMOVE_WORKTREE,
        ActionType.QUEUE_RETROSPECTIVE_REVIEW,
        ActionType.QUEUE_REWORK,
        ActionType.QUEUE_TECH_LEAD,
        # Withdraws a queued investigation from in-process state only (#6994
        # launch-time revalidation). No GitHub write, so no claim to verify.
        ActionType.DROP_TECH_LEAD,
        ActionType.CREATE_TECH_LEAD_ISSUE,
        ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE,
        ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE,
        ActionType.SURFACE_TECH_LEAD_PROPOSAL,
        ActionType.RESET_RETRY_ISSUE,
        ActionType.KILL_HUNG_SESSION,
        ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS,
        ActionType.CLEANUP_SESSION,
        ActionType.RECONCILE_HISTORY_ENTRY,
        ActionType.CREATE_PR,
    }

    def test_all_action_types_accounted_for(self):
        """Every ActionType must be in either GITHUB_WRITE_ACTIONS or EXEMPT_ACTIONS."""
        all_types = set(ActionType)
        accounted = self.GITHUB_WRITE_ACTIONS | self.EXEMPT_ACTIONS
        unaccounted = all_types - accounted

        assert not unaccounted, (
            f"New ActionType(s) {unaccounted} not classified for ClaimGate audit. "
            f"If these write to GitHub on a claimed issue, add to GITHUB_WRITE_ACTIONS. "
            f"If not, add to EXEMPT_ACTIONS with a comment explaining why."
        )

    def test_github_write_actions_call_verify(self):
        """Verify that action handler source code calls _verify_claim_before_write."""
        import inspect
        from issue_orchestrator.control.action_applier import ActionApplier

        # Map action types to handler method names
        handler_map = {
            ActionType.ADD_LABEL: "_apply_add_label",
            ActionType.REMOVE_LABEL: "_apply_remove_label",
            ActionType.SYNC_LABELS: "_apply_sync_labels",
            ActionType.SHED_RECOVERED_WORKFLOW_LABELS: (
                "_apply_shed_recovered_workflow_labels"
            ),
            ActionType.RECOVER_TERMINAL_ISSUE: "_apply_recover_terminal_issue",
            ActionType.ADD_COMMENT: "_apply_add_comment",
            ActionType.SUPERSEDE_PR: "_apply_supersede_pr",
            ActionType.CLOSE_ISSUE: "_apply_close_issue",
            ActionType.SET_ISSUE_STATE: "_apply_set_issue_state",
            ActionType.ESCALATE_TO_HUMAN: "_apply_escalate",
            ActionType.QUEUE_REVIEW: "_apply_queue_review",
            ActionType.ENQUEUE_TO_MERGE_QUEUE: "_apply_enqueue_to_merge_queue",
        }

        for action_type in self.GITHUB_WRITE_ACTIONS:
            handler_name = handler_map.get(action_type)
            assert handler_name, f"No handler mapping for {action_type}"

            # noqa: SLF001 - Inspecting handler source to verify ClaimGate wiring
            handler = getattr(ActionApplier, handler_name, None)
            assert handler, f"Handler {handler_name} not found on ActionApplier"

            source = inspect.getsource(handler)
            assert "_verify_claim_before_write" in source, (
                f"Handler {handler_name} for {action_type} does not call "
                f"_verify_claim_before_write — all GitHub writes on claimed "
                f"issues must verify claim ownership first"
            )


class TestTechLeadMutationsCrossTheReconciliationGate:
    """#6957 round-5 review F15/A6: the extracted tech-lead dispatch is guarded.

    Every tech-lead action carries ``build_expected_for_mutation()``, but the
    extracted owners were invoked directly, so the applier's hard
    optimistic-concurrency gate never ran on this branch. A case file paused
    behind ``io:needs-reconcile`` could still have a promotion filed against it,
    receive evidence comments, or be settled.
    """

    CASE_FILE = 65

    @pytest.fixture
    def guarded(self, mock_labels, mock_sessions, mock_events, mock_repository_host):
        """An applier with reconciliation live and the case file PAUSED."""
        from issue_orchestrator.control.action_applier import ActionApplier

        reader = MagicMock()
        reader.read_issue_labels.return_value = [
            "tech-lead-observation",
            "io:needs-reconcile",
        ]
        authority = MagicMock()
        target = MagicMock()
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            reconcile=True,
            tech_lead_ops=authority,
            promotion_target=target,
        )
        return applier, authority, target, mock_repository_host

    @staticmethod
    def _actions(case_file: int):
        from issue_orchestrator.control.actions import (
            AppendPatternObservationAction,
            PromoteTechLeadFindingAction,
            ReportPromotedFindingEvidenceAction,
            SettleTechLeadPromotionAction,
        )
        from issue_orchestrator.control.reconciliation import (
            build_expected_for_mutation,
        )
        from issue_orchestrator.domain.tech_lead_findings import PatternObservation

        expected = build_expected_for_mutation()
        marker = "<!-- issue-orchestrator:tech-lead-promotion:v1:abc -->"
        return [
            AppendPatternObservationAction(
                issue_number=case_file,
                pattern_signature="sig",
                observation=PatternObservation(
                    observation_id="r1:s:A1", comment="observed again"
                ),
                expected=expected,
            ),
            PromoteTechLeadFindingAction(
                signature="sig",
                case_file_issue_number=case_file,
                target_repo="owner/upstream",
                title="[tech-lead:repo] sig",
                body=f"body\n\n{marker}",
                labels=("agent:backend",),
                observation_count=2,
                idempotency_marker=marker,
                expected=expected,
            ),
            ReportPromotedFindingEvidenceAction(
                signature="sig",
                case_file_issue_number=case_file,
                target_repo="owner/upstream",
                target_issue_number=500,
                observation_count=3,
                comment="more evidence",
                expected=expected,
            ),
            SettleTechLeadPromotionAction(
                signature="sig",
                case_file_issue_number=case_file,
                target_repo="owner/upstream",
                target_issue_number=500,
                shipped=True,
                merged_pr_url="https://x/pull/1",
                expected=expected,
            ),
        ]

    def test_each_mutating_action_raises_and_writes_nothing(self, guarded):
        from issue_orchestrator.control.reconciliation import ReconciliationRequired

        applier, authority, target, repository_host = guarded

        for action in self._actions(self.CASE_FILE):
            with pytest.raises(ReconciliationRequired):
                applier.apply(action)

        # Zero writes anywhere: no cross-repo filing/commenting, no case-file
        # comment, and no durable ledger row.
        assert target.method_calls == []
        assert authority.method_calls == []
        repository_host.add_comment.assert_not_called()
        repository_host.create_issue.assert_not_called()
        repository_host.update_issue_state.assert_not_called()

    def test_the_gate_checks_the_managed_repos_case_file(self, guarded):
        """The promoted issue lives in another repo; the case file is the subject."""
        from issue_orchestrator.control.reconciliation import ReconciliationRequired

        applier, _authority, _target, _repository_host = guarded
        reader = applier.fresh_issue_reader

        for action in self._actions(self.CASE_FILE):
            reader.read_issue_labels.reset_mock()
            with pytest.raises(ReconciliationRequired):
                applier.apply(action)
            assert reader.read_issue_labels.call_args[0][0] == self.CASE_FILE

    def test_an_unpaused_case_file_reaches_its_owner(self, guarded):
        """The gate blocks a PAUSED case file, not every promotion action."""
        applier, authority, _target, _repository_host = guarded
        applier.fresh_issue_reader.read_issue_labels.return_value = [
            "tech-lead-observation"
        ]
        authority.has_pattern_observation.return_value = True  # already recorded

        [append, *_rest] = self._actions(self.CASE_FILE)
        result = applier.apply(append)

        assert result.success
        authority.has_pattern_observation.assert_called_once()


class TestActLevelOpsCrossTheGateExactlyOnce:
    """#6957 round-2 review F5/A5: one reconciliation owner, one fresh read.

    The centralized dispatch wrapper now gates every mutating tech-lead command,
    but ``_apply_tech_lead_op`` kept its own ``_require_expected`` call. Since
    the fresh reader deliberately bypasses every cache, an allowed reset or kill
    performed TWO correctness-critical GitHub reads per dispatch — against this
    repo's limited-API discipline — and left mutation policy owned in two
    places. The registry test substitutes inert handlers, so only a boundary
    test on the composed applier can see the duplicate.
    """

    TARGET = 12
    PROPOSAL = 800

    @pytest.fixture
    def counted(self, mock_labels, mock_sessions, mock_events, mock_repository_host):
        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.actions import ActionResult

        reader = MagicMock()
        # No pause label: the gate ALLOWS these, which is the case that used to
        # pay for two reads. A blocked gate short-circuits after one either way.
        reader.read_issue_labels.return_value = ["in-progress"]
        # The kill path re-confirms consent through the repository host (a
        # different port), so it never contributes to the fresh-read count.
        mock_repository_host.get_issue.return_value = Issue(
            number=self.PROPOSAL, title="Tech Lead proposal", labels=["agent:tech-lead"]
        )
        executor = MagicMock()
        executor.apply.side_effect = lambda action: ActionResult.ok(action)
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            reconcile=True,
            tech_lead_ops=MagicMock(),
            tech_lead_reset_retry=executor,
            tech_lead_kill_session=executor,
        )
        return applier, reader, executor

    def _reset(self):
        from issue_orchestrator.control.actions import ResetRetryIssueAction
        from issue_orchestrator.control.reconciliation import (
            build_expected_for_mutation,
        )

        return ResetRetryIssueAction(
            issue_number=self.TARGET,
            proposal_id="A1",
            expected=build_expected_for_mutation(),
        )

    def _kill(self):
        from issue_orchestrator.control.actions import KillHungSessionAction
        from issue_orchestrator.control.reconciliation import (
            build_expected_for_mutation,
        )

        return KillHungSessionAction(
            issue_number=self.TARGET,
            proposal_id="A1",
            proposal_issue_number=self.PROPOSAL,
            expected=build_expected_for_mutation(),
        )

    def test_an_allowed_reset_reads_fresh_labels_once(self, counted):
        applier, reader, executor = counted

        applier.apply(self._reset())

        assert reader.read_issue_labels.call_count == 1
        assert reader.read_issue_labels.call_args[0][0] == self.TARGET
        executor.apply.assert_called_once()

    def test_an_allowed_kill_reads_fresh_labels_once(self, counted):
        applier, reader, executor = counted

        applier.apply(self._kill())

        assert reader.read_issue_labels.call_count == 1
        assert reader.read_issue_labels.call_args[0][0] == self.TARGET
        executor.apply.assert_called_once()

    @pytest.mark.parametrize("op", ("reset", "kill"))
    def test_a_paused_target_still_stops_the_executor(self, counted, op):
        """Removing the duplicate must not remove the gate itself."""
        from issue_orchestrator.control.reconciliation import ReconciliationRequired

        applier, reader, executor = counted
        reader.read_issue_labels.return_value = ["in-progress", "io:needs-reconcile"]

        with pytest.raises(ReconciliationRequired):
            applier.apply(self._reset() if op == "reset" else self._kill())

        executor.apply.assert_not_called()


class TestTechLeadIssueCreationCrossesTheReconciliationGate:
    """#6957 round-6 review F3/A3: creation is guarded like every other mutation.

    The first fix wrapped the four commands whose subjects the dispatch table
    happened to spell out by hand, and issue CREATION — a remote issue, its
    labels, a ledger row and anchor comments, the largest mutation of the lot —
    kept sailing past the gate. So a source anchor paused behind
    ``io:needs-reconcile`` could still cause a brand-new case file or gated
    proposal issue.
    """

    ANCHOR = 77

    @pytest.fixture
    def guarded(self, mock_labels, mock_sessions, mock_events, mock_repository_host):
        """An applier with reconciliation live and the ANCHOR issue PAUSED."""
        from issue_orchestrator.control.action_applier import ActionApplier

        from issue_orchestrator.control.tech_lead_run_ownership import (
            single_instance_run_ownership,
        )

        reader = MagicMock()
        reader.read_issue_labels.return_value = ["tech-lead", "io:needs-reconcile"]
        authority = MagicMock()
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            reconcile=True,
            tech_lead_ops=authority,
            run_ownership=single_instance_run_ownership(),
        )
        return applier, authority, mock_repository_host

    @classmethod
    def _creations(cls):
        from issue_orchestrator.control.actions import (
            CreateTechLeadCaseFileIssueAction,
            CreateTechLeadProposalIssueAction,
        )
        from issue_orchestrator.control.reconciliation import (
            build_expected_for_mutation,
        )
        from issue_orchestrator.domain.tech_lead_findings import PatternObservation
        from issue_orchestrator.domain.tech_lead_session import (
            StoredTechLeadOp,
            TechLeadCreationOrigin,
        )

        expected = build_expected_for_mutation()
        origin = TechLeadCreationOrigin.derived_from_anchor(cls.ANCHOR)
        marker = "<!-- issue-orchestrator:tech-lead-case-file:v1:abc -->"
        return [
            CreateTechLeadCaseFileIssueAction(
                title="Pattern case file: sig",
                body=f"documentation only\n\n{marker}",
                labels=("agent:tech-lead", "tech-lead-observation"),
                pattern_signature="sig",
                origin=origin,
                idempotency_marker=marker,
                observations=(
                    PatternObservation(observation_id="r1:s:A1", comment="observed"),
                ),
                expected=expected,
            ),
            CreateTechLeadProposalIssueAction(
                title="Tech Lead proposal: reset_retry #12",
                body="documentation only",
                labels=("agent:tech-lead", "proposed-tech-lead"),
                origin=origin,
                op=StoredTechLeadOp(
                    op_type="reset_retry",
                    target_issue_number=12,
                    rationale="stuck",
                    source_run_id="r1",
                    source_session_name="s",
                    source_action_id="A1",
                    created_at="2026-08-04T00:00:00Z",
                ),
                expected=expected,
            ),
        ]

    def test_a_paused_anchor_creates_nothing_anywhere(self, guarded):
        from issue_orchestrator.control.reconciliation import ReconciliationRequired

        applier, authority, repository_host = guarded

        for action in self._creations():
            with pytest.raises(ReconciliationRequired):
                applier.apply(action)

        # No remote issue, no label provisioning, no ledger row, no comment.
        repository_host.create_issue.assert_not_called()
        repository_host.add_comment.assert_not_called()
        assert authority.method_calls == []

    def test_the_gate_checks_the_anchor_the_creation_was_decided_from(self, guarded):
        from issue_orchestrator.control.reconciliation import ReconciliationRequired

        applier, _authority, _repository_host = guarded
        reader = applier.fresh_issue_reader

        for action in self._creations():
            reader.read_issue_labels.reset_mock()
            with pytest.raises(ReconciliationRequired):
                applier.apply(action)
            assert reader.read_issue_labels.call_args[0][0] == self.ANCHOR

    def test_an_unpaused_anchor_reaches_the_creation_owner(
        self, mock_labels, mock_sessions, mock_events, mock_repository_host
    ):
        """The gate blocks a PAUSED anchor, not every tech-lead creation."""
        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        reader = MagicMock()
        reader.read_issue_labels.return_value = ["tech-lead"]
        mock_repository_host.find_issue_by_marker.return_value = None
        mock_repository_host.list_labels.return_value = [
            {"name": "agent:tech-lead"},
            {"name": "tech-lead-observation"},
        ]
        mock_repository_host.create_issue.return_value = {"number": 900}
        applier = ActionApplier(
            labels=mock_labels,
            sessions=mock_sessions,
            events=mock_events,
            repository_host=mock_repository_host,
            fresh_issue_reader=reader,
            reconcile=True,
            tech_lead_ops=InMemoryTechLeadAuthorityStore(),
        )

        [case_file, _proposal] = self._creations()
        result = applier.apply(case_file)

        assert result.success, result.error
        mock_repository_host.create_issue.assert_called_once()

    def test_an_anchor_authoring_creation_needs_no_subject(self, guarded):
        """The batch/health-review anchor IS the first issue — nothing to gate on."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction

        applier, _authority, repository_host = guarded
        repository_host.create_issue.return_value = 901

        applier.apply(
            CreateTechLeadIssueAction(
                title="Tech Lead Batch Review",
                body="b",
                labels=("agent:tech-lead",),
                pr_count=3,
                origin=TechLeadCreationOrigin.authors_anchor(),
            )
        )

        repository_host.create_issue.assert_called_once()
        # ...and it cost no reconciliation read, because there is no subject.
        applier.fresh_issue_reader.read_issue_labels.assert_not_called()

    def test_a_dropped_subject_cannot_be_dispatched_at_all(self, guarded):
        """A follow-up whose guard fields were dropped is unrepresentable.

        The applier-level backstop only ever saw "expectations, no subject". A
        composition that dropped BOTH looked exactly like anchor authoring and
        wrote unguarded, so the command boundary is what rejects it now (#6957
        round-2 review F6/A6) — before an action object exists to dispatch.
        """
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.control.reconciliation import (
            build_expected_for_mutation,
        )

        _applier, _authority, repository_host = guarded

        # One field dropped: the subject is gone but the expectations remain.
        with pytest.raises(ValueError, match="reconciliation subject was dropped"):
            CreateTechLeadIssueAction(
                title="follow-up",
                body="b",
                labels=("agent:backend",),
                origin=TechLeadCreationOrigin.authors_anchor(),
                expected=build_expected_for_mutation(),
            )
        # Both dropped: the follow-up keeps its origin, so the missing
        # ExpectedState is caught instead of passing as anchor authoring.
        with pytest.raises(ValueError, match="must carry an ExpectedState"):
            CreateTechLeadIssueAction(
                title="follow-up",
                body="b",
                labels=("agent:backend",),
                origin=TechLeadCreationOrigin.derived_from_anchor(self.ANCHOR),
            )
        repository_host.create_issue.assert_not_called()

    def test_the_creation_command_cannot_omit_its_origin(self):
        """The origin is required, not defaulted — omission is a TypeError.

        This is what makes the two-field omission unrepresentable rather than
        merely rejected: there is no value the caller can leave out and have
        the command guess authority from (#6957 round-2 F6/A6).
        """
        from issue_orchestrator.control.actions import (
            CreateTechLeadCaseFileIssueAction,
            CreateTechLeadIssueAction,
            CreateTechLeadProposalIssueAction,
        )

        for command in (
            CreateTechLeadIssueAction,
            CreateTechLeadProposalIssueAction,
            CreateTechLeadCaseFileIssueAction,
        ):
            with pytest.raises(TypeError, match="origin"):
                command(title="t", body="b", labels=("agent:backend",))  # type: ignore[call-arg]
