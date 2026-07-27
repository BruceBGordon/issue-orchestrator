"""Unit tests for the Planner.

These tests verify the planner's pure policy logic without any external dependencies.
The planner decides "should we?" - no mocks for tmux/GitHub needed.
"""

import pytest
import logging
from pathlib import Path
from unittest.mock import Mock, MagicMock

from issue_orchestrator.infra.config import Config
from issue_orchestrator.control.planner import (
    Planner,
)
from issue_orchestrator.control.planner_types import (
    Plan,
    OrchestratorSnapshot,
    SkippedItem,
)
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from issue_orchestrator.control.actions import (
    ActionType,
    AddCommentAction,
    AddLabelAction,
    LaunchSessionAction,
    RecoverTerminalIssueAction,
    SessionType,
    SyncLabelsAction,
    RemoveLabelAction,
)
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
    AgentConfig,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredFailure,
    DiscoveredMergeQueueEnqueue,
    DiscoveredRetrospectiveReview,
)

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.workflows import (
    RetrospectiveReviewWorkflow,
    ReviewWorkflow,
    ReworkWorkflow,
    TechLeadWorkflow,
)
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.session_run_helpers import make_session_run_assets


def make_config(**kwargs) -> Config:
    """Create a test config with sensible defaults."""
    defaults = {
        "repo": "test/repo",
        "max_concurrent_sessions": 3,
    }
    defaults.update(kwargs)
    return Config(**defaults)


def make_issue(number: int, title: str = "Test issue", **kwargs) -> Issue:
    """Create a test issue."""
    defaults = {
        "number": number,
        "title": title,
        "body": "",
        "labels": [],
        "state": "open",
        "milestone": None,
        "milestone_number": None,
        "milestone_due_on": None,
    }
    defaults.update(kwargs)
    return Issue(**defaults)


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE) -> Session:
    """Create a test session for an issue."""
    from pathlib import Path
    from datetime import datetime

    agent_config = AgentConfig(
        prompt_path=Path("/tmp/test.md"),
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(
            Path(f"/tmp/worktree-{issue.number}"),
            session_name=f"issue-{issue.number}",
        ),
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )


def make_snapshot(
    issues: list[Issue] | None = None,
    active_sessions: list[Session] | None = None,
    pending_reviews: list[PendingReview] | None = None,
    pending_reworks: list[PendingRework] | None = None,
    pending_tech_lead: list[PendingTechLeadReview] | None = None,
    paused: bool = False,
    **kwargs,
) -> OrchestratorSnapshot:
    """Create a test snapshot."""
    return OrchestratorSnapshot(
        issues=tuple(issues or []),
        active_sessions=tuple(active_sessions or []),
        pending_reviews=tuple(pending_reviews or []),
        pending_reworks=tuple(pending_reworks or []),
        pending_tech_lead=tuple(pending_tech_lead or []),
        paused=paused,
        **kwargs,
    )


class StaticIssueChecker:
    """Issue state checker for dependency-gating planner tests."""

    def __init__(self, states: dict[int, str], milestone: str | None = "M1"):
        self.states = states
        self.milestone = milestone

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        state = self.states.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone=self.milestone)

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.states.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.milestone


class TestPlanEmpty:
    """Tests for empty plan scenarios."""

    def test_empty_plan_when_paused(self):
        """Planner returns empty plan when orchestrator is paused."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            paused=True,
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0
        assert len(plan.skipped) == 0


class TestProviderResilienceLabels:
    def test_removes_label_when_review_rework_tech_lead_providers_recover(self):
        config = make_config()
        config.code_review_agent = "agent:reviewer"
        config.tech_lead_review_agent = "agent:tech-lead"
        config.agents["agent:web"] = AgentConfig(prompt_path=Path("/tmp/web.md"), provider=None)
        config.agents["agent:reviewer"] = AgentConfig(prompt_path=Path("/tmp/review.md"), provider="review-provider")
        config.agents["agent:fixer"] = AgentConfig(prompt_path=Path("/tmp/fix.md"), provider="rework-provider")
        config.agents["agent:tech-lead"] = AgentConfig(prompt_path=Path("/tmp/tech-lead.md"), provider="tech-lead-provider")

        scheduler = Scheduler(config)
        provider_resilience = ProviderResilienceManager(
            config.provider_resilience,
            store=InMemoryProviderCircuitStore(),
            events=MagicMock(),
        )
        planner = Planner(config=config, scheduler=scheduler, provider_resilience=provider_resilience)

        issue1 = make_issue(1, labels=["agent:web", config.get_label_provider_unavailable()])
        issue2 = make_issue(2, labels=["agent:web", config.get_label_provider_unavailable()])
        issue3 = make_issue(3, labels=["agent:web", config.get_label_provider_unavailable()])

        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=101,
            pr_url="https://example.com/pr/101",
            branch_name="branch-101",
            _issue_number=1,
            agent_label=None,
        )
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="2"),
            agent_type="agent:fixer",
            rework_cycle=1,
            issue_number=2,
        )
        pending_tech_lead = PendingTechLeadReview(
            issue_number=3, title="Tech Lead 3", flavor=TechLeadSessionFlavor.BATCH_REVIEW
        )

        snapshot = make_snapshot(
            issues=[issue1, issue2, issue3],
            pending_reviews=[pending_review],
            pending_reworks=[pending_rework],
            pending_tech_lead=[pending_tech_lead],
        )

        plan = planner.plan(snapshot)

        removed = [a for a in plan.actions if getattr(a, "action_type", None) == ActionType.REMOVE_LABEL]
        removed_numbers = {a.issue_number for a in removed}
        assert removed_numbers == {1, 2, 3}

    def test_empty_plan_when_at_capacity(self):
        """Planner returns empty plan when at max capacity."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        issue2 = make_issue(2)
        snapshot = make_snapshot(
            issues=[make_issue(3), make_issue(4)],
            active_sessions=[make_session(issue1), make_session(issue2)],
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0

    def test_empty_plan_when_no_issues(self):
        """Planner returns empty plan when no issues available."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(issues=[])

        plan = planner.plan(snapshot)

        assert plan.action_count == 0


class TestPlanIssues:
    """Tests for issue planning logic."""

    def test_plans_single_issue_launch(self):
        """Planner creates action to launch a single available issue."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(issues=[make_issue(42)])

        plan = planner.plan(snapshot)

        assert plan.action_count == 1
        action = plan.actions[0]
        assert isinstance(action, LaunchSessionAction)
        assert action.session_type == SessionType.ISSUE
        assert action.number == 42

    def test_plans_multiple_issues_up_to_capacity(self):
        """Planner creates actions for issues up to remaining capacity."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # 1 session running, 2 slots available
        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[make_issue(2), make_issue(3), make_issue(4)],
            active_sessions=[make_session(issue1)],
        )

        plan = planner.plan(snapshot)

        # Should only plan 2 issues (remaining capacity)
        assert plan.action_count == 2
        issue_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(issue_actions) == 2

    def test_skips_already_active_issues(self):
        """Planner doesn't plan actions for issues already being worked."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[issue1, make_issue(2)],
            active_sessions=[make_session(issue1)],
        )

        plan = planner.plan(snapshot)

        # Should only plan issue 2, not issue 1 (already active)
        assert plan.action_count == 1
        assert plan.actions[0].number == 2


class TestObservedCompletionDecommissioned:
    """Pin the decommissioning of the dormant observed-completion label path.

    Completion label policy is owned solely by the live completion path
    (CompletionHandler / handle_session_completion). The planner must no longer
    carry a parallel projection surface fed by observed_completions.
    """

    def test_snapshot_and_state_have_no_observed_completions_surface(self):
        from issue_orchestrator.domain.models import OrchestratorState

        assert not hasattr(OrchestratorState(), "observed_completions")
        assert "observed_completions" not in OrchestratorSnapshot.__dataclass_fields__

    def test_planner_has_no_observed_completion_projection(self):
        assert not hasattr(Planner, "_plan_observed_completion_labels")


class TestMaxIssuesToStart:
    """Tests for the max-issues-to-start planning limit."""

    def test_respects_max_issues_to_start(self):
        """Planner respects the max_issues_to_start limit."""
        config = make_config(max_concurrent_sessions=5)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2), make_issue(3)],
            max_issues_to_start=2,
            issues_started_count=1,  # Already started 1
        )

        plan = planner.plan(snapshot)

        # Should only plan 1 more issue (max=2, started=1)
        assert plan.action_count == 1


class TestPlanValidationRetries:
    """Tests for validation retry planning."""

    def test_plans_validation_retry_when_max_issues_to_start_reached(self):
        """Validation retries continue existing work and bypass max_to_start."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=1,
                    issue_title="Retry me",
                    agent_label="agent:developer",
                    worktree_path="/tmp/repo-1",
                    branch_name="1-retry",
                    original_prompt="original task",
                    validation_error="dirty worktree",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                    validation_cmd="make test",
                ),
            ],
            max_issues_to_start=1,
            issues_started_count=1,
        )

        plan = planner.plan(snapshot)

        retry_actions = plan.actions_of_type(ActionType.LAUNCH_VALIDATION_RETRY)
        issue_actions = [
            action for action in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if action.session_type == SessionType.ISSUE
        ]
        assert len(retry_actions) == 1
        assert retry_actions[0].issue_number == 1
        assert issue_actions == []

    def test_skips_validation_retry_for_active_issue(self):
        """Validation retry planner does not double-launch an active issue."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(1)

        snapshot = make_snapshot(
            issues=[issue],
            active_sessions=[make_session(issue)],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=1,
                    issue_title="Retry me",
                    agent_label="agent:developer",
                    worktree_path="/tmp/repo-1",
                    branch_name="1-retry",
                    original_prompt="original task",
                    validation_error="dirty worktree",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                    validation_cmd="make test",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_VALIDATION_RETRY) == []
        assert plan.skipped[0].item_type == "validation_retry"
        assert plan.skipped[0].reason == "active session running"


class TestPlanReviews:
    """Tests for review planning with workflow."""

    def test_plans_reviews_when_workflow_configured(self):
        """Planner uses ReviewWorkflow to decide on reviews."""
        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)

        # Mock the review workflow
        mock_workflow = Mock()
        mock_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
        ]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
            ],
        )

        plan = planner.plan(snapshot)

        # Should have review launch action
        review_actions = [a for a in plan.actions if a.session_type == SessionType.REVIEW]
        assert len(review_actions) == 1
        assert review_actions[0].number == 100

    def test_plans_retrospective_reviews_when_workflow_configured(self):
        """Planner uses RetrospectiveReviewWorkflow for review-first launches."""
        config = make_config(
            code_review_agent="agent:reviewer",
            retrospective_review_enabled=True,
        )
        scheduler = Scheduler(config)
        pending = PendingRetrospectiveReview(
            issue_key=FakeIssueKey(name="365"),
            issue_number=365,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )

        mock_workflow = Mock()
        mock_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [pending]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            retrospective_review_workflow=mock_workflow,
        )

        plan = planner.plan(make_snapshot(pending_retrospective_reviews=[pending]))

        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.RETROSPECTIVE_REVIEW
        assert launch_actions[0].number == 365

    def test_discovered_retrospective_review_queues_and_blocks_code_launch(self):
        """Trigger-labeled issues queue retrospective review instead of coding."""
        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(365, labels=["agent:web", "lack-of-review-redo"])
        discovered = DiscoveredRetrospectiveReview(
            issue_number=365,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="365",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        plan = planner.plan(
            make_snapshot(
                issues=[issue],
                discovered_retrospective_reviews=[discovered],
            )
        )

        queue_actions = plan.actions_of_type(ActionType.QUEUE_RETROSPECTIVE_REVIEW)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(queue_actions) == 1
        assert queue_actions[0].issue_number == 365
        assert queue_actions[0].prior_pr_number == 512
        assert launch_actions == []
        assert any(
            skipped.item_type == "issue"
            and skipped.number == 365
            and skipped.reason == "pending retrospective review"
            for skipped in plan.skipped
        )


class TestPlanHelpers:
    """Tests for Plan and snapshot helper methods."""

    def test_plan_has_action_for(self):
        """Plan.has_action_for correctly identifies planned actions."""
        plan = Plan(
            actions=(
                LaunchSessionAction(session_type=SessionType.ISSUE, number=42, command="", working_dir=""),
            ),
            skipped=(),
        )

        assert plan.has_action_for(42) is True
        assert plan.has_action_for(99) is False

    def test_snapshot_active_issue_numbers(self):
        """Snapshot correctly computes active issue numbers."""
        issue1 = make_issue(1)
        issue2 = make_issue(2)

        snapshot = make_snapshot(
            issues=[issue1, issue2, make_issue(3)],
            active_sessions=[make_session(issue1), make_session(issue2)],
        )

        assert snapshot.active_issue_numbers == frozenset({1, 2})

    def test_skipped_item_creation(self):
        """SkippedItem correctly records skipped items."""
        item = SkippedItem(
            item_type="issue",
            number=42,
            reason="dependency: blocked by #41",
        )

        assert item.item_type == "issue"
        assert item.number == 42
        assert "blocked" in item.reason


class TestPlannerDependencyGating:
    """Regression coverage for planner/scheduler dependency wiring."""

    def test_planner_dependency_evaluator_blocks_unsatisfied_dependency(self):
        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    2,
                    title="Dependent issue",
                    body="Depends-on: #100",
                    milestone="M1",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        assert plan.skipped == (
            SkippedItem(
                item_type="issue",
                number=2,
                reason="dependency: Blocked - waiting on: #100",
            ),
        )
        assert scheduler.dependency_evaluator is evaluator

    def test_planner_blocks_stack_successor_with_missing_predecessor_state(self):
        """A stack successor stays blocked when no predecessor facts exist."""
        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({20: "open"}),
            events=Mock(),
        )  # no facts provider -> conservatively blocked
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        snapshot = make_snapshot(
            issues=[
                make_issue(2, title="Successor", body="Stack-after: #20", milestone="M1"),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        assert len(plan.skipped) == 1
        assert plan.skipped[0].number == 2
        assert "dependency:" in plan.skipped[0].reason

    def test_planner_launches_stack_successor_when_predecessor_ready(self):
        """A validated + reviewed + usable predecessor branch lets the successor launch."""
        from issue_orchestrator.domain.dependencies import DependencyTarget
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        class _Provider:
            def gather_facts(self, targets):
                return {
                    t: PredecessorFacts(
                        branch_usable=True, validation_passed=True,
                        agent_reviewed=True, branch_name="20-base",
                    )
                    for t in targets
                }

        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({20: "open"}),
            events=Mock(),
            predecessor_facts_provider=_Provider(),
        )
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        snapshot = make_snapshot(
            issues=[
                make_issue(2, title="Successor", body="Stack-after: #20", milestone="M1"),
            ],
        )

        plan = planner.plan(snapshot)

        launches = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert [a.number for a in launches] == [2]
        assert all("dependency:" not in s.reason for s in plan.skipped)

    def test_planner_rejects_dependency_evaluator_without_scheduler_wiring(self):
        config = make_config()
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config)

        with pytest.raises(ValueError, match="Scheduler dependency evaluator is required"):
            Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        assert scheduler.dependency_evaluator is None

    def test_planner_uses_scheduler_dependency_evaluator_when_not_passed_directly(self):
        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    2,
                    title="Dependent issue",
                    body="Depends-on: #100",
                    milestone="M1",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        assert planner.dependency_evaluator is evaluator

    def test_planner_rejects_mismatched_scheduler_dependency_evaluator(self):
        config = make_config()
        scheduler_evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        planner_evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=scheduler_evaluator)

        with pytest.raises(ValueError, match="dependency evaluators"):
            Planner(
                config=config,
                scheduler=scheduler,
                dependency_evaluator=planner_evaluator,
            )


