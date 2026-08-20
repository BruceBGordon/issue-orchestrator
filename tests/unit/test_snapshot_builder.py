"""Unit tests for SnapshotBuilder."""

from unittest.mock import MagicMock, Mock, call

import pytest

from issue_orchestrator.control.snapshot_builder import SnapshotBuilder, _select_primary_pr
from issue_orchestrator.domain.models import Issue, OrchestratorState, Session, AgentConfig
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.unit.session_run_helpers import make_session_run_assets
from issue_orchestrator.domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseState,
)


def _paused_state() -> PauseState:
    """An operator-paused state — the pause_state equivalent of the old ``paused=True``."""
    return PauseState.paused_now(
        reason=PauseReason.OPERATOR, actor=PauseActor.CONTROL_API
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.agents = {"agent:web": Mock()}
    config.filtering.label = None
    config.filtering.milestone = None
    config.filtering.milestones = []
    config.filtering.fetch_limit = 100
    return config


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost with default behaviors."""
    host = MagicMock()
    host.list_issues.return_value = []
    host.get_prs_for_issue.return_value = []
    return host


@pytest.fixture
def builder(mock_config, mock_repository_host):
    """Create a SnapshotBuilder instance for testing."""
    return SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)


def make_issue(number: int, title: str = None, labels: list[str] = None, state: str = "open") -> Issue:
    """Helper to create test issues."""
    return Issue(
        number=number,
        title=title or f"Issue {number}",
        labels=["agent:web"] if labels is None else labels,
        state=state,
    )


def make_pr(
    number: int,
    state: str = "open",
    labels: list[str] = None,
    branch: str = None,
) -> PRInfo:
    """Helper to create test PRInfo objects."""
    return PRInfo(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/test/repo/pull/{number}",
        branch=branch or f"feature-{number}",
        body="",
        state=state,
        labels=labels or [],
    )


# =============================================================================
# Tests for build_snapshot - Basic Behavior
# =============================================================================


class TestBuildSnapshotBasic:
    """Tests for build_snapshot core functionality."""

    def test_build_snapshot_returns_correct_structure(self, builder, mock_repository_host):
        """Verify snapshot has expected top-level keys."""
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=42,
            last_tick_id=10,
        )

        assert snapshot["snapshot_id"] == 42
        assert "orchestrator" in snapshot
        assert "issues" in snapshot
        assert snapshot["orchestrator"]["last_tick_id"] == 10

    def test_build_snapshot_empty_state_is_idle(self, builder, mock_repository_host):
        """Empty orchestrator state should report as idle."""
        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        assert snapshot["orchestrator"]["idle"] is True
        assert snapshot["orchestrator"]["paused"] is False

    def test_build_snapshot_paused_state(self, builder, mock_repository_host):
        """Paused state is reflected in snapshot."""
        state = OrchestratorState(pause_state=_paused_state())

        snapshot = builder.build_snapshot(state=state, snapshot_id=1, last_tick_id=None)

        assert snapshot["orchestrator"]["paused"] is True


# =============================================================================
# Tests for Idle State Calculation
# =============================================================================


class TestIdleStateCalculation:
    """Tests for determining when orchestrator is idle."""

    def test_not_idle_when_active_sessions_exist(self, builder, mock_repository_host, tmp_path):
        """Having active sessions means not idle."""
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]
        agent_config = AgentConfig(prompt_path=tmp_path / "prompt.md")

        session_key = SessionKey(issue=FakeIssueKey(name="1"), task=TaskKind.CODE)
        session = Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config,
            terminal_id="test-session",
            worktree_path=tmp_path / "worktree",
            branch_name="1-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="test-session",
            ),
        )
        state = OrchestratorState(active_sessions=[session])

        snapshot = builder.build_snapshot(state=state, snapshot_id=1, last_tick_id=None)

        assert snapshot["orchestrator"]["idle"] is False

    def test_not_idle_when_pending_reviews_exist(self, builder, mock_repository_host):
        """Having pending reviews means not idle."""
        from issue_orchestrator.domain.models import PendingReview

        review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="1-feature",
            _issue_number=1,
        )
        state = OrchestratorState(pending_reviews=[review])

        snapshot = builder.build_snapshot(state=state, snapshot_id=1, last_tick_id=None)

        assert snapshot["orchestrator"]["idle"] is False

    def test_not_idle_when_pending_reworks_exist(self, builder, mock_repository_host):
        """Having pending reworks means not idle."""
        from issue_orchestrator.domain.models import PendingRework

        rework = PendingRework(
            issue_key=FakeIssueKey(name="1"),
            agent_type="agent:web",
            rework_cycle=1,
        )
        state = OrchestratorState(pending_reworks=[rework])

        snapshot = builder.build_snapshot(state=state, snapshot_id=1, last_tick_id=None)

        assert snapshot["orchestrator"]["idle"] is False

    def test_not_idle_when_pending_tech_lead_reviews_exist(self, builder, mock_repository_host):
        """Having pending tech_lead reviews means not idle."""
        from issue_orchestrator.domain.models import PendingTechLeadReview
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        tech_lead = PendingTechLeadReview(
            issue_number=999,
            title="Tech Lead review",
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        )
        state = OrchestratorState(pending_tech_lead_reviews=[tech_lead])

        snapshot = builder.build_snapshot(state=state, snapshot_id=1, last_tick_id=None)

        assert snapshot["orchestrator"]["idle"] is False


# =============================================================================
# Tests for Issue Fetching (covers lines 64-69, 73-86)
# =============================================================================


class TestIssueFetching:
    """Tests for _fetch_issues behavior."""

    def test_fetch_issues_with_filter_label(self, mock_config, mock_repository_host):
        """When filtering.label is set, it's included in the label query."""
        mock_config.filtering.label = "e2e-test"
        builder = SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]

        builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        # Should include filtering.label in labels list along with agent label
        mock_repository_host.list_issues.assert_called_once_with(
            labels=["e2e-test", "agent:web"],
            milestone=None,
            limit=100,
        )

    def test_fetch_issues_without_milestones_uses_none(self, mock_config, mock_repository_host):
        """When no milestones configured, uses [None] for single query."""
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        builder = SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]

        builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        # milestone=None means no milestone filter
        mock_repository_host.list_issues.assert_called_once_with(
            labels=["agent:web"],
            milestone=None,
            limit=100,
        )

    def test_fetch_issues_multiple_milestones_dedupes(self, mock_config, mock_repository_host):
        """Fetch across milestones without duplicating issues."""
        mock_config.filtering.milestones = ["M1", "M2"]
        builder = SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)

        issue_1 = make_issue(1)
        issue_2 = make_issue(2)
        # Issue 1 appears in both milestones
        mock_repository_host.list_issues.side_effect = [
            [issue_1],
            [issue_1, issue_2],
        ]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=2,
        )

        # Should dedupe: only 2 unique issues
        assert set(snapshot["issues"].keys()) == {"1", "2"}
        assert mock_repository_host.list_issues.call_args_list == [
            call(labels=["agent:web"], milestone="M1", limit=100),
            call(labels=["agent:web"], milestone="M2", limit=100),
        ]

    def test_fetch_issues_multiple_agents(self, mock_config, mock_repository_host):
        """Each agent type triggers a separate query."""
        mock_config.agents = {"agent:web": Mock(), "agent:api": Mock()}
        builder = SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)

        issue_1 = make_issue(1, labels=["agent:web"])
        issue_2 = make_issue(2, labels=["agent:api"])
        mock_repository_host.list_issues.side_effect = [[issue_1], [issue_2]]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        assert len(snapshot["issues"]) == 2
        assert mock_repository_host.list_issues.call_count == 2


