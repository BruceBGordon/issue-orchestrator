"""Unit tests for OrchestratorSupport.

These tests verify the behavior of support functions extracted from the Orchestrator.
Tests are behavior-centric, focusing on invariants, state transitions, and outcomes.
"""

import time
import pytest
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, PropertyMock
from typing import Optional

from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.control.orchestrator_support import (
    OrchestratorSupport,
    log_transition,
    init_orchestrator_components,
    pause_issue_for_reconciliation,
    clear_discovered_facts,
    emit_heartbeat_if_needed,
    check_health,
    run_planning_cycle,
    run_tick,
    _fetch_and_update_queue,
    _iter_blocked_history_issue_numbers,
    _record_issue_refreshes,
    _reconcile_closed_issue_history,
    _select_hot_issue_numbers,
    _track_stale_ticks,
)
from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.issue_fetch_resilience import (
    IssueFetchResilience,
    PermanentIssueFetchError,
    TransientIssueFetchError,
)
from issue_orchestrator.control.reconciliation import (
    ReconciliationRequired,
    ExpectedState,
    ExternalSnapshot,
    get_pause_label,
)
from issue_orchestrator.control.session_history import CLOSED_ISSUE_HISTORY_STATUS_REASON
from issue_orchestrator.control.pending_session_queues import PendingSessionQueues
from issue_orchestrator.control.actions import (
    ActionResult,
    ActionType,
    AddLabelAction,
    LaunchSessionAction,
    SessionType,
)
from issue_orchestrator.control.health_gate import HealthGate, HealthDecision
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    OrchestratorState,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
    PendingCleanup,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeEscalation,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredMergeQueueEnqueue,
    DiscoveredRetrospectiveReview,
    DiscoveredReview,
    DiscoveredRework,
    DiscoveredEscalation,
    DiscoveredFailure,
    AgentConfig,
    ImmediateCleanup,
    SessionHistoryEntry,
    SessionHistoryStatus,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from tests.unit.session_run_helpers import make_session_run_assets
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.infra.config import Config


# =============================================================================
# Test Fixtures and Helpers
# =============================================================================


@pytest.fixture
def mock_event_sink():
    """Create a mock EventSink for tracking published events."""
    sink = MagicMock()
    sink.events = []

    def capture_publish(event):
        sink.events.append(event)

    sink.publish = Mock(side_effect=capture_publish)
    return sink


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    host = MagicMock()
    host.add_label = Mock()
    host.remove_label = Mock()
    host.create_issue_key = Mock(side_effect=lambda n: FakeIssueKey(name=str(n)))
    return host


@pytest.fixture
def mock_action_applier():
    """Create a mock ActionApplier."""
    applier = MagicMock()
    applier.apply = Mock()
    return applier


@pytest.fixture
def sample_orchestrator_state():
    """Create a sample OrchestratorState for testing."""
    return OrchestratorState(
        active_sessions=[],
        paused=False,
        pending_reviews=[],
        pending_reworks=[],
        pending_tech_lead_reviews=[],
        discovered_reviews=[],
        discovered_reworks=[],
        discovered_escalations=[],
        discovered_failures=[],
    )


@pytest.fixture
def sample_event_context():
    """Create a mock EventContext."""
    ctx = MagicMock()
    ctx.tick_id = 1
    ctx.enrich = Mock(side_effect=lambda d: d)
    return ctx


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample AgentConfig."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )


def make_issue(number: int, title: str = "Test Issue", labels: list | None = None) -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        labels=labels or [],
        state="open",
    )


def make_history_entry(issue_number: int, status: SessionHistoryStatus) -> SessionHistoryEntry:
    """Create a test session history entry."""
    return SessionHistoryEntry(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        agent_type="agent:web",
        status=status,
        runtime_minutes=1,
        pr_url=None,
        status_reason=status,
        completed_at=datetime.now(),
    )


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE, tmp_path: Path = None) -> Session:
    """Create a test session for an issue."""
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    worktree = tmp_path or Path("/tmp/test-worktree")

    return Session(
        key=session_key,
        issue=issue,
        agent_config=AgentConfig(
            prompt_path=Path("/tmp/prompt.md"),
        ),
        terminal_id=f"session-{issue.number}",
        worktree_path=worktree,
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(
            worktree,
            session_name=f"session-{issue.number}",
        ),
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )


