"""Unit tests for FactGatherer."""

import pytest

from issue_orchestrator.domain.pause_state import PauseState
from tests.conftest import operator_paused_state
from unittest.mock import MagicMock, Mock, call
from pathlib import Path

from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    Session,
    AgentConfig,
    PendingReview,
    PendingRework,
    PendingCleanup,
    PendingTechLeadReview,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredFailure,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.ports import PRInfo
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.session_run_helpers import make_session_run_assets


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.filtering.max_to_start = 5
    config.tech_lead_review_agent = None
    config.tech_lead_review_threshold = 0
    config.tech_lead_review_label = None
    config.code_review_agent = None
    config.code_reviewed_label = None
    config.tech_lead_reviewed_label = None
    return config


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    host = MagicMock()
    host.list_issues.return_value = []
    host.get_prs_with_label.return_value = []
    return host


@pytest.fixture
def sample_issues():
    """Create sample issues for testing."""
    return [
        Issue(number=1, title="Issue 1", labels=["agent:web"]),
        Issue(number=2, title="Issue 2", labels=["agent:web"]),
    ]


@pytest.fixture
def sample_state():
    """Create a sample orchestrator state."""
    return OrchestratorState()


@pytest.fixture
def fact_gatherer(mock_config, mock_repository_host):
    """Create a FactGatherer instance."""
    return FactGatherer(config=mock_config, repository_host=mock_repository_host)


class TestFactGathererE2ESlotFacts:
    """The E2E worker-slot facts are threaded from the injected reader into the
    snapshot only when e2e.occupies_session_slot is on (observation boundary)."""

    def test_flag_off_leaves_facts_false_without_calling_reader(
        self, mock_config, mock_repository_host, sample_state, sample_issues
    ):
        assert mock_config.e2e.occupies_session_slot is False
        reader = Mock()
        gatherer = FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            e2e_slot_reader=reader,
        )

        snapshot = gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.e2e_occupies_slot is False
        assert snapshot.e2e_due is False
        reader.assert_not_called()

    def test_running_signal_sets_occupies_slot(
        self, mock_config, mock_repository_host, sample_state, sample_issues
    ):
        from issue_orchestrator.control.planner_types import E2ESlotSignals

        mock_config.e2e.occupies_session_slot = True
        gatherer = FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            e2e_slot_reader=lambda: E2ESlotSignals(occupies_slot=True, due=False),
        )

        snapshot = gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.e2e_occupies_slot is True
        assert snapshot.e2e_due is False

    def test_due_signal_sets_e2e_due(
        self, mock_config, mock_repository_host, sample_state, sample_issues
    ):
        from issue_orchestrator.control.planner_types import E2ESlotSignals

        mock_config.e2e.occupies_session_slot = True
        gatherer = FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            e2e_slot_reader=lambda: E2ESlotSignals(occupies_slot=False, due=True),
        )

        snapshot = gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.e2e_occupies_slot is False
        assert snapshot.e2e_due is True

    def test_unwired_reader_leaves_facts_false(
        self, mock_config, mock_repository_host, sample_state, sample_issues
    ):
        """Flag on but no reader wired (unrelated test composition) stays safe."""
        mock_config.e2e.occupies_session_slot = True
        gatherer = FactGatherer(
            config=mock_config, repository_host=mock_repository_host
        )

        snapshot = gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.e2e_occupies_slot is False
        assert snapshot.e2e_due is False