# =============================================================================
# Tests for Issue View Population
# =============================================================================


class TestIssueViewPopulation:
    """Tests for how issues are represented in the snapshot."""

    def test_issue_view_contains_expected_fields(self, builder, mock_repository_host):
        """Each issue view has expected fields."""
        issue = make_issue(42, labels=["agent:web", "priority:high"], state="open")
        mock_repository_host.list_issues.return_value = [issue]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        issue_view = snapshot["issues"]["42"]
        assert issue_view["labels"] == ["agent:web", "priority:high"]
        assert issue_view["state"] == "open"
        assert issue_view["apply_attempts"] == 0
        assert issue_view["reconcile_required"] == 0
        assert "pr" in issue_view


# =============================================================================
# Tests for PR View (_get_pr_view - covers lines 89-108)
# =============================================================================


class TestPRView:
    """Tests for _get_pr_view behavior."""

    def test_pr_view_when_no_prs_exist(self, builder, mock_repository_host):
        """When no PRs for issue, returns empty pr view."""
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]
        mock_repository_host.get_prs_for_issue.return_value = []

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        pr_view = snapshot["issues"]["1"]["pr"]
        assert pr_view["number"] is None
        assert pr_view["draft"] is None
        assert pr_view["labels"] == []

    def test_pr_view_when_pr_exists(self, builder, mock_repository_host):
        """When PR exists, pr view contains PR info."""
        issue = make_issue(1)
        pr = make_pr(100, state="open", labels=["needs-review"], branch="1-feature")
        mock_repository_host.list_issues.return_value = [issue]
        mock_repository_host.get_prs_for_issue.return_value = [pr]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        pr_view = snapshot["issues"]["1"]["pr"]
        assert pr_view["number"] == 100
        assert pr_view["draft"] is None  # Not fetched
        assert pr_view["labels"] == ["needs-review"]

    def test_pr_view_when_get_prs_raises_exception(self, builder, mock_repository_host):
        """When get_prs_for_issue raises, returns empty pr view."""
        issue = make_issue(1)
        mock_repository_host.list_issues.return_value = [issue]
        mock_repository_host.get_prs_for_issue.side_effect = Exception("API error")

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        # Should gracefully return empty PR view
        pr_view = snapshot["issues"]["1"]["pr"]
        assert pr_view["number"] is None
        assert pr_view["draft"] is None
        assert pr_view["labels"] == []