class TestExplainSkip:
    """Tests for the explain_skip method."""

    def test_explain_skip_already_active(self):
        """Explains why an already-active issue is skipped."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[issue1],
            active_sessions=[make_session(issue1)],
        )

        reason = planner.explain_skip(1, snapshot)

        assert "active session" in reason.lower()

    def test_explain_skip_paused(self):
        """Explains that orchestrator is paused."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1)],
            paused=True,
        )

        reason = planner.explain_skip(1, snapshot)

        assert "paused" in reason.lower()

    def test_explain_skip_at_capacity(self):
        """Explains that system is at capacity."""
        config = make_config(max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[make_session(issue1)],
        )

        reason = planner.explain_skip(2, snapshot)

        assert "capacity" in reason.lower()


class TestPlanDiscoveredReviews:
    """Tests for Planner's _plan_discovered_reviews method.

    This method processes DiscoveredReview facts from session completions
    and produces QueueReviewAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_review(self):
        """Planner produces QueueReviewAction for discovered reviews."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    42,
                    labels=["agent:web", "verbose"],
                )
            ],
            discovered_reviews=(discovered,),
            pending_reviews=(),  # Not already queued
        )

        plan = planner.plan(snapshot)

        # Should have a QueueReviewAction
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        action = queue_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.pr_url == "https://github.com/test/repo/pull/100"
        assert action.branch_name == "feature/issue-42"
        assert action.issue_labels == ("agent:web", "verbose")

    def test_skips_already_queued_reviews(self):
        """Planner skips discovered reviews that are already in pending_reviews."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        # Already queued in pending_reviews
        already_pending = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(already_pending,),
        )

        plan = planner.plan(snapshot)

        # Should NOT have a QueueReviewAction for already-queued PR
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_no_queue_actions_when_no_discovered_reviews(self):
        """Planner produces no queue actions when no discovered reviews."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_reviews=(),  # Empty
        )

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_queue_action_has_expected_state(self):
        """Planner attaches ExpectedState to QueueReviewAction."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(),
        )

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        action = queue_actions[0]

        # Should have ExpectedState attached
        assert action.expected is not None
        # Should forbid the pause label
        assert "io:needs-reconcile" in action.expected.forbidden_labels

    def test_add_label_action_has_expected_state(self):
        """Planner attaches ExpectedState to AddLabelAction for pr-pending."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(),
        )

        plan = planner.plan(snapshot)

        # Find the AddLabelAction for pr-pending
        add_label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(add_label_actions) == 1
        action = add_label_actions[0]

        # Should have ExpectedState attached
        assert action.expected is not None
        # Should forbid the pause label
        assert "io:needs-reconcile" in action.expected.forbidden_labels