class TestQueueFetchPlanner:
    """Tests for queue fetch-layer planner behavior."""

    def _make_config(self) -> Config:
        config = Config()
        config.queue_refresh_seconds = 600
        config.fetch_layer_enabled = True
        config.fetch_layer_network_sync_seconds = 60
        config.fetch_layer_full_scan_interval_seconds = 3600
        config.fetch_layer_discovery_limit = 10
        config.fetch_layer_max_hot_issues_per_cycle = 10
        config.fetch_layer_pr_scan_every_n_refreshes = 2
        config.fetch_layer_dependency_scan_every_n_refreshes = 1
        return config

    def test_manual_refresh_uses_full_scan(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [make_issue(2, labels=["agent:web"])]

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_called_once()
        github_workflow.refresh_issues.assert_not_called()
        assert state.queue_last_refresh_mode == "full"
        assert state.queue_refresh_count == 1
        assert state.queue_last_full_scan_at > 0

    def test_network_refresh_syncs_open_issue_corpus_on_the_read_phase(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [
            make_issue(1, labels=["agent:web"])
        ]
        corpus_sync = Mock()

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            open_issue_corpus=corpus_sync,
        )

        corpus_sync.sync.assert_called_once_with()

    def test_scheduled_refresh_uses_incremental_when_full_scan_not_due(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, title="Updated 1", labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = [make_issue(3, labels=["agent:web"])]

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_not_called()
        github_workflow.refresh_issues.assert_called_once()
        github_workflow.fetch_discovery_issues.assert_called_once_with(config.filtering.milestone, 10)
        assert state.queue_last_refresh_mode == "incremental"
        assert state.queue_refresh_count == 1
        queue_numbers = {issue.number for issue in state.cached_queue_issues}
        assert queue_numbers == {1, 2, 3}

    def test_scan_cadence_skips_pr_scans_until_due(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_pr_scan_every_n_refreshes = 3
        config.fetch_layer_dependency_scan_every_n_refreshes = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,  # next refresh count = 2
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=False,
        )
        scheduler.evaluate_issues.assert_called_once()

    def test_pr_scan_uses_single_workflow_call_when_due(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_pr_scan_every_n_refreshes = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,  # next refresh count = 2
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=True,
        )
        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()

    def test_visibility_aware_hot_list_prioritizes_visible_issues(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_visibility_aware_enabled = True
        config.fetch_layer_max_hot_issues_per_cycle = 2
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            ui_visible_issue_numbers=[99],
            ui_visible_updated_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.refresh_issues.assert_called_once_with([99, 1])

    def test_selective_sync_planner_can_skip_noncritical_scans(self, mock_event_sink, mock_repository_host):
        config = self._make_config()
        config.fetch_layer_selective_sync_planner_enabled = True
        config.fetch_layer_pr_scan_every_n_refreshes = 10
        config.fetch_layer_dependency_scan_every_n_refreshes = 10
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_refresh_count=1,
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.scan_needs_code_review_prs.assert_not_called()
        github_workflow.scan_needs_rework_prs.assert_not_called()
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=False,
        )
        scheduler.evaluate_issues.assert_not_called()

    def test_fetch_prunes_refresh_timestamps_for_issues_no_longer_tracked(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            issue_refresh_timestamps={1: 100.0, 999: 200.0},
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert 1 in state.issue_refresh_timestamps
        assert 999 not in state.issue_refresh_timestamps

    def test_delta_sync_updates_watermark_only_after_successful_fetch(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.return_value = (
            [make_issue(2, labels=["agent:web"])],
            "2026-01-01T01:00:00Z",
        )
        github_workflow.issue_in_scope.return_value = True

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_delta_issues.assert_called_once_with(
            since="2026-01-01T00:00:00Z",
            fetch_limit=10,
        )
        assert state.queue_delta_watermark == "2026-01-01T01:00:00Z"

    def test_delta_sync_error_propagates_without_mutating_queue(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        # The issue-list fetch is guarded, so a transient 503 surfaces as a
        # TransientIssueFetchError (with the raw GitHubHttpError as its cause)
        # rather than the raw error — but the queue must still be untouched.
        with pytest.raises(TransientIssueFetchError) as exc_info:
            _fetch_and_update_queue(
                config=config,
                events=mock_event_sink,
                state=state,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                refresh_requested=False,
                inflight_stable_ids={},
                issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            )

        assert isinstance(exc_info.value.__cause__, GitHubHttpError)
        assert exc_info.value.__cause__.status_code == 503
        assert [issue.number for issue in state.cached_queue_issues] == [1]
        assert state.queue_delta_watermark == "2026-01-01T00:00:00Z"
        assert state.queue_refresh_count == 0
        assert state.queue_refresh_in_progress is False

    def test_post_fetch_pr_scan_error_is_not_classified_as_issue_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A RepositoryHostError from a *post-fetch* PR scan must surface as the
        raw error — not be reclassified as an issue-list fetch failure.

        The resilience guard wraps only the issue-list fetch. If it wrapped the
        whole queue update, this 404 from ``scan_pending_pr_work`` would be
        promoted to a PermanentIssueFetchError at tolerance 1 and shut the
        orchestrator down as if the repository were missing.
        """
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        # The issue-list fetch itself succeeds...
        github_workflow.fetch_all_issues.return_value = [make_issue(1, labels=["agent:web"])]
        # ...but the downstream PR scan hits a repository-host 404.
        github_workflow.scan_pending_pr_work.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/pulls failed: 404", status_code=404
        )
        # Tolerance 1 means the *first* issue-fetch 404 would fail fast — so if
        # the PR-scan 404 were (wrongly) routed through the policy, this would
        # raise PermanentIssueFetchError instead of the raw error.
        resilience = IssueFetchResilience("owner/repo", repo_not_found_tolerance=1)

        with pytest.raises(GitHubHttpError) as exc_info:
            _fetch_and_update_queue(
                config=config,
                events=mock_event_sink,
                state=state,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                refresh_requested=True,  # manual → full scan → PR scan runs
                inflight_stable_ids={},
                issue_fetch_resilience=resilience,
            )

        # Raw repository error surfaces, NOT a resilience classification.
        assert exc_info.value.status_code == 404
        assert not isinstance(exc_info.value, TransientIssueFetchError)
        assert not isinstance(exc_info.value, PermanentIssueFetchError)
        github_workflow.scan_pending_pr_work.assert_called_once_with(
            state,
            include_general_scans=True,
        )
        # The fetch succeeded, so the policy recorded success: a *genuine*
        # issue-fetch 404 afterwards is still the first in its streak (proving
        # the PR-scan 404 was never counted by the policy).
        verdict = resilience.record_failure(
            GitHubHttpError("issues 404", status_code=404)
        )
        assert verdict.consecutive_repo_not_found == 1

    def test_delta_sync_removes_out_of_scope_issue_from_cached_queue(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(7, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
            queue_delta_watermark="2026-01-01T00:00:00Z",
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(7, labels=["agent:web"])]
        github_workflow.fetch_delta_issues.return_value = (
            [make_issue(7, labels=["agent:web"], title="No longer scoped")],
            "2026-01-01T01:00:00Z",
        )
        github_workflow.issue_in_scope.return_value = False

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert all(issue.number != 7 for issue in state.cached_queue_issues)

    def test_run_planning_cycle_uses_network_sync_interval_not_queue_refresh(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        config.queue_refresh_seconds = 0
        config.fetch_layer_network_sync_seconds = 120
        state = OrchestratorState()
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        github_workflow = Mock()

        last_sync = time.time()
        next_sync, refresh_requested = run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=Mock(),
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=last_sync,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert next_sync == last_sync
        assert refresh_requested is False
        github_workflow.fetch_all_issues.assert_not_called()

    def test_run_planning_cycle_rechecks_pending_queue_shrink_after_delay(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        config.queue_refresh_seconds = 0
        config.fetch_layer_network_sync_seconds = 3600
        prior_issues = [
            make_issue(number, labels=["agent:web"]) for number in range(1, 21)
        ]
        state = OrchestratorState(
            cached_scope_issues=list(prior_issues),
            cached_queue_issues=list(prior_issues),
            queue_pending_shrink_missing_issue_numbers=list(range(2, 21)),
            queue_pending_shrink_confirm_at=1.0,
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = list(prior_issues[1:11])
        github_workflow.fetch_discovery_issues.return_value = []
        last_sync = time.time()

        next_sync, refresh_requested = run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=last_sync,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert next_sync >= last_sync
        assert state.queue_last_network_sync_at >= last_sync
        assert refresh_requested is False
        assert state.queue_last_refresh_mode == "incremental"
        github_workflow.fetch_all_issues.assert_not_called()
        github_workflow.refresh_issues.assert_called_once_with(list(range(2, 12)))
        assert state.queue_pending_shrink_missing_issue_numbers == []

    def test_run_planning_cycle_survives_transient_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A transient issue-list failure keeps the cached queue and keeps going."""
        config = self._make_config()
        cached = [make_issue(1, labels=["agent:web"]), make_issue(2, labels=["agent:web"])]
        state = OrchestratorState(
            cached_queue_issues=list(cached),
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        fact_gatherer.create_snapshot.return_value = Mock()
        planner = Mock()
        planner.plan.return_value = Mock(action_count=0, actions=[])
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/issues failed: 404", status_code=404
        )

        # Must NOT raise — the orchestrator stays up on a recoverable blip.
        run_planning_cycle(
            config=config,
            events=mock_event_sink,
            event_context=Mock(enrich=lambda payload: payload),
            state=state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            apply_plan_fn=Mock(),
            clear_discovered_facts_fn=Mock(),
            last_network_sync=0.0,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        github_workflow.fetch_all_issues.assert_called_once()
        # Cached queue is preserved, and planning still proceeds with it.
        assert [issue.number for issue in state.cached_queue_issues] == [1, 2]
        planner.plan.assert_called_once()

    def test_run_planning_cycle_fails_fast_on_permanent_fetch_failure(
        self, mock_event_sink, mock_repository_host
    ):
        """A persistent repo-not-found propagates a clear, actionable error."""
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        fact_gatherer = Mock()
        planner = Mock()
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.side_effect = GitHubHttpError(
            "GitHub GET /repos/owner/repo/issues failed: 404", status_code=404
        )

        with pytest.raises(PermanentIssueFetchError) as exc_info:
            run_planning_cycle(
                config=config,
                events=mock_event_sink,
                event_context=Mock(enrich=lambda payload: payload),
                state=state,
                fact_gatherer=fact_gatherer,
                planner=planner,
                repository_host=mock_repository_host,
                scheduler=scheduler,
                github_workflow=github_workflow,
                apply_plan_fn=Mock(),
                clear_discovered_facts_fn=Mock(),
                last_network_sync=0.0,
                refresh_requested=True,
                inflight_stable_ids={},
                issue_fetch_resilience=IssueFetchResilience(
                    "owner/repo", repo_not_found_tolerance=1
                ),
            )

        assert "owner/repo" in str(exc_info.value)
        # Planning is never reached when the fetch fails fast.
        planner.plan.assert_not_called()

    def test_suspicious_full_scan_shrink_retains_queue_and_watermark(
        self, mock_event_sink, mock_repository_host
    ):
        config = self._make_config()
        state = OrchestratorState(
            cached_scope_issues=[
                make_issue(number, labels=["agent:web"]) for number in range(1, 21)
            ],
            cached_queue_issues=[
                make_issue(number, labels=["agent:web"]) for number in range(1, 21)
            ],
            queue_delta_watermark="2026-01-01T00:00:00Z",
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [
            make_issue(1, labels=["agent:web"])
        ]
        queue_cache_store = Mock()

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            queue_cache_store=queue_cache_store,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert [issue.number for issue in state.cached_queue_issues] == list(range(1, 21))
        assert [issue.number for issue in state.cached_scope_issues] == list(range(1, 21))
        assert state.queue_pending_shrink_missing_issue_numbers == list(range(2, 21))
        assert state.queue_delta_watermark == "2026-01-01T00:00:00Z"
        queue_cache_store.save_snapshot.assert_called_once_with(
            state.cached_scope_issues,
            "2026-01-01T00:00:00Z",
            repo=config.repo or "",
        )

    def test_fetch_logs_gh_cost_per_cycle(self, mock_event_sink, mock_repository_host, caplog):
        import logging

        caplog.set_level(logging.INFO)
        config = self._make_config()
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.refresh_issues.return_value = [make_issue(1, labels=["agent:web"])]
        github_workflow.fetch_discovery_issues.return_value = []

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        assert "[FETCH-COST]" in caplog.text

    def test_dependency_scan_passes_active_sessions_to_evaluate(
        self, mock_event_sink, mock_repository_host
    ):
        """#6873 R1 wiring: the dependency-scan availability pass MUST feed live
        sessions to the scheduler — else an in-progress issue is mis-classified
        available and the blocked->front policy front-queues actively-worked work.
        Fails on the pre-fix wiring, which called evaluate_issues without them."""
        config = self._make_config()
        issue = make_issue(1, labels=["agent:web"])
        session = make_session(issue)
        state = OrchestratorState(
            active_sessions=[session],
            cached_queue_issues=[issue],
            queue_last_full_scan_at=time.time(),
        )
        scheduler = Mock()
        scheduler.evaluate_issues.return_value = []
        github_workflow = Mock()
        github_workflow.fetch_all_issues.return_value = [issue]

        _fetch_and_update_queue(
            config=config,
            events=mock_event_sink,
            state=state,
            repository_host=mock_repository_host,
            scheduler=scheduler,
            github_workflow=github_workflow,
            refresh_requested=True,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        )

        scheduler.evaluate_issues.assert_called_once()
        assert (
            scheduler.evaluate_issues.call_args.kwargs.get("active_sessions")
            == state.active_sessions
        )

    def test_blocked_history_issues_are_hot_refresh_candidates(self):
        state = OrchestratorState(
            cached_queue_issues=[make_issue(1, labels=["agent:web"])],
            session_history=[
                make_history_entry(270, "needs_human"),
                make_history_entry(271, "completed"),
            ],
        )

        hot_numbers = _select_hot_issue_numbers(
            state,
            limit=10,
            visibility_aware_enabled=False,
        )

        assert 270 in hot_numbers
        assert 271 not in hot_numbers

    def test_live_pending_work_has_hot_refresh_priority_over_blocked_history(self):
        state = OrchestratorState(
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey("270"),
                    pr_number=6073,
                    pr_url="https://github.com/test/repo/pull/6073",
                    branch_name="feature/270",
                    _issue_number=270,
                ),
            ],
            session_history=[make_history_entry(271, "needs_human")],
        )

        hot_numbers = _select_hot_issue_numbers(
            state,
            limit=1,
            visibility_aware_enabled=False,
        )

        assert hot_numbers == [270]

    def test_blocked_history_hot_refresh_candidates_are_deduplicated(self):
        state = OrchestratorState(
            session_history=[
                make_history_entry(270, "blocked"),
                make_history_entry(270, "needs_human"),
                make_history_entry(271, "needs_human"),
            ],
        )

        assert list(_iter_blocked_history_issue_numbers(state)) == [271, 270]

    def test_closed_issue_refresh_reconciles_blocked_history(self):
        closed_issue = make_issue(270, labels=["agent:web"])
        closed_issue.state = "closed"
        history_entry = make_history_entry(270, "needs_human")
        state = OrchestratorState(session_history=[history_entry])

        mutations = _reconcile_closed_issue_history(state, [closed_issue])

        assert len(mutations) == 1
        assert mutations[0].previous_status == "needs_human"
        assert history_entry.status == "closed"
        assert history_entry.status_reason == CLOSED_ISSUE_HISTORY_STATUS_REASON

    def test_open_issue_refresh_does_not_reconcile_blocked_history(self):
        open_issue = make_issue(270, labels=["agent:web"])
        history_entry = make_history_entry(270, "needs_human")
        state = OrchestratorState(session_history=[history_entry])

        mutations = _reconcile_closed_issue_history(state, [open_issue])

        assert mutations == []
        assert history_entry.status == "needs_human"


# =============================================================================
# Tests for log_transition
# =============================================================================


class TestLogTransition:
    """Tests for the log_transition helper function."""

    def test_logs_state_transition_format(self, caplog):
        """log_transition emits correctly formatted log message."""
        import logging
        caplog.set_level(logging.INFO)

        log_transition(
            entity_type="issue",
            number=42,
            from_state="AVAILABLE",
            to_state="IN_PROGRESS",
            reason="session started",
        )

        # Verify log format includes all components
        assert "[TRANSITION]" in caplog.text
        assert "issue #42" in caplog.text
        assert "AVAILABLE" in caplog.text
        assert "IN_PROGRESS" in caplog.text
        assert "session started" in caplog.text

    def test_logs_extra_details_at_debug_level(self, caplog):
        """log_transition logs extra details at DEBUG level."""
        import logging
        caplog.set_level(logging.DEBUG)

        log_transition(
            entity_type="review",
            number=100,
            from_state="QUEUED",
            to_state="STARTED",
            reason="reviewer assigned",
            extra={"reviewer": "agent:reviewer", "cycle": 1},
        )

        assert "#100" in caplog.text
        assert "extra" in caplog.text


# =============================================================================
# Tests for pause_issue_for_reconciliation
# =============================================================================


class TestPauseIssueForReconciliation:
    """Tests for pause_issue_for_reconciliation function."""

    def test_adds_pause_label_on_reconciliation_failure(
        self, mock_event_sink, mock_action_applier, sample_event_context
    ):
        """Reconciliation failure adds pause label to issue."""
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=42,
            reason="Labels changed externally",
        )

        # Should add the pause label
        pause_label = get_pause_label()
        mock_action_applier.apply.assert_called_once()
        action = mock_action_applier.apply.call_args[0][0]
        assert isinstance(action, AddLabelAction)
        assert action.issue_number == 42
        assert action.label == pause_label

    def test_emits_issue_paused_event(
        self, mock_event_sink, mock_action_applier, sample_event_context
    ):
        """Reconciliation pause emits ISSUE_PAUSED_RECONCILE event."""
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=99,
            reason="State drift detected",
        )

        # Should publish event
        mock_event_sink.publish.assert_called_once()
        event = mock_event_sink.events[0]
        assert event.name == EventName.ISSUE_PAUSED_RECONCILE
        assert event.data["issue_number"] == 99
        assert "State drift" in event.data["reason"]

    def test_handles_add_label_failure_gracefully(
        self, mock_event_sink, mock_action_applier, sample_event_context, caplog
    ):
        """If add_label fails, function logs error but doesn't raise."""
        import logging
        caplog.set_level(logging.ERROR)

        mock_action_applier.apply.side_effect = Exception("API error")

        # Should not raise
        pause_issue_for_reconciliation(
            events=mock_event_sink,
            action_applier=mock_action_applier,
            event_context=sample_event_context,
            issue_number=42,
            reason="Test failure",
        )

        # Should log error
        assert "Failed to add pause label" in caplog.text


# =============================================================================
# Tests for clear_discovered_facts
# =============================================================================


class TestClearDiscoveredFacts:
    """Tests for clear_discovered_facts function."""

    def test_clears_all_discovered_fact_lists(self, sample_orchestrator_state):
        """clear_discovered_facts empties all discovered* lists."""
        # Populate with test data
        sample_orchestrator_state.discovered_reviews = [
            DiscoveredReview(issue_number=1, pr_number=100, pr_url="url", branch_name="branch")
        ]
        sample_orchestrator_state.discovered_awaiting_merge_reconciliations = [
            DiscoveredAwaitingMergeReconciliation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status="merged",
                status_reason="PR merged; awaiting merge reconciled",
                source="pull_request",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_drifts = [
            DiscoveredAwaitingMergeDrift(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status_reason="PR closed; issue remains open",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_escalations = [
            DiscoveredAwaitingMergeEscalation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
                rework_cycle=1,
                kind="branch_protection_blocked",
                reason="Branch protection blocks merge.",
            )
        ]
        sample_orchestrator_state.discovered_merge_queue_enqueues = [
            DiscoveredMergeQueueEnqueue(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
            )
        ]
        sample_orchestrator_state.discovered_reworks = [
            DiscoveredRework(issue_number=2, pr_number=200, branch_name="br", agent_type="agent:dev", rework_cycle=1)
        ]
        sample_orchestrator_state.discovered_escalations = [
            DiscoveredEscalation(issue_number=3, pr_number=300, rework_cycle=3)
        ]
        sample_orchestrator_state.discovered_failures = [
            DiscoveredFailure(issue_number=4, issue_title="Test", failure_reason="failed")
        ]

        clear_discovered_facts(sample_orchestrator_state, Config(), tick_paused=False)

        # All lists should be empty
        assert len(sample_orchestrator_state.discovered_reviews) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_reconciliations) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_drifts) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_escalations) == 0
        assert len(sample_orchestrator_state.discovered_merge_queue_enqueues) == 0
        assert len(sample_orchestrator_state.discovered_reworks) == 0
        assert len(sample_orchestrator_state.discovered_escalations) == 0
        assert len(sample_orchestrator_state.discovered_failures) == 0

    def test_clears_immediate_cleanups(self, sample_orchestrator_state):
        """clear_discovered_facts also clears immediate_cleanups.

        This regression test ensures immediate_cleanups is cleared to prevent
        infinite cleanup loops where the same completed sessions are processed
        repeatedly.
        """
        # Add immediate cleanup entries
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(
                issue_number=1,
                terminal_id="issue-1",
                worktree_path="/tmp/worktree-1",
                reason="completed"
            ),
            ImmediateCleanup(
                issue_number=2,
                terminal_id="review-2",
                worktree_path="/tmp/worktree-2",
                reason="failed"
            ),
        ]

        clear_discovered_facts(sample_orchestrator_state, Config(), tick_paused=False)

        assert len(sample_orchestrator_state.immediate_cleanups) == 0

    def test_preserves_non_discovered_state(self, sample_orchestrator_state):
        """clear_discovered_facts does not affect other state fields."""
        # Set up some non-discovered state
        sample_orchestrator_state.paused = True
        sample_orchestrator_state.issues_started_count = 5
        sample_orchestrator_state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"),
                pr_number=100,
                pr_url="url",
                branch_name="branch",
                _issue_number=1,
            )
        ]

        clear_discovered_facts(sample_orchestrator_state, Config(), tick_paused=False)

        # Non-discovered fields should be untouched
        assert sample_orchestrator_state.paused is True
        assert sample_orchestrator_state.issues_started_count == 5
        assert len(sample_orchestrator_state.pending_reviews) == 1


