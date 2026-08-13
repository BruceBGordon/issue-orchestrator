"""Test the complete flow of reviews being queued and launched after coding sessions.

This test verifies the end-to-end behavior:
1. Coding session completes with a PR
2. should_queue_review returns True
3. discovered_reviews is populated
4. Planner generates QueueReviewAction and AddLabelAction
5. Review is added to pending_reviews
6. Planner generates LaunchSessionAction for review
7. Review session is launched

This is a critical path - if any step fails, reviews won't run after coding.
The bug discovered on 2026-01-07 where sessions got blocked-failed was caused by
missing session_exists_by_name hook in the terminal plugin, which made sessions appear
terminated immediately (before completing). This test ensures the happy path works.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    OrchestratorState,
    SessionHistoryEntry,
    AgentConfig,
    DiscoveredReview,
    PendingReview,
)
from issue_orchestrator.control.actions import (
    QueueReviewAction,
    AddLabelAction,
    LaunchSessionAction,
    SessionType,
)
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.completion_handler import (
    CleanupDecision,
    CompletionHandler,
    SessionStatus,
)
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.control.workflows.review_workflow import ReviewDecision
from issue_orchestrator.ports import NullEventSink
from tests.conftest import make_provider_availability
from tests.unit.session_run_helpers import make_session_run_assets


def make_config(**kwargs) -> Config:
    """Create a test config with sensible defaults."""
    defaults = {
        "repo": "test/repo",
        "max_concurrent_sessions": 3,
    }
    defaults.update(kwargs)
    return Config(**defaults)


def make_repository_host(prs):
    return MagicMock(
        get_prs_for_branch=lambda _branch: prs,
        get_pr=lambda _pr_number: None,
        set_pr_draft=MagicMock(),
    )


def make_completion_handler(config: Config, repository_host) -> CompletionHandler:
    session_output = MagicMock(spec=SessionOutput)
    session_output.find_run_dir.return_value = None
    from issue_orchestrator.ports.tech_lead_authority import (
        InMemoryTechLeadAuthorityStore,
    )
    from issue_orchestrator.ports.open_issue_corpus_store import (
        InMemoryOpenIssueCorpusStore,
    )
    from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager

    return CompletionHandler(
        config=config,
        events=NullEventSink(),
        repository_host=repository_host,
        get_issue_machine_fn=lambda _issue: None,
        get_session_machine_fn=lambda _terminal_id: None,
        get_review_machine_fn=lambda _pr_number: None,
        session_output=session_output,
        tech_lead_authority=InMemoryTechLeadAuthorityStore(),
        open_issue_corpus=OpenIssueCorpusManager(
            repository_host,
            InMemoryOpenIssueCorpusStore(),
            is_enabled=lambda: config.tech_lead.dedup.enabled,
        ),
        active_session_run_id=lambda _n: None,
        provider_availability=make_provider_availability(config),
    )


@pytest.fixture
def sample_issue():
    """Create a sample issue."""
    return Issue(number=123, title="Test Issue", labels=["agent:test"])


@pytest.fixture
def sample_agent_config():
    """Create a sample agent config."""
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.md"),
        model="sonnet",
        timeout_minutes=60,
        skip_review=False,
    )


@pytest.fixture
def sample_session(sample_issue, sample_agent_config, tmp_path):
    """Create a sample coding session."""
    issue_key = FakeIssueKey("123")
    return Session(
        key=SessionKey(issue=issue_key, task=TaskKind.CODE),
        issue=sample_issue,
        agent_config=sample_agent_config,
        terminal_id="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="123-feature",
        run_assets=make_session_run_assets(
            tmp_path / "worktree",
            session_name="issue-123",
        ),
    )


class TestReviewAfterCodingFlow:
    """Test the complete flow from coding completion to review launch.

    This is the critical path that ensures reviews actually run after coding.
    """

    def test_handle_session_completion_populates_discovered_reviews(
        self, sample_session
    ):
        """Session completion with PR populates discovered_reviews.

        This is the bridge between session completion and planner - if
        discovered_reviews is not populated, the planner won't know to
        queue a review.
        """
        config = MagicMock()
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_tech_lead.close_ai_session_tabs = True

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:test",
                status="completed",
                runtime_minutes=10,
                pr_url="https://github.com/test/repo/pull/456",
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=True,
            pr_url="https://github.com/test/repo/pull/456",
            pr_number=456,
        )

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 1, (
            "discovered_reviews must be populated when should_queue_review=True"
        )
        assert state.discovered_reviews[0].pr_number == 456
        assert state.discovered_reviews[0].issue_number == 123

    def test_auto_mode_fallback_queues_review(
        self, sample_session
    ):
        """Auto mode should still queue review when exchange doesn't run."""
        config = make_config(code_review_agent="agent:reviewer")
        config.review_exchange_mode = "auto"

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        repository_host = make_repository_host(
            prs=[MagicMock(url="https://github.com/test/repo/pull/456", number=456, labels=[])]
        )
        completion_handler = make_completion_handler(config, repository_host)

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            review_exchange_completed=False,
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 1
        planner = Planner(config=config, scheduler=Scheduler(config))
        snapshot = OrchestratorSnapshot(
            issues=(),
            active_sessions=(),
            pending_reviews=(),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
            discovered_reviews=tuple(state.discovered_reviews),
        )
        plan = planner.plan(snapshot)
        assert any(isinstance(a, QueueReviewAction) for a in plan.actions)

    def test_exchange_completed_skips_review_queue(
        self, sample_session
    ):
        """Exchange-completed PRs should not enqueue review actions."""
        config = make_config(code_review_agent="agent:reviewer")
        config.review_exchange_mode = "via-mcp"

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        repository_host = make_repository_host(
            prs=[MagicMock(url="https://github.com/test/repo/pull/456", number=456, labels=[])]
        )
        completion_handler = make_completion_handler(config, repository_host)

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            review_exchange_completed=True,
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 0
    def test_planner_generates_queue_review_action_from_discovered_reviews(self):
        """Planner produces QueueReviewAction from discovered_reviews.

        This verifies the planner correctly processes discovered_reviews
        and produces the action needed to queue the review.
        """
        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=123,
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
        )

        snapshot = OrchestratorSnapshot(
            issues=(),
            active_sessions=(),
            pending_reviews=(),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
            discovered_reviews=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Should have QueueReviewAction
        queue_actions = [a for a in plan.actions if isinstance(a, QueueReviewAction)]
        assert len(queue_actions) == 1, (
            "Planner must generate QueueReviewAction for discovered reviews"
        )
        assert queue_actions[0].pr_number == 456

        # Should also have AddLabelAction for pr-pending
        label_actions = [a for a in plan.actions if isinstance(a, AddLabelAction)]
        pr_pending_actions = [a for a in label_actions if a.label == "pr-pending"]
        assert len(pr_pending_actions) == 1, (
            "Planner must add pr-pending label for discovered reviews"
        )

    def test_planner_launches_review_when_pending(self):
        """Planner generates LaunchSessionAction for pending reviews.

        After QueueReviewAction is applied, the review moves to pending_reviews.
        On the next planning cycle, the planner should launch the review session.
        """
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=3)
        scheduler = Scheduler(config)

        pending = PendingReview(
            issue_key=FakeIssueKey("123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        # Create a mock review workflow that allows launching
        mock_review_workflow = MagicMock()
        mock_review_workflow.is_configured.return_value = True
        mock_review_workflow.should_launch_reviews.return_value = ReviewDecision(
            should_launch=True,
            items_to_launch=(pending,),
            skip_reason=None,
            available_capacity=3,
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = OrchestratorSnapshot(
            issues=(),
            active_sessions=(),  # No active sessions (capacity available)
            pending_reviews=(pending,),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
        )

        plan = planner.plan(snapshot)

        # Should have LaunchSessionAction for review
        launch_actions = [
            a for a in plan.actions
            if isinstance(a, LaunchSessionAction) and a.session_type == SessionType.REVIEW
        ]
        assert len(launch_actions) == 1, (
            "Planner must generate LaunchSessionAction for pending reviews"
        )
        assert launch_actions[0].number == 456


class TestReviewNotQueuedWhenConditionsNotMet:
    """Negative tests: Verify reviews are NOT queued in wrong conditions."""

    def test_no_review_when_no_pr(self, sample_session):
        """Reviews not queued when session completes without PR."""
        config = MagicMock()
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_tech_lead.close_ai_session_tabs = True

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:test",
                status="completed",
                runtime_minutes=10,
                pr_url="",  # No PR!
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,  # No review
            pr_url=None,
            pr_number=None,
        )

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 0, (
            "No review should be queued when session has no PR"
        )

    def test_no_review_when_session_failed(self, sample_session):
        """Reviews not queued when session fails.

        This is the scenario from the 2026-01-07 bug - sessions were marked
        FAILED before completion, so no review was queued.
        """
        config = MagicMock()
        config.code_review_agent = "agent:reviewer"
        config.cleanup.without_tech_lead.close_ai_session_tabs = True

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:test",
                status="failed",
                runtime_minutes=1,
                pr_url="",
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.FAILED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 0, (
            "No review should be queued when session fails"
        )

    def test_no_review_when_code_review_agent_not_configured(self, sample_session):
        """Reviews not queued when code_review_agent is not configured."""
        config = MagicMock()
        config.code_review_agent = None  # Not configured!
        config.cleanup.without_tech_lead.close_ai_session_tabs = True

        state = OrchestratorState()
        state.active_sessions = [sample_session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:test",
                status="completed",
                runtime_minutes=10,
                pr_url="https://github.com/test/repo/pull/456",
            ),
            cleanup=CleanupDecision.immediate(),
            should_queue_review=False,  # Handler respects config
            pr_url="https://github.com/test/repo/pull/456",
            pr_number=456,
        )

        handle_session_completion(
            session=sample_session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            pending_work_claims=_test_claim_store(),
        )

        assert len(state.discovered_reviews) == 0, (
            "No review should be queued when code_review_agent not configured"
        )


def _test_claim_store(tmp_path=None):
    """The orchestrator-owned claim store completion now requires (#6999 F9)."""
    import tempfile
    from pathlib import Path as _Path

    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(
        _Path(tmp_path) if tmp_path is not None else _Path(tempfile.mkdtemp())
    )