class TestPlanAwaitingMergeReconciliations:
    """Tests for planning awaiting-merge history reconciliation facts."""

    def test_plans_terminal_recovery_action(self):
        """A terminal (non-drift) reconciliation plans a single owner command
        that sheds labels then finalizes history — not a standalone history
        reconciliation that could be terminalized before cleanup."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        # No standalone history reconciliation for the terminal path — the
        # owner command finalizes history internally, after the shed succeeds.
        assert plan.actions_of_type(ActionType.RECONCILE_HISTORY_ENTRY) == []
        actions = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, RecoverTerminalIssueAction)
        assert action.issue_number == 228
        assert action.pr_number == 318
        assert action.pr_url == "https://github.com/test/repo/pull/318"
        assert action.status == "merged"
        assert action.status_reason == "PR merged; awaiting merge reconciled"
        assert action.source == "pull_request"
        assert action.issue_key == "M1-228"
        # Carries the reconciliation pause guard the old terminal-cleanup
        # RemoveLabelAction used to carry: a paused issue (io:needs-reconcile)
        # must not be shed or finalized behind fail-closed drift handling. The
        # applier enforces this at the owner-command boundary (#6431 F1).
        assert action.expected is not None
        assert "io:needs-reconcile" in action.expected.forbidden_labels

    def test_terminal_pr_merged_reconciliation_recovers_terminal_issue(self):
        """When a PR is observed merged, plan one RecoverTerminalIssueAction
        that sheds the issue's transient workflow labels (pr-pending,
        publish-failed, publish-fail-count-N, blocking) and then finalizes
        history. The applier reads the issue's live labels to pick the exact
        set and cleans both GitHub and the local label_store mirror.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert len(recover_actions) == 1
        action = recover_actions[0]
        assert action.issue_key == "M1-228"
        assert action.status == "merged"
        assert "merged" in action.reason

    def test_terminal_issue_closed_reconciliation_recovers_terminal_issue(self):
        """When the parent issue is closed (regardless of PR state), the same
        owner command applies: shed every transient workflow label, then
        finalize history.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            status_reason="Issue closed; awaiting merge reconciled",
            source="issue",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert len(recover_actions) == 1

    def test_terminal_reconciliation_dedupes_against_drift(self):
        """When a drift action already removes pr-pending for the same
        issue (PR closed but issue still open), don't shed: the drift is
        ADDING blocked:pr-closed, so shedding blocking labels would
        contradict it. The drift path finalizes history on its own (standalone
        ReconcileHistoryEntryAction) and the SyncLabelsAction owns pr-pending
        removal — no RecoverTerminalIssueAction is planned.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        reconciliation = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            status_reason="PR closed",
            source="pull_request",
            issue_key="M1-228",
        )
        drift = DiscoveredAwaitingMergeDrift(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR closed; issue remains open",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(reconciliation,),
            discovered_awaiting_merge_drifts=(drift,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert recover_actions == [], (
            "Drift ADDS blocked:pr-closed; shedding blocking labels would "
            "contradict the drift. Only history finalize + SyncLabelsAction "
            "should act."
        )
        # Drift still finalizes its history entry on its own.
        history_actions = [
            a for a in plan.actions_of_type(ActionType.RECONCILE_HISTORY_ENTRY)
            if a.issue_number == 228
        ]
        assert len(history_actions) == 1
        sync_actions = plan.actions_of_type(ActionType.SYNC_LABELS)
        assert len(sync_actions) == 1
        assert "pr-pending" in sync_actions[0].remove_labels

    def test_plans_pr_closed_drift_label_sync_action(self):
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        drift = DiscoveredAwaitingMergeDrift(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR closed; issue remains open",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_drifts=(drift,),
        )

        plan = planner.plan(snapshot)

        actions = plan.actions_of_type(ActionType.SYNC_LABELS)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, SyncLabelsAction)
        assert action.issue_number == 228
        assert action.add_labels == ("blocked:pr-closed",)
        assert action.remove_labels == ("pr-pending",)
        assert action.issue_key == "M1-228"
        assert action.reason == "PR closed; issue remains open"
        assert action.expected is not None
        assert action.expected.required_labels == frozenset({"pr-pending"})


class TestPlanTechLeadIssueCreation:
    """Tests for Planner's _plan_tech_lead_issue_creation method.

    This method processes TechLeadFacts and produces CreateTechLeadIssueAction
    when the threshold is met and no existing tech_lead issue exists.
    """

    def test_creates_tech_lead_issue_at_threshold(self):
        """Planner produces CreateTechLeadIssueAction when threshold is met."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=3,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=3,
            threshold=3,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2"), (3, "PR 3")),
        )

        snapshot = make_snapshot(
            tech_lead_facts=tech_lead_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]
        assert "Tech Lead Batch Review" in action.title
        assert action.pr_count == 3
        assert "agent:tech-lead" in action.labels

    def test_completed_closed_batch_does_not_retrigger_empty_batch(self):
        """Reviewer repro (#6768 r5): after a successful batch (PRs carry
        tech-lead-reviewed, tracker closed) re-gathering + planning against the
        same PR observations must not create an empty successor batch.

        Facts come from the REAL FactGatherer so the shared candidate
        predicate (fact side == manifest side) is what this exercises.
        """
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.control.fact_gatherer import FactGatherer
        from issue_orchestrator.domain.models import OrchestratorState
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=2,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        host = MagicMock()
        host.get_prs_with_label.return_value = [
            PRInfo(number=1, url="...", title="PR 1", branch="b1",
                   labels=["code-reviewed", "tech-lead-reviewed"], body="", state="open"),
            PRInfo(number=2, url="...", title="PR 2", branch="b2",
                   labels=["code-reviewed", "tech-lead-reviewed"], body="", state="open"),
        ]
        # The completed batch's tracking issue is CLOSED: open-state queries
        # (the finder's contract) no longer return it.
        host.list_issues.return_value = []

        facts = FactGatherer(config=config, repository_host=host).gather_tech_lead_facts(
            OrchestratorState()
        )
        assert facts is not None
        assert facts.pr_count == 0
        assert facts.existing_tech_lead_issue is None

        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        plan = planner.plan(make_snapshot(tech_lead_facts=facts))

        create_actions = [
            a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE
        ]
        assert create_actions == []

    def test_no_tech_lead_issue_below_threshold(self):
        """Planner produces no CreateTechLeadIssueAction when below threshold."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=5,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=2,  # Below threshold of 5
            threshold=5,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
        )

        snapshot = make_snapshot(
            tech_lead_facts=tech_lead_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 0

    def test_no_tech_lead_issue_when_existing_issue(self):
        """Planner produces no CreateTechLeadIssueAction when existing issue exists."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=3,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=5,  # Above threshold
            threshold=3,
            existing_tech_lead_issue=100,  # Already exists!
            watch_label="code-reviewed",
            prs=tuple(),
        )

        snapshot = make_snapshot(
            tech_lead_facts=tech_lead_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 0

    def test_no_tech_lead_issue_when_no_facts(self):
        """Planner produces no CreateTechLeadIssueAction when tech_lead_facts is None."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()  # No tech_lead config
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            tech_lead_facts=None,  # No facts gathered
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 0

    def test_tech_lead_issue_body_includes_pr_list(self):
        """Planner includes PR details in tech_lead issue body."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=2,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=2,
            threshold=2,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((10, "Fix bug A"), (20, "Add feature B")),
        )

        snapshot = make_snapshot(
            tech_lead_facts=tech_lead_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        body = create_actions[0].body
        assert "PR #10" in body
        assert "Fix bug A" in body
        assert "PR #20" in body
        assert "Add feature B" in body
        assert "code-reviewed" in body
        assert "tech-lead-reviewed" in body

    def test_tech_lead_issue_explicit_milestone_applied(self):
        """Explicit milestone strategy travels as a NAME intent (#6769 F4).

        The planner plans the configured name; the create-issue applier is
        the single name->number resolution boundary, so planning makes zero
        milestone API reads.
        """
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType, TechLeadMilestoneIntent
        from issue_orchestrator.infra.config import MilestoneStrategyConfig, TechLeadConfig

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=1,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        config.tech_lead = TechLeadConfig(
            milestone_strategy=MilestoneStrategyConfig(explicit="M5"),
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=1,
            threshold=1,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"),),
        )

        plan = planner.plan(make_snapshot(tech_lead_facts=tech_lead_facts))

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        assert create_actions[0].milestone == TechLeadMilestoneIntent(explicit_name="M5")

    def test_tech_lead_issue_inherits_labels_from_source(self):
        """Planner inherits labels from source issues based on tech_lead config."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TechLeadConfig, MilestoneStrategyConfig

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=2,
        )
        # Configure label inheritance
        config.tech_lead = TechLeadConfig(
            inherit_labels=["io-e2e-test-data", "team:backend"],
            explicit_labels=["needs-batch-review"],
            milestone_strategy=MilestoneStrategyConfig(),
            priority="P2",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Source labels include one that should be inherited
        tech_lead_facts = TechLeadFacts(
            pr_count=3,
            threshold=2,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2"), (3, "PR 3")),
            source_labels=frozenset(["io-e2e-test-data", "other-label"]),
            source_milestones=(),
        )

        snapshot = make_snapshot(tech_lead_facts=tech_lead_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should have agent label, explicit labels, and inherited labels
        assert "agent:tech-lead" in action.labels
        assert "needs-batch-review" in action.labels
        assert "io-e2e-test-data" in action.labels  # Inherited (was in source_labels)
        assert "team:backend" not in action.labels  # Not inherited (not in source_labels)
        assert action.title.startswith("[P2-000]")

    def test_tech_lead_issue_inherits_milestone_latest(self):
        """Planner picks latest milestone from source issues."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TechLeadConfig, MilestoneStrategyConfig

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=2,
        )
        config.tech_lead = TechLeadConfig(
            milestone_strategy=MilestoneStrategyConfig(inherit_from_issues="latest"),
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=2,
            threshold=2,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
            source_labels=frozenset(),
            source_milestones=((1, "M1"), (3, "M3"), (2, "M2")),  # Unsorted
        )

        snapshot = make_snapshot(tech_lead_facts=tech_lead_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should pick highest milestone number (3 = M3) — known at planning
        # time, so the intent carries the number directly.
        assert action.milestone.inherited_number == 3
        assert action.milestone.explicit_name is None

    def test_tech_lead_issue_inherits_milestone_earliest(self):
        """Planner picks earliest milestone from source issues."""
        from issue_orchestrator.domain.models import TechLeadFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TechLeadConfig, MilestoneStrategyConfig

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=2,
        )
        config.tech_lead = TechLeadConfig(
            milestone_strategy=MilestoneStrategyConfig(inherit_from_issues="earliest"),
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        tech_lead_facts = TechLeadFacts(
            pr_count=2,
            threshold=2,
            existing_tech_lead_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
            source_labels=frozenset(),
            source_milestones=((3, "M3"), (1, "M1"), (2, "M2")),  # Unsorted
        )

        snapshot = make_snapshot(tech_lead_facts=tech_lead_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should pick lowest milestone number (1 = M1)
        assert action.milestone.inherited_number == 1
        assert action.milestone.explicit_name is None


class TestPlanApprovedTechLeadOpExecutions:
    """Approved gated proposals (#6778): the plan carries the stored op's
    execution action; still-gated proposals plan nothing."""

    @staticmethod
    def _op(target: int, op_type: str = "reset_retry"):
        from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp

        return StoredTechLeadOp(
            op_type=op_type,
            target_issue_number=target,
            rationale="r",
            source_run_id="run-1",
            source_session_name="issue-99",
            source_action_id="A2",
            created_at="2026-07-11T00:00:00+00:00",
        )

    def _plan(self, approved_ops):
        from issue_orchestrator.domain.models import TechLeadFacts

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=3,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        planner = Planner(config=config, scheduler=Scheduler(config))
        snapshot = make_snapshot(
            tech_lead_facts=TechLeadFacts(
                threshold=3,
                watch_label="code-reviewed",
                approved_tech_lead_ops=approved_ops,
            ),
        )
        return planner.plan(snapshot)

    def test_approved_ops_plan_execution_actions(self):
        from issue_orchestrator.control.actions import (
            KillHungSessionAction,
            ResetRetryIssueAction,
        )
        from issue_orchestrator.domain.tech_lead_session import ApprovedTechLeadOp

        plan = self._plan((
            ApprovedTechLeadOp(proposal_issue_number=500, op=self._op(13)),
            ApprovedTechLeadOp(
                proposal_issue_number=501,
                op=self._op(14, "kill_hung_session"),
            ),
        ))

        [reset] = [a for a in plan.actions if isinstance(a, ResetRetryIssueAction)]
        assert reset.issue_number == 13
        assert reset.proposal_issue_number == 500
        [kill] = [a for a in plan.actions if isinstance(a, KillHungSessionAction)]
        assert kill.issue_number == 14
        assert kill.proposal_issue_number == 501

    def test_no_approved_ops_plans_no_executions(self):
        """Still-gated proposals never reach the facts, so nothing plans."""
        from issue_orchestrator.control.actions import (
            KillHungSessionAction,
            ResetRetryIssueAction,
        )

        plan = self._plan(())

        assert not any(
            isinstance(a, (ResetRetryIssueAction, KillHungSessionAction))
            for a in plan.actions
        )

    def _plan_with_candidates(self, candidates):
        from issue_orchestrator.domain.models import TechLeadFacts

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_threshold=3,
            tech_lead_reviewed_label="tech-lead-reviewed",
        )
        planner = Planner(config=config, scheduler=Scheduler(config))
        snapshot = make_snapshot(
            tech_lead_facts=TechLeadFacts(
                threshold=3,
                watch_label="code-reviewed",
                absent_proposal_op_candidates=candidates,
            ),
        )
        return planner.plan(snapshot)

    def test_absent_op_candidates_emit_confirm_and_discard_action(self):
        """R7/R10: the planner turns the read-only absent-candidate fact into a
        single DiscardTerminalTechLeadProposalOpsAction; the applier confirms each
        with a targeted read before discarding."""
        from issue_orchestrator.control.actions import (
            DiscardTerminalTechLeadProposalOpsAction,
        )

        plan = self._plan_with_candidates((501, 777))

        [discard] = [
            a
            for a in plan.actions
            if isinstance(a, DiscardTerminalTechLeadProposalOpsAction)
        ]
        assert discard.candidate_issue_numbers == (501, 777)

    def test_no_absent_candidates_plans_no_discard(self):
        from issue_orchestrator.control.actions import (
            DiscardTerminalTechLeadProposalOpsAction,
        )

        plan = self._plan_with_candidates(())

        assert not any(
            isinstance(a, DiscardTerminalTechLeadProposalOpsAction)
            for a in plan.actions
        )


class TestPlanHealthReviewIssueCreation:
    """Health-review anchor creation planning (ADR-0031 §4).

    Policy lives in control/health_review_trigger; these tests exercise it
    through the full planner (facts -> CreateTechLeadIssueAction).
    """

    @staticmethod
    def _make_planner(interval_minutes: int = 60, events=None):
        config = make_config(tech_lead_review_agent="agent:tech-lead")
        config.tech_lead.health_review.interval_minutes = interval_minutes
        # The periodic trigger routes through the owned paused/capacity gate
        # (TechLeadWorkflow), so the planner MUST carry that workflow — the same
        # owner that emits TECH_LEAD_SKIPPED (#6763 finding 2).
        events = events if events is not None else InMemoryEventSink()
        workflow = TechLeadWorkflow(config=config, events=events)
        planner = Planner(
            config=config, scheduler=Scheduler(config), tech_lead_workflow=workflow
        )
        return planner, config

    @staticmethod
    def _health_facts(**kwargs):
        from issue_orchestrator.domain.models import TechLeadFacts

        defaults = {"health_review_due": True, "existing_health_review_issue": None}
        defaults.update(kwargs)
        return TechLeadFacts(**defaults)

    @staticmethod
    def _create_actions(plan):
        return [
            a for a in plan.actions
            if a.action_type == ActionType.CREATE_TECH_LEAD_ISSUE
        ]

    def test_creates_anchor_with_agent_and_marker_labels_when_due(self):
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        planner, _ = self._make_planner()
        plan = planner.plan(make_snapshot(tech_lead_facts=self._health_facts()))

        actions = self._create_actions(plan)
        assert len(actions) == 1
        action = actions[0]
        assert action.title == "Health Review — walk the floor"
        assert "agent:tech-lead" in action.labels
        assert HEALTH_REVIEW_MARKER_LABEL in action.labels
        # The owner states the variant on the action; the creation boundary
        # reports that decision rather than re-reading the marker label (#6780).
        assert action.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert action.pr_count == 0
        assert "board snapshot" in action.body.lower()
        assert "ADR-0031" in action.body

    def test_includes_filter_label_when_configured(self):
        """Filtered runs must scope the anchor so pickup and dedup both see it."""
        planner, config = self._make_planner()
        config.filtering.label = "io:e2e:run-1"

        plan = planner.plan(make_snapshot(tech_lead_facts=self._health_facts()))

        (action,) = self._create_actions(plan)
        assert "io:e2e:run-1" in action.labels

    def test_skips_when_not_due(self):
        planner, _ = self._make_planner()
        facts = self._health_facts(health_review_due=False)

        plan = planner.plan(make_snapshot(tech_lead_facts=facts))

        assert self._create_actions(plan) == []

    def test_problem_storm_creates_one_unscheduled_health_review(self):
        """K recent problems escalate to one health anchor carrying the whole
        cohort, even when the periodic interval is not due (#6780).

        The cohort is also queued as individual investigations in the same
        plan: the anchor's intake collapses them on a successful create, so
        persisting first is what keeps the problems recoverable if the create
        never lands (#6780). See TestReactiveTechLeadStormEscalation.
        """
        planner, config = self._make_planner(interval_minutes=0)
        config.tech_lead.health_review.storm_threshold = 3
        config.tech_lead.health_review.storm_window_minutes = 5
        problems = tuple(
            DiscoveredFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="failed",
                observed_at=1_000.0,
            )
            for number in (3, 1, 2)
        )
        planner = Planner(
            config=config,
            scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
            clock=lambda: 1_100.0,
        )

        plan = planner.plan(make_snapshot(discovered_failures=problems))

        [action] = self._create_actions(plan)
        assert tuple(p.issue_number for p in action.storm_problems) == (1, 2, 3)
        assert action.reason == "problem storm: 3 issues inside settle window"
        assert action.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert "instead of" in action.body
        # Persist-first: the cohort is queued ahead of the anchor that retires it.
        assert sorted(
            a.issue_number for a in plan.actions_of_type(ActionType.QUEUE_TECH_LEAD)
        ) == [1, 2, 3]

    def test_skips_when_existing_anchor_open(self):
        planner, _ = self._make_planner()
        facts = self._health_facts(existing_health_review_issue=321)

        plan = planner.plan(make_snapshot(tech_lead_facts=facts))

        assert self._create_actions(plan) == []

    def test_skips_when_health_review_pending_launch(self):
        """Dedup keys off the queue item's typed flavor, not its title."""
        planner, _ = self._make_planner()
        pending = PendingTechLeadReview(
            issue_number=321,
            title="renamed by an operator",
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        )

        plan = planner.plan(
            make_snapshot(tech_lead_facts=self._health_facts(), pending_tech_lead=[pending])
        )

        assert self._create_actions(plan) == []

    def test_pending_batch_launch_does_not_block_health_review(self):
        """A queued BATCH item must not dedupe the health anchor (independent
        triggers; only a pending HEALTH_REVIEW covers the creation window)."""
        planner, _ = self._make_planner()
        pending = PendingTechLeadReview(
            issue_number=100,
            title="Tech Lead Batch Review: 5 PRs pending",
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        )

        plan = planner.plan(
            make_snapshot(tech_lead_facts=self._health_facts(), pending_tech_lead=[pending])
        )

        assert len(self._create_actions(plan)) == 1

    def test_health_only_facts_never_create_batch_issue(self):
        """threshold<=0 facts (health-only) must not trip batch creation."""
        planner, _ = self._make_planner()
        facts = self._health_facts(
            health_review_due=False, pr_count=0, threshold=0, watch_label=""
        )

        plan = planner.plan(make_snapshot(tech_lead_facts=facts))

        assert self._create_actions(plan) == []

    @staticmethod
    def _tech_lead_skipped(events: InMemoryEventSink):
        return [e for e in events.events if e.name == "tech_lead.skipped"]

    def test_paused_skips_creation_and_emits_tech_lead_skipped(self):
        """A due health review, while paused, files NO anchor and is observably
        skipped through the owned gate (not silently dropped, #6763 finding 2).

        ``Planner.plan()`` early-returns an empty plan when paused; the health
        gate must still run so TECH_LEAD_SKIPPED carries the paused reason.
        """
        events = InMemoryEventSink()
        planner, _ = self._make_planner(events=events)

        plan = planner.plan(
            make_snapshot(tech_lead_facts=self._health_facts(), paused=True)
        )

        assert self._create_actions(plan) == []
        skipped = self._tech_lead_skipped(events)
        assert [e.data["reason"] for e in skipped] == ["orchestrator_paused"]

    def test_at_capacity_skips_creation_and_emits_tech_lead_skipped(self):
        """At capacity the anchor is NOT filed and TECH_LEAD_SKIPPED carries the
        capacity reason — the phase-1 create must route through the gate, not
        fire before it (#6763 finding 2)."""
        events = InMemoryEventSink()
        planner, config = self._make_planner(events=events)
        config.max_concurrent_sessions = 2
        active = [make_session(make_issue(1)), make_session(make_issue(2))]

        plan = planner.plan(
            make_snapshot(
                tech_lead_facts=self._health_facts(), active_sessions=active
            )
        )

        assert self._create_actions(plan) == []
        skipped = self._tech_lead_skipped(events)
        assert len(skipped) == 1
        assert skipped[0].data["reason"] == "no_capacity"
        # Owner-computed availability (worker budget 2 - 2 active = 0) (#6892 A2).
        assert skipped[0].data["available"] == 0

    def test_open_gate_still_creates_and_emits_no_skip(self):
        """Belt and braces: with the gate open the anchor is filed and NO
        spurious TECH_LEAD_SKIPPED is emitted (the happy path stays clean)."""
        events = InMemoryEventSink()
        planner, _ = self._make_planner(events=events)

        plan = planner.plan(make_snapshot(tech_lead_facts=self._health_facts()))

        assert len(self._create_actions(plan)) == 1
        assert self._tech_lead_skipped(events) == []


class TestReactiveTechLeadStormEscalation:
    """Persist-first storm escalation (#6780).

    The cohort is ALWAYS queued as individual investigations — the pending
    queue is the only cross-tick carrier of a problem once discovered_failures
    is cleared at end of tick — and the anchor is planned after them, so a
    successful create collapses them at intake. Every path that leaves the
    cohort without an anchor (existing/pending health review, no capacity, a
    failed create, the apply-time cooldown) therefore keeps the investigations
    queued. Suppression only holds back launches on the tick the cohort is
    actually escalated; it never decides retention.
    """

    STORM_CLOCK = 1_100.0
    WINDOW_OBSERVED_AT = 1_000.0

    def _planner(self, *, max_concurrent: int = 3, events=None):
        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=True,
            max_concurrent_sessions=max_concurrent,
        )
        # Isolate the storm path from the periodic interval.
        config.tech_lead.health_review.interval_minutes = 0
        config.tech_lead.health_review.storm_threshold = 3
        config.tech_lead.health_review.storm_window_minutes = 5
        events = events if events is not None else InMemoryEventSink()
        planner = Planner(
            config=config,
            scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=events),
            clock=lambda: self.STORM_CLOCK,
        )
        return planner, config

    def _cohort(self, numbers=(1, 2, 3)) -> tuple[DiscoveredFailure, ...]:
        return tuple(
            DiscoveredFailure(
                issue_number=n,
                issue_title=f"Problem {n}",
                failure_reason="failed",
                observed_at=self.WINDOW_OBSERVED_AT,
            )
            for n in numbers
        )

    @staticmethod
    def _queued_tech_lead_issue_numbers(plan) -> list[int]:
        return sorted(
            a.issue_number
            for a in plan.actions
            if a.action_type == ActionType.QUEUE_TECH_LEAD
        )

    def test_storm_persists_cohort_before_planning_its_anchor(self):
        """Persist-first: an escalating storm queues the cohort as individual
        investigations AND plans the anchor, with the QUEUE_TECH_LEAD actions
        ordered strictly BEFORE the create. Intake collapses them on a
        successful create; if the create never lands, those queued items are
        what keeps the cohort alive (#6780)."""
        planner, _ = self._planner()

        plan = planner.plan(make_snapshot(discovered_failures=self._cohort()))

        assert len(plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE)) == 1
        assert self._queued_tech_lead_issue_numbers(plan) == [1, 2, 3]
        # Ordering is load-bearing: apply_plan applies in plan order, and the
        # anchor's intake is what retires the investigations it supersedes.
        action_types = [a.action_type for a in plan.actions]
        last_queue = max(
            i for i, t in enumerate(action_types) if t == ActionType.QUEUE_TECH_LEAD
        )
        assert action_types.index(ActionType.CREATE_TECH_LEAD_ISSUE) > last_queue

    def test_storm_defers_to_investigations_when_anchor_already_open(self):
        """A not-due tick with an open anchor must not mint a second one.

        ``health_review_due=False`` alongside a populated
        ``existing_health_review_issue`` is exactly what the gatherer produces
        on a storm-only tick, because the storm — not just due-ness — arms the
        anchor scan. See
        ``TestFactGathererHealthReviewFacts::test_storm_arms_anchor_scan_when_interval_is_not_due``
        for the gatherer-driven half of this contract; this half asserts the
        planner honours the fact.
        """
        from issue_orchestrator.domain.models import TechLeadFacts

        planner, _ = self._planner()
        facts = TechLeadFacts(
            health_review_due=False, existing_health_review_issue=555
        )

        plan = planner.plan(
            make_snapshot(discovered_failures=self._cohort(), tech_lead_facts=facts)
        )

        # No anchor carries the cohort, so the individual investigations must
        # be queued instead of silently dropped.
        assert plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE) == []
        assert self._queued_tech_lead_issue_numbers(plan) == [1, 2, 3]

    def test_storm_defers_to_investigations_when_health_review_pending(self):
        planner, _ = self._planner()
        pending = [
            PendingTechLeadReview(
                issue_number=777,
                title="Health Review",
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            )
        ]

        plan = planner.plan(
            make_snapshot(
                discovered_failures=self._cohort(), pending_tech_lead=pending
            )
        )

        assert plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE) == []
        assert self._queued_tech_lead_issue_numbers(plan) == [1, 2, 3]

    def test_storm_defers_to_investigations_when_at_capacity(self):
        planner, _ = self._planner(max_concurrent=2)
        active = [make_session(make_issue(50)), make_session(make_issue(51))]

        plan = planner.plan(
            make_snapshot(
                discovered_failures=self._cohort(), active_sessions=active
            )
        )

        assert plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE) == []
        assert self._queued_tech_lead_issue_numbers(plan) == [1, 2, 3]

    def test_paused_storm_plans_nothing(self):
        """A paused tick plans no reactive actions: apply_plan refuses to apply
        anything while paused, so emitting fallback actions here would be dead
        code that reports work it never does. The cohort instead survives
        because clear_discovered_facts retains facts on a paused tick — see
        TestClearDiscoveredFacts and the paused end-to-end coverage in
        test_orchestrator_support (#6780)."""
        planner, _ = self._planner()

        plan = planner.plan(
            make_snapshot(discovered_failures=self._cohort(), paused=True)
        )

        assert plan.actions == ()

    def _cohort_with_one_already_queued(self):
        """Cohort where #1 is already a queued investigation and #2/#3 are
        freshly discovered — enough members to trip the storm threshold."""
        already = PendingTechLeadReview(
            issue_number=1,
            title="Investigate 1",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=1,
                issue_title="Problem 1",
                failure_reason="failed",
                observed_at=self.WINDOW_OBSERVED_AT,
            ),
        )
        discovered = self._cohort((2, 3))
        return already, discovered

    @staticmethod
    def _launched_tech_lead_issue_numbers(plan) -> list[int]:
        return sorted(
            a.number
            for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.TECH_LEAD
        )

    def test_escalated_storm_suppresses_a_member_investigation_launch(self):
        """When the cohort escalates, an already-queued member investigation is
        held back from launch (the anchor covers it, and intake removes it)."""
        planner, _ = self._planner()
        already, discovered = self._cohort_with_one_already_queued()

        plan = planner.plan(
            make_snapshot(
                discovered_failures=discovered, pending_tech_lead=[already]
            )
        )

        assert len(plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE)) == 1
        assert self._launched_tech_lead_issue_numbers(plan) == []

    def test_deferred_storm_does_not_suppress_a_member_investigation_launch(self):
        """When the cohort is deferred, the already-queued member investigation
        must be allowed to launch — the deferred storm suppresses nothing."""
        from issue_orchestrator.domain.models import TechLeadFacts

        planner, _ = self._planner()
        already, discovered = self._cohort_with_one_already_queued()
        facts = TechLeadFacts(
            health_review_due=False, existing_health_review_issue=555
        )

        plan = planner.plan(
            make_snapshot(
                discovered_failures=discovered,
                pending_tech_lead=[already],
                tech_lead_facts=facts,
            )
        )

        assert plan.actions_of_type(ActionType.CREATE_TECH_LEAD_ISSUE) == []
        assert self._launched_tech_lead_issue_numbers(plan) == [1]