# =============================================================================
# Tests for emit_heartbeat_if_needed
# =============================================================================


class TestEmitHeartbeatIfNeeded:
    """Tests for emit_heartbeat_if_needed function."""

    def test_emits_heartbeat_after_interval(self, mock_event_sink, sample_event_context, sample_orchestrator_state):
        """Heartbeat is emitted when interval has elapsed."""
        last_update = time.time() - 60  # 60 seconds ago

        new_timestamp = emit_heartbeat_if_needed(
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            last_ui_update=last_update,
            ui_update_interval=30,  # 30 second interval
        )

        # Should have published heartbeat
        mock_event_sink.publish.assert_called_once()
        event = mock_event_sink.events[0]
        assert event.name == EventName.ORCHESTRATOR_IDLE

        # Should return updated timestamp
        assert new_timestamp > last_update

    def test_no_heartbeat_before_interval(self, mock_event_sink, sample_event_context, sample_orchestrator_state):
        """No heartbeat when interval has not elapsed."""
        last_update = time.time() - 5  # 5 seconds ago

        new_timestamp = emit_heartbeat_if_needed(
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            last_ui_update=last_update,
            ui_update_interval=30,  # 30 second interval
        )

        # Should NOT have published heartbeat
        mock_event_sink.publish.assert_not_called()

        # Should return same timestamp
        assert new_timestamp == last_update


# =============================================================================
# Tests for check_health
# =============================================================================


class TestCheckHealth:
    """Tests for check_health function."""

    def test_returns_ok_when_healthy(self):
        """check_health returns OK decision when system is healthy."""
        health_gate = HealthGate()

        decision = check_health(
            health_gate=health_gate,
            paused=False,
        )

        assert decision.can_proceed is True

    def test_returns_blocked_when_paused(self):
        """check_health returns blocked when orchestrator is paused."""
        health_gate = HealthGate()

        decision = check_health(
            health_gate=health_gate,
            paused=True,
        )

        assert decision.can_proceed is False
        assert "paused" in decision.reason


# =============================================================================
# Tests for OrchestratorSupport.apply_plan
# =============================================================================