class TestFactGathererCreateSnapshot:
    """Tests for create_snapshot method."""

    def test_create_snapshot_basic(self, fact_gatherer, sample_state, sample_issues):
        """Test creating a basic snapshot."""
        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.issues) == 2
        assert snapshot.issues[0].number == 1
        assert snapshot.paused is False
        assert snapshot.issues_started_count == 0

    def test_create_snapshot_with_active_sessions(
        self, fact_gatherer, sample_state, sample_issues, sample_agent_config, tmp_path
    ):
        """Test snapshot includes active sessions."""
        issue_key = FakeIssueKey(name=str(sample_issues[0].number))
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        session = Session(
            key=session_key,
            issue=sample_issues[0],
            agent_config=sample_agent_config,
            terminal_id="issue-1",
            worktree_path=tmp_path / "worktree",
            branch_name="1-issue-1",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-1",
            ),
        )
        sample_state.active_sessions = [session]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.active_sessions) == 1
        assert snapshot.active_sessions[0].issue.number == 1

    def test_create_snapshot_with_pending_reviews(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot includes pending reviews."""
        review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="1-issue-1",
            _issue_number=1,
        )
        sample_state.pending_reviews = [review]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert len(snapshot.pending_reviews) == 1
        assert snapshot.pending_reviews[0].pr_number == 10

    def test_create_snapshot_includes_awaiting_merge_reconciliation_facts(
        self,
        fact_gatherer,
        sample_state,
        sample_issues,
    ):
        """Snapshot carries awaiting-merge reconciliation facts to the planner."""
        fact = DiscoveredAwaitingMergeReconciliation(
            issue_number=1,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
        )
        sample_state.discovered_awaiting_merge_reconciliations = [fact]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.discovered_awaiting_merge_reconciliations == (fact,)

    def test_create_snapshot_includes_awaiting_merge_drift_facts(
        self,
        fact_gatherer,
        sample_state,
        sample_issues,
    ):
        """Snapshot carries awaiting-merge label drift facts to the planner."""
        fact = DiscoveredAwaitingMergeDrift(
            issue_number=1,
            pr_number=10,
            pr_url="https://github.com/owner/repo/pull/10",
            status_reason="PR closed; issue remains open",
        )
        sample_state.discovered_awaiting_merge_drifts = [fact]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.discovered_awaiting_merge_drifts == (fact,)

    def test_create_snapshot_with_priority_queue(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot includes priority queue."""
        sample_state.priority_queue = [5, 3, 1]

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.priority_queue == (5, 3, 1)

    def test_create_snapshot_paused_state(
        self, fact_gatherer, sample_state, sample_issues
    ):
        """Test snapshot reflects paused state."""
        sample_state.pause_state = operator_paused_state()

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.paused is True


class TestFactGathererEvents:
    """Tests for fetch-time event emission."""

    def test_fetch_issues_emits_issues_fetched_without_label_churn(self, mock_config, mock_repository_host):
        mock_config.agents = {"agent:web": MagicMock()}
        mock_repository_host.list_issues.return_value = [
            Issue(number=1, title="Issue 1", labels=["agent:web"]),
        ]
        events = InMemoryEventSink()
        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host, events=events)

        gatherer.fetch_issues(labels_for_agent=[], milestone=None)

        assert events.has_event(EventName.ISSUES_FETCHED.value)
        assert not events.has_event(EventName.ISSUE_LABELS_CHANGED.value)

    def test_fetch_issues_repeated_cycles_do_not_emit_label_churn(self, mock_config, mock_repository_host):
        mock_config.agents = {"agent:web": MagicMock()}
        mock_repository_host.list_issues.return_value = [
            Issue(number=1, title="Issue 1", labels=["agent:web"]),
        ]
        events = InMemoryEventSink()
        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host, events=events)

        gatherer.fetch_issues(labels_for_agent=[], milestone=None)
        gatherer.fetch_issues(labels_for_agent=[], milestone=None)

        assert len(events.get_events(EventName.ISSUES_FETCHED.value)) == 2
        assert len(events.get_events(EventName.ISSUE_LABELS_CHANGED.value)) == 0

    def test_create_snapshot_max_issues_to_start(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test snapshot includes max_issues_to_start from config."""
        mock_config.filtering.max_to_start = 10

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.max_issues_to_start == 10

    def test_create_snapshot_max_issues_zero_becomes_none(
        self, fact_gatherer, sample_state, sample_issues, mock_config
    ):
        """Test filtering.max_to_start=0 becomes None (unlimited)."""
        mock_config.filtering.max_to_start = 0

        snapshot = fact_gatherer.create_snapshot(sample_state, sample_issues)

        assert snapshot.max_issues_to_start is None


class TestFactGathererTechLeadFacts:
    """Tests for gather_tech_lead_facts method."""

    def test_tech_lead_facts_returns_none_when_not_configured(
        self, fact_gatherer, sample_state
    ):
        """Test returns None when tech_lead review not configured."""
        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is None

    def test_master_switch_returns_none_without_repository_reads(
        self,
        fact_gatherer,
        sample_state,
        mock_config,
        mock_repository_host,
    ):
        """Dormant sub-settings cannot leak scans past the single master gate."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead.enabled = False
        mock_config.tech_lead_review_threshold = 2
        mock_config.tech_lead.health_review.interval_minutes = 60
        mock_config.tech_lead.stuck_sweep.enabled = True

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is None
        mock_repository_host.get_prs_with_label.assert_not_called()
        mock_repository_host.list_issues.assert_not_called()

    def test_tech_lead_facts_returns_none_when_threshold_zero(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Test returns None when threshold is 0."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 0

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is None

    def test_tech_lead_facts_uses_default_watch_label_when_none_configured(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """With agent+threshold set, the watch label falls back to the default.

        The single watch-label owner (Config.tech_lead_watch_label, #6768 B3)
        returns "code-reviewed" when neither label is configured — the same
        default the manifest builder always applied — instead of silently
        disabling the trigger while the manifest side stayed armed.
        """
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 5
        mock_config.tech_lead_review_label = None
        mock_config.code_reviewed_label = None
        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.watch_label == "code-reviewed"

    def test_tech_lead_facts_counts_prs_with_label(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test counts PRs with the watch label."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
            PRInfo(number=11, url="...", title="PR 11", branch="b2", labels=[], body="", state="open"),
            PRInfo(number=12, url="...", title="PR 12", branch="b3", labels=[], body="", state="open"),
        ]
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.pr_count == 3
        assert result.threshold == 2
        assert result.watch_label == "code-reviewed"

    def test_tech_lead_facts_exclude_terminally_triaged_prs(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Terminally-triaged PRs never count toward the threshold (#6768 r5).

        Fact gathering shares the manifest builder's candidate predicate;
        counting tech-lead-reviewed/tech-lead-failed PRs that the manifest then
        filters out is what created endless empty-batch tracking issues.
        """
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="Still pending", branch="b1",
                   labels=["code-reviewed"], body="", state="open"),
            PRInfo(number=11, url="...", title="Audited", branch="b2",
                   labels=["code-reviewed", "tech-lead-reviewed"], body="", state="open"),
            PRInfo(number=12, url="...", title="Audit failed", branch="b3",
                   labels=["code-reviewed", "tech-lead-failed"], body="", state="open"),
        ]
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.pr_count == 1
        assert result.prs == ((10, "Still pending"),)

    def test_tech_lead_facts_never_read_milestones(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Fact gathering is observation only (#6769 finding 4): even with
        the explicit strategy configured and creation imminent, milestone
        name->number resolution belongs to the create-issue applier — the
        gatherer must make zero list_milestones calls."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 1
        mock_config.code_reviewed_label = "code-reviewed"
        mock_config.tech_lead.milestone_strategy.explicit = "M5"
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
        ]
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.pr_count == 1
        mock_repository_host.list_milestones.assert_not_called()

    def test_tech_lead_facts_detects_existing_issue(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test detects existing tech_lead review issue."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = [
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:tech-lead"]),
        ]

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.existing_tech_lead_issue == 100

    def test_tech_lead_facts_ignore_closed_batch_tracking_issue(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """A CLOSED batch tracking issue no longer suppresses batch creation (#6768 r4).

        Successful batch completion closes the tracking issue; the existing-batch
        finder queries state="open", so the closed batch stops matching and
        existing_tech_lead_issue clears, allowing the next threshold trigger.
        """
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        closed_batch = Issue(
            number=100,
            title="Tech Lead Batch Review: 5 PRs pending",
            labels=["agent:tech-lead"],
        )

        def list_issues(labels=None, state="open", limit=100, **kwargs):
            # Honor GitHub state filtering: the closed batch only appears in
            # non-open queries.
            del labels, limit, kwargs
            return [] if state == "open" else [closed_batch]

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.side_effect = list_issues

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.existing_tech_lead_issue is None

    def test_tech_lead_facts_ignores_existing_issue_outside_filter_label(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test filtered runs ignore tech_lead issues outside the active label scope."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"
        mock_config.filtering.label = "io:e2e:run-1"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = [
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:tech-lead"]),
        ]

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.existing_tech_lead_issue is None

    def test_tech_lead_facts_uses_tech_lead_label_over_code_reviewed(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test prefers tech_lead_review_label over code_reviewed_label."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.tech_lead_review_label = "ready-for-tech-lead"
        mock_config.code_reviewed_label = "code-reviewed"

        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues.return_value = []

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        assert result.watch_label == "ready-for-tech-lead"
        mock_repository_host.get_prs_with_label.assert_called_with("ready-for-tech-lead", state="all")

    def test_tech_lead_facts_collects_labels_from_prs_and_linked_issues(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test collects labels from both PRs and their linked issues.

        When gathering tech_lead facts, source_labels should include:
        - Labels directly on the PRs
        - Labels on issues linked from the PR body/title (e.g., "Fixes #123")

        This enables tech_lead.inherit_labels to work with labels from linked issues.
        """
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"

        # PRs have some labels and reference linked issues in their titles
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(
                number=10,
                url="...",
                title="#100: Fix the bug",
                branch="b1",
                labels=["pr-label-1"],
                body="Fixes #101",
                state="open",
            ),
            PRInfo(
                number=11,
                url="...",
                title="PR 11 for #102",
                branch="b2",
                labels=["pr-label-2"],
                body="",
                state="open",
            ),
        ]
        mock_repository_host.list_issues.return_value = []

        # Linked issues have different labels (including one we want to inherit)
        def get_issue_side_effect(issue_num):
            issues = {
                100: Issue(number=100, title="Issue 100", labels=["agent:backend", "io-e2e-test-data"]),
                101: Issue(number=101, title="Issue 101", labels=["agent:backend", "priority:high"]),
                102: Issue(number=102, title="Issue 102", labels=["agent:frontend", "test-data"]),
            }
            return issues.get(issue_num)

        mock_repository_host.get_issue.side_effect = get_issue_side_effect

        result = fact_gatherer.gather_tech_lead_facts(sample_state)

        assert result is not None
        # Should include PR labels
        assert "pr-label-1" in result.source_labels
        assert "pr-label-2" in result.source_labels
        # Should include labels from linked issues
        assert "agent:backend" in result.source_labels
        assert "agent:frontend" in result.source_labels
        assert "io-e2e-test-data" in result.source_labels
        assert "test-data" in result.source_labels
        assert "priority:high" in result.source_labels


class _LabelFilteringTracker:
    """RepositoryHost fake honoring GitHub's server-side label AND-filter.

    Returns only issues that carry EVERY requested label, then applies the
    page ``limit`` — exactly the behavior that lets an older anchor fall off a
    too-small first page of the broad tech-lead-agent scan (#6763 finding 4). It
    records each query so tests can prove the dedup lookup is marker-scoped.
    """

    def __init__(self, issues):
        self._issues = list(issues)
        self.calls: list[dict] = []

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.calls.append(
            {
                "labels": list(labels or []),
                "state": state,
                "limit": limit,
                **kwargs,
            }
        )
        wanted = {label.casefold() for label in (labels or [])}
        matched = [
            issue
            for issue in self._issues
            if issue.state == state or state == "all"
            if wanted <= {label.casefold() for label in issue.labels}
        ]
        return matched[:limit]

    def get_prs_with_label(self, *args, **kwargs):
        return []


class TestFactGathererHealthReviewFacts:
    """Health-review trigger facts (ADR-0031 §4).

    The interval gates the health fields, tech_lead_review_threshold gates the
    batch fields — each feature works alone, and both share ONE list_issues
    scan for anchor-issue dedup (GitHub API discipline).
    """

    @staticmethod
    def _arm_health_review(mock_config, interval_minutes: int = 60) -> None:
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead.health_review.interval_minutes = interval_minutes

    def test_due_when_interval_elapsed_with_batch_disabled(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """threshold=0 + interval set: health fields populate, batch stays inert."""
        self._arm_health_review(mock_config)
        sample_state.last_health_review_at = 1_000.0
        sample_state.priority_queue = [42]  # non-empty board: not suppressed by the change-gate

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=1_000.0 + 3600)

        assert result is not None
        assert result.health_review_due is True
        assert result.existing_health_review_issue is None
        # Batch fields inert: no watch label, no PRs, threshold 0.
        assert result.watch_label == ""
        assert result.pr_count == 0
        assert result.prs == ()
        assert result.threshold == 0
        # No PR fetch when batch is disabled — health review costs only the
        # single exhaustive anchor/case-file scan.
        mock_repository_host.get_prs_with_label.assert_not_called()
        mock_repository_host.list_issues.assert_called_once()
        assert mock_repository_host.list_issues.call_args.kwargs["exhaustive"] is True

    def test_not_due_within_interval(
        self, fact_gatherer, sample_state, mock_config
    ):
        self._arm_health_review(mock_config, interval_minutes=60)
        sample_state.last_health_review_at = 1_000.0

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=1_000.0 + 1800)

        assert result is not None
        assert result.health_review_due is False

    def test_never_run_is_due_immediately(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Never-run + a non-empty board is due immediately (backlog first run)."""
        self._arm_health_review(mock_config, interval_minutes=60)
        sample_state.priority_queue = [42]  # a backlog item to review on first run

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.health_review_due is True

    def test_disabled_interval_returns_none_without_batch(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """interval=0 + threshold=0 keeps the pre-existing None (no API calls)."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead.health_review.interval_minutes = 0

        assert fact_gatherer.gather_tech_lead_facts(sample_state) is None
        mock_repository_host.list_issues.assert_not_called()
        mock_repository_host.get_prs_with_label.assert_not_called()

    def test_interval_without_tech_lead_agent_is_disabled(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        mock_config.tech_lead_review_agent = None
        mock_config.tech_lead.health_review.interval_minutes = 60

        assert fact_gatherer.gather_tech_lead_facts(sample_state) is None
        mock_repository_host.list_issues.assert_not_called()

    def test_health_only_facts_skip_explicit_milestone_resolution(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Batch-disabled facts never resolve tech_lead.milestone_strategy.explicit.

        The name -> number lookup costs a list_milestones call that only batch
        creation consumes; health-only gathering must not pay it (GitHub API
        discipline) nor fail on an unresolvable name.
        """
        self._arm_health_review(mock_config)
        mock_config.tech_lead.milestone_strategy.explicit = "M9"

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        mock_repository_host.list_milestones.assert_not_called()

    @staticmethod
    def _arm_storm(mock_config, sample_state, *, count: int = 3) -> None:
        """Put ``count`` fresh problems on the board with threshold=3."""
        from issue_orchestrator.domain.models import DiscoveredFailure

        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_review_on_failure = True
        mock_config.tech_lead.health_review.storm_threshold = 3
        mock_config.tech_lead.health_review.storm_window_minutes = 5
        for number in range(41, 41 + count):
            sample_state.record_discovered_failure(
                DiscoveredFailure(number, f"Problem {number}", "failed")
            )

    @staticmethod
    def _open_anchor():
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        return Issue(
            number=100,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )

    def test_storm_arms_anchor_scan_when_interval_is_not_due(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """A storm can fire an anchor on a tick the interval is NOT due, so the
        open-anchor scan must be armed by the storm too.

        Arming only on due-ness leaves ``existing_health_review_issue`` None on
        exactly the ticks a storm creates an anchor, so an anchor that is
        already open — including one currently RUNNING, which no queue-derived
        fallback can see — gets duplicated.
        """
        self._arm_health_review(mock_config, interval_minutes=60)
        sample_state.last_health_review_at = 999_000.0
        self._arm_storm(mock_config, sample_state)
        mock_repository_host.list_issues.return_value = [self._open_anchor()]

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_060.0)

        assert result is not None
        assert result.health_review_due is False
        assert result.existing_health_review_issue == 100

    def test_storm_only_config_arms_anchor_scan(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """interval_minutes=0 disables only the PERIODIC trigger.

        Storm escalation stays live, so a storm-only configuration must still
        gather the dedup fact — otherwise gathering bails out before any scan
        and every storm mints another anchor.
        """
        mock_config.tech_lead.health_review.interval_minutes = 0
        self._arm_storm(mock_config, sample_state)
        mock_repository_host.list_issues.return_value = [self._open_anchor()]

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.health_review_due is False
        assert result.existing_health_review_issue == 100

    def test_no_scan_when_problems_stay_below_the_storm_threshold(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """GitHub API discipline: the extra scan is bought only by a real storm.

        Two problems cannot escalate, so a not-due tick still makes zero calls.
        """
        mock_config.tech_lead.health_review.interval_minutes = 0
        self._arm_storm(mock_config, sample_state, count=2)

        assert fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0) is None
        mock_repository_host.list_issues.assert_not_called()

    def test_disabled_storm_threshold_arms_no_scan(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """storm_threshold=0 disables escalation, so it arms nothing either."""
        mock_config.tech_lead.health_review.interval_minutes = 0
        self._arm_storm(mock_config, sample_state, count=5)
        mock_config.tech_lead.health_review.storm_threshold = 0

        assert fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0) is None
        mock_repository_host.list_issues.assert_not_called()

    def test_existing_marker_labeled_issue_detected(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """An open marker-labeled anchor dedupes creation (crash-safe)."""
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        self._arm_health_review(mock_config)
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=200,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            ),
        ]

        sample_state.priority_queue = [42]  # non-empty board so the anchor scan runs
        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.health_review_due is True
        assert result.existing_health_review_issue == 200
        # The marker-labeled anchor must NOT be misread as a batch anchor.
        assert result.existing_tech_lead_issue is None
        mock_repository_host.list_issues.assert_called_once()

    def test_both_triggers_share_one_issue_scan(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Batch + health armed together: one list_issues call, both classified."""
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        self._arm_health_review(mock_config)
        mock_config.tech_lead_review_threshold = 2
        mock_config.code_reviewed_label = "code-reviewed"
        mock_repository_host.list_issues.return_value = [
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:tech-lead"]),
            Issue(
                number=200,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            ),
        ]

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.existing_tech_lead_issue == 100
        assert result.existing_health_review_issue == 200
        assert result.watch_label == "code-reviewed"
        mock_repository_host.list_issues.assert_called_once()

    def test_marker_issue_outside_filter_label_ignored(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Filtered runs ignore anchors outside the active label scope."""
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        self._arm_health_review(mock_config)
        mock_config.filtering.label = "io:e2e:run-1"
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=200,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            ),
        ]

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.existing_health_review_issue is None

    def test_not_due_health_only_makes_zero_list_issues_calls(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """GitHub API discipline: due-ness is computed FIRST, so a health-only
        config that is not yet due makes ZERO GitHub calls — no anchor fact can
        affect planning before the review is due (#6763 finding 3)."""
        self._arm_health_review(mock_config, interval_minutes=60)
        sample_state.last_health_review_at = 1_000.0

        result = fact_gatherer.gather_tech_lead_facts(sample_state, now=1_000.0 + 1800)

        assert result is not None
        assert result.health_review_due is False
        assert result.existing_health_review_issue is None
        # No scan ran, so the case-file projection is NOT observed this tick;
        # the flag stays False so the board publisher retains its last
        # projection instead of wiping it with the empty tuple (#6781 R2).
        assert result.case_files_scanned is False
        assert result.open_case_files == ()
        mock_repository_host.list_issues.assert_not_called()
        mock_repository_host.get_prs_with_label.assert_not_called()

    def test_marker_anchor_beyond_first_page_is_deduped(
        self, sample_state, mock_config
    ):
        """Crash-safe dedup must be exhaustive: a marker anchor sitting BEYOND
        the first ten tech-lead-agent items is still found, so no duplicate anchor
        is created (#6763 finding 4).

        The fake honors GitHub's label AND-filter plus page limit, so a broad
        ``[tech_lead_agent]``/limit=10 scan would strand the anchor at position
        11; the marker-scoped lookup finds it regardless of position.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        self._arm_health_review(mock_config)
        crowd = [
            Issue(number=n, title=f"Batch {n}", labels=["agent:tech-lead"])
            for n in range(1, 12)
        ]
        anchor = Issue(
            number=200,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
        )
        tracker = _LabelFilteringTracker([*crowd, anchor])
        gatherer = FactGatherer(config=mock_config, repository_host=tracker)
        sample_state.priority_queue = [42]  # non-empty board so the anchor scan runs

        result = gatherer.gather_tech_lead_facts(sample_state, now=999_999.0)

        assert result is not None
        assert result.health_review_due is True
        assert result.existing_health_review_issue == 200
        # The due health review uses the shared exhaustive tech-lead-agent scan,
        # which both finds the anchor beyond the first page and supplies open
        # case files to the health-review snapshot (#6781).
        assert tracker.calls == [
            {
                "labels": ["agent:tech-lead"],
                "state": "open",
                "limit": 2000,
                "exhaustive": True,
            }
        ]


class TestFactGathererCleanupFacts:
    """Tests for gather_cleanup_facts method."""

    def test_cleanup_facts_returns_none_when_no_pending(
        self, fact_gatherer, sample_state
    ):
        """Test returns None when no pending cleanups."""
        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is None


    def test_cleanup_facts_returns_none_when_no_review_workflow(
        self, fact_gatherer, sample_state, mock_config
    ):
        """Test returns CleanupFacts with defaults when no review workflow configured.

        Even without a review workflow, we return CleanupFacts so that
        immediate cleanups can still be processed.
        """
        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt"),
            )
        ]
        mock_config.tech_lead_review_agent = None
        mock_config.code_review_agent = None

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        # Returns CleanupFacts with empty reviewed_pr_numbers (deferred cleanups won't match)
        # but immediate_cleanups can still be processed
        assert result is not None
        assert result.reviewed_pr_numbers == frozenset()  # No review label to check
        assert len(result.pending_cleanups) == 1

    def test_cleanup_facts_with_tech_lead_workflow(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test cleanup facts with tech_lead review workflow."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_reviewed_label = "tech-lead-reviewed"
        mock_config.cleanup.with_tech_lead.close_ai_session_tabs = True
        mock_config.cleanup.with_tech_lead.remove_worktrees = True

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
        ]

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is not None
        assert len(result.pending_cleanups) == 1
        assert 10 in result.reviewed_pr_numbers
        assert result.close_tabs is True
        assert result.remove_worktrees is True

    def test_cleanup_facts_with_code_review_workflow(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test cleanup facts with code review workflow (no tech_lead)."""
        mock_config.tech_lead_review_agent = None
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_reviewed_label = "code-reviewed"
        mock_config.cleanup.without_tech_lead.close_ai_session_tabs = False
        mock_config.cleanup.without_tech_lead.remove_worktrees = True

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=10, url="...", title="PR 10", branch="b1", labels=[], body="", state="open"),
        ]

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        assert result is not None
        assert result.close_tabs is False
        assert result.remove_worktrees is True

    def test_cleanup_facts_handles_api_error(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Test handles API error gracefully.

        On API error, we still return CleanupFacts but with empty reviewed_pr_numbers.
        This allows immediate cleanups to still work even if deferred cleanup lookup fails.
        """
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]

        mock_repository_host.get_prs_with_label.side_effect = Exception("API error")

        result = fact_gatherer.gather_cleanup_facts(sample_state)

        # Returns CleanupFacts with empty reviewed_pr_numbers on API error
        # Deferred cleanups won't trigger, but immediate cleanups still work
        assert result is not None
        assert result.reviewed_pr_numbers == frozenset()
        assert len(result.pending_cleanups) == 1

    def test_cleanup_facts_propagates_repository_host_error(
        self, fact_gatherer, sample_state, mock_config, mock_repository_host
    ):
        """Repository-host failures should fail the snapshot instead of hiding staleness."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_reviewed_label = "tech-lead-reviewed"
        sample_state.pending_cleanups = [
            PendingCleanup(
                issue=Issue(number=1, title="Test issue", labels=[]),
                pr_number=10,
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="1-issue-1",
                terminal_id="issue-1",
                worktree_path=Path("/tmp/wt1"),
            )
        ]
        mock_repository_host.get_prs_with_label.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            fact_gatherer.gather_cleanup_facts(sample_state)

        assert exc_info.value.status_code == 503


class TestFactGathererFetchIssues:
    """Tests for fetch_issues method."""

    def test_fetch_issues_multiple_milestones_dedupes(
        self, mock_config, mock_repository_host
    ):
        """Fetch across multiple milestones, deduping overlapping issues."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = ["M1", "M2"]
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 100

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web"])
        mock_repository_host.list_issues.side_effect = [
            [issue_1],
            [issue_1, issue_2],
        ]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        assert [issue.number for issue in results] == [1, 2]
        assert mock_repository_host.list_issues.call_args_list == [
            call(labels=["test-label", "agent:web"], milestone="M1", limit=100, required_stable_ids=None),
            call(labels=["test-label", "agent:web"], milestone="M2", limit=100, required_stable_ids=None),
        ]

    def test_fetch_issues_uses_milestone_param_when_unfiltered(
        self, mock_config, mock_repository_host
    ):
        """Use explicit milestone when config has no filter."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        mock_repository_host.list_issues.return_value = [issue_1]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"], milestone="M3")

        assert [issue.number for issue in results] == [1]
        mock_repository_host.list_issues.assert_called_once_with(
            labels=["test-label", "agent:web"],
            milestone="M3",
            limit=50,
            required_stable_ids=None,
        )

    def test_fetch_issues_applies_exclude_labels_filter(
        self, mock_config, mock_repository_host
    ):
        """Test that exclude_labels filters out matching issues."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_labels = ["test-data"]  # Exclude issues with this label

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "test-data"])  # Should be excluded
        issue_3 = Issue(number=3, title="Issue 3", labels=["agent:web", "bug"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2, issue_3]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        # Issue 2 should be excluded because it has 'test-data' label
        assert [issue.number for issue in results] == [1, 3]

    def test_fetch_issues_exclude_labels_empty_passes_all(
        self, mock_config, mock_repository_host
    ):
        """Test that empty exclude_labels passes all issues through."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_labels = []  # No exclusions

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "test-data"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        # All issues should pass through
        assert [issue.number for issue in results] == [1, 2]

    def test_fetch_issues_applies_exclude_label_prefixes_filter(
        self, mock_config, mock_repository_host
    ):
        """Issue fetch excludes issues carrying labels in excluded namespaces."""
        mock_config.agents = {"agent:web": Mock()}
        mock_config.filtering.milestones = []
        mock_config.filtering.milestone = None
        mock_config.filtering.fetch_limit = 50
        mock_config.filtering.exclude_label_prefixes = ["io:e2e:"]

        issue_1 = Issue(number=1, title="Issue 1", labels=["agent:web"])
        issue_2 = Issue(number=2, title="Issue 2", labels=["agent:web", "io:e2e:isolated-4057"])
        issue_3 = Issue(number=3, title="Issue 3", labels=["agent:web", "bug"])
        mock_repository_host.list_issues.return_value = [issue_1, issue_2, issue_3]

        gatherer = FactGatherer(config=mock_config, repository_host=mock_repository_host)
        results = gatherer.fetch_issues(labels_for_agent=["test-label"])

        assert [issue.number for issue in results] == [1, 3]


class TestGatedProposalScanClassification:
    """The ONE anchor scan classifies gated proposals too (#6778)."""

    def _op(self, target: int, op_type: str = "reset_retry"):
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

    def _gatherer(self, mock_config, mock_repository_host, ops):
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        mock_config.tech_lead_review_agent = "tech-lead-agent"
        mock_config.tech_lead_review_threshold = 5
        mock_config.code_reviewed_label = "code-reviewed"
        store = InMemoryTechLeadAuthorityStore()
        for issue_number, op in ops:
            store.record_op(issue_number=issue_number, op=op)
        return FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            tech_lead_authority=store,
        )

    def test_approved_op_classified_from_same_scan(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        """An op-backed issue WITHOUT the gate label is approved; the anchor
        classification still works on the remaining issues — all from one
        list_issues call."""
        mock_repository_host.list_issues.return_value = [
            Issue(number=500, title="Tech Lead proposal: reset & retry issue #13 from scratch", labels=["tech-lead-agent"]),
            Issue(number=7, title="Tech Lead Batch Review: 3 PRs pending", labels=["tech-lead-agent"]),
        ]
        gatherer = self._gatherer(
            mock_config, mock_repository_host, [(500, self._op(13))]
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        assert facts is not None
        [approved] = facts.approved_tech_lead_ops
        assert approved.proposal_issue_number == 500
        assert approved.op.target_issue_number == 13
        assert facts.existing_tech_lead_issue == 7
        # Exactly one issue scan was made for anchors + proposals.
        assert mock_repository_host.list_issues.call_count == 1

    def test_still_gated_proposal_yields_nothing_and_never_becomes_anchor(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=500,
                title="Tech Lead proposal: kill hung session for issue #14",
                labels=["tech-lead-agent", "proposed-tech-lead"],
            ),
        ]
        gatherer = self._gatherer(
            mock_config,
            mock_repository_host,
            [(500, self._op(14, "kill_hung_session"))],
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        assert facts is not None
        assert facts.approved_tech_lead_ops == ()
        assert facts.existing_tech_lead_issue is None
        assert facts.existing_health_review_issue is None

    def test_exhaustive_scan_limit_prevents_hiding_ops_behind_a_backlog(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        """R4: the anchor/proposal scan pages the COMPLETE matching set."""
        from issue_orchestrator.control.tech_lead_proposals import (
            TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        )

        mock_repository_host.list_issues.return_value = []
        gatherer = self._gatherer(mock_config, mock_repository_host, [])

        gatherer.gather_tech_lead_facts(sample_state)

        _, kwargs = mock_repository_host.list_issues.call_args
        assert kwargs["limit"] == TECH_LEAD_PROPOSAL_SCAN_LIMIT
        assert TECH_LEAD_PROPOSAL_SCAN_LIMIT > 100  # forces the adapter to paginate

    def test_ledger_row_absent_from_scan_is_surfaced_as_candidate_read_only(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        """R7/R10: an op whose proposal issue is absent from the exhaustive scan
        is surfaced as a cleanup CANDIDATE, but fact gathering is READ-ONLY —
        it must NOT discard the ledger row during observation. Cleanup flows
        through the planner's DiscardTerminalTechLeadProposalOpsAction/owner,
        which confirms with a targeted read first (a truncated scan must never
        delete a live op)."""
        # #500 is still an open proposal; #501's issue is absent from the scan.
        mock_repository_host.list_issues.return_value = [
            Issue(number=500, title="Tech Lead proposal", labels=["tech-lead-agent", "proposed-tech-lead"]),
        ]
        gatherer = self._gatherer(
            mock_config,
            mock_repository_host,
            [(500, self._op(13)), (501, self._op(14, "kill_hung_session"))],
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        # The absent row is surfaced as a candidate...
        assert facts is not None
        assert facts.absent_proposal_op_candidates == (501,)
        # ...but the store is UNTOUCHED — observation never mutates the ledger.
        assert sorted(n for n, _ in gatherer.tech_lead_authority.list_ops()) == [500, 501]
        # A second scan is still read-only (no self-heal happens here).
        gatherer.gather_tech_lead_facts(sample_state)
        assert sorted(n for n, _ in gatherer.tech_lead_authority.list_ops()) == [500, 501]

    def test_without_store_gate_labeled_issues_are_still_excluded(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        mock_config.tech_lead_review_agent = "tech-lead-agent"
        mock_config.tech_lead_review_threshold = 5
        mock_config.code_reviewed_label = "code-reviewed"
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=500,
                title="Tech Lead proposal: reset & retry issue #13 from scratch",
                labels=["tech-lead-agent", "proposed-tech-lead"],
            ),
        ]
        gatherer = FactGatherer(
            config=mock_config, repository_host=mock_repository_host
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        assert facts is not None
        assert facts.approved_tech_lead_ops == ()
        assert facts.existing_tech_lead_issue is None
    def _gatherer_batch_disabled(self, mock_config, mock_repository_host, ops):
        """Threshold=0 (batch trigger OFF) but a tech lead agent + wired ledger.

        The proposal machinery must still reconcile in this shape (#6779 R12):
        proposal advancement is decoupled from the batch review threshold.
        """
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        mock_config.tech_lead_review_agent = "tech-lead-agent"
        mock_config.tech_lead_review_threshold = 0  # batch trigger disabled
        mock_config.code_reviewed_label = "code-reviewed"
        store = InMemoryTechLeadAuthorityStore()
        for issue_number, op in ops:
            store.record_op(issue_number=issue_number, op=op)
        return FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            tech_lead_authority=store,
        )

    def test_batch_disabled_proposals_still_execute_and_clean_up(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        """R12: with the batch threshold at 0 (batch trigger OFF) but a tech_lead
        agent configured, an APPROVED gated proposal's op is still executed and
        a terminal/absent proposal op is still surfaced for cleanup. Proposal
        reconcile is decoupled from the batch review threshold — otherwise
        manual-approval / default-threshold proposals never advance or
        self-heal."""
        # #500 is approved (op-backed issue WITHOUT the gate label); #501's
        # issue is absent from the scan (terminal cleanup candidate).
        mock_repository_host.list_issues.return_value = [
            Issue(number=500, title="Tech Lead proposal for issue #13", labels=["tech-lead-agent"]),
        ]
        gatherer = self._gatherer_batch_disabled(
            mock_config,
            mock_repository_host,
            [(500, self._op(13)), (501, self._op(14, "kill_hung_session"))],
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        assert facts is not None
        assert facts.threshold == 0  # batch trigger stays OFF
        assert facts.existing_tech_lead_issue is None  # no batch anchor surfaced
        # The approved op is executed even though the batch threshold is 0.
        [approved] = facts.approved_tech_lead_ops
        assert approved.proposal_issue_number == 500
        assert approved.op.target_issue_number == 13
        # The terminal/absent op is still surfaced for cleanup.
        assert facts.absent_proposal_op_candidates == (501,)

    def test_batch_disabled_empty_ledger_makes_no_scan(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        """R12 frugality: batch threshold 0 + tech lead agent but an EMPTY ledger
        has nothing to reconcile, so it produces None and makes ZERO GitHub
        calls (no scan is worth making)."""
        mock_config.tech_lead.health_review.interval_minutes = 0
        gatherer = self._gatherer_batch_disabled(mock_config, mock_repository_host, [])

        assert gatherer.gather_tech_lead_facts(sample_state) is None
        mock_repository_host.list_issues.assert_not_called()


class TestCaseFileScanClassification:
    """The ONE anchor scan also classifies open pattern case files (#6781)."""

    def _gatherer(self, mock_config, mock_repository_host, *, board_publisher=None):
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        mock_config.tech_lead_review_agent = "tech-lead-agent"
        mock_config.tech_lead_review_threshold = 5
        mock_config.code_reviewed_label = "code-reviewed"
        return FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            tech_lead_authority=InMemoryTechLeadAuthorityStore(),
            board_publisher=board_publisher,
        )

    def test_case_file_classified_into_snapshot_and_never_anchor(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        from issue_orchestrator.domain.tech_lead_session import TECH_LEAD_OBSERVATION_LABEL

        mock_repository_host.list_issues.return_value = [
            Issue(
                number=800,
                # A title that LOOKS like an anchor must not fool the split.
                title="Tech Lead Batch Review: recurring db timeout",
                labels=["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL, "area:db"],
            ),
            Issue(
                number=7,
                title="Tech Lead Batch Review: 3 PRs pending",
                labels=["tech-lead-agent"],
            ),
        ]
        gatherer = self._gatherer(mock_config, mock_repository_host)

        facts = gatherer.gather_tech_lead_facts(sample_state)

        assert facts is not None
        [case_file] = facts.open_case_files
        assert case_file.issue_number == 800
        assert case_file.area == "db"
        # The observation-labeled issue is NEVER an anchor; #7 still is.
        assert facts.existing_tech_lead_issue == 7
        # The anchor scan ran, so the projection is authoritative this tick.
        assert facts.case_files_scanned is True
        # Still just one issue scan for anchors + proposals + case files.
        assert mock_repository_host.list_issues.call_count == 1

    def test_board_publisher_receives_facts_and_health_review_timestamp(
        self, mock_config, mock_repository_host, sample_state
    ) -> None:
        from issue_orchestrator.domain.tech_lead_session import TECH_LEAD_OBSERVATION_LABEL

        class _RecordingPublisher:
            def __init__(self) -> None:
                self.calls = []

            def publish(self, facts, *, last_health_review_at) -> None:
                self.calls.append((facts, last_health_review_at))

        publisher = _RecordingPublisher()
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=800,
                title="Pattern case file: db-timeout",
                labels=["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL],
            ),
        ]
        gatherer = self._gatherer(
            mock_config, mock_repository_host, board_publisher=publisher
        )

        facts = gatherer.gather_tech_lead_facts(sample_state)

        # Fire-and-forget projection sink got the gathered facts + state.
        assert len(publisher.calls) == 1
        published_facts, last_health_review_at = publisher.calls[0]
        assert published_facts is facts
        assert last_health_review_at == sample_state.last_health_review_at
        assert len(published_facts.open_case_files) == 1

    def test_no_scan_tick_preserves_prior_case_file_projection(
        self, mock_config, mock_repository_host, sample_state, tmp_path
    ) -> None:
        """#6781 R2 end-to-end: a scanned tick populates the board projection;
        a later health-armed/not-due/no-op tick makes NO scan and must not wipe
        it. This wires the real gatherer -> real publisher path so the
        ``case_files_scanned`` flag the gatherer stamps actually governs whether
        the projection the board snapshot builder reads is retained.
        """
        from issue_orchestrator.control.tech_lead_board import (
            TechLeadBoardPublisher,
            tech_lead_board_path,
        )
        from issue_orchestrator.domain.tech_lead_session import (
            TECH_LEAD_OBSERVATION_LABEL,
        )
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        # Health review armed; batch disabled (threshold 0 -> no watch label);
        # empty op ledger. The only thing that ever triggers the anchor scan
        # here is health-review due-ness.
        mock_config.tech_lead_review_agent = "tech-lead-agent"
        mock_config.tech_lead_review_threshold = 0
        mock_config.tech_lead.health_review.interval_minutes = 60
        mock_config.code_reviewed_label = "code-reviewed"

        publisher = TechLeadBoardPublisher(
            board_path=tech_lead_board_path(tmp_path),
            authority=InMemoryTechLeadAuthorityStore(),
        )
        gatherer = FactGatherer(
            config=mock_config,
            repository_host=mock_repository_host,
            tech_lead_authority=InMemoryTechLeadAuthorityStore(),
            board_publisher=publisher,
        )

        # Tick 1: health review is due -> the anchor scan runs and observes an
        # open pattern case file.
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=800,
                title="Pattern case file: db-timeout",
                labels=["tech-lead-agent", TECH_LEAD_OBSERVATION_LABEL, "area:db"],
            ),
        ]
        sample_state.last_health_review_at = 1_000.0
        sample_state.priority_queue = [42]  # non-empty board so tick 1 is due
        scanned = gatherer.gather_tech_lead_facts(sample_state, now=1_000.0 + 3600)
        assert scanned is not None
        assert scanned.case_files_scanned is True
        assert [cf.issue_number for cf in publisher.case_files()] == [800]

        # The review ran, so its timestamp advances (orchestrator authority).
        sample_state.last_health_review_at = 1_000.0 + 3600

        # Tick 2: not due, empty ledger, batch off -> NO scan this tick.
        mock_repository_host.list_issues.reset_mock()
        mock_repository_host.list_issues.return_value = []
        not_scanned = gatherer.gather_tech_lead_facts(
            sample_state, now=1_000.0 + 3600 + 60
        )
        assert not_scanned is not None
        assert not_scanned.case_files_scanned is False
        assert not_scanned.open_case_files == ()
        # Zero GitHub calls on the frugal tick (GitHub API discipline).
        mock_repository_host.list_issues.assert_not_called()
        # The projection the board snapshot builder reads is preserved, not
        # wiped by the empty tuple the frugal tick carried.
        assert [cf.issue_number for cf in publisher.case_files()] == [800]


class TestClearDiscoveredFacts:
    """Retention rule for the tick-scoped fact buffers (#6780).

    The clear exists to drop facts the tick CONSUMED. A paused tick consumes
    nothing — the Planner returns an empty plan and ``apply_plan`` refuses to
    apply actions while paused — so clearing there would silently discard
    problems that nothing else records. A session that fails while paused is
    discovered exactly once, so a dropped storm cohort is unrecoverable even
    after resume.
    """

    @staticmethod
    def _config() -> Config:
        config = Config(repo="test/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_on_failure = True
        return config

    @staticmethod
    def _state_with_cohort() -> OrchestratorState:
        state = OrchestratorState()
        for number in (41, 42, 43):
            state.record_discovered_failure(
                DiscoveredFailure(
                    issue_number=number,
                    issue_title=f"Problem {number}",
                    failure_reason="failed",
                    observed_at=1_000.0,
                )
            )
        return state

    def test_paused_tick_retains_discovered_failures(self):
        """A paused tick planned nothing, so it consumed nothing."""
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        state = self._state_with_cohort()

        clear_discovered_facts(state, self._config(), tick_paused=True)

        assert [f.issue_number for f in state.discovered_failures] == [41, 42, 43]

    def test_running_tick_clears_discovered_failures(self):
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        state = self._state_with_cohort()

        clear_discovered_facts(state, self._config(), tick_paused=False)

        assert state.discovered_failures == []

    def test_retention_ignores_live_state_paused(self):
        """Retention is the TICK's decision, never a fresh shared-state read.

        ``state.paused`` is mutated from the web thread and can flip between
        the snapshot and this call. Both directions are pinned here: the clear
        must follow ``tick_paused`` even when live state says the opposite.
        """
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts

        resumed_mid_tick = self._state_with_cohort()
        resumed_mid_tick.pause_state = PauseState.running()
        clear_discovered_facts(resumed_mid_tick, self._config(), tick_paused=True)
        assert [
            f.issue_number for f in resumed_mid_tick.discovered_failures
        ] == [41, 42, 43]

        paused_mid_apply = self._state_with_cohort()
        paused_mid_apply.pause_state = operator_paused_state()
        clear_discovered_facts(paused_mid_apply, self._config(), tick_paused=False)
        assert paused_mid_apply.discovered_failures == []

    def test_running_tick_retains_disposable_cleanup_but_drops_normal(self):
        """F8 second guard: a disposable scratch-worktree cleanup survives the
        clear (it is pruned on SUCCESS by ``_handle_cleanup_session``, so any
        that reaches here had its removal FAIL and must be re-planned), while a
        normal cleanup is consumed once and dropped."""
        from issue_orchestrator.control.fact_gatherer import clear_discovered_facts
        from issue_orchestrator.domain.models import ImmediateCleanup

        state = OrchestratorState()
        state.immediate_cleanups.append(
            ImmediateCleanup(5980, "issue-5980", "/tmp/repo-tech-lead-5980", "completed",
                             scratch_worktree=True)
        )
        state.immediate_cleanups.append(
            ImmediateCleanup(7, "issue-7", "/tmp/worktree-7", "completed")
        )

        clear_discovered_facts(state, self._config(), tick_paused=False)

        remaining = {c.issue_number for c in state.immediate_cleanups}
        assert remaining == {5980}  # disposable retained, normal dropped