class TestPlanDiscoveredReworks:
    """Tests for Planner's _plan_discovered_reworks method.

    This method processes DiscoveredRework facts from scans
    and produces QueueReworkAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_rework(self):
        """Planner produces QueueReworkAction for discovered reworks."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),  # Not already queued
        )

        plan = planner.plan(snapshot)

        # Should have a QueueReworkAction
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 1
        action = queue_actions[0]
        assert action.issue_number == 42
        assert action.rework_cycle == 2

    def test_removes_pr_pending_label_when_queueing_rework(self):
        """Planner removes pr-pending label so scheduler considers issue available."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        # Should have RemoveLabelAction for pr-pending BEFORE QueueReworkAction
        remove_actions = [a for a in plan.actions if a.action_type == ActionType.REMOVE_LABEL and a.label == "pr-pending"]
        assert len(remove_actions) == 1
        assert remove_actions[0].issue_number == 42

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 1

    def test_skips_already_queued_reworks(self):
        """Planner skips discovered reworks that are already in pending_reworks."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.domain.issue_key import GitHubIssueKey
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=1,
        )

        # Already queued in pending_reworks
        already_pending = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="42"),
            agent_type="agent:developer",
            rework_cycle=1,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(already_pending,),
        )

        plan = planner.plan(snapshot)

        # Should NOT have a QueueReworkAction for already-queued rework
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 0

    def test_post_publish_validation_rework_flips_pr_back_to_needs_rework(self):
        """Planner removes code-reviewed, adds needs-rework, and comments on the PR."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback=(
                "POST-PUBLISH VALIDATION FAILURE (address these issues):\n\n"
                "PR #100 is no longer ready to merge."
            ),
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        remove_reviewed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "code-reviewed"
        ]
        add_needs_rework = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and a.issue_number == 100
            and a.label == "needs-rework"
        ]
        comment_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_COMMENT
            and a.number == 100
            and a.is_pr
        ]
        queue_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.QUEUE_REWORK
        ]

        assert len(remove_reviewed) == 1
        assert len(add_needs_rework) == 1
        assert len(comment_actions) == 1
        assert "<!-- io:post-publish-validation -->" in comment_actions[0].comment
        assert "POST-PUBLISH VALIDATION FAILURE" in comment_actions[0].comment
        assert len(queue_actions) == 1
        assert queue_actions[0].source == "post_publish_validation"
        assert queue_actions[0].feedback == discovered.feedback

    def test_post_publish_recovery_clears_stale_needs_human(self):
        """When recovery routes a previously-escalated PR back to rework
        (clear_needs_human=True), the planner removes the stale needs-human
        label so the PR isn't left both queued-for-rework and human-flagged."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback="PR #100 failed checks; routing back to rework.",
            clear_needs_human=True,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        remove_needs_human = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "needs-human"
        ]
        assert len(remove_needs_human) == 1

    def test_post_publish_rework_without_recovery_keeps_needs_human_untouched(self):
        """The normal post-publish path (clear_needs_human=False) must not emit
        a needs-human removal."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback="PR #100 failed checks; routing back to rework.",
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        remove_needs_human = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "needs-human"
        ]
        assert remove_needs_human == []

    def test_post_publish_validation_rework_skips_duplicate_marker_comment(self):
        """When the marker comment already exists on the PR, the planner
        suppresses the duplicate comment but keeps the label flip and queue."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback="PR #100 is no longer ready to merge.",
            feedback_comment_already_posted=True,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        comment_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_COMMENT
            and a.number == 100
            and a.is_pr
        ]
        remove_reviewed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "code-reviewed"
        ]
        add_needs_rework = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and a.issue_number == 100
            and a.label == "needs-rework"
        ]
        queue_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.QUEUE_REWORK
        ]

        # Comment is deduped away...
        assert comment_actions == []
        # ...but the rest of the post-publish flip is unchanged.
        assert len(remove_reviewed) == 1
        assert len(add_needs_rework) == 1
        assert len(queue_actions) == 1
        assert queue_actions[0].feedback == discovered.feedback