class TestOrchestratorSupportApplyPlan:
    """Tests for OrchestratorSupport.apply_plan method."""

    @pytest.fixture
    def support(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create an OrchestratorSupport instance for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_tech_lead = MagicMock()
        mock_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        mock_session_manager = MagicMock()
        mock_action_applier = MagicMock()
        mock_fact_gatherer = MagicMock()
        mock_planner = MagicMock()
        mock_worktree_manager = MagicMock()
        mock_state_machine_manager = MagicMock()
        mock_cleanup_manager = MagicMock()
        mock_cleanup_manager.should_retry_tech_lead_issue = Mock(return_value=True)

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=mock_session_manager,
            action_applier=mock_action_applier,
            fact_gatherer=mock_fact_gatherer,
            planner=mock_planner,
            worktree_manager=mock_worktree_manager,
            state_machine_manager=mock_state_machine_manager,
            cleanup_manager=mock_cleanup_manager,
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_apply_plan_launch_releases_blocked_front_by_issue_identity(self, support):
        """#6873 R5/N4/N5: blocked->front launch cleanup, driven through the PUBLIC
        ``apply_plan`` success path (not the private handler), keys on the launched
        session's CANONICAL issue identity and is scoped to ISSUE launches.

        A successful ISSUE launch of #7 releases its owned entry; a REVIEW launch
        for PR #9 (canonical reviewed issue #5) must NOT release the unrelated
        blocked->front-owned issue #9 — the PR/issue number collision (R5) and the
        issue-scoped invariant, proven at the OrchestratorSupport boundary.
        """
        from issue_orchestrator.control.actions import (
            ActionResult,
            LaunchSessionAction,
            SessionType,
        )
        from issue_orchestrator.control.planner_types import Plan
        from issue_orchestrator.control.retry_history_state import RetryHistoryState

        RetryHistoryState(support.state).prioritize_blocked_front(7)
        RetryHistoryState(support.state).prioritize_blocked_front(9)

        issue_launch = LaunchSessionAction(session_type=SessionType.ISSUE, number=7)
        review_launch = LaunchSessionAction(session_type=SessionType.REVIEW, number=9)

        def apply(action):
            # The wired ActionApplier returns the launched session's canonical
            # issue number; a REVIEW of PR #9 reviews issue #5 (identity != number).
            number = 5 if action.session_type is SessionType.REVIEW else action.number
            return ActionResult.ok(action, issue_number=number)

        support.action_applier.apply.side_effect = apply
        support.apply_plan(Plan(actions=(issue_launch, review_launch), skipped=()), MagicMock())

        assert 7 not in support.state.priority_queue
        assert 7 not in support.state.blocked_front_prioritized
        assert 9 in support.state.priority_queue
        assert 9 in support.state.blocked_front_prioritized

    def test_apply_plan_failed_launch_does_not_release_blocked_front(self, support):
        """#6873 N5: a FAILED launch never runs the success path, so blocked->front
        cleanup is not invoked — the owned entry survives. Proven through the real
        ``apply_plan`` failure routing, not by reasoning about it in isolation."""
        from issue_orchestrator.control.actions import (
            ActionResult,
            LaunchSessionAction,
            SessionType,
        )
        from issue_orchestrator.control.planner_types import Plan
        from issue_orchestrator.control.retry_history_state import RetryHistoryState

        RetryHistoryState(support.state).prioritize_blocked_front(7)
        issue_launch = LaunchSessionAction(session_type=SessionType.ISSUE, number=7)

        support.action_applier.apply.side_effect = lambda action: ActionResult.fail(
            action, "worktree create failed"
        )
        support.apply_plan(Plan(actions=(issue_launch,), skipped=()), MagicMock())

        assert 7 in support.state.priority_queue
        assert 7 in support.state.blocked_front_prioritized

    def test_empty_plan_does_nothing(self, support, mock_event_sink):
        """Empty plan (action_count=0) does not emit events or apply actions."""
        empty_plan = MagicMock()
        empty_plan.action_count = 0
        empty_plan.actions = []

        pause_callback = Mock()
        support.apply_plan(empty_plan, pause_callback)

        # No events should be published for empty plan
        assert mock_event_sink.publish.call_count == 0
        pause_callback.assert_not_called()

    def test_emits_apply_started_and_completed_events(self, support, mock_event_sink):
        """Applying a plan emits APPLY_STARTED and APPLY_COMPLETED events."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.ADD_LABEL

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.details = {}
        support.action_applier.apply = Mock(return_value=mock_result)

        support.apply_plan(plan, Mock())

        # Should have APPLY_STARTED and APPLY_COMPLETED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.APPLY_STARTED in event_names
        assert EventName.APPLY_COMPLETED in event_names

    def test_stops_applying_when_paused(self, support, sample_orchestrator_state):
        """Plan application stops when orchestrator becomes paused."""
        actions = [MagicMock(action_type=ActionType.ADD_LABEL) for _ in range(3)]
        plan = MagicMock()
        plan.action_count = 3
        plan.actions = actions

        # Pause after first action
        apply_count = 0
        def pause_on_second_apply(action):
            nonlocal apply_count
            apply_count += 1
            if apply_count == 1:
                sample_orchestrator_state.paused = True
            result = MagicMock()
            result.success = True
            result.details = {}
            return result

        support.action_applier.apply = Mock(side_effect=pause_on_second_apply)

        support.apply_plan(plan, Mock())

        # Should have stopped after 1 action (paused before second)
        assert apply_count == 1

    def test_reconciliation_required_pauses_issue(self, support, mock_event_sink):
        """ReconciliationRequired exception triggers issue pause callback."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.ADD_LABEL

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        # Simulate reconciliation failure
        support.action_applier.apply = Mock(
            side_effect=ReconciliationRequired(
                entity_type="issue",
                entity_id=42,
                expected=ExternalSnapshot.for_issue(42, {"in-progress"}),
                actual=ExternalSnapshot.for_issue(42, {"blocked"}),
                reason="Labels changed",
            )
        )

        pause_callback = Mock()
        support.apply_plan(plan, pause_callback)

        # Should have called pause callback
        pause_callback.assert_called_once_with(42, "Labels changed")

        # Should have emitted RECONCILIATION_REQUIRED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.RECONCILIATION_REQUIRED in event_names

    def test_tech_lead_issue_skipped_on_cooldown(self, support, mock_event_sink):
        """CREATE_TECH_LEAD_ISSUE action is skipped when on cooldown."""
        mock_action = MagicMock()
        mock_action.action_type = ActionType.CREATE_TECH_LEAD_ISSUE

        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [mock_action]

        # Set cooldown active
        support.cleanup_manager.should_retry_tech_lead_issue = Mock(return_value=False)

        support.apply_plan(plan, Mock())

        # Should not have called apply for this action
        support.action_applier.apply.assert_not_called()

        # Should have emitted APPLY_FAILED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.APPLY_FAILED in event_names

    def test_failed_label_action_marks_failed_this_cycle(self, support, sample_orchestrator_state):
        """Label mutation failures mark the issue failed_this_cycle."""
        action = AddLabelAction(issue_number=42, label="in-progress")
        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [action]

        support.action_applier.apply = Mock(
            return_value=ActionResult.fail(action, "boom")
        )

        support.apply_plan(plan, Mock())

        assert 42 in sample_orchestrator_state.failed_this_cycle

    def test_failed_launch_action_marks_failed_this_cycle(self, support, sample_orchestrator_state):
        """Launch failures must mark the issue failed_this_cycle for queue diagnostics."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=42,
            command="",
            working_dir="",
        )
        plan = MagicMock()
        plan.action_count = 1
        plan.actions = [action]

        support.action_applier.apply = Mock(
            return_value=ActionResult.fail(action, "worktree create failed")
        )

        support.apply_plan(plan, Mock())

        assert 42 in sample_orchestrator_state.failed_this_cycle


# =============================================================================
# Tests for OrchestratorSupport.clear_discovered_facts method
# =============================================================================


def _unpaused_tick(state):
    """The snapshot of a normal running tick — the input the clear keys off."""
    from issue_orchestrator.control.planner_types import OrchestratorSnapshot

    return OrchestratorSnapshot.from_state(issues=(), state=state)


class TestOrchestratorSupportClearDiscoveredFacts:
    """Tests for OrchestratorSupport.clear_discovered_facts method.

    This tests the instance method, not the module-level function.
    Critical for preventing infinite cleanup loops (bug fix regression test).
    """

    @pytest.fixture
    def support(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create an OrchestratorSupport instance for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_tech_lead = MagicMock()
        mock_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_clears_immediate_cleanups_via_method(self, support, sample_orchestrator_state):
        """OrchestratorSupport.clear_discovered_facts() clears immediate_cleanups.

        This is the method called by Orchestrator._clear_discovered_facts().
        Regression test for infinite cleanup loop bug.
        """
        # Populate immediate_cleanups
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(
                issue_number=1,
                terminal_id="issue-1",
                worktree_path="/tmp/wt-1",
                reason="completed"
            ),
        ]

        # Call the instance method (not the module-level function), passing
        # the tick's own snapshot — the read that decides retention.
        support.clear_discovered_facts(_unpaused_tick(sample_orchestrator_state))

        assert len(sample_orchestrator_state.immediate_cleanups) == 0

    def test_clears_all_discovered_lists_via_method(self, support, sample_orchestrator_state):
        """OrchestratorSupport.clear_discovered_facts() clears all discovery lists."""
        # Populate all discoverable state
        sample_orchestrator_state.discovered_reviews = [
            DiscoveredReview(issue_number=1, pr_number=100, pr_url="url", branch_name="br")
        ]
        sample_orchestrator_state.discovered_awaiting_merge_reconciliations = [
            DiscoveredAwaitingMergeReconciliation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status="closed",
                status_reason="PR closed; awaiting merge reconciled",
                source="pull_request",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_drifts = [
            DiscoveredAwaitingMergeDrift(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                status_reason="PR closed; issue remains open",
            )
        ]
        sample_orchestrator_state.discovered_awaiting_merge_escalations = [
            DiscoveredAwaitingMergeEscalation(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
                rework_cycle=1,
                kind="branch_protection_blocked",
                reason="Branch protection blocks merge.",
            )
        ]
        sample_orchestrator_state.discovered_merge_queue_enqueues = [
            DiscoveredMergeQueueEnqueue(
                issue_number=1,
                pr_number=100,
                pr_url="url",
                issue_key="M0-001",
            )
        ]
        sample_orchestrator_state.discovered_reworks = [
            DiscoveredRework(issue_number=2, pr_number=200, branch_name="br", agent_type="a", rework_cycle=1)
        ]
        sample_orchestrator_state.discovered_escalations = [
            DiscoveredEscalation(issue_number=3, pr_number=300, rework_cycle=3)
        ]
        sample_orchestrator_state.discovered_failures = [
            DiscoveredFailure(issue_number=4, issue_title="T", failure_reason="f")
        ]
        sample_orchestrator_state.immediate_cleanups = [
            ImmediateCleanup(issue_number=5, terminal_id="t", worktree_path="p", reason="r")
        ]

        support.clear_discovered_facts(_unpaused_tick(sample_orchestrator_state))

        assert len(sample_orchestrator_state.discovered_reviews) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_reconciliations) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_drifts) == 0
        assert len(sample_orchestrator_state.discovered_awaiting_merge_escalations) == 0
        assert len(sample_orchestrator_state.discovered_merge_queue_enqueues) == 0
        assert len(sample_orchestrator_state.discovered_reworks) == 0
        assert len(sample_orchestrator_state.discovered_escalations) == 0
        assert len(sample_orchestrator_state.discovered_failures) == 0
        assert len(sample_orchestrator_state.immediate_cleanups) == 0


# =============================================================================
# Tests for OrchestratorSupport._update_state_after_action
# =============================================================================


class TestUpdateStateAfterAction:
    """Tests for OrchestratorSupport._update_state_after_action method."""

    @pytest.fixture
    def support_with_state(
        self,
        sample_orchestrator_state,
        mock_event_sink,
        mock_repository_host,
        sample_event_context,
    ):
        """Create support with accessible state.

        A real in-memory tech_lead authority is wired because the storm collapse
        is bound to DURABLE cohort persistence (#6780): the composition
        root always supplies the store, and without one intake declines to
        retire the individual investigations.
        """
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()
        mock_config.cleanup.without_tech_lead = MagicMock()
        mock_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        mock_config.code_review_agent = None

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
            tech_lead_authority=InMemoryTechLeadAuthorityStore(),
        )

    @staticmethod
    def _apply_via_plan(support, actions, **details) -> None:
        """Drive the producer boundary through the public ``apply_plan`` seam.

        The action applier is faked at its port boundary (``apply`` returns a
        successful ``ActionResult`` carrying ``details``, the shape the real
        applier produces), so ``apply_plan`` runs the real success-handling
        path — including the post-apply state update that queues tech_lead work.
        This keeps coverage on the public planner/action-owner command path
        rather than reaching into the private ``_update_state_after_action``.
        """
        from issue_orchestrator.control.planner_types import Plan

        support.action_applier.apply.side_effect = lambda action: ActionResult.ok(
            action, **details
        )
        support.apply_plan(Plan(actions=tuple(actions), skipped=()), MagicMock())

    def test_queue_review_adds_to_pending_reviews(self, support_with_state, mock_repository_host):
        """QUEUE_REVIEW action adds PendingReview to state."""
        from issue_orchestrator.control.actions import QueueReviewAction

        action = QueueReviewAction(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
            issue_labels=("agent:web", "verbose"),
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_reviews
        assert len(support_with_state.state.pending_reviews) == 1
        review = support_with_state.state.pending_reviews[0]
        assert review.pr_number == 100
        assert review.branch_name == "issue-42"
        assert review.issue_labels == ("agent:web", "verbose")

    def test_queue_review_skips_duplicate(self, support_with_state, mock_repository_host):
        """QUEUE_REVIEW action skips if PR already in pending_reviews."""
        from issue_orchestrator.control.actions import QueueReviewAction

        # Pre-populate with existing review
        existing = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
            _issue_number=42,
        )
        support_with_state.state.pending_reviews.append(existing)

        action = QueueReviewAction(
            issue_number=42,
            pr_number=100,  # Same PR
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="issue-42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing duplicate detection behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should NOT have added duplicate
        assert len(support_with_state.state.pending_reviews) == 1

    def test_disposable_cleanup_pruned_on_success(self, support_with_state):
        """F8: a disposable scratch-worktree cleanup fact is dropped once its
        removal SUCCEEDS, so it is not needlessly re-planned every tick."""
        from issue_orchestrator.control.actions import CleanupSessionAction
        from issue_orchestrator.domain.models import ImmediateCleanup

        state = support_with_state.state
        state.immediate_cleanups.append(
            ImmediateCleanup(
                5980, "issue-5980", "/tmp/scratch", "completed", scratch_worktree=True
            )
        )
        action = CleanupSessionAction(
            issue_number=5980, pr_number=0, terminal_id="issue-5980",
            worktree_path="/tmp/scratch", remove_worktrees=True, disposable_worktree=True,
        )
        result = MagicMock(success=True, details={})

        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert 5980 not in {c.issue_number for c in state.immediate_cleanups}

    def test_disposable_cleanup_retained_on_failure(self, support_with_state):
        """F8 second guard: a FAILED disposable cleanup keeps its fact so the
        next tick re-plans the (forced) removal instead of leaking the worktree."""
        from issue_orchestrator.control.actions import CleanupSessionAction
        from issue_orchestrator.domain.models import ImmediateCleanup

        state = support_with_state.state
        state.immediate_cleanups.append(
            ImmediateCleanup(
                5980, "issue-5980", "/tmp/scratch", "completed", scratch_worktree=True
            )
        )
        action = CleanupSessionAction(
            issue_number=5980, pr_number=0, terminal_id="issue-5980",
            worktree_path="/tmp/scratch", remove_worktrees=True, disposable_worktree=True,
        )
        result = MagicMock(success=False, details={})

        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert 5980 in {c.issue_number for c in state.immediate_cleanups}

    def test_queue_retrospective_review_adds_to_pending_reviews(
        self,
        support_with_state,
    ):
        """QUEUE_RETROSPECTIVE_REVIEW action adds PendingRetrospectiveReview to state."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_retrospective_reviews) == 1
        review = support_with_state.state.pending_retrospective_reviews[0]
        assert review.issue_number == 42
        assert review.agent_label == "agent:web"
        assert review.trigger_label == "lack-of-review-redo"

    def test_queue_retrospective_review_skips_issue_already_in_flight(
        self,
        support_with_state,
    ):
        """QUEUE_RETROSPECTIVE_REVIEW uses the centralized in-flight guard."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        issue = Issue(number=42, title="Active review", labels=["agent:web"])
        support_with_state.state.active_sessions.append(
            Session(
                key=SessionKey(
                    issue=FakeIssueKey("42"),
                    task=TaskKind.RETROSPECTIVE_REVIEW,
                ),
                issue=issue,
                agent_config=AgentConfig(prompt_path=Path("/tmp/prompt.md")),
                terminal_id="retrospective-review-42",
                worktree_path=Path("/tmp/work42"),
                branch_name="issue-42",
                run_assets=make_session_run_assets(
                    Path("/tmp/work42"),
                    session_name="retrospective-review-42",
                ),
            )
        )
        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing duplicate detection behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert support_with_state.state.pending_retrospective_reviews == []

    def test_queue_retrospective_review_promotes_discovered_fact(
        self,
        support_with_state,
    ):
        """Discovered retrospective review facts are the source, not a duplicate."""
        from issue_orchestrator.control.actions import QueueRetrospectiveReviewAction

        support_with_state.state.discovered_retrospective_reviews.append(
            DiscoveredRetrospectiveReview(
                issue_number=42,
                issue_title="Review existing work",
                agent_label="agent:web",
                trigger_label="lack-of-review-redo",
                issue_key="42",
            )
        )
        action = QueueRetrospectiveReviewAction(
            issue_number=42,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="42",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing discovered fact to pending queue transition.
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_retrospective_reviews) == 1
        assert (
            support_with_state.state.pending_retrospective_reviews[0].issue_number
            == 42
        )

    def test_queue_rework_adds_to_pending_reworks(self, support_with_state, mock_repository_host):
        """QUEUE_REWORK action adds PendingRework to state."""
        from issue_orchestrator.control.actions import QueueReworkAction

        # Add discovered rework for agent type lookup
        support_with_state.state.discovered_reworks = [
            DiscoveredRework(
                issue_number=42,
                pr_number=100,
                branch_name="issue-42",
                agent_type="agent:developer",
                rework_cycle=2,
            )
        ]

        action = QueueReworkAction(
            issue_number=42,
            rework_cycle=2,
            source="post_publish_validation",
            feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\nResolve merge conflicts.",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_reworks
        assert len(support_with_state.state.pending_reworks) == 1
        rework = support_with_state.state.pending_reworks[0]
        assert rework.agent_type == "agent:developer"
        assert rework.rework_cycle == 2
        assert rework.source == "post_publish_validation"
        assert "POST-PUBLISH VALIDATION FAILURE" in (rework.feedback or "")

    def test_cleanup_session_removes_from_pending_cleanups(self, support_with_state):
        """CLEANUP_SESSION action removes cleanup from pending_cleanups."""
        from issue_orchestrator.control.actions import CleanupSessionAction

        # Pre-populate with cleanup
        mock_issue = MagicMock()
        mock_issue.number = 42
        cleanup = PendingCleanup(
            issue=mock_issue,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            terminal_id="session-42",
            worktree_path=Path("/tmp/worktree"),
        )
        support_with_state.state.pending_cleanups.append(cleanup)

        action = CleanupSessionAction(
            issue_number=42,
            pr_number=100,
            terminal_id="session-42",
            worktree_path="/tmp/worktree",
            close_tabs=True,
            remove_worktrees=True,
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have removed from pending_cleanups
        assert len(support_with_state.state.pending_cleanups) == 0

    def test_create_tech_lead_issue_adds_to_pending_tech_lead(self, support_with_state):
        """CREATE_TECH_LEAD_ISSUE action adds to pending_tech_lead_reviews."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction

        action = CreateTechLeadIssueAction(
            title="Tech Lead Batch Review",
            body="Review these PRs",
            labels=("agent:tech-lead",),
            pr_count=5,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )
        result = MagicMock(success=True, details={"issue_number": 999})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        # Should have added to pending_tech_lead_reviews as a batch review:
        # threshold-created tracking issues audit the PR manifest (#6768 B5).
        assert len(support_with_state.state.pending_tech_lead_reviews) == 1
        tech_lead = support_with_state.state.pending_tech_lead_reviews[0]
        assert tech_lead.issue_number == 999
        assert tech_lead.flavor is TechLeadSessionFlavor.BATCH_REVIEW

    def test_queue_tech_lead_adds_failure_investigation(self, support_with_state):
        """QUEUE_TECH_LEAD queues a failure investigation, not a batch review (#6768 B5)."""
        from issue_orchestrator.control.actions import QueueTechLeadAction
        from issue_orchestrator.domain.models import DiscoveredFailure

        failure = DiscoveredFailure(
            issue_number=42, issue_title="Test issue", failure_reason="failed"
        )
        action = QueueTechLeadAction(
            issue_number=42,
            title="Investigate: Test issue (failed)",
            failure=failure,
            reason="Session failed with status 'failed'",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_tech_lead_reviews) == 1
        tech_lead = support_with_state.state.pending_tech_lead_reviews[0]
        assert tech_lead.issue_number == 42
        assert tech_lead.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        # The typed failure context rides the queue item so the launch-time
        # board snapshot (a later tick) still contains the triggering failure.
        assert tech_lead.failure is failure

    def test_queue_tech_lead_without_failure_context_fails_fast(self, support_with_state):
        """A QUEUE_TECH_LEAD action lacking failure context is a producer bug.

        ``QueueTechLeadAction.failure`` is a required keyword field, so the
        contract is enforced at action CONSTRUCTION — an investigation cannot
        even be described without its triggering failure.
        """
        from issue_orchestrator.control.actions import QueueTechLeadAction

        with pytest.raises(TypeError, match="failure"):
            QueueTechLeadAction(
                issue_number=42,
                title="Investigate: Test issue (failed)",
                reason="Session failed with status 'failed'",
            )

        assert support_with_state.state.pending_tech_lead_reviews == []

    def test_create_tech_lead_issue_dedups_existing_queue_entry(self, support_with_state):
        """The create-success path must not double-queue an issue (#6768 round 3).

        Before the owner API, this producer appended without any dedup check.
        """
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.domain.models import PendingTechLeadReview

        existing = PendingTechLeadReview(
            issue_number=999,
            title="Tech Lead Batch Review",
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        )
        support_with_state.state.pending_tech_lead_reviews.append(existing)

        action = CreateTechLeadIssueAction(
            title="Tech Lead Batch Review",
            body="Review these PRs",
            labels=("agent:tech-lead",),
            pr_count=5,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )
        result = MagicMock(success=True, details={"issue_number": 999})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert support_with_state.state.pending_tech_lead_reviews == [existing]

    def test_batch_tech_lead_issue_does_not_stamp_health_review(self, support_with_state):
        """Unmarked batch creation must NOT touch last_health_review_at."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction

        store = MagicMock()
        support_with_state.queue_cache_store = store
        action = CreateTechLeadIssueAction(
            title="Tech Lead Batch Review",
            body="Review these PRs",
            labels=("agent:tech-lead",),
            pr_count=5,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        self._apply_via_plan(support_with_state, [action], issue_number=999)

        assert support_with_state.state.last_health_review_at == 0.0
        store.save_last_health_review_at.assert_not_called()
        # And the intake routed to the BATCH owner operation, not health.
        (tech_lead,) = support_with_state.state.pending_tech_lead_reviews
        assert tech_lead.flavor is TechLeadSessionFlavor.BATCH_REVIEW

    def test_marker_labeled_creation_stamps_and_persists_health_review(
        self, support_with_state
    ):
        """Marker-labeled creation queues a HEALTH_REVIEW and stamps state AND
        the durable store (ADR-0031 §4)."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        store = MagicMock()
        support_with_state.queue_cache_store = store
        action = CreateTechLeadIssueAction(
            title="Health Review — walk the floor",
            body="Walk the floor",
            labels=("agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL),
            pr_count=0,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        before = time.time()
        self._apply_via_plan(support_with_state, [action], issue_number=1000)

        stamped = support_with_state.state.last_health_review_at
        assert stamped >= before
        store.save_last_health_review_at.assert_called_once_with(stamped)
        # The anchor enters the pending-tech-lead launch queue with the variant
        # the marker label declares (typed intake, #6768 round 3).
        (tech_lead,) = support_with_state.state.pending_tech_lead_reviews
        assert tech_lead.issue_number == 1000
        assert tech_lead.flavor is TechLeadSessionFlavor.HEALTH_REVIEW

    def test_storm_health_creation_replaces_member_investigations(
        self, support_with_state
    ):
        """Successful anchor intake atomically leaves one health review and
        zero per-member failure investigations (#6780)."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        cohort = tuple(
            DiscoveredFailure(number, f"Problem {number}", "failed")
            for number in (41, 42, 43)
        )
        queues = PendingSessionQueues(support_with_state.state)
        for failure in cohort:
            queues.queue_failure_investigation(
                failure.issue_number,
                f"Investigate {failure.issue_number}",
                failure=failure,
            )
        action = CreateTechLeadIssueAction(
            title="Health Review — walk the floor",
            body="Problem storm",
            labels=("agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL),
            pr_count=0,
            storm_problems=cohort,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        self._apply_via_plan(support_with_state, [action], issue_number=1000)

        [queued] = support_with_state.state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert queued.problem_cohort == cohort

    @staticmethod
    def _storm_config():
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo", max_concurrent_sessions=3)
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_on_failure = True
        # Isolate the storm path from the periodic health-review interval.
        config.tech_lead.health_review.interval_minutes = 0
        config.tech_lead.health_review.storm_threshold = 3
        config.tech_lead.health_review.storm_window_minutes = 5
        return config

    def _plan_storm_tick(self, support, config):
        """Record a 3-problem storm cohort and return the real planner's plan."""
        from issue_orchestrator.control.planner import Planner
        from issue_orchestrator.control.planner_types import OrchestratorSnapshot
        from issue_orchestrator.control.scheduler import Scheduler
        from issue_orchestrator.control.workflows import TechLeadWorkflow
        from issue_orchestrator.ports.event_sink import InMemoryEventSink

        for number in (41, 42, 43):
            support.state.record_discovered_failure(
                DiscoveredFailure(
                    number, f"Problem {number}", "failed", observed_at=1_000.0
                )
            )
        planner = Planner(
            config=config,
            scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=InMemoryEventSink()),
            clock=lambda: 1_100.0,
        )
        snapshot = OrchestratorSnapshot.from_state(
            issues=(),
            state=support.state,
            discovered_failures=tuple(support.state.discovered_failures),
        )
        return planner.plan(snapshot)

    @staticmethod
    def _tick_snapshot(support):
        """The snapshot THIS tick planned from — including its paused read."""
        from issue_orchestrator.control.planner_types import OrchestratorSnapshot

        return OrchestratorSnapshot.from_state(
            issues=(),
            state=support.state,
            discovered_failures=tuple(support.state.discovered_failures),
        )

    @staticmethod
    def _queued_investigation_numbers(support) -> list[int]:
        return sorted(
            t.issue_number
            for t in support.state.pending_tech_lead_reviews
            if t.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        )

    def test_escalated_storm_collapses_investigations_into_anchor_cohort(
        self, support_with_state
    ):
        """End-to-end (#6780) happy path: the cohort is queued FIRST, then
        the created anchor's intake collapses it — leaving exactly one pending
        health review carrying ``problem_cohort`` and zero leftover individual
        investigations. Persist-first must not double-book the work.
        """
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        config = self._storm_config()
        plan = self._plan_storm_tick(support_with_state, config)

        self._apply_via_plan(support_with_state, list(plan.actions), issue_number=999)
        clear_discovered_facts(support_with_state.state, config, tick_paused=False)

        assert self._queued_investigation_numbers(support_with_state) == []
        (queued,) = support_with_state.state.pending_tech_lead_reviews
        assert queued.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert sorted(p.issue_number for p in queued.problem_cohort) == [41, 42, 43]

    def test_storm_investigations_survive_a_failed_anchor_create(
        self, support_with_state
    ):
        """End-to-end (#6780): the anchor create FAILS at apply time (a
        GitHub outage/rate-limit/permission error), so no intake runs and no
        cohort is persisted. The queued investigations must therefore remain —
        binding suppression to a create the planner merely *planned* would drop
        the cohort at the unconditional end-of-tick clear.
        """
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts
        from issue_orchestrator.control.planner_types import Plan

        config = self._storm_config()
        plan = self._plan_storm_tick(support_with_state, config)

        # Only the GitHub create fails; queue operations are in-memory no-ops
        # in the real applier and always succeed.
        support_with_state.action_applier.apply.side_effect = lambda action: (
            ActionResult.fail(action, error="GitHub 403")
            if action.action_type == ActionType.CREATE_TECH_LEAD_ISSUE
            else ActionResult.ok(action)
        )
        support_with_state.apply_plan(
            Plan(actions=tuple(plan.actions), skipped=()), MagicMock()
        )
        clear_discovered_facts(support_with_state.state, config, tick_paused=False)

        assert support_with_state.state.discovered_failures == []
        assert self._queued_investigation_numbers(support_with_state) == [41, 42, 43]

    def test_paused_storm_retains_cohort_facts_across_the_tick(
        self, support_with_state
    ):
        """End-to-end (#6780): a paused tick plans nothing and applies
        nothing (``apply_plan`` breaks on paused), so the discovered facts must
        be RETAINED rather than cleared. A session that fails while paused is
        discovered exactly once — clearing here would lose the cohort forever,
        even after resume.
        """
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        config = self._storm_config()
        support_with_state.state.paused = True
        plan = self._plan_storm_tick(support_with_state, config)

        assert plan.actions == ()
        self._apply_via_plan(support_with_state, list(plan.actions))
        clear_discovered_facts(support_with_state.state, config, tick_paused=True)

        assert [
            f.issue_number for f in support_with_state.state.discovered_failures
        ] == [41, 42, 43]

    def test_retention_follows_the_tick_when_operator_resumes_mid_tick(
        self, support_with_state
    ):
        """The operator RESUMES between this tick's snapshot and its clear.

        The tick planned from ``paused=True`` — an empty plan that consumed
        nothing — so its facts must survive, even though ``state.paused`` now
        reads False. ``state.paused`` is mutated from the web thread and the
        clear is separated from the snapshot by a fetch, planning and apply, so
        a fresh read here decides retention from a DIFFERENT tick's pause state
        and wipes a cohort nothing recorded.
        """
        config = self._storm_config()
        support_with_state.state.paused = True
        plan = self._plan_storm_tick(support_with_state, config)
        tick = self._tick_snapshot(support_with_state)
        assert plan.actions == ()
        assert tick.paused is True

        support_with_state.state.paused = False

        support_with_state.clear_discovered_facts(tick)

        assert [
            f.issue_number for f in support_with_state.state.discovered_failures
        ] == [41, 42, 43]

    def test_retention_follows_the_tick_when_operator_pauses_mid_apply(
        self, support_with_state
    ):
        """The operator PAUSES after the anchor create already applied.

        The tick planned and applied unpaused, and intake has already collapsed
        the cohort into the anchor — the facts ARE consumed and must be
        cleared. Retaining them (what a fresh ``state.paused`` read does) leaves
        every member in ``discovered_failures`` with its investigation already
        retired, so the next tick re-queues each one individually while the
        anchor still owns it in its cohort: the exact double-work the
        escalation exists to prevent.
        """
        config = self._storm_config()
        plan = self._plan_storm_tick(support_with_state, config)
        tick = self._tick_snapshot(support_with_state)
        assert tick.paused is False
        self._apply_via_plan(support_with_state, list(plan.actions), issue_number=999)

        support_with_state.state.paused = True

        support_with_state.clear_discovered_facts(tick)

        assert support_with_state.state.discovered_failures == []
        assert self._queued_investigation_numbers(support_with_state) == []

    def test_deferred_storm_investigations_survive_apply_and_fact_clear(
        self, support_with_state
    ):
        """End-to-end (#6780): when a storm cannot escalate (here an
        already-pending health review blocks a new anchor), the queued
        investigations survive the end-of-tick discovered-fact clear — the
        cohort is NOT lost.

        Drives the real planner -> apply -> clear chain so the persist-first
        decision and the durable queue outcome are exercised together, not just
        asserted on the in-memory plan.
        """
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        config = self._storm_config()
        # An already-pending health review defers a new storm anchor.
        support_with_state.state.pending_tech_lead_reviews.append(
            PendingTechLeadReview(
                issue_number=999,
                title="Health Review",
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            )
        )
        plan = self._plan_storm_tick(support_with_state, config)

        self._apply_via_plan(support_with_state, list(plan.actions))
        # The end-of-tick fact clear runs unconditionally; the fallback
        # investigations must have migrated to the durable pending queue.
        clear_discovered_facts(support_with_state.state, config, tick_paused=False)

        assert support_with_state.state.discovered_failures == []
        assert self._queued_investigation_numbers(support_with_state) == [41, 42, 43]

    def test_storm_collapse_declines_when_the_cohort_cannot_be_persisted(
        self, support_with_state, caplog
    ):
        """End-to-end (#6780): the durable cohort write FAILS at intake.

        Collapsing retires the per-issue investigations, so it may only happen
        once the cohort is somewhere that outlives this process. When the
        ledger write fails the anchor still exists on GitHub — it cannot be
        reported as an apply failure — so the collapse simply does not happen:
        the individual investigations stay queued and the problems are still
        worked, degraded rather than dropped, and loudly.
        """
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        config = self._storm_config()
        authority = MagicMock()
        authority.record_storm_cohort.side_effect = RuntimeError("disk full")
        authority.list_storm_cohorts.return_value = ()
        support_with_state.tech_lead_authority = authority
        plan = self._plan_storm_tick(support_with_state, config)

        with caplog.at_level("ERROR"):
            self._apply_via_plan(
                support_with_state, list(plan.actions), issue_number=999
            )
        clear_discovered_facts(
            support_with_state.state, config, authority, tick_paused=False
        )

        assert self._queued_investigation_numbers(support_with_state) == [41, 42, 43]
        (anchor,) = [
            t
            for t in support_with_state.state.pending_tech_lead_reviews
            if t.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        ]
        assert anchor.problem_cohort == (), (
            "an anchor that could not persist its cohort must not claim one"
        )
        assert "storm cohort" in caplog.text

    def test_marker_labeled_creation_survives_store_persist_failure(
        self, support_with_state, caplog
    ):
        """Persistence failure warns but keeps the in-memory stamp and queue entry."""
        from issue_orchestrator.control.actions import CreateTechLeadIssueAction
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        store = MagicMock()
        store.save_last_health_review_at.side_effect = RuntimeError("disk full")
        support_with_state.queue_cache_store = store
        action = CreateTechLeadIssueAction(
            title="Health Review — walk the floor",
            body="Walk the floor",
            labels=(HEALTH_REVIEW_MARKER_LABEL,),
            pr_count=0,
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

        with caplog.at_level("WARNING"):
            self._apply_via_plan(support_with_state, [action], issue_number=1001)

        assert support_with_state.state.last_health_review_at > 0.0
        assert len(support_with_state.state.pending_tech_lead_reviews) == 1
        assert "last_health_review_at" in caplog.text

    def test_queue_tech_lead_dedups_existing_queue_entry(self, support_with_state):
        """Repeated QUEUE_TECH_LEAD for the same issue stays a single queue entry."""
        from issue_orchestrator.control.actions import QueueTechLeadAction
        from issue_orchestrator.domain.models import DiscoveredFailure

        action = QueueTechLeadAction(
            issue_number=42,
            title="Investigate: Test issue (failed)",
            failure=DiscoveredFailure(
                issue_number=42, issue_title="Test issue", failure_reason="failed"
            ),
            reason="Session failed with status 'failed'",
        )
        result = MagicMock(success=True, details={})

        # noqa: SLF001 - Testing state mutation behavior of private method
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001
        support_with_state._update_state_after_action(action, result)  # noqa: SLF001

        assert len(support_with_state.state.pending_tech_lead_reviews) == 1


# =============================================================================
# Tests for run_tick
# =============================================================================


class TestRunTick:
    """Tests for run_tick function."""

    def test_increments_loop_iteration(self, sample_orchestrator_state, mock_event_sink, sample_event_context):
        """run_tick increments loop iteration counter."""
        inflight = {}

        new_iteration, should_continue = run_tick(
            loop_iteration=5,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert new_iteration == 6

    def test_returns_false_when_shutdown_requested(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick returns should_continue=False when shutdown requested."""
        inflight = {}

        _, should_continue = run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=True,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert should_continue is False

    def test_emits_tick_started_and_completed_events(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick emits TICK_STARTED and TICK_COMPLETED events."""
        inflight = {}

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.TICK_STARTED in event_names
        assert EventName.TICK_COMPLETED in event_names

    def test_emits_tick_slow_with_phase_breakdown_when_tick_overruns(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context, monkeypatch
    ):
        """A tick that overruns the heartbeat budget emits a machine TICK_SLOW
        event carrying the sub-phase breakdown (so the UI can attribute the
        stall instead of only inferring it from heartbeat age)."""
        import types

        clock = {"mono": 1000.0}
        fake_time = types.SimpleNamespace(
            monotonic=lambda: clock["mono"],
            time=lambda: 1_700_000_000.0,
        )
        monkeypatch.setattr(
            "issue_orchestrator.control.orchestrator_support.time", fake_time
        )

        def slow_active() -> None:
            clock["mono"] += 153.9  # the synchronous publish that froze the tick

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=slow_active,
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        slow_events = [e for e in mock_event_sink.events if e.name == EventName.TICK_SLOW]
        assert len(slow_events) == 1
        payload = slow_events[0].data
        assert payload["duration_seconds"] == 153.9
        assert payload["active_seconds"] == 153.9
        assert payload["dominant_phase"] == "active_sessions"

    def test_no_tick_slow_event_for_fast_tick(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """A normal (fast) tick must not emit TICK_SLOW."""
        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert not [e for e in mock_event_sink.events if e.name == EventName.TICK_SLOW]

    def test_skips_planning_when_global_health_check_fails(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick skips planning for a system-wide health blocker."""
        inflight = {}
        planning_fn = Mock()

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.blocked("rate_limit_low")),
            run_planning_cycle_fn=planning_fn,
            emit_heartbeat_fn=Mock(),
        )

        # Planning should NOT have been called
        planning_fn.assert_not_called()

        # Should emit PLAN_NOOP event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PLAN_NOOP in event_names

    def test_worker_saturation_does_not_skip_planning(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """Capacity is planner-owned, so an active worker cannot suppress the
        planning cycle that may launch a reserved tech lead."""
        sample_orchestrator_state.active_sessions.append(MagicMock())
        planning_fn = Mock()
        health_gate = HealthGate()

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=lambda: check_health(
                health_gate=health_gate,
                paused=sample_orchestrator_state.paused,
            ),
            run_planning_cycle_fn=planning_fn,
            emit_heartbeat_fn=Mock(),
        )

        planning_fn.assert_called_once_with()
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PLAN_NOOP not in event_names

    def test_manual_refresh_runs_while_paused(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick allows a pending queue refresh while keeping paused planning safe."""
        inflight = {}
        sample_orchestrator_state.paused = True
        sample_orchestrator_state.queue_refresh_requested = True
        planning_fn = Mock()

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.blocked("paused")),
            run_planning_cycle_fn=planning_fn,
            emit_heartbeat_fn=Mock(),
        )

        planning_fn.assert_called_once()
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PLAN_NOOP not in event_names

    def test_prunes_expired_inflight_ids(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        """run_tick removes expired inflight stable IDs."""
        now = time.monotonic()
        inflight = {
            "expired-id": now - 10,  # Already expired
            "valid-id": now + 100,   # Still valid
        }

        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids=inflight,
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        # Expired ID should be removed
        assert "expired-id" not in inflight
        # Valid ID should remain
        assert "valid-id" in inflight


# =============================================================================
# Tests for OrchestratorSupport.request_refresh
# =============================================================================


class TestRequestRefresh:
    """Tests for OrchestratorSupport.request_refresh method."""

    @pytest.fixture
    def support(self, sample_orchestrator_state, mock_event_sink, mock_repository_host, sample_event_context):
        """Create a minimal OrchestratorSupport for testing."""
        mock_config = MagicMock()
        mock_config.cleanup = MagicMock()

        return OrchestratorSupport(
            config=mock_config,
            events=mock_event_sink,
            repository_host=mock_repository_host,
            state=sample_orchestrator_state,
            event_context=sample_event_context,
            session_manager=MagicMock(),
            action_applier=MagicMock(),
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=Mock(),
            kill_session=Mock(),
        )

    def test_adds_inflight_ids_with_expiry(self, support):
        """request_refresh adds inflight IDs with expiry timestamp."""
        inflight_dict = {}
        ttl = 60.0

        support.request_refresh(
            inflight_stable_ids={"id-1", "id-2"},
            inflight_dict=inflight_dict,
            ttl=ttl,
        )

        # Both IDs should be in dict
        assert "id-1" in inflight_dict
        assert "id-2" in inflight_dict

        # Expiry should be in the future
        now = time.monotonic()
        assert inflight_dict["id-1"] > now
        assert inflight_dict["id-2"] > now

    def test_handles_empty_inflight_ids(self, support, caplog):
        """request_refresh handles None/empty inflight IDs gracefully."""
        import logging
        caplog.set_level(logging.INFO)

        inflight_dict = {}

        support.request_refresh(
            inflight_stable_ids=None,
            inflight_dict=inflight_dict,
            ttl=60.0,
        )

        # Dict should remain empty
        assert len(inflight_dict) == 0
        # Should log the refresh
        assert "[REFRESH]" in caplog.text


# =============================================================================
# Tests for _track_stale_ticks
# =============================================================================


class TestTrackStaleTicks:
    """Tests for _track_stale_ticks function."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock Config with stale_escalation_ticks."""
        config = MagicMock()
        config.stale_escalation_ticks = 3  # Default threshold
        return config

    def test_increments_tick_count_for_stale_issues(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """_track_stale_ticks increments counter for stale issues."""
        stale_issues = [make_issue(42), make_issue(43)]

        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=stale_issues,
        )

        # Both issues should have tick count of 1
        assert sample_orchestrator_state.stale_issue_ticks[42] == 1
        assert sample_orchestrator_state.stale_issue_ticks[43] == 1

    def test_accumulates_ticks_over_multiple_calls(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """_track_stale_ticks accumulates tick counts across calls."""
        stale_issues = [make_issue(42)]

        # Call three times
        for _ in range(3):
            _track_stale_ticks(
                config=mock_config,
                events=mock_event_sink,
                event_context=sample_event_context,
                state=sample_orchestrator_state,
                stale_issues=stale_issues,
            )

        # Tick count should be 3
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3

    def test_emits_cleared_event_when_issue_leaves_stale_set(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """STALE_IN_PROGRESS_CLEARED is emitted when issue is no longer stale."""
        # Set up: issue 42 was stale for 2 ticks
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # Now call with empty stale list (issue 42 is no longer stale)
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[],  # Issue 42 is no longer stale
        )

        # Issue should be removed from tracking
        assert 42 not in sample_orchestrator_state.stale_issue_ticks

        # Should have emitted STALE_IN_PROGRESS_CLEARED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.STALE_IN_PROGRESS_CLEARED in event_names

        # Verify event payload
        cleared_event = next(
            e for e in mock_event_sink.events
            if e.name == EventName.STALE_IN_PROGRESS_CLEARED
        )
        assert cleared_event.data["issue_number"] == 42

    def test_emits_persistent_stale_event_when_threshold_exceeded(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """PERSISTENT_STALE_DETECTED is emitted when threshold is exceeded."""
        mock_config.stale_escalation_ticks = 3

        # Pre-populate: issue has been stale for 2 ticks already
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # This call will increment to 3, hitting the threshold
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Tick count should now be 3
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3

        # Should have emitted PERSISTENT_STALE_DETECTED event
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED in event_names

        # Verify event payload
        persistent_event = next(
            e for e in mock_event_sink.events
            if e.name == EventName.PERSISTENT_STALE_DETECTED
        )
        assert persistent_event.data["issue_number"] == 42
        assert persistent_event.data["consecutive_ticks"] == 3
        assert persistent_event.data["threshold"] == 3

    def test_no_persistent_event_when_threshold_disabled(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """No PERSISTENT_STALE_DETECTED when stale_escalation_ticks is 0."""
        mock_config.stale_escalation_ticks = 0  # Disabled

        # Pre-populate: issue has been stale for many ticks
        sample_orchestrator_state.stale_issue_ticks[42] = 10

        # Call again
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Should NOT emit PERSISTENT_STALE_DETECTED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED not in event_names

    def test_no_persistent_event_below_threshold(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """No PERSISTENT_STALE_DETECTED when below threshold."""
        mock_config.stale_escalation_ticks = 5

        # Only 2 ticks so far
        sample_orchestrator_state.stale_issue_ticks[42] = 2

        # Call again (now 3 ticks, but threshold is 5)
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Should NOT emit PERSISTENT_STALE_DETECTED
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.PERSISTENT_STALE_DETECTED not in event_names

    def test_clears_multiple_issues_simultaneously(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """Multiple issues can be cleared in one call."""
        # Set up: multiple issues were stale
        sample_orchestrator_state.stale_issue_ticks[42] = 2
        sample_orchestrator_state.stale_issue_ticks[43] = 3
        sample_orchestrator_state.stale_issue_ticks[44] = 1

        # Now none are stale
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[],
        )

        # All should be cleared
        assert len(sample_orchestrator_state.stale_issue_ticks) == 0

        # Should have 3 STALE_IN_PROGRESS_CLEARED events
        cleared_events = [
            e for e in mock_event_sink.events
            if e.name == EventName.STALE_IN_PROGRESS_CLEARED
        ]
        assert len(cleared_events) == 3

        # All issue numbers should be represented
        cleared_issue_nums = {e.data["issue_number"] for e in cleared_events}
        assert cleared_issue_nums == {42, 43, 44}

    def test_mixed_scenario_some_cleared_some_escalated(
        self, mock_config, mock_event_sink, sample_event_context, sample_orchestrator_state
    ):
        """Test mixed scenario: some issues cleared, one escalated."""
        mock_config.stale_escalation_ticks = 3

        # Set up: issue 42 is about to hit threshold, issue 43 will be cleared
        sample_orchestrator_state.stale_issue_ticks[42] = 2
        sample_orchestrator_state.stale_issue_ticks[43] = 5  # Will be cleared

        # Issue 42 still stale, issue 43 is not
        _track_stale_ticks(
            config=mock_config,
            events=mock_event_sink,
            event_context=sample_event_context,
            state=sample_orchestrator_state,
            stale_issues=[make_issue(42)],
        )

        # Issue 42 should be at 3 ticks
        assert sample_orchestrator_state.stale_issue_ticks[42] == 3
        # Issue 43 should be cleared
        assert 43 not in sample_orchestrator_state.stale_issue_ticks

        # Should have both events
        event_names = [e.name for e in mock_event_sink.events]
        assert EventName.STALE_IN_PROGRESS_CLEARED in event_names


def test_record_issue_refreshes_updates_ui_freshness_map():
    state = OrchestratorState()

    _record_issue_refreshes(
        state=state,
        refreshed_numbers={4057, 4058},
        refreshed_at=1234.5,
    )

    assert state.issue_refresh_timestamps == {4057: 1234.5, 4058: 1234.5}
    assert state.issue_last_refreshed_at == {4057: 1234.5, 4058: 1234.5}

class TestRunTickHeartbeat:
    '''Heartbeat fields populated by run_tick must reflect tick lifecycle.'''

    def test_successful_tick_updates_completion_timestamp(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        run_tick(
            loop_iteration=1,
            event_context=sample_event_context,
            inflight_stable_ids={},
            state=sample_orchestrator_state,
            events=mock_event_sink,
            shutdown_requested=False,
            process_active_sessions_fn=Mock(),
            check_health_fn=Mock(return_value=HealthDecision.ok()),
            run_planning_cycle_fn=Mock(),
            emit_heartbeat_fn=Mock(),
        )

        assert sample_orchestrator_state.last_tick_started_at > 0
        assert sample_orchestrator_state.last_tick_completed_at > 0
        assert (
            sample_orchestrator_state.last_tick_completed_at
            >= sample_orchestrator_state.last_tick_started_at
        )
        # Phase cleared back to empty after a successful tick.
        assert sample_orchestrator_state.current_tick_phase == ''

    def test_tick_exception_leaves_phase_set_so_stale_reason_diagnoses_stall(
        self, sample_orchestrator_state, mock_event_sink, sample_event_context
    ):
        '''A raising phase must leave last_tick_completed_at stale AND expose the
        phase on OrchestratorState. The dashboard turns that combination into
        the actionable 'Orchestrator tick stalled' banner — if we wrote a
        completion timestamp in a finally: block we would hide the outage
        from the UI.
        '''
        def boom() -> None:
            raise RuntimeError('active sessions exploded')

        sample_orchestrator_state.last_tick_completed_at = 0.0

        with pytest.raises(RuntimeError, match='active sessions exploded'):
            run_tick(
                loop_iteration=1,
                event_context=sample_event_context,
                inflight_stable_ids={},
                state=sample_orchestrator_state,
                events=mock_event_sink,
                shutdown_requested=False,
                process_active_sessions_fn=boom,
                check_health_fn=Mock(return_value=HealthDecision.ok()),
                run_planning_cycle_fn=Mock(),
                emit_heartbeat_fn=Mock(),
            )

        # Started was written before boom ran; phase is still the one that
        # raised. last_tick_completed_at was never updated, so the dashboard
        # age-based stall check will fire on the next refresh.
        assert sample_orchestrator_state.last_tick_started_at > 0
        assert sample_orchestrator_state.last_tick_completed_at == 0.0
        assert sample_orchestrator_state.current_tick_phase == 'active_sessions'