# =============================================================================
# Tests for _select_primary_pr (covers lines 111-116)
# =============================================================================


class TestSelectPrimaryPR:
    """Tests for _select_primary_pr function."""

    def test_select_primary_pr_prefers_open(self):
        """Open PRs take priority over merged and closed."""
        open_pr = make_pr(1, state="open")
        merged_pr = make_pr(2, state="merged")
        closed_pr = make_pr(3, state="closed")

        result = _select_primary_pr([closed_pr, merged_pr, open_pr])

        assert result.number == 1
        assert result.state == "open"

    def test_select_primary_pr_prefers_merged_over_closed(self):
        """Merged PRs take priority over closed."""
        merged_pr = make_pr(1, state="merged")
        closed_pr = make_pr(2, state="closed")

        result = _select_primary_pr([closed_pr, merged_pr])

        assert result.number == 1
        assert result.state == "merged"

    def test_select_primary_pr_closed_when_only_option(self):
        """Closed PR is selected when no open or merged."""
        closed_pr = make_pr(1, state="closed")

        result = _select_primary_pr([closed_pr])

        assert result.number == 1
        assert result.state == "closed"

    def test_select_primary_pr_first_of_same_state(self):
        """When multiple PRs have same priority state, first one wins."""
        pr1 = make_pr(1, state="open")
        pr2 = make_pr(2, state="open")

        result = _select_primary_pr([pr1, pr2])

        assert result.number == 1

    def test_select_primary_pr_unknown_state_falls_back_to_first(self):
        """Unknown states fall through to first PR in list."""
        unknown_pr1 = make_pr(1, state="draft")
        unknown_pr2 = make_pr(2, state="pending")

        result = _select_primary_pr([unknown_pr1, unknown_pr2])

        # Neither "draft" nor "pending" match open/merged/closed
        # So falls back to first PR
        assert result.number == 1

    def test_select_primary_pr_mixed_states_order(self):
        """Complex scenario with many PRs in different states."""
        prs = [
            make_pr(1, state="closed"),
            make_pr(2, state="draft"),  # unknown
            make_pr(3, state="merged"),
            make_pr(4, state="open"),
            make_pr(5, state="closed"),
        ]

        result = _select_primary_pr(prs)

        # Should prefer the open one
        assert result.number == 4
        assert result.state == "open"


# =============================================================================
# Tests for Edge Cases
# =============================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_issues_list(self, builder, mock_repository_host):
        """Snapshot works with no issues."""
        mock_repository_host.list_issues.return_value = []

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        assert snapshot["issues"] == {}

    def test_last_tick_id_none(self, builder, mock_repository_host):
        """last_tick_id can be None."""
        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        assert snapshot["orchestrator"]["last_tick_id"] is None

    def test_issue_key_used_as_dict_key(self, builder, mock_repository_host):
        """Issue's stable_id is used as the dictionary key."""
        issue = make_issue(42)
        mock_repository_host.list_issues.return_value = [issue]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        # Uses issue.key.stable_id() which for Issue is the number as string
        assert "42" in snapshot["issues"]

    def test_issue_with_empty_labels(self, builder, mock_repository_host):
        """Issues with empty labels work correctly."""
        issue = make_issue(1, labels=[])
        mock_repository_host.list_issues.return_value = [issue]

        snapshot = builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        assert snapshot["issues"]["1"]["labels"] == []

    def test_filter_label_combined_with_agent_label(self, mock_config, mock_repository_host):
        """filtering.label is combined with agent label in queries."""
        mock_config.filtering.label = "test-data"
        mock_config.agents = {"agent:dev": Mock()}
        builder = SnapshotBuilder(config=mock_config, repository_host=mock_repository_host)

        builder.build_snapshot(
            state=OrchestratorState(),
            snapshot_id=1,
            last_tick_id=None,
        )

        mock_repository_host.list_issues.assert_called_with(
            labels=["test-data", "agent:dev"],
            milestone=None,
            limit=100,
        )