class TestPlanDiscoveredEscalations:
    """Tests for Planner's _plan_discovered_escalations method.

    This method processes DiscoveredEscalation facts from scans
    and produces EscalateToHumanAction for the orchestrator to apply.
    """

    def test_plans_escalate_action_for_discovered_escalation(self):
        """Planner produces EscalateToHumanAction for discovered escalations."""
        from issue_orchestrator.domain.models import DiscoveredEscalation
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer", max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredEscalation(
            issue_number=42,
            pr_number=100,
            rework_cycle=3,  # Exceeded max of 2
        )

        snapshot = make_snapshot(
            discovered_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Should have an EscalateToHumanAction
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.rework_cycles == 3  # Passed through from scanner; ActionApplier subtracts 1 for display
        assert action.issue_key == "42"  # Falls back to str(issue_number) when no Issue in snapshot

    def test_discovered_escalation_uses_stable_issue_key(self):
        """Planner resolves stable issue_key from snapshot issues for discovered escalations."""
        from issue_orchestrator.domain.models import DiscoveredEscalation
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer", max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue = make_issue(number=42, title="[M0-721] Fix the widget")
        discovered = DiscoveredEscalation(
            issue_number=42,
            pr_number=100,
            rework_cycle=3,
        )

        snapshot = make_snapshot(
            issues=[issue],
            discovered_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        assert escalate_actions[0].issue_key == "M0-721"

    def test_no_escalate_actions_when_no_discovered_escalations(self):
        """Planner produces no escalate actions when no discovered escalations."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_escalations=(),  # Empty
        )

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 0


class TestPlanAwaitingMergeEscalations:
    """Tests for Planner._plan_awaiting_merge_escalations.

    Distinct from rework-cycle exhaustion: the PR was *approved* and
    is being escalated because either CI checks stalled or branch
    protection blocks merge in a way code rework can't unstick.
    """

    def test_checks_pending_timeout_emits_escalate_with_override_comment(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="checks_pending_timeout",
            reason="Required GitHub checks have been pending for ~31 minute(s) ...",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.escalation_reason == "post-publish: checks_pending_timeout"
        assert action.comment_override is not None
        assert "CI checks timed out" in action.comment_override
        # The legacy rework-cycles narrative must NOT appear in the override
        assert "rework cycles" not in action.comment_override.lower()

    def test_branch_protection_blocked_emits_escalate_with_protection_copy(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            code_review_agent="agent:reviewer",
            label_prefix="bot",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="branch_protection_blocked",
            reason="Branch protection blocks merge despite all required checks passing.",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.needs_human_label == "bot:needs-human"
        assert action.comment_override is not None
        assert "branch protection" in action.comment_override.lower()
        assert "`bot:needs-human`" in action.comment_override
        assert "`blocked-needs-human`" not in action.comment_override

    def test_status_rollup_permission_denied_emits_escalate_with_capability_copy(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="status_rollup_permission_denied",
            reason=(
                "PR #100 is reviewer-approved but its merge-readiness is "
                "'unstable' ... the configured GitHub token lacks permission "
                "to read check status (statusCheckRollup) ..."
            ),
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.escalation_reason == "post-publish: status_rollup_permission_denied"
        assert action.comment_override is not None
        assert "cannot read check status" in action.comment_override.lower()


class TestPlanDiscoveredFailures:
    """Tests for the planner's failure-investigation queueing.

    Exercised through the public ``plan()`` API: DiscoveredFailure facts from
    session completions produce QueueTechLeadAction for the orchestrator to apply.
    """

    def test_plans_tech_lead_action_for_discovered_failure(self):
        """Planner produces QueueTechLeadAction for discovered failures."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Should have a QueueTechLeadAction
        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 1
        action = tech_lead_actions[0]
        assert action.issue_number == 42
        assert "failed" in action.title
        # The typed failure context must ride the action across the plan/apply
        # boundary; discovered_failures is cleared after planning, so this is
        # the only path by which the launch-time board snapshot can contain
        # the investigation's own triggering failure.
        assert action.failure is discovered

    def test_no_tech_lead_action_when_disabled(self):
        """Planner produces no tech_lead actions when tech_lead_review_on_failure is disabled."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=False,  # Disabled
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 0

    def test_no_tech_lead_action_when_no_agent_configured(self):
        """Planner produces no tech_lead actions when no tech_lead_review_agent configured."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent=None,  # Not configured
            tech_lead_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 0

    def test_skips_already_queued_tech_lead(self):
        """Planner skips failures for issues already queued for tech_lead."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        # Already queued for tech_lead
        pending_tech_lead = PendingTechLeadReview(
            issue_number=42,
            title="Already queued",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=42, issue_title="Already queued", failure_reason="failed"
            ),
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
            pending_tech_lead=[pending_tech_lead],
        )

        plan = planner.plan(snapshot)

        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 0

    def test_no_tech_lead_actions_when_no_discovered_failures(self):
        """Planner produces no tech_lead actions when no discovered failures."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_failures=(),  # Empty
        )

        plan = planner.plan(snapshot)

        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 0


class TestPlanCleanups:
    """Tests for Planner's _plan_cleanups method.

    This method processes CleanupFacts from cleanup fact-gathering
    and produces CleanupSessionAction for the orchestrator to apply.
    """

    def test_plans_cleanup_action_for_reviewed_pr(self):
        """Planner produces CleanupSessionAction when PR has been reviewed."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Pending cleanup with PR #100
        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),  # PR #100 has been reviewed
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Should have a CleanupSessionAction
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1
        action = cleanup_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.terminal_id == "session-42"
        assert action.worktree_path == "/tmp/worktree-42"
        assert action.close_tabs is True
        assert action.remove_worktrees is True

    def test_no_cleanup_when_pr_not_reviewed(self):
        """Planner produces no CleanupSessionAction when PR is not reviewed."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Pending cleanup with PR #100, but PR #100 is NOT in reviewed set
        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({200, 300}),  # Different PRs reviewed
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Should NOT have a CleanupSessionAction
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 0

    def test_no_cleanup_when_no_facts(self):
        """Planner produces no CleanupSessionAction when cleanup_facts is None."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            cleanup_facts=None,  # No facts gathered
        )

        plan = planner.plan(snapshot)

        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 0

    def test_immediate_cleanup_held_for_failure_investigation(self):
        """Held immediate cleanups are skipped; unheld ones still plan (#6771 r3).

        A failed session's ImmediateCleanup lands in the same pass as its
        DiscoveredFailure, but the investigation launches a tick later —
        removing the worktree first deletes the artifact hints the
        investigation was queued to read. The Planner must apply NO removal
        for issues in CleanupFacts.held_issue_numbers while planning every
        other immediate cleanup normally.
        """
        from issue_orchestrator.domain.models import CleanupFacts, ImmediateCleanup
        from issue_orchestrator.control.actions import ActionType

        config = make_config(tech_lead_review_agent="agent:tech-lead")
        planner = Planner(config=config, scheduler=Scheduler(config))

        cleanup_facts = CleanupFacts(
            pending_cleanups=(),
            reviewed_pr_numbers=frozenset(),
            close_tabs=True,
            remove_worktrees=True,
            immediate_cleanups=(
                ImmediateCleanup(42, "issue-42", "/tmp/worktree-42", "failed"),
                ImmediateCleanup(7, "issue-7", "/tmp/worktree-7", "completed"),
            ),
            held_issue_numbers=frozenset({42}),
        )

        plan = planner.plan(make_snapshot(cleanup_facts=cleanup_facts))

        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert [a.issue_number for a in cleanup_actions] == [7], (
            "the held failed-session worktree must not be cleaned up while "
            "its failure investigation still references it"
        )

    def test_cleanup_respects_close_tabs_setting(self):
        """Planner respects the close_tabs setting from CleanupFacts."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),
            close_tabs=False,  # Don't close tabs
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1
        assert cleanup_actions[0].close_tabs is False
        assert cleanup_actions[0].remove_worktrees is True

    def test_scratch_worktree_cleanup_forces_removal_despite_config(self):
        """A disposable tech-lead-investigation scratch worktree is always removed
        on completion, even when the cleanup config keeps worktrees (#6823), so
        scratch workspaces never accumulate. A non-scratch cleanup still honors
        the config."""
        from issue_orchestrator.domain.models import CleanupFacts, ImmediateCleanup
        from issue_orchestrator.control.actions import ActionType

        config = make_config(tech_lead_review_agent="agent:tech-lead")
        planner = Planner(config=config, scheduler=Scheduler(config))

        cleanup_facts = CleanupFacts(
            pending_cleanups=(),
            reviewed_pr_numbers=frozenset(),
            close_tabs=True,
            remove_worktrees=False,  # Config: keep worktrees for inspection
            immediate_cleanups=(
                ImmediateCleanup(
                    5980,
                    "issue-5980",
                    "/tmp/repo-tech-lead-5980-abc",
                    "completed",
                    scratch_worktree=True,
                ),
                ImmediateCleanup(7, "issue-7", "/tmp/worktree-7", "completed"),
            ),
        )

        plan = planner.plan(make_snapshot(cleanup_facts=cleanup_facts))

        cleanup_actions = {
            a.issue_number: a
            for a in plan.actions
            if a.action_type == ActionType.CLEANUP_SESSION
        }
        # Scratch worktree removed unconditionally; ordinary cleanup honors config.
        assert cleanup_actions[5980].remove_worktrees is True
        assert cleanup_actions[7].remove_worktrees is False
        # F8: the disposable identity survives to the action so the applier can
        # FORCE removal only for the scratch worktree, never for the coding one.
        assert cleanup_actions[5980].disposable_worktree is True
        assert cleanup_actions[7].disposable_worktree is False


# =============================================================================
# BEHAVIOR-CENTRIC TESTS: Priority and Action Ordering
# =============================================================================

class TestFailureInvestigationCleanupLifecycle:
    """End-to-end lifecycle of the failure-investigation cleanup hold (#6771 r3).

    Drives the real FactGatherer + Planner + end-of-tick clear across ticks:
    failure discovery -> same-tick plan applies NO removal for the held
    worktree -> investigation queued/launched with readable hints -> after
    the investigation completes the deferred cleanup proceeds with removal.
    """

    def test_hold_spans_discovery_to_investigation_completion(self, tmp_path):
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.control.fact_gatherer import (
            FactGatherer,
            clear_discovered_facts,
        )
        from issue_orchestrator.control.session_routing import PendingSessionQueues
        from issue_orchestrator.domain.models import (
            ImmediateCleanup,
            OrchestratorState,
        )

        config = make_config(tech_lead_review_agent="agent:tech-lead")
        config.tech_lead_review_on_failure = True
        config.cleanup.with_tech_lead.remove_worktrees = True
        config.cleanup.with_tech_lead.close_ai_session_tabs = True

        gatherer = FactGatherer(config=config, repository_host=MagicMock())
        planner = Planner(config=config, scheduler=Scheduler(config))

        worktree = tmp_path / "worktree-42"
        hint = (
            worktree / ".issue-orchestrator" / "sessions" / "run__issue-42"
            / "failure-diagnostic.json"
        )
        hint.parent.mkdir(parents=True)
        hint.write_text("{}")

        state = OrchestratorState()
        failure = DiscoveredFailure(
            issue_number=42,
            issue_title="Broken thing",
            failure_reason="failed",
            artifact_hints=(str(hint),),
        )
        state.record_discovered_failure(failure)
        state.immediate_cleanups.extend(
            [ImmediateCleanup(42, "issue-42", str(worktree), "failed")]
        )

        def plan_tick():
            facts = gatherer.gather_cleanup_facts(state)
            snapshot = make_snapshot(
                active_sessions=list(state.active_sessions),
                pending_tech_lead=list(state.pending_tech_lead_reviews),
                discovered_failures=tuple(state.discovered_failures),
                cleanup_facts=facts,
            )
            return planner.plan(snapshot)

        def cleanup_actions(plan):
            return [
                a for a in plan.actions
                if a.action_type == ActionType.CLEANUP_SESSION
            ]

        # Tick 1 — discovery: tech_lead is queued AND no removal is applied for
        # the held worktree even though remove_worktrees is configured true.
        plan = plan_tick()
        queue_actions = [
            a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD
        ]
        assert len(queue_actions) == 1
        assert cleanup_actions(plan) == []
        # The post-apply seam queues the investigation, then the tick clear.
        PendingSessionQueues(state).queue_failure_investigation(
            42, "Investigate: Broken thing (failed)", failure=failure
        )
        clear_discovered_facts(state, config, tick_paused=False)
        assert [c.issue_number for c in state.immediate_cleanups] == [42], (
            "the held cleanup must survive the end-of-tick fact clear"
        )
        assert state.discovered_failures == []

        # Tick 2 — investigation queued: still held; queued hints readable.
        plan = plan_tick()
        assert cleanup_actions(plan) == []
        queued = state.pending_tech_lead_reviews[0]
        assert queued.failure is not None
        assert all(Path(h).exists() for h in queued.failure.artifact_hints), (
            "the investigation must launch with readable artifact hints"
        )
        clear_discovered_facts(state, config, tick_paused=False)
        assert [c.issue_number for c in state.immediate_cleanups] == [42]

        # Tick 3 — investigation active: launch consumed the queue item and
        # registered the tech_lead session; the hold follows the session.
        PendingSessionQueues(state).remove_tech_lead(42)
        state.active_sessions.append(
            make_session(make_issue(42, labels=["agent:tech-lead"]))
        )
        plan = plan_tick()
        assert cleanup_actions(plan) == []
        clear_discovered_facts(state, config, tick_paused=False)
        assert [c.issue_number for c in state.immediate_cleanups] == [42]

        # Tick 4 — investigation completed: the hold releases by
        # re-evaluation and the deferred cleanup proceeds with removal.
        state.active_sessions.clear()
        plan = plan_tick()
        assert [
            (a.issue_number, a.worktree_path, a.remove_worktrees)
            for a in cleanup_actions(plan)
        ] == [(42, str(worktree), True)]
        clear_discovered_facts(state, config, tick_paused=False)
        assert state.immediate_cleanups == []


class TestStormCohortCleanupLifecycle:
    """End-to-end lifecycle of the STORM-COHORT cleanup hold (#6780).

    A storm collapses the per-issue failure investigations into one health-
    review anchor, so from that moment nothing in the queue is keyed by the
    members' issue numbers — the cohort is. Holding only failure
    investigations therefore let the members' worktrees be removed before the
    review could read them, while their ``artifact_hints`` still pointed at
    the deleted paths.

    Drives the real intake owner + FactGatherer + Planner + end-of-tick clear
    across the anchor's whole life: collapse -> pending -> active -> done.
    """

    def test_collapsed_cohort_is_held_until_the_health_review_ends(self, tmp_path):
        from issue_orchestrator.control.actions import (
            ActionType,
            CreateTechLeadIssueAction,
        )
        from issue_orchestrator.control.fact_gatherer import (
            FactGatherer,
            clear_discovered_facts,
        )
        from issue_orchestrator.control.health_review_trigger import (
            intake_created_tech_lead_anchor,
        )
        from issue_orchestrator.control.session_routing import PendingSessionQueues
        from issue_orchestrator.domain.models import (
            ImmediateCleanup,
            OrchestratorState,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        config = make_config(tech_lead_review_agent="agent:tech-lead")
        config.tech_lead_review_on_failure = True
        config.cleanup.with_tech_lead.remove_worktrees = True
        config.tech_lead.health_review.storm_threshold = 3
        config.tech_lead.health_review.storm_window_minutes = 5

        authority = InMemoryTechLeadAuthorityStore()
        gatherer = FactGatherer(
            config=config,
            repository_host=MagicMock(),
            tech_lead_authority=authority,
        )
        planner = Planner(config=config, scheduler=Scheduler(config))
        state = OrchestratorState()

        members = (41, 42, 43)
        worktrees: dict[int, Path] = {}
        cohort: list[DiscoveredFailure] = []
        for number in members:
            worktree = tmp_path / f"worktree-{number}"
            hint = worktree / "failure-diagnostic.json"
            hint.parent.mkdir(parents=True)
            hint.write_text("{}")
            worktrees[number] = worktree
            failure = DiscoveredFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="failed",
                artifact_hints=(str(hint),),
                observed_at=1_000.0,
            )
            cohort.append(failure)
            state.record_discovered_failure(failure)
            state.immediate_cleanups.append(
                ImmediateCleanup(number, f"issue-{number}", str(worktree), "failed")
            )

        def plan_tick():
            facts = gatherer.gather_cleanup_facts(state)
            return planner.plan(
                make_snapshot(
                    active_sessions=list(state.active_sessions),
                    pending_tech_lead=list(state.pending_tech_lead_reviews),
                    discovered_failures=tuple(state.discovered_failures),
                    cleanup_facts=facts,
                )
            )

        def cleanup_numbers(plan) -> list[int]:
            return sorted(
                a.issue_number
                for a in plan.actions
                if a.action_type == ActionType.CLEANUP_SESSION
            )

        # Tick 1 — the storm escalates. The real intake owner collapses the
        # investigations into the anchor's cohort, exactly as the post-apply
        # seam does for a successful CreateTechLeadIssueAction.
        queues = PendingSessionQueues(state)
        for failure in cohort:
            queues.queue_failure_investigation(
                failure.issue_number,
                f"Investigate {failure.issue_number}",
                failure=failure,
            )
        intake_created_tech_lead_anchor(
            CreateTechLeadIssueAction(
                title="Health Review — walk the floor",
                body="Problem storm",
                labels=("agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL),
                pr_count=0,
                storm_problems=tuple(cohort),
            ),
            999,
            state,
            None,
            authority,
        )
        clear_discovered_facts(state, config, authority, tick_paused=False)

        assert [t.issue_number for t in state.pending_tech_lead_reviews] == [999], (
            "the collapse must leave exactly the anchor queued"
        )
        assert sorted(c.issue_number for c in state.immediate_cleanups) == [
            41,
            42,
            43,
        ], "the collapsed cohort's cleanups must survive the end-of-tick clear"

        # Tick 2 — anchor PENDING launch: the cohort holds every member's
        # worktree, and the hints the review will read are still on disk.
        assert cleanup_numbers(plan_tick()) == []
        clear_discovered_facts(state, config, authority, tick_paused=False)
        (queued,) = state.pending_tech_lead_reviews
        assert all(
            Path(hint).exists()
            for problem in queued.problem_cohort
            for hint in problem.artifact_hints
        ), "the health review must launch with readable artifact hints"

        # Tick 3 — anchor ACTIVE: launch consumed the queue item, so the
        # durable cohort ledger is the only thing still naming these
        # artifacts. The hold must follow the running review.
        queues.remove_tech_lead(999)
        state.active_sessions.append(
            make_session(make_issue(999, labels=["agent:tech-lead"]))
        )
        assert cleanup_numbers(plan_tick()) == []
        clear_discovered_facts(state, config, authority, tick_paused=False)
        assert sorted(c.issue_number for c in state.immediate_cleanups) == [
            41,
            42,
            43,
        ]

        # Tick 4 — review completed: the retention owner discarded the cohort
        # row and the session is gone, so the hold releases by re-evaluation
        # and every member's worktree is finally removed.
        authority.discard_storm_cohort(anchor_issue_number=999)
        state.active_sessions.clear()
        plan = plan_tick()
        assert cleanup_numbers(plan) == [41, 42, 43]
        assert all(
            a.remove_worktrees
            for a in plan.actions
            if a.action_type == ActionType.CLEANUP_SESSION
        )
        clear_discovered_facts(state, config, authority, tick_paused=False)
        assert state.immediate_cleanups == []

    def test_inert_cohort_row_does_not_hold_cleanup_forever(self, tmp_path):
        """A row whose anchor is neither pending nor active grants no hold.

        The ledger is intersected with live tech_lead work precisely so that a
        row leaked by an anchor that never reached completion (dropped after
        exhausted launch retries) cannot strand a worktree forever.
        """
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.control.fact_gatherer import FactGatherer
        from issue_orchestrator.domain.models import (
            ImmediateCleanup,
            OrchestratorState,
        )
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        config = make_config(tech_lead_review_agent="agent:tech-lead")
        config.tech_lead_review_on_failure = True
        config.cleanup.with_tech_lead.remove_worktrees = True

        authority = InMemoryTechLeadAuthorityStore()
        authority.record_storm_cohort(
            anchor_issue_number=999,
            cohort=(DiscoveredFailure(41, "Problem 41", "failed"),),
        )
        gatherer = FactGatherer(
            config=config,
            repository_host=MagicMock(),
            tech_lead_authority=authority,
        )
        planner = Planner(config=config, scheduler=Scheduler(config))

        state = OrchestratorState()
        state.immediate_cleanups.append(
            ImmediateCleanup(41, "issue-41", str(tmp_path / "worktree-41"), "failed")
        )

        plan = planner.plan(
            make_snapshot(cleanup_facts=gatherer.gather_cleanup_facts(state))
        )

        assert [
            a.issue_number
            for a in plan.actions
            if a.action_type == ActionType.CLEANUP_SESSION
        ] == [41]


class TestActionPriority:
    """Tests for action priority: Reviews > Reworks > Tech Lead > Issues.

    The planner enforces a strict priority order to ensure completed work
    (PRs waiting for review) is processed before starting new work.
    """

    def test_reviews_take_priority_over_issues(self):
        """Reviews are launched before issues when both are available."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)

        # Mock review workflow to return a review
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
        ]
        mock_review_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        # Both issues and reviews are available
        snapshot = make_snapshot(
            issues=[make_issue(2)],  # Issue waiting
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch review, NOT issue (only 1 slot available)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.REVIEW
        assert launch_actions[0].number == 100

    def test_reworks_take_priority_over_tech_lead(self):
        """Reworks are launched before tech_lead when both are available."""
        from tests.conftest import MockEventSink

        config = make_config(
            code_review_agent="agent:reviewer",
            tech_lead_review_agent="agent:tech-lead",
            max_concurrent_sessions=1,
        )
        scheduler = Scheduler(config)

        # Mock rework workflow to return a rework
        mock_rework_workflow = Mock()
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="1"),
            agent_type="agent:developer",
            rework_cycle=1,
        )
        mock_decision.reworks_to_launch = [pending_rework]
        mock_rework_workflow.should_launch_reworks.return_value = mock_decision
        mock_rework_workflow.should_escalate.return_value = Mock(should_escalate=False)

        # Mock tech_lead workflow (should not be called if reworks consume capacity)
        mock_tech_lead_workflow = Mock()
        mock_tech_lead_workflow.is_configured.return_value = True

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=mock_rework_workflow,
            tech_lead_workflow=mock_tech_lead_workflow,
        )

        snapshot = make_snapshot(
            pending_reworks=[pending_rework],
            pending_tech_lead=[
                PendingTechLeadReview(
                    issue_number=2,
                    title="Investigate failure",
                    flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                    failure=DiscoveredFailure(
                        issue_number=2,
                        issue_title="Investigate failure",
                        failure_reason="failed",
                    ),
                ),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch rework (priority over tech_lead)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.REWORK

    def test_issues_launch_when_pending_reviews_exist_but_no_review_launches(self):
        """Pending reviews should not starve issue work when review launches are skipped."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=3)
        scheduler = Scheduler(config)

        # Mock review workflow that doesn't launch (e.g., all reviews in progress)
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=False, skip_reason="Already reviewing"
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="10"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=10),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch issues because no review launches were started this tick
        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        assert len(issue_actions) == 2

    def test_reviews_get_priority_but_remaining_capacity_goes_to_issues(self):
        """Reviews consume capacity first, remaining slots go to new issues."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=3)
        scheduler = Scheduler(config)

        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="10"),
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            _issue_number=10,
        )
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=[pending_review],
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[pending_review],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REVIEW
        ]
        # Review gets 1 slot, remaining 2 slots go to issues
        assert len(review_actions) == 1
        assert len(issue_actions) == 2

    def test_no_issues_when_reviews_exhaust_capacity(self):
        """When reviews consume all capacity, no issues are launched."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)

        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="10"),
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            _issue_number=10,
        )
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=[pending_review],
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1)],
            pending_reviews=[pending_review],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REVIEW
        ]
        # Review takes the only slot, no room for issues
        assert len(review_actions) == 1
        assert len(issue_actions) == 0

    def test_issues_launched_when_no_pending_work(self):
        """New issues are launched when no reviews, reworks, or tech_lead are pending."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[],
            pending_reworks=[],
            pending_tech_lead=[],
        )

        plan = planner.plan(snapshot)

        # Should launch issues since nothing else is pending
        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        assert len(issue_actions) == 2


class TestEdgeCases:
    """Tests for edge cases and unusual input combinations."""

    def test_empty_snapshot_produces_empty_plan(self):
        """A completely empty snapshot produces no actions."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[],
            active_sessions=[],
            pending_reviews=[],
            pending_reworks=[],
            pending_tech_lead=[],
            discovered_reviews=(),
            discovered_reworks=(),
            discovered_escalations=(),
            discovered_failures=(),
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0
        assert len(plan.skipped) == 0

    def test_multiple_discovered_reviews_all_queued(self):
        """Multiple discovered reviews all produce queue actions."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = [
            DiscoveredReview(issue_number=1, pr_number=101, pr_url="url1", branch_name="branch1"),
            DiscoveredReview(issue_number=2, pr_number=102, pr_url="url2", branch_name="branch2"),
            DiscoveredReview(issue_number=3, pr_number=103, pr_url="url3", branch_name="branch3"),
        ]

        snapshot = make_snapshot(discovered_reviews=tuple(discovered))

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 3

        # Also produces AddLabel actions for pr-pending
        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 3

    def test_discovered_review_without_code_review_agent_only_adds_label(self):
        """Discovered review without code_review_agent configured only adds pr-pending label."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent=None)  # No review agent
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(discovered_reviews=(discovered,))

        plan = planner.plan(snapshot)

        # Should have AddLabelAction for pr-pending but NO QueueReviewAction
        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 1

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_conflicting_signals_pending_review_and_discovered_same_pr(self):
        """When a PR is both pending and discovered, don't re-queue."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Same PR in both discovered and pending
        pr_number = 100
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=pr_number,
            pr_url="url",
            branch_name="branch",
        )
        pending = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=pr_number,
            pr_url="url",
            branch_name="branch",
            _issue_number=42,
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=[pending],
        )

        plan = planner.plan(snapshot)

        # Should NOT re-queue since already pending
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_issue_in_discovered_reviews_excluded_from_launch(self):
        """Issues with discovered reviews are filtered out from new work launches.

        The planner filters issues that appear in discovered_reviews or discovered_reworks
        from being launched, even if they're in the available issues list.
        """
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=5)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue 42 has a discovered review
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        # Both issues in available list, but issue 42 has discovered review
        snapshot = make_snapshot(
            issues=[make_issue(42), make_issue(43)],
            discovered_reviews=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Issue 42 should NOT be launched (has pending review in discovered_reviews)
        # Issue 43 CAN be launched if no other pending work
        # But wait - discovered_reviews produces QueueReviewAction, which means
        # pending_reviews will have an item AFTER the action is applied.
        # The exclusion happens via issues_with_pending_reviews check in _plan_issues

        issue_launches = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]

        # Issue 42 is excluded (in discovered_reviews)
        # Issue 43 could be launched, but check what numbers actually launched
        launched_numbers = {a.number for a in issue_launches}
        assert 42 not in launched_numbers, "Issue 42 should not be launched (has discovered review)"

        # Issue 43 may or may not launch depending on whether pending_reviews
        # blocks new issues - but the discovered_reviews ARE converted to
        # pending_reviews indirectly via queue actions. The check is:
        # has_pending_work looks at snapshot.pending_reviews (which is empty here),
        # so issue 43 could still launch since the queue action hasn't been applied yet.
        # The key invariant is: issue 42 must not be launched.

    def test_trace_queue_decision_logs_session_history_skip(self, caplog: pytest.LogCaptureFixture):
        """Planner emits an explicit trace-queue decision for session_history skips."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(4057, title="Issue 4057")
        snapshot = make_snapshot(
            issues=[issue],
            session_history_issue_numbers=frozenset({4057}),
        )

        with caplog.at_level(logging.INFO):
            planner.plan(snapshot)

        assert "trace-queue-decision issue=4057 decision=skip reason=session_history" in caplog.text

    def test_trace_queue_decision_logs_blocking_label_detail(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """Planner traces name the blocking labels instead of only blocked_label."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(
            520,
            title="Stack successor",
            labels=["blocked-cross-milestone", "agent:backend"],
        )
        snapshot = make_snapshot(issues=[issue])

        with caplog.at_level(logging.INFO):
            planner.plan(snapshot)

        assert (
            "trace-queue-decision issue=520 decision=skip reason=blocked_label "
            "detail=blocking labels: blocked-cross-milestone (Cross-milestone dep)"
        ) in caplog.text

    def test_trace_queue_decision_logs_only_when_reason_changes(self, caplog: pytest.LogCaptureFixture):
        """Queue decision traces are emitted on change, not every tick."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(4057, title="Issue 4057")
        snapshot = make_snapshot(
            issues=[issue],
            session_history_issue_numbers=frozenset({4057}),
        )

        with caplog.at_level(logging.INFO):
            planner.plan(snapshot)
            first_count = caplog.text.count("trace-queue-decision issue=4057 decision=skip reason=session_history")
            planner.plan(snapshot)
            second_count = caplog.text.count("trace-queue-decision issue=4057 decision=skip reason=session_history")

        assert first_count == 1
        assert second_count == 1

    def test_trace_queue_decision_logs_when_blocking_label_detail_changes(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """A changed blocking label emits a new trace even with the same reason."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        with caplog.at_level(logging.INFO):
            planner.plan(make_snapshot(issues=[
                make_issue(514, labels=["publish-failed"]),
            ]))
            planner.plan(make_snapshot(issues=[
                make_issue(514, labels=["blocked-cross-milestone"]),
            ]))

        assert caplog.text.count(
            "trace-queue-decision issue=514 decision=skip reason=blocked_label"
        ) == 2
        assert "detail=blocking labels: publish-failed (Publishing failed)" in caplog.text
        assert (
            "detail=blocking labels: blocked-cross-milestone "
            "(Cross-milestone dep)"
        ) in caplog.text

    def test_multiple_escalations_all_produce_actions(self):
        """Multiple discovered escalations all produce escalate actions."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        escalations = [
            DiscoveredEscalation(issue_number=1, pr_number=101, rework_cycle=3),
            DiscoveredEscalation(issue_number=2, pr_number=102, rework_cycle=4),
        ]

        snapshot = make_snapshot(discovered_escalations=tuple(escalations))

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 2
        assert {a.issue_number for a in escalate_actions} == {1, 2}

    def test_max_capacity_reached_mid_planning(self):
        """Actions respect capacity even when multiple types compete."""
        from tests.conftest import MockEventSink

        config = make_config(
            code_review_agent="agent:reviewer",
            max_concurrent_sessions=2,
        )
        scheduler = Scheduler(config)

        # Mock review workflow returning 3 reviews
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        reviews = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=101, pr_url="u1", branch_name="b1", _issue_number=1),
            PendingReview(issue_key=FakeIssueKey(name="2"), pr_number=102, pr_url="u2", branch_name="b2", _issue_number=2),
            PendingReview(issue_key=FakeIssueKey(name="3"), pr_number=103, pr_url="u3", branch_name="b3", _issue_number=3),
        ]
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=reviews,
        )

        # Mock rework workflow returning 2 reworks
        mock_rework_workflow = Mock()
        reworks = [
            PendingRework(issue_key=FakeIssueKey(name="10"), agent_type="agent:dev", rework_cycle=1),
            PendingRework(issue_key=FakeIssueKey(name="11"), agent_type="agent:dev", rework_cycle=1),
        ]
        mock_rework_workflow.should_launch_reworks.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reworks_to_launch=reworks,
        )
        mock_rework_workflow.should_escalate.return_value = Mock(should_escalate=False)

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
            rework_workflow=mock_rework_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=reviews,
            pending_reworks=reworks,
        )

        plan = planner.plan(snapshot)

        # Should launch exactly 2 sessions (max capacity), all reviews (higher priority)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 2
        assert all(a.session_type == SessionType.REVIEW for a in launch_actions)


class TestPlanQueueActionsOnlyPhase:
    """Tests that queue actions (Phase 1) happen even at capacity.

    The planner has two phases:
    - Phase 1: Queue population (AddLabel, QueueReview, etc.) - always runs
    - Phase 2: Session launches - only when capacity available
    """

    def test_queue_actions_produced_at_capacity(self):
        """Queue actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        # New review discovered
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[active_session],  # At capacity
            discovered_reviews=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Queue actions should still be produced
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1

        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 1

        # But no launch actions (at capacity)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 0

    def test_escalation_actions_produced_at_capacity(self):
        """Escalation actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_concurrent_sessions=1, max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        escalation = DiscoveredEscalation(issue_number=42, pr_number=100, rework_cycle=3)

        snapshot = make_snapshot(
            active_sessions=[active_session],
            discovered_escalations=(escalation,),
        )

        plan = planner.plan(snapshot)

        # Escalation action should still be produced
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1

    def test_tech_lead_queue_actions_produced_at_capacity(self):
        """Tech Lead queue actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredFailure

        config = make_config(
            max_concurrent_sessions=1,
            tech_lead_review_agent="agent:tech-lead",
            tech_lead_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        failure = DiscoveredFailure(
            issue_number=42,
            issue_title="Failed issue",
            failure_reason="timed_out",
        )

        snapshot = make_snapshot(
            active_sessions=[active_session],
            discovered_failures=(failure,),
        )

        plan = planner.plan(snapshot)

        # Tech Lead queue action should still be produced
        tech_lead_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TECH_LEAD]
        assert len(tech_lead_actions) == 1

    def test_cleanup_actions_produced_at_capacity(self):
        """Cleanup actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import CleanupFacts

        config = make_config(max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            active_sessions=[active_session],
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Cleanup action should still be produced
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1


class TestReworkEscalationWithinReworkPlanning:
    """Tests for escalation detection during rework planning phase."""

    def test_rework_escalates_when_max_cycles_exceeded(self):
        """Rework is escalated instead of launched when max cycles exceeded."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReworkWorkflow

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)

        # Create real rework workflow with mock event sink
        events = MockEventSink()
        rework_workflow = ReworkWorkflow(config=config, events=events)

        # Pending rework at cycle 3 (exceeds max of 2)
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:developer",
            rework_cycle=3,
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=rework_workflow,
        )

        snapshot = make_snapshot(pending_reworks=[pending_rework])

        plan = planner.plan(snapshot)

        # Should escalate, not launch
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        launch_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REWORK
        ]

        assert len(escalate_actions) == 1
        assert len(launch_actions) == 0
        assert escalate_actions[0].issue_number == 42

    def test_rework_launches_when_under_max_cycles(self):
        """Rework is launched when under max cycles."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReworkWorkflow

        config = make_config(max_rework_cycles=3)
        scheduler = Scheduler(config)

        events = MockEventSink()
        rework_workflow = ReworkWorkflow(config=config, events=events)

        # Pending rework at cycle 2 (under max of 3)
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:developer",
            rework_cycle=2,
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=rework_workflow,
        )

        snapshot = make_snapshot(pending_reworks=[pending_rework])

        plan = planner.plan(snapshot)

        # Should launch, not escalate
        launch_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REWORK
        ]
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]

        assert len(launch_actions) == 1
        assert len(escalate_actions) == 0
        assert launch_actions[0].number == 42


class TestSnapshotFromState:
    """Tests for OrchestratorSnapshot.from_state factory method."""

    def test_snapshot_from_state_captures_all_fields(self):
        """Snapshot correctly captures all state fields."""
        from issue_orchestrator.domain.models import (
            OrchestratorState,
            DiscoveredReview,
            DiscoveredRework,
            DiscoveredEscalation,
            DiscoveredFailure,
            TechLeadFacts,
            CleanupFacts,
        )

        state = OrchestratorState()
        issue = make_issue(1)
        session = make_session(issue)
        state.active_sessions = [session]
        state.paused = True
        state.priority_queue = [1, 2, 3]
        state.issues_started_count = 5

        review = PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="b", _issue_number=1)
        state.pending_reviews = [review]

        rework = PendingRework(issue_key=FakeIssueKey(name="2"), agent_type="agent:dev", rework_cycle=1)
        state.pending_reworks = [rework]

        tech_lead = PendingTechLeadReview(
            issue_number=3, title="Tech Lead", flavor=TechLeadSessionFlavor.BATCH_REVIEW
        )
        state.pending_tech_lead_reviews = [tech_lead]
        validation_retry = PendingValidationRetry(
            issue_number=4,
            issue_title="Retry",
            agent_label="agent:dev",
            worktree_path="/tmp/repo-4",
            branch_name="4-retry",
            original_prompt="original",
            validation_error="dirty",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state.pending_validation_retries = [validation_retry]

        discovered_review = DiscoveredReview(issue_number=10, pr_number=110, pr_url="url", branch_name="b")
        discovered_awaiting_merge_reconciliation = DiscoveredAwaitingMergeReconciliation(
            issue_number=10,
            pr_number=110,
            pr_url="url",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
        )
        discovered_awaiting_merge_drift = DiscoveredAwaitingMergeDrift(
            issue_number=10,
            pr_number=110,
            pr_url="url",
            status_reason="PR closed; issue remains open",
        )
        discovered_rework = DiscoveredRework(issue_number=11, pr_number=111, branch_name="b", agent_type="a", rework_cycle=1)
        discovered_escalation = DiscoveredEscalation(issue_number=12, pr_number=112, rework_cycle=5)
        discovered_failure = DiscoveredFailure(issue_number=13, issue_title="F", failure_reason="failed")
        tech_lead_facts = TechLeadFacts(pr_count=2, threshold=3)
        cleanup_facts = CleanupFacts(pending_cleanups=(), reviewed_pr_numbers=frozenset())

        snapshot = OrchestratorSnapshot.from_state(
            issues=[issue],
            state=state,
            max_issues_to_start=10,
            discovered_reviews=[discovered_review],
            discovered_awaiting_merge_reconciliations=[
                discovered_awaiting_merge_reconciliation
            ],
            discovered_awaiting_merge_drifts=[
                discovered_awaiting_merge_drift
            ],
            discovered_reworks=[discovered_rework],
            discovered_escalations=[discovered_escalation],
            discovered_failures=[discovered_failure],
            tech_lead_facts=tech_lead_facts,
            cleanup_facts=cleanup_facts,
        )

        assert len(snapshot.issues) == 1
        assert len(snapshot.active_sessions) == 1
        assert snapshot.paused is True
        assert snapshot.priority_queue == (1, 2, 3)
        assert snapshot.issues_started_count == 5
        assert snapshot.max_issues_to_start == 10
        assert len(snapshot.pending_reviews) == 1
        assert len(snapshot.pending_reworks) == 1
        assert len(snapshot.pending_tech_lead) == 1
        assert len(snapshot.pending_validation_retries) == 1
        assert len(snapshot.discovered_reviews) == 1
        assert len(snapshot.discovered_awaiting_merge_reconciliations) == 1
        assert len(snapshot.discovered_awaiting_merge_drifts) == 1
        assert len(snapshot.discovered_reworks) == 1
        assert len(snapshot.discovered_escalations) == 1
        assert len(snapshot.discovered_failures) == 1
        assert snapshot.tech_lead_facts is not None
        assert snapshot.cleanup_facts is not None


class TestActionReasonMessages:
    """Tests that action reason messages are informative."""

    def test_queue_review_action_has_descriptive_reason(self):
        """QueueReviewAction includes PR number in reason."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        snapshot = make_snapshot(discovered_reviews=(discovered,))
        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        assert "#100" in queue_actions[0].reason

    def test_escalation_action_has_descriptive_reason(self):
        """EscalateToHumanAction includes cycle count in reason."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        escalation = DiscoveredEscalation(issue_number=42, pr_number=100, rework_cycle=3)
        snapshot = make_snapshot(discovered_escalations=(escalation,))
        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        # Reason should mention cycles
        assert "2" in escalate_actions[0].reason  # rework_cycle - 1

    def test_launch_session_action_has_priority_reason(self):
        """LaunchSessionAction includes priority info in reason."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue = make_issue(42, title="[P1-001] High priority task", milestone="M1")
        snapshot = make_snapshot(issues=[issue])
        plan = planner.plan(snapshot)

        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        # Reason should include priority and milestone info
        reason = launch_actions[0].reason
        assert "milestone=M1" in reason or "P1" in reason


class TestMultiplePendingTypesInteraction:
    """Tests for interactions when multiple pending types exist simultaneously."""

    def test_pending_work_gets_priority_remaining_capacity_goes_to_issues(self):
        """Pending review/tech_lead consume slots first, remaining capacity goes to issues."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReviewWorkflow, TechLeadWorkflow

        config = make_config(
            code_review_agent="agent:reviewer",
            tech_lead_review_agent="agent:tech-lead",
            max_concurrent_sessions=5,
        )
        scheduler = Scheduler(config)
        events = MockEventSink()
        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=ReviewWorkflow(config=config, events=events),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=events),
        )

        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="100"),
            pr_number=200,
            pr_url="https://example.test/pull/200",
            branch_name="issue-100",
            _issue_number=100,
        )
        pending_tech_lead = PendingTechLeadReview(
            issue_number=101,
            title="Investigate",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=101, issue_title="Investigate", failure_reason="failed"
            ),
        )

        snapshot = make_snapshot(
            issues=[make_issue(100), make_issue(1), make_issue(2)],
            pending_reviews=[pending_review],
            pending_tech_lead=[pending_tech_lead],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_tech_lead_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type in {SessionType.REVIEW, SessionType.TECH_LEAD}
        ]
        # Review + tech_lead get 2 slots, issue 100 excluded (has pending review),
        # issues 1 and 2 get the remaining 3 slots
        assert len(review_tech_lead_actions) == 2
        assert len(issue_actions) == 2
        assert {a.number for a in issue_actions} == {1, 2}

    def test_capacity_shared_across_all_types(self):
        """Capacity is correctly shared when multiple types launch."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import (
            ReviewWorkflow,
            ReworkWorkflow,
            TechLeadWorkflow,
        )

        config = make_config(
            code_review_agent="agent:reviewer",
            tech_lead_review_agent="agent:tech-lead",
            max_concurrent_sessions=3,
        )
        scheduler = Scheduler(config)
        events = MockEventSink()

        review_workflow = ReviewWorkflow(config=config, events=events)
        rework_workflow = ReworkWorkflow(config=config, events=events)
        tech_lead_workflow = TechLeadWorkflow(config=config, events=events)

        # 1 review, 1 rework, 1 tech_lead, 3 issues - only 3 capacity
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=101,
            pr_url="url",
            branch_name="b",
            _issue_number=1,
        )
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="2"),
            agent_type="agent:dev",
            rework_cycle=1,
        )
        pending_tech_lead = PendingTechLeadReview(
            issue_number=3,
            title="Investigate",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=3, issue_title="Investigate", failure_reason="failed"
            ),
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=review_workflow,
            rework_workflow=rework_workflow,
            tech_lead_workflow=tech_lead_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(4), make_issue(5), make_issue(6)],
            pending_reviews=[pending_review],
            pending_reworks=[pending_rework],
            pending_tech_lead=[pending_tech_lead],
        )

        plan = planner.plan(snapshot)

        # Should have exactly 3 launch actions total
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 3

        # Priority order: review first, then rework, then tech_lead
        types = [a.session_type for a in launch_actions]
        assert types[0] == SessionType.REVIEW
        assert types[1] == SessionType.REWORK
        assert types[2] == SessionType.TECH_LEAD

        # No issue launches (pending work exists)
        issue_launches = [a for a in launch_actions if a.session_type == SessionType.ISSUE]
        assert len(issue_launches) == 0


class TestReservedTechLeadConcurrency:
    """tech_lead.max_concurrent: a reserved additive budget for the tech lead.

    None (default) = tech_lead shares the worker budget (unchanged). An int = a
    separate additive budget so the tech lead runs even when workers saturate
    ``max_concurrent_sessions``.
    """

    def _planner(self, config) -> Planner:
        return Planner(
            config=config,
            scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=InMemoryEventSink()),
        )

    def _pending_tech_lead(self, number: int = 101) -> PendingTechLeadReview:
        return PendingTechLeadReview(
            issue_number=number,
            title="Investigate",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=number, issue_title="Investigate", failure_reason="failed"
            ),
        )

    def _tech_lead_session(self, number: int, agent_label: str) -> Session:
        """An active tech_lead session is one whose agent_label is the tech lead agent."""
        session = make_session(make_issue(number, labels=[agent_label]))
        session.agent_label = agent_label
        return session

    def _tech_lead_launches(self, plan) -> list:
        return [
            a
            for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.TECH_LEAD
        ]

    def test_reserved_slot_launches_tech_lead_at_worker_saturation(self):
        """Worker budget full (shared capacity 0), tech_lead still launches from
        its own reserved slot."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1
        planner = self._planner(config)

        # One long coding session saturates the single worker slot.
        worker = make_session(make_issue(1))
        snapshot = make_snapshot(
            issues=[make_issue(1)],
            active_sessions=[worker],
            pending_tech_lead=[self._pending_tech_lead(101)],
        )

        launches = self._tech_lead_launches(planner.plan(snapshot))
        assert [a.number for a in launches] == [101]

    def test_shared_budget_none_skips_tech_lead_at_saturation(self):
        """None (default): tech_lead shares the worker budget, so a full worker
        budget means no tech_lead launches - unchanged behavior."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        assert config.tech_lead.max_concurrent is None
        planner = self._planner(config)

        worker = make_session(make_issue(1))
        snapshot = make_snapshot(
            issues=[make_issue(1)],
            active_sessions=[worker],
            pending_tech_lead=[self._pending_tech_lead(101)],
        )

        assert self._tech_lead_launches(planner.plan(snapshot)) == []

    def _issue_launches(self, plan) -> list:
        return [
            a
            for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]

    def test_active_tech_lead_session_does_not_reduce_worker_capacity_when_reserved(self):
        """Additive budget: a running tech_lead session neither consumes worker
        capacity nor counts against max_concurrent_sessions, so a worker still
        launches into the free worker slot."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1
        planner = self._planner(config)

        # One active tech_lead session; a fresh worker issue is pending.
        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )

        launches = self._issue_launches(planner.plan(snapshot))
        assert [a.number for a in launches] == [2]

    def test_active_tech_lead_session_consumes_shared_budget_when_none(self):
        """None (default): a tech_lead session shares the worker budget, so it DOES
        occupy the single worker slot and no worker launches - unchanged."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        planner = self._planner(config)

        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )

        assert self._issue_launches(planner.plan(snapshot)) == []

    def test_reserved_budget_bounds_concurrent_tech_lead_launches(self):
        """A reserved budget of 1 launches at most one tech_lead even with two
        pending, and the second is skipped."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1
        planner = self._planner(config)

        worker = make_session(make_issue(1))
        snapshot = make_snapshot(
            issues=[make_issue(1)],
            active_sessions=[worker],
            pending_tech_lead=[self._pending_tech_lead(101), self._pending_tech_lead(102)],
        )

        launches = self._tech_lead_launches(planner.plan(snapshot))
        assert len(launches) == 1


class TestStuckSweepEscalation:
    """R1 (#6824): stuck-sweep-exhausted issues escalate to needs-human through
    the Planner/Applier — an authoritative, LABEL-ONLY write (no comment, which
    could not be made crash-safe), not a direct GitHub call from observation."""

    def test_escalation_is_label_only(self):
        # R1 (#6824): the authoritative escalation is the idempotent needs-human
        # LABEL only — no comment (which could not be made retry-safe/durable).
        config = make_config()
        planner = Planner(config=config, scheduler=Scheduler(config))
        needs_human = planner._lm.needs_human  # noqa: SLF001

        plan = planner.plan(make_snapshot(stuck_sweep_escalations=(777,)))

        labels = [
            a for a in plan.actions
            if isinstance(a, AddLabelAction) and a.issue_number == 777
        ]
        assert [a.label for a in labels] == [needs_human]
        # No comment is emitted for the escalation (label is authoritative).
        assert not [
            a for a in plan.actions
            if isinstance(a, AddCommentAction) and a.number == 777
        ]

    def test_no_escalations_when_buffer_empty(self):
        config = make_config()
        planner = Planner(config=config, scheduler=Scheduler(config))
        plan = planner.plan(make_snapshot(stuck_sweep_escalations=()))
        assert not [
            a for a in plan.actions
            if isinstance(a, AddLabelAction) and a.label == planner._lm.needs_human  # noqa: SLF001
        ]


class TestReservedTechLeadDoesNotStealWorkerReviewCapacity:
    """F5: reserved tech_lead concurrency must not steal worker review/rework/
    retrospective capacity. The worker workflows gate on the owner-computed
    WORKER-only active count, not raw ``snapshot.active_count`` — so an active
    reserved-tech-lead session leaves the worker slot free. Real workflows are
    wired so the internal capacity gate actually runs.
    """

    def _tech_lead_session(self, number: int, agent_label: str) -> Session:
        session = make_session(make_issue(number, labels=[agent_label]))
        session.agent_label = agent_label
        return session

    def _config(self, *, reserved: bool):
        config = make_config(
            tech_lead_review_agent="agent:tech-lead",
            code_review_agent="agent:reviewer",
            max_concurrent_sessions=1,
            retrospective_review_enabled=True,
        )
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        if reserved:
            config.tech_lead.max_concurrent = 1
        return config

    def _pending_review(self) -> PendingReview:
        return PendingReview(
            issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url",
            branch_name="branch", _issue_number=1,
        )

    def _pending_rework(self) -> PendingRework:
        return PendingRework(
            issue_key=FakeIssueKey(name="2"), agent_type="agent:fixer",
            rework_cycle=1, issue_number=2,
        )

    def _pending_retrospective(self) -> PendingRetrospectiveReview:
        return PendingRetrospectiveReview(
            issue_key=FakeIssueKey(name="3"), issue_number=3,
            issue_title="Review existing work", agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )

    def _launches_of(self, plan, session_type):
        return [
            a
            for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == session_type
        ]

    def test_review_launches_despite_active_reserved_tech_lead(self):
        config = self._config(reserved=True)
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            review_workflow=ReviewWorkflow(config, InMemoryEventSink()),
        )
        snapshot = make_snapshot(
            pending_reviews=[self._pending_review()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        launches = self._launches_of(planner.plan(snapshot), SessionType.REVIEW)
        assert [a.number for a in launches] == [100]

    def test_review_shares_budget_and_skips_when_not_reserved(self):
        config = self._config(reserved=False)
        assert config.tech_lead.max_concurrent is None
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            review_workflow=ReviewWorkflow(config, InMemoryEventSink()),
        )
        snapshot = make_snapshot(
            pending_reviews=[self._pending_review()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        # Shared budget: the tech_lead session occupies the one worker slot — no
        # review launches (unchanged behavior).
        assert self._launches_of(planner.plan(snapshot), SessionType.REVIEW) == []

    def test_rework_launches_despite_active_reserved_tech_lead(self):
        config = self._config(reserved=True)
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            rework_workflow=ReworkWorkflow(config, InMemoryEventSink()),
        )
        snapshot = make_snapshot(
            pending_reworks=[self._pending_rework()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        launches = self._launches_of(planner.plan(snapshot), SessionType.REWORK)
        assert [a.number for a in launches] == [2]

    def test_rework_shares_budget_and_skips_when_not_reserved(self):
        config = self._config(reserved=False)
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            rework_workflow=ReworkWorkflow(config, InMemoryEventSink()),
        )
        snapshot = make_snapshot(
            pending_reworks=[self._pending_rework()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        assert self._launches_of(planner.plan(snapshot), SessionType.REWORK) == []

    def test_retrospective_launches_despite_active_reserved_tech_lead(self):
        config = self._config(reserved=True)
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            retrospective_review_workflow=RetrospectiveReviewWorkflow(
                config, InMemoryEventSink()
            ),
        )
        snapshot = make_snapshot(
            pending_retrospective_reviews=[self._pending_retrospective()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        launches = self._launches_of(
            planner.plan(snapshot), SessionType.RETROSPECTIVE_REVIEW
        )
        assert [a.number for a in launches] == [3]

    def test_retrospective_shares_budget_and_skips_when_not_reserved(self):
        config = self._config(reserved=False)
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            retrospective_review_workflow=RetrospectiveReviewWorkflow(
                config, InMemoryEventSink()
            ),
        )
        snapshot = make_snapshot(
            pending_retrospective_reviews=[self._pending_retrospective()],
            active_sessions=[self._tech_lead_session(50, "agent:tech-lead")],
        )
        assert self._launches_of(
            planner.plan(snapshot), SessionType.RETROSPECTIVE_REVIEW
        ) == []


class TestE2EFirstClassWorkload:
    """e2e.occupies_session_slot: E2E as a first-class WORKER workload.

    OFF (default) leaves every capacity path byte-for-byte unchanged. ON, a
    running E2E occupies one worker slot (worker capacity -1), and a due suite
    reserves a worker slot AFTER completion work but BEFORE new issues — beating
    new issues yet never preempting reviews/reworks/tech_lead. It is charged to the
    worker budget, never the reserved tech_lead slot.
    """

    def _plain_planner(self, config) -> Planner:
        return Planner(config=config, scheduler=Scheduler(config))

    def _launches(self, plan, session_type) -> list:
        return [
            a
            for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == session_type
        ]

    def _tech_lead_session(self, number: int, agent_label: str) -> Session:
        session = make_session(make_issue(number, labels=[agent_label]))
        session.agent_label = agent_label
        return session

    def _review_workflow(self):
        wf = Mock()
        wf.is_configured.return_value = True
        decision = Mock()
        decision.should_launch = True
        decision.skip_reason = None
        decision.reviews_to_launch = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url",
                branch_name="b", _issue_number=1, agent_label=None,
            )
        ]
        wf.should_launch_reviews.return_value = decision
        return wf

    # ---- OFF path: byte-for-byte unchanged (observed via plan()) ----

    def test_off_shared_budget_fills_every_worker_slot(self):
        """Flag off (snapshot defaults): with one active session and three
        available issues the two remaining worker slots BOTH launch — the
        shared-budget capacity math is unchanged."""
        config = make_config(max_concurrent_sessions=3)
        planner = self._plain_planner(config)
        snap = make_snapshot(
            issues=[make_issue(2), make_issue(3), make_issue(4)],
            active_sessions=[make_session(make_issue(1))],
        )
        assert snap.e2e_occupies_slot is False
        assert snap.e2e_due is False
        assert len(self._launches(planner.plan(snap), SessionType.ISSUE)) == 2

    def test_off_reserved_budget_additive_tech_lead_unchanged(self):
        """Flag off with a reserved tech_lead budget is unchanged: an active
        tech_lead session stays additive (worker capacity 2 launches both issues)
        while the reserved budget still admits one more tech_lead launch."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=3
        )
        config.tech_lead.max_concurrent = 2
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=InMemoryEventSink()),
        )
        pending = PendingTechLeadReview(
            issue_number=101, title="Investigate",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=101, issue_title="Investigate", failure_reason="failed"
            ),
        )
        snap = make_snapshot(
            issues=[make_issue(2), make_issue(3)],
            active_sessions=[
                make_session(make_issue(1)),
                self._tech_lead_session(50, "agent:tech-lead"),
            ],
            pending_tech_lead=[pending],
        )
        plan = planner.plan(snap)
        # worker capacity = 3 - (2 active - 1 tech_lead) = 2 → both issues launch;
        # reserved = 2 - 1 active tech_lead = 1 → one more tech_lead launches.
        assert len(self._launches(plan, SessionType.ISSUE)) == 2
        assert len(self._launches(plan, SessionType.TECH_LEAD)) == 1

    def test_off_new_issue_launches_normally(self):
        config = make_config(max_concurrent_sessions=1)
        planner = self._plain_planner(config)
        snap = make_snapshot(issues=[make_issue(2)])
        assert [a.number for a in self._launches(planner.plan(snap), SessionType.ISSUE)] == [2]

    # ---- E2E running: occupies a worker slot ----

    def test_running_reduces_worker_capacity_by_one(self):
        """max=3, three available issues, an E2E run holding one slot → only
        two agents launch (worker capacity dropped by 1)."""
        config = make_config(max_concurrent_sessions=3)
        planner = self._plain_planner(config)
        snap = make_snapshot(
            issues=[make_issue(1), make_issue(2), make_issue(3)],
            e2e_occupies_slot=True,
        )
        assert len(self._launches(planner.plan(snap), SessionType.ISSUE)) == 2

    def test_running_launches_one_fewer_issue(self):
        """Two slots, two issues, an E2E run holding one → only one agent."""
        config = make_config(max_concurrent_sessions=2)
        planner = self._plain_planner(config)
        snap = make_snapshot(
            issues=[make_issue(1), make_issue(2)], e2e_occupies_slot=True
        )
        assert len(self._launches(planner.plan(snap), SessionType.ISSUE)) == 1

    def test_running_charges_worker_budget_not_reserved_tech_lead(self):
        """The single worker slot is held by E2E, yet the tech lead still
        launches from its own reserved slot: E2E is charged to the worker
        budget, NOT the tech_lead reserved slot."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1
        planner = Planner(
            config=config,
            scheduler=Scheduler(config),
            tech_lead_workflow=TechLeadWorkflow(config=config, events=InMemoryEventSink()),
        )
        pending = PendingTechLeadReview(
            issue_number=101, title="Investigate",
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            failure=DiscoveredFailure(
                issue_number=101, issue_title="Investigate", failure_reason="failed"
            ),
        )
        snap = make_snapshot(
            issues=[make_issue(1)], pending_tech_lead=[pending], e2e_occupies_slot=True
        )
        plan = planner.plan(snap)
        assert [a.number for a in self._launches(plan, SessionType.TECH_LEAD)] == [101]
        assert self._launches(plan, SessionType.ISSUE) == []

    # ---- E2E due: reservation (ahead of issues, behind completion work) ----

    def test_due_reserves_the_only_slot_ahead_of_new_issue(self):
        config = make_config(max_concurrent_sessions=1)
        planner = self._plain_planner(config)
        snap = make_snapshot(issues=[make_issue(2)], e2e_due=True)
        # The lone worker slot is held back for the due suite → no issue.
        assert self._launches(planner.plan(snap), SessionType.ISSUE) == []

    def test_due_never_steals_a_slot_from_completion_work(self):
        """The due reservation is applied AFTER completion work, so it never
        reduces the capacity reviews draw from: max=2 with two pending reviews
        and a due suite still launches BOTH reviews."""
        config = make_config(
            code_review_agent="agent:reviewer", max_concurrent_sessions=2
        )
        wf = Mock()
        wf.is_configured.return_value = True
        decision = Mock()
        decision.should_launch = True
        decision.skip_reason = None
        decision.reviews_to_launch = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="u",
                branch_name="b", _issue_number=1, agent_label=None,
            ),
            PendingReview(
                issue_key=FakeIssueKey(name="2"), pr_number=200, pr_url="u",
                branch_name="b", _issue_number=2, agent_label=None,
            ),
        ]
        wf.should_launch_reviews.return_value = decision
        planner = Planner(
            config=config, scheduler=Scheduler(config), review_workflow=wf
        )
        snap = make_snapshot(
            pending_reviews=list(decision.reviews_to_launch), e2e_due=True
        )
        plan = planner.plan(snap)
        assert len(self._launches(plan, SessionType.REVIEW)) == 2

    def test_due_yields_to_in_flight_review_but_beats_new_issue(self):
        """max=3, one pending review + two new issues + due suite: the review
        launches (E2E never preempts it), the suite reserves the next slot, and
        only ONE of the two new issues launches (E2E beat the other)."""
        config = make_config(
            code_review_agent="agent:reviewer", max_concurrent_sessions=3
        )
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            review_workflow=self._review_workflow(),
        )
        snap = make_snapshot(
            issues=[make_issue(2), make_issue(3)],
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url",
                    branch_name="b", _issue_number=1,
                )
            ],
            e2e_due=True,
        )
        plan = planner.plan(snap)
        assert len(self._launches(plan, SessionType.REVIEW)) == 1
        assert len(self._launches(plan, SessionType.ISSUE)) == 1

    def test_baseline_without_due_launches_both_new_issues(self):
        """Same board without the due reservation launches BOTH new issues —
        proving the missing issue above is the reserved E2E slot, not a fluke."""
        config = make_config(
            code_review_agent="agent:reviewer", max_concurrent_sessions=3
        )
        planner = Planner(
            config=config, scheduler=Scheduler(config),
            review_workflow=self._review_workflow(),
        )
        snap = make_snapshot(
            issues=[make_issue(2), make_issue(3)],
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url",
                    branch_name="b", _issue_number=1,
                )
            ],
        )
        plan = planner.plan(snap)
        assert len(self._launches(plan, SessionType.REVIEW)) == 1
        assert len(self._launches(plan, SessionType.ISSUE)) == 2


class TestPlanStaleInProgressCleanup:
    """Tests for planner's stale in-progress label cleanup.

    When an issue has the in-progress label but no active session exists,
    the planner should generate a RemoveLabelAction to clean up the stale label.
    """

    def test_no_stale_issues_no_actions(self):
        """When no stale in-progress issues, no cleanup actions are generated."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # No stale issues
        snapshot = make_snapshot(
            issues=[make_issue(1)],
            stale_in_progress_issues=(),  # Empty
        )

        plan = planner.plan(snapshot)

        # Should be no RemoveLabel actions
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 0

    def test_stale_issue_generates_remove_label_action(self):
        """Stale in-progress issue generates RemoveLabelAction."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
        )

        plan = planner.plan(snapshot)

        # Should have a RemoveLabel action for issue #1
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 1
        assert remove_actions[0].issue_number == 1
        assert remove_actions[0].label == "in-progress"
        assert "stale" in remove_actions[0].reason.lower()

    def test_multiple_stale_issues_generate_multiple_actions(self):
        """Multiple stale issues generate multiple RemoveLabelActions."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issues = [
            make_issue(1, labels=["in-progress"]),
            make_issue(2, labels=["in-progress"]),
            make_issue(3, labels=["in-progress"]),
        ]

        snapshot = make_snapshot(
            issues=stale_issues,
            stale_in_progress_issues=tuple(stale_issues),
        )

        plan = planner.plan(snapshot)

        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 3

        # Verify all issue numbers are covered
        issue_numbers = {a.issue_number for a in remove_actions}
        assert issue_numbers == {1, 2, 3}

    def test_stale_cleanup_runs_when_paused(self):
        """When orchestrator is paused, no stale cleanup actions are generated."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
            paused=True,  # Orchestrator is paused
        )

        plan = planner.plan(snapshot)

        # Paused orchestrator returns empty plan
        assert plan.action_count == 0

    def test_stale_cleanup_is_phase_1_action(self):
        """Stale cleanup happens in Phase 1 (queue population), before capacity check."""
        config = make_config(max_concurrent_sessions=0)  # No capacity
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
        )

        plan = planner.plan(snapshot)

        # Even with no capacity, stale cleanup should happen (Phase 1)
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 1

    def test_stale_cleanup_with_active_session_not_stale(self):
        """Issue with active session is NOT in stale_in_progress_issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue has active session - NOT stale
        issue_with_session = make_issue(1, labels=["in-progress"])
        session = make_session(issue_with_session)

        snapshot = make_snapshot(
            issues=[issue_with_session],
            active_sessions=[session],
            stale_in_progress_issues=(),  # Not stale - has session
        )

        plan = planner.plan(snapshot)

        # No cleanup actions
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 0


class TestMergeQueueEnqueuePlanning:
    """Discovered merge-queue facts become EnqueueToMergeQueueActions."""

    def test_enqueue_fact_becomes_enqueue_action(self):
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_merge_queue_enqueues=[
                DiscoveredMergeQueueEnqueue(
                    issue_number=228,
                    pr_number=318,
                    pr_url="https://github.com/owner/repo/pull/318",
                    issue_key="M1-228",
                )
            ],
        )

        plan = planner.plan(snapshot)

        actions = plan.actions_of_type(ActionType.ENQUEUE_TO_MERGE_QUEUE)
        assert len(actions) == 1
        assert actions[0].pr_number == 318
        assert actions[0].issue_number == 228
        assert actions[0].issue_key == "M1-228"

    def test_no_enqueue_actions_without_facts(self):
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        plan = planner.plan(make_snapshot())

        assert plan.actions_of_type(ActionType.ENQUEUE_TO_MERGE_QUEUE) == []
