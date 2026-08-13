"""Unit tests for StartupManager."""

import json
from pathlib import Path

import pytest

from unittest.mock import MagicMock, call, patch

from issue_orchestrator.control.session_launch_types import LaunchResult
from issue_orchestrator.control.session_routing import orchestrator_launch_tech_lead_session
from issue_orchestrator.control.startup_manager import StartupManager
from issue_orchestrator.control.issue_fetch_resilience import IssueFetchResilience
from issue_orchestrator.control.worktree_reconciliation import WorktreeRecoverySummary
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction
from issue_orchestrator.execution.label_store import LabelStore
from issue_orchestrator.ports.repository_host import RepositoryHostError
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import (
    Issue,
    OrchestratorState,
    Session,
    AgentConfig,
    PendingReview,
    PendingTechLeadReview,
    ORCHESTRATOR_PR_MARKER,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor


def _startup_worktree_reconciler() -> MagicMock:
    reconciler = MagicMock()
    reconciler.recover.return_value = WorktreeRecoverySummary(0, 0, 0)
    return reconciler


class _FakeLabelSet:
    """In-memory LabelSet standing in for the GitHub label surface.

    Lets a real ActionApplier exercise its write-through against genuine
    mutable state instead of a MagicMock, so tests observe the labels GitHub
    would actually carry after recovery.
    """

    def __init__(self, labels_by_issue: dict[int, set[str]]) -> None:
        self._labels: dict[int, set[str]] = {
            number: set(labels) for number, labels in labels_by_issue.items()
        }

    def add_label(self, issue_number: int, label: str) -> None:
        self._labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self._labels.get(issue_number, set()).discard(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        return label in self._labels.get(issue_number, set())

    def labels_for(self, issue_number: int) -> set[str]:
        return set(self._labels.get(issue_number, set()))


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.max_concurrent_sessions = 3
    config.code_review_agent = None
    config.code_review_label = None
    config.tech_lead_review_agent = None
    config.filtering.label = None
    config.filtering.milestone = None
    config.filtering.fetch_limit = 100
    config.dangerous = MagicMock()
    config.dangerous.allow_unsupported_agents = True
    return config


@pytest.fixture
def mock_events():
    """Create a mock EventSink."""
    return MagicMock()


@pytest.fixture
def mock_runner():
    """Create a mock SessionRunner."""
    runner = MagicMock()
    runner.cleanup_idle_sessions.return_value = 0
    runner.discover_running_sessions.return_value = []
    return runner


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost."""
    repo = MagicMock()
    repo.list_issues.return_value = []
    repo.get_prs_with_label.return_value = []
    return repo


@pytest.fixture
def mock_action_applier():
    """Create a mock ActionApplier."""
    applier = MagicMock()
    applier.apply = MagicMock()
    return applier


@pytest.fixture
def mock_label_store():
    """Create a mock LabelStore."""
    store = MagicMock()
    store.load_all.return_value = {}
    return store


@pytest.fixture
def mock_issue_branches_fn():
    """Create a mock issue branches provider."""
    return MagicMock(return_value={})


@pytest.fixture
def sample_state():
    """Create a sample orchestrator state."""
    return OrchestratorState()


@pytest.fixture
def startup_manager(
    mock_config,
    mock_events,
    mock_runner,
    mock_repository_host,
    mock_action_applier,
    mock_issue_branches_fn,
    mock_label_store,
):
    """Create a StartupManager with mocks."""
    return StartupManager(
        config=mock_config,
        events=mock_events,
        runner=mock_runner,
        repository_host=mock_repository_host,
        action_applier=mock_action_applier,
        issue_branches_fn=mock_issue_branches_fn,
        session_exists_fn=lambda name: False,
        restore_sessions_fn=MagicMock(),
        launch_session_fn=lambda issue: None,
        update_queue_cache_fn=lambda: None,
        issue_fetch_resilience=IssueFetchResilience("owner/repo"),
        startup_worktree_reconciler=_startup_worktree_reconciler(),
        label_store=mock_label_store,
    )


class TestStartupManagerBasic:
    """Basic tests for StartupManager."""

    @pytest.mark.asyncio
    async def test_run_startup_completes(self, startup_manager, sample_state):
        """Test that startup runs to completion."""

        await startup_manager.run_startup(sample_state)

        assert sample_state.startup_status == "complete"
        assert sample_state.startup_message == ""
        startup_manager._startup_worktree_reconciler.recover.assert_called_once_with(  # noqa: SLF001
            sample_state
        )

    @pytest.mark.asyncio
    async def test_run_startup_emits_config_event(
        self, startup_manager, sample_state, mock_events
    ):
        """Test that startup emits config event."""

        await startup_manager.run_startup(sample_state)

        # Check that config.merged event was published
        calls = mock_events.publish.call_args_list
        assert any("config.merged" in str(call) for call in calls)

    @pytest.mark.asyncio
    async def test_run_startup_emits_ready_event(
        self, startup_manager, sample_state, mock_events
    ):
        """Test that startup emits ready event."""

        await startup_manager.run_startup(sample_state)

        # Check that orchestrator.ready event was published
        calls = mock_events.publish.call_args_list
        assert any("orchestrator.ready" in str(call) for call in calls)


class TestStartupManagerCleanup:
    """Tests for cleanup during startup."""

    @pytest.mark.asyncio
    async def test_cleans_up_idle_sessions(
        self, startup_manager, sample_state, mock_runner
    ):
        """Test that idle sessions are cleaned up."""
        mock_runner.cleanup_idle_sessions.return_value = 3

        await startup_manager.run_startup(sample_state)

        mock_runner.cleanup_idle_sessions.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovers_running_sessions(
        self, startup_manager, sample_state, mock_runner
    ):
        """Test that running sessions are discovered."""
        mock_runner.discover_running_sessions.return_value = [
            {"session_name": "issue-123", "issue_number": 123}
        ]

        await startup_manager.run_startup(sample_state)

        mock_runner.discover_running_sessions.assert_called_once()


class TestStartupManagerInProgressIssues:
    """Tests for handling in-progress issues."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_clears_orphaned_label(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_action_applier,
        mock_repository_host,
        mock_config,
    ):
        """Test that orphaned in-progress labels are cleared."""
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate orphaned label
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        mock_action_applier.apply.assert_called_once()
        action = mock_action_applier.apply.call_args.args[0]
        assert isinstance(action, RemoveLabelAction)
        assert action.issue_number == 1
        assert action.label == "in-progress"

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_reconciles_issues_with_open_prs(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_action_applier,
        mock_repository_host,
        mock_config,
    ):
        """Test that issues with open PRs get pr-pending label and in-progress removed (S2 crash recovery)."""
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has open PR
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/123"
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        # S2 crash recovery: add pr-pending, remove in-progress
        assert mock_action_applier.apply.call_count == 2
        actions = [call.args[0] for call in mock_action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "pr-pending" for a in actions)
        assert any(isinstance(a, RemoveLabelAction) and a.label == "in-progress" for a in actions)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_warm_start_logs_and_recovers_locally_in_progress_issue_missing_from_cache(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        cached_issue = Issue(number=1, title="Cached", labels=["agent:web"])
        missing_issue = Issue(number=4057, title="Missing", labels=["agent:web", "in-progress"])
        sample_state.cached_queue_issues = [cached_issue]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = missing_issue

        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        # 4057 is probed twice for legitimate, distinct reasons: the in-progress
        # recovery refetches it, and the later label_store reconcile re-reads it
        # fresh because startup recovery mutated its labels (clearing the stale
        # in-progress), so its pre-mutation cache entry can't be trusted.
        mock_repository_host.get_issue.assert_any_call(4057)
        assert all(
            call_args == call(4057)
            for call_args in mock_repository_host.get_issue.call_args_list
        )
        analyzed_issues = [call.kwargs["issue"].number for call in mock_analyze.call_args_list]
        assert analyzed_issues == [4057]
        assert any(issue.number == 4057 for issue in sample_state.cached_queue_issues)
        assert "Cached queue omitted 1 locally in-progress issue(s): [4057]" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_check_in_progress_persists_recovered_label_store_issue(
        self,
        mock_analyze,
        mock_config,
        mock_events,
        mock_runner,
        sample_state,
        mock_repository_host,
        mock_action_applier,
        mock_issue_branches_fn,
        mock_label_store,
    ):
        cached_issue = Issue(number=1, title="Cached", labels=["agent:web"])
        missing_issue = Issue(number=4057, title="Missing", labels=["agent:web", "in-progress"])
        sample_state.cached_scope_issues = [cached_issue]
        sample_state.cached_queue_issues = [cached_issue]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = missing_issue
        queue_cache_store = MagicMock()
        queue_cache_store.load_issues.return_value = []
        queue_cache_store.load_watermark.return_value = None
        startup_manager = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=queue_cache_store,
            label_store=mock_label_store,
        )

        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        assert any(issue.number == 4057 for issue in sample_state.cached_scope_issues)
        saved_issues, saved_watermark = queue_cache_store.save_snapshot.call_args.args[:2]
        assert [issue.number for issue in saved_issues] == [1, 4057]
        assert saved_watermark == sample_state.queue_delta_watermark
        assert queue_cache_store.save_snapshot.call_args.kwargs == {"repo": "owner/repo"}

    @pytest.mark.asyncio
    async def test_warm_start_logs_when_cached_queue_matches_local_in_progress(
        self,
        startup_manager,
        sample_state,
        mock_label_store,
        caplog,
    ):
        sample_state.cached_queue_issues = [
            Issue(number=4057, title="Cached", labels=["agent:web", "in-progress"]),
        ]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert "Local label store and cached queue agree on 1 in-progress issue(s)" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_warm_start_logs_when_missing_locally_in_progress_issue_cannot_be_refetched(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        sample_state.cached_queue_issues = [
            Issue(number=1, title="Cached", labels=["agent:web"]),
        ]
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = None

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        # The in-progress recovery probes 4057 (and fails). The later
        # label_store reconcile backstop, finding 4057 only in the store and
        # not the warm cache, also probes it — and likewise leaves the mirror
        # untouched on a None result. Both legitimately consult get_issue, so
        # assert the probe happened rather than pinning an exact call count.
        mock_repository_host.get_issue.assert_any_call(4057)
        assert all(
            call_args == call(4057)
            for call_args in mock_repository_host.get_issue.call_args_list
        )
        mock_analyze.assert_not_called()
        assert "Cached queue omitted 1 locally in-progress issue(s): [4057]" in caplog.text
        assert "Failed to refetch locally in-progress issue missing from cache: issue=4057" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_warm_start_deduplicates_recovered_issue_still_marked_in_progress_on_github(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
    ):
        sample_state.cached_queue_issues = [
            Issue(number=1, title="Cached", labels=["agent:web"]),
        ]
        recovered_issue = Issue(number=4057, title="Recovered", labels=["agent:web", "in-progress"])
        mock_label_store.load_all.return_value = {
            4057: {"in-progress", "agent:web"},
        }
        mock_repository_host.get_issue.return_value = recovered_issue

        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = False
        mock_state.is_orphaned_label = True
        mock_analyze.return_value = mock_state

        await startup_manager.run_startup(sample_state)

        analyzed_issues = [call.kwargs["issue"].number for call in mock_analyze.call_args_list]
        assert analyzed_issues == [4057]


class TestStartupManagerLabelStoreReconcile:
    """Startup reconciles the local label_store mirror against GitHub."""

    @pytest.mark.asyncio
    async def test_reconcile_prunes_stale_store_label_using_cache(
        self, startup_manager, sample_state, mock_label_store, mock_repository_host,
    ):
        # Store still records publish-failed for a recovered issue; the warm
        # cache shows GitHub no longer carries it, so the mirror is pruned with
        # zero extra GitHub calls.
        sample_state.cached_queue_issues = [
            Issue(number=228, title="Done", labels=["agent:backend"]),
        ]
        mock_label_store.load_all.return_value = {228: {"publish-failed"}}

        await startup_manager.run_startup(sample_state)

        # The reconciler rewrites the issue's rows atomically via save_labels;
        # GitHub no longer carries the orchestrator label, so the desired set is
        # empty.
        mock_label_store.save_labels.assert_any_call(228, set())
        mock_repository_host.get_issue.assert_not_called()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_reconcile_does_not_undo_startup_pr_pending_recovery(
        self,
        mock_analyze,
        mock_config,
        mock_events,
        mock_runner,
        mock_issue_branches_fn,
        sample_state,
        tmp_path,
    ):
        """Reconciliation must not revert startup's own pr-pending recovery.

        Startup recovery detects an open PR for a cached in-progress issue and
        applies pr-pending / removes in-progress. Those mutations write through
        to label_store, so the issue's pre-mutation warm-cache entry is now
        stale. The later label_store reconciliation must read GitHub fresh for
        that issue rather than treating the stale snapshot as truth — otherwise
        it restores in-progress and drops the freshly persisted pr-pending,
        re-introducing the very drift the change eliminates (#6431 F1).
        """
        # Real store + a fake GitHub label surface so the write-through and the
        # reconciler operate on genuine state, not hand-coded mocks.
        store = LabelStore(tmp_path / "label_store.sqlite")
        store.add_label(1, "in-progress")  # orchestrator claim before the crash
        github_labels = _FakeLabelSet({1: {"agent:web", "in-progress"}})

        applier = ActionApplier(
            labels=github_labels,
            sessions=MagicMock(),
            events=mock_events,
            label_store=store,
        )

        mock_repository_host = MagicMock()
        mock_repository_host.list_issues.return_value = []
        mock_repository_host.get_prs_with_label.return_value = []
        # Fresh GitHub reads reflect the live (post-mutation) label surface.
        mock_repository_host.get_issue.side_effect = lambda n: Issue(
            number=n, title=f"Issue {n}", labels=sorted(github_labels.labels_for(n))
        )

        startup_manager = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            label_store=store,
        )

        # Warm cache snapshot captured before recovery: still in-progress.
        sample_state.cached_queue_issues = [
            Issue(number=1, title="Issue 1", labels=["agent:web", "in-progress"]),
        ]

        analysis = MagicMock()
        analysis.has_session = False
        analysis.has_open_pr = True
        analysis.has_partial_work = False
        analysis.is_orphaned_label = False
        analysis.pr_url = "https://github.com/owner/repo/pull/9"
        mock_analyze.return_value = analysis

        await startup_manager.run_startup(sample_state)

        # Recovery applied the swap on both GitHub and the mirror...
        assert github_labels.labels_for(1) == {"agent:web", "pr-pending"}
        # ...and reconciliation left the mirror in agreement with GitHub
        # instead of resurrecting in-progress / dropping pr-pending.
        assert store.load_labels(1) == {"pr-pending"}

    def _make_degraded_startup(
        self,
        tmp_path,
        mock_config,
        mock_events,
        mock_runner,
        mock_issue_branches_fn,
        get_issue,
    ):
        """Build a StartupManager whose queue sync degrades to a stale snapshot.

        - label_store already holds the current set ({pr-pending}) from a prior
          run's recovery.
        - The persisted queue snapshot is stale (still pre-mutation in-progress).
        - The delta sync fails transiently, forcing the degraded fallback onto
          that stale snapshot.
        - ``get_issue`` controls what a fresh per-issue read returns.
        """
        store = LabelStore(tmp_path / "label_store.sqlite")
        store.add_label(1, "pr-pending")

        queue_cache_store = MagicMock()
        queue_cache_store.load_issues.return_value = [
            Issue(number=1, title="Issue 1", labels=["agent:web", "in-progress"]),
        ]
        queue_cache_store.load_watermark.return_value = "2025-01-01T00:00:00Z"

        mock_repository_host = MagicMock()
        mock_repository_host.get_prs_with_label.return_value = []
        mock_repository_host.list_issues_delta.side_effect = RepositoryHostError(
            "transient GitHub blip"
        )
        mock_repository_host.get_issue.side_effect = get_issue

        sm = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=MagicMock(),
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=queue_cache_store,
            label_store=store,
        )
        return sm, store, mock_repository_host

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_degraded_startup_does_not_reconcile_from_stale_snapshot(
        self,
        mock_analyze,
        mock_config,
        mock_events,
        mock_runner,
        mock_issue_branches_fn,
        sample_state,
        tmp_path,
    ):
        """A degraded startup must not treat the persisted snapshot as truth.

        When the GitHub queue refresh fails transiently, startup comes up on the
        persisted last-known-good snapshot, which can predate a prior run's
        label mutation. Reconciliation must read GitHub fresh for such issues
        rather than rewriting the mirror back to the stale persisted labels
        (#6431 round-2 F1).
        """
        mock_analyze.return_value = MagicMock(
            has_session=False,
            has_open_pr=False,
            has_partial_work=False,
            is_orphaned_label=False,
        )

        sm, store, repo = self._make_degraded_startup(
            tmp_path,
            mock_config,
            mock_events,
            mock_runner,
            mock_issue_branches_fn,
            # Live GitHub still carries the current pr-pending, not in-progress.
            get_issue=lambda n: Issue(
                number=n, title=f"Issue {n}", labels=["agent:web", "pr-pending"]
            ),
        )

        await sm.run_startup(sample_state)

        # Reconciliation read GitHub fresh and left the mirror at the current
        # pr-pending instead of resurrecting the stale persisted in-progress.
        assert store.load_labels(1) == {"pr-pending"}
        repo.get_issue.assert_any_call(1)

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_degraded_startup_leaves_store_untouched_when_fresh_read_fails(
        self,
        mock_analyze,
        mock_config,
        mock_events,
        mock_runner,
        mock_issue_branches_fn,
        sample_state,
        tmp_path,
    ):
        """Degraded startup + failed fresh read must leave the mirror untouched.

        With no verified-fresh cache to trust and the per-issue read returning
        nothing, the reconciler must not guess from the stale persisted
        snapshot — it leaves the store as-is for a later startup to retry.
        """
        mock_analyze.return_value = MagicMock(
            has_session=False,
            has_open_pr=False,
            has_partial_work=False,
            is_orphaned_label=False,
        )

        sm, store, repo = self._make_degraded_startup(
            tmp_path,
            mock_config,
            mock_events,
            mock_runner,
            mock_issue_branches_fn,
            get_issue=lambda n: None,  # fresh read unavailable
        )

        await sm.run_startup(sample_state)

        assert store.load_labels(1) == {"pr-pending"}
        repo.get_issue.assert_any_call(1)


class TestStartupManagerCodeReviewRecovery:
    """Tests for code review recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_reviews(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that pending code reviews are recovered."""
        mock_config.agents = {}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-code-review"

        from issue_orchestrator.ports import PRInfo
        pr = PRInfo(
            number=10,
            url="https://github.com/owner/repo/pull/10",
            title="Test PR",
            branch="1-feature",
            labels=["needs-code-review"],
            body=f"Closes #1\n\n{ORCHESTRATOR_PR_MARKER}",
            state="open",
        )
        mock_repository_host.get_prs_with_label.return_value = [pr]
        mock_repository_host.get_issue.return_value = Issue(
            number=1,
            title="Issue with review PR",
            labels=["agent:web"],
            repo="owner/repo",
        )

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_reviews) == 1
        assert sample_state.pending_reviews[0].pr_number == 10

    @pytest.mark.asyncio
    async def test_skips_pending_review_pr_without_orchestrator_marker(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Startup review recovery ignores manually-created PRs."""
        mock_config.agents = {}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-code-review"

        from issue_orchestrator.ports import PRInfo
        pr = PRInfo(
            number=10,
            url="https://github.com/owner/repo/pull/10",
            title="Manual PR",
            branch="1-feature",
            labels=["needs-code-review"],
            body="Closes #1",
            state="open",
        )
        mock_repository_host.get_prs_with_label.return_value = [pr]

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_reviews == []
        mock_repository_host.get_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_pending_review_pr_from_prior_attempt_branch(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
        mock_issue_branches_fn,
        caplog,
    ):
        mock_config.agents = {}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-code-review"
        mock_issue_branches_fn.return_value = {1: "1-fresh-branch"}

        from issue_orchestrator.ports import PRInfo
        pr = PRInfo(
            number=10,
            url="https://github.com/owner/repo/pull/10",
            title="Test PR",
            branch="1-old-branch",
            labels=["needs-code-review"],
            body=f"Closes #1\n\n{ORCHESTRATOR_PR_MARKER}",
            state="open",
        )
        mock_repository_host.get_prs_with_label.return_value = [pr]
        mock_repository_host.get_issue.return_value = Issue(
            number=1,
            title="Issue with stale review PR",
            labels=["agent:web"],
            repo="owner/repo",
        )

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert sample_state.pending_reviews == []
        assert (
            "Ignoring review PR from prior attempt: pr=10 issue=1 branch=1-old-branch expected_branch=1-fresh-branch"
            in caplog.text
        )

    @pytest.mark.asyncio
    async def test_skips_stale_pending_review_for_blocked_issue(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
        caplog,
    ):
        mock_config.agents = {}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-code-review"

        from issue_orchestrator.ports import PRInfo

        pr = PRInfo(
            number=10,
            url="https://github.com/owner/repo/pull/10",
            title="Test PR",
            branch="1-feature",
            labels=["needs-code-review"],
            body=f"Closes #1\n\n{ORCHESTRATOR_PR_MARKER}",
            state="open",
        )
        issue = Issue(
            number=1,
            title="Blocked issue",
            labels=["agent:web", "blocked-failed", "needs-rework"],
            repo="owner/repo",
        )
        mock_repository_host.get_prs_with_label.return_value = [pr]
        mock_repository_host.get_issue.return_value = issue

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert sample_state.pending_reviews == []
        assert (
            "Dropping stale pending review recovery: pr=10 issue=1 reason=issue_blocked"
            in caplog.text
        )


class TestStartupManagerAwaitingMergeRecovery:
    """Tests for dashboard visibility recovery of pr-pending issues."""

    @pytest.mark.asyncio
    async def test_recovers_branchless_pr_pending_issue_with_open_pr_into_session_history(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        issue = Issue(number=4057, title="Provider circuit breaker UI", labels=["agent:backend", "pr-pending"])
        mock_label_store.load_all.return_value = {
            4057: {"agent:backend", "pr-pending"},
        }
        mock_repository_host.get_issue.return_value = issue
        mock_repository_host.get_prs_for_issue.return_value = [
            PRInfo(
                number=5337,
                url="https://github.com/owner/repo/pull/5337",
                title="#4057: Provider circuit breaker UI",
                branch="4057-provider-circuit-breaker-ui",
                labels=["code-reviewed"],
                body="",
                state="open",
            )
        ]

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert len(sample_state.session_history) == 1
        entry = sample_state.session_history[0]
        assert entry.issue_number == 4057
        assert entry.pr_url == "https://github.com/owner/repo/pull/5337"
        assert 4057 in sample_state.issue_refresh_timestamps
        assert 4057 in sample_state.issue_last_refreshed_at
        assert sample_state.issue_refresh_timestamps[4057] == pytest.approx(
            sample_state.issue_last_refreshed_at[4057]
        )
        mock_repository_host.get_prs_for_issue.assert_called_once_with(4057, state="open")
        assert "Recovered 1 pr-pending issue(s) into dashboard history" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_recovers_pr_pending_issue_into_session_history(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        issue = Issue(number=4057, title="Provider circuit breaker UI", labels=["agent:backend", "pr-pending"])
        mock_label_store.load_all.return_value = {
            4057: {"agent:backend", "pr-pending"},
        }
        mock_repository_host.get_issue.return_value = issue

        mock_state = MagicMock()
        mock_state.has_open_pr = True
        mock_state.pr_url = "https://github.com/owner/repo/pull/5337"
        mock_analyze.return_value = mock_state

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert len(sample_state.session_history) == 1
        entry = sample_state.session_history[0]
        assert entry.issue_number == 4057
        assert entry.status == "completed"
        assert entry.pr_url == "https://github.com/owner/repo/pull/5337"
        assert entry.agent_type == "agent:backend"
        assert 4057 in sample_state.issue_refresh_timestamps
        assert 4057 in sample_state.issue_last_refreshed_at
        assert sample_state.issue_refresh_timestamps[4057] == pytest.approx(
            sample_state.issue_last_refreshed_at[4057]
        )
        assert "Recovered 1 pr-pending issue(s) into dashboard history" in caplog.text

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_skips_pr_pending_history_recovery_without_open_pr(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_label_store,
        caplog,
    ):
        issue = Issue(number=4057, title="Provider circuit breaker UI", labels=["agent:backend", "pr-pending"])
        mock_label_store.load_all.return_value = {
            4057: {"agent:backend", "pr-pending"},
        }
        mock_repository_host.get_issue.return_value = issue

        mock_state = MagicMock()
        mock_state.has_open_pr = False
        mock_state.pr_url = None
        mock_analyze.return_value = mock_state

        with caplog.at_level("INFO"):
            await startup_manager.run_startup(sample_state)

        assert sample_state.session_history == []
        assert "Skipping pr-pending dashboard recovery without open PR: issue=4057" in caplog.text


class TestStartupManagerTechLeadRecovery:
    """Tests for tech_lead review recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_tech_lead(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Test that pending tech_lead reviews are recovered."""
        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        tech_lead_issue = Issue(
            number=100,
            title="Batch Review: 5 PRs",
            labels=["agent:tech-lead"]
        )
        mock_repository_host.list_issues.return_value = [tech_lead_issue]

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_tech_lead_reviews) == 1
        assert sample_state.pending_tech_lead_reviews[0].issue_number == 100
        # Recovery restores agent-labeled batch tracking issues; the queue
        # entry must say so or the launch runs it as a failure investigation
        # that skips manifest prep and audits nothing (#6768 B5).
        assert (
            sample_state.pending_tech_lead_reviews[0].flavor
            is TechLeadSessionFlavor.BATCH_REVIEW
        )

    @pytest.mark.asyncio
    async def test_recovered_tech_lead_launches_with_batch_flavor(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
        tmp_path,
    ):
        """Startup-recovery -> launch boundary keeps the batch flavor (#6768 B5)."""
        from issue_orchestrator.execution.pending_work_claim_store import (
            SqlitePendingWorkClaimStore,
        )

        mock_config.agents = {"agent:tech-lead": MagicMock()}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        tech_lead_issue = Issue(
            number=100,
            title="Batch Review: 5 PRs",
            labels=["agent:tech-lead"]
        )
        mock_repository_host.list_issues.return_value = [tech_lead_issue]

        await startup_manager.run_startup(sample_state)

        (recovered,) = sample_state.pending_tech_lead_reviews
        launcher = MagicMock()
        launcher.launch_issue_session.return_value = LaunchResult(
            session=None, success=False
        )
        launcher.session_manager.runner.discover_running_sessions.return_value = []

        orchestrator_launch_tech_lead_session(
            recovered, sample_state, mock_config, launcher, MagicMock(),
            # The real orchestrator-owned ledger, because the launch settles a
            # durable claim as well as the queue item (#6999 F4).
            SqlitePendingWorkClaimStore.for_repo(tmp_path),
        )

        launch_call = launcher.launch_issue_session.call_args
        assert launch_call.args[0].number == 100
        assert launch_call.kwargs["tech_lead_scope"].flavor is TechLeadSessionFlavor.BATCH_REVIEW

    @pytest.mark.asyncio
    async def test_recovers_marker_labeled_anchor_as_health_review(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """A marker-labeled anchor recovers with the HEALTH flavor (ADR-0031 §4).

        The queued flavor is forwarded verbatim to launch (#6768 B5), so
        recovering the anchor as BATCH_REVIEW would relaunch it as a batch
        audit — manifest prep, batch authority, manifest labels on
        completion. The marker label is the crash-safe classifier.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        mock_repository_host.list_issues.return_value = [
            Issue(
                number=200,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            ),
            Issue(number=100, title="Batch Review: 5 PRs", labels=["agent:tech-lead"]),
        ]

        await startup_manager.run_startup(sample_state)

        by_number = {
            item.issue_number: item for item in sample_state.pending_tech_lead_reviews
        }
        assert set(by_number) == {100, 200}
        assert by_number[200].flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert by_number[200].failure is None
        assert by_number[100].flavor is TechLeadSessionFlavor.BATCH_REVIEW

    @pytest.mark.asyncio
    async def test_recovery_ignores_closed_batch_tracking_issue(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """A COMPLETED (closed) batch tracking issue is never requeued (#6768 r4).

        Successful batch completion closes the tracking issue; recovery asks
        the host only for OPEN agent-labeled issues (the port's default state),
        so the closed batch disappears from the recovery query.
        """
        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

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

        mock_repository_host.list_issues.side_effect = list_issues

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_tech_lead_reviews == []
        # The recovery query must not widen to closed issues.
        for query in mock_repository_host.list_issues.call_args_list:
            assert query.kwargs.get("state", "open") == "open"

    @pytest.mark.asyncio
    async def test_recovery_skips_already_queued_tech_lead(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Recovery routes through the queue owner and never double-queues."""
        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        existing = PendingTechLeadReview(
            issue_number=100,
            title="Batch Review: 5 PRs",
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        )
        sample_state.pending_tech_lead_reviews.append(existing)

        tech_lead_issue = Issue(
            number=100,
            title="Batch Review: 5 PRs",
            labels=["agent:tech-lead"]
        )
        mock_repository_host.list_issues.return_value = [tech_lead_issue]

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_tech_lead_reviews == [existing]

    @pytest.mark.asyncio
    async def test_recovery_ignores_anchor_outside_filter_scope(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """Startup and fact gathering share ONE eligibility rule: a run-scoped
        restart must NOT queue another run's anchor (#6763 finding 7).

        The out-of-scope anchor carries the tech lead agent label but not the
        active ``filtering.label``; the shared scoped discovery owner drops it.
        """
        from issue_orchestrator.domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
        )

        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.filtering.label = "io:e2e:run-1"

        mock_repository_host.list_issues.return_value = [
            Issue(
                number=200,
                title="Health Review — walk the floor",
                labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            ),
        ]

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_tech_lead_reviews == []

    @pytest.mark.asyncio
    async def test_recovery_discovers_more_than_twenty_anchors(
        self,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_config,
    ):
        """The old startup scan capped at limit=20 and could strand an older
        anchor; the shared exhaustive owner recovers all of them (#6763 f7)."""
        mock_config.agents = {}
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        anchors = [
            Issue(number=n, title=f"Batch Review {n}", labels=["agent:tech-lead"])
            for n in range(1, 26)
        ]

        def list_issues(labels=None, state="open", limit=100, **kwargs):
            # Honor GitHub's page limit so a regression back to limit=20 would
            # strand the tail — exactly the bug this guards.
            del labels, state, kwargs
            return anchors[:limit]

        mock_repository_host.list_issues.side_effect = list_issues

        await startup_manager.run_startup(sample_state)

        assert {item.issue_number for item in sample_state.pending_tech_lead_reviews} == {
            n for n in range(1, 26)
        }
        # The discovery query asked for a page large enough to hold them all.
        assert any(
            call.kwargs.get("limit", 0) >= 25
            for call in mock_repository_host.list_issues.call_args_list
        )


class TestStartupManagerResumePartialWork:
    """Tests for resuming partial work."""

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_resumes_partial_work(
        self,
        mock_analyze,
        startup_manager,
        sample_state,
        mock_repository_host,
        mock_action_applier,
        mock_config,
        mock_issue_branches_fn,
    ):
        """Test that issues with partial work are resumed."""
        mock_issue_branches_fn.return_value = {1: "1-feature"}
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        # Mock analyze_issue to indicate has partial work
        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "1-feature"
        mock_analyze.return_value = mock_state

        # Mock launch_session_fn to return a session
        mock_session = MagicMock()
        # noqa: SLF001 - Injecting mock for session launch in test
        startup_manager._launch_session = lambda i: mock_session  # noqa: SLF001

        await startup_manager.run_startup(sample_state)

        # The session should have been launched (check via callback)
        # Since we mocked the callback, we verify state wasn't modified with label removal
        mock_action_applier.apply.assert_not_called()

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_start_paused_queues_partial_work_without_launching(
        self,
        mock_analyze,
        sample_state,
        mock_config,
        mock_events,
        mock_runner,
        mock_repository_host,
        mock_action_applier,
        mock_issue_branches_fn,
        mock_label_store,
    ):
        """Start-paused recovery must not launch fresh sessions for partial work."""
        sample_state.paused = True
        mock_issue_branches_fn.return_value = {1: "1-feature"}
        mock_config.agents = {"agent:web": MagicMock()}

        issue = Issue(number=1, title="Test Issue", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]

        mock_state = MagicMock()
        mock_state.has_session = False
        mock_state.has_open_pr = False
        mock_state.has_partial_work = True
        mock_state.branch = "1-feature"
        mock_analyze.return_value = mock_state

        launch_session = MagicMock()
        manager = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=launch_session,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            label_store=mock_label_store,
        )

        await manager.run_startup(sample_state)

        launch_session.assert_not_called()
        assert sample_state.active_sessions == []
        assert sample_state.priority_queue == [1]

    @pytest.mark.asyncio
    @patch("issue_orchestrator.control.startup_manager.analyze_issue")
    async def test_reserved_tech_lead_does_not_block_partial_worker_resume(
        self,
        mock_analyze,
        sample_state,
        mock_config,
        mock_events,
        mock_runner,
        mock_repository_host,
        mock_action_applier,
        mock_issue_branches_fn,
        mock_label_store,
    ):
        """Startup recovery uses the worker lane, not total session count."""
        from tests.unit.test_planner import make_session

        mock_config.max_concurrent_sessions = 1
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead.max_concurrent = 1
        mock_config.agents = {"agent:web": MagicMock()}
        mock_issue_branches_fn.return_value = {1: "1-feature"}

        issue = Issue(number=1, title="Partial work", labels=["agent:web", "in-progress"])
        mock_repository_host.list_issues.return_value = [issue]
        mock_analyze.return_value = MagicMock(
            has_session=False,
            has_open_pr=False,
            has_partial_work=True,
            branch="1-feature",
        )

        tech_lead_session = make_session(
            Issue(number=9, title="Tech lead", labels=["agent:tech-lead"])
        )
        tech_lead_session.agent_label = "agent:tech-lead"
        sample_state.active_sessions = [tech_lead_session]
        launch_session = MagicMock(return_value=MagicMock())
        manager = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=launch_session,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            label_store=mock_label_store,
        )

        await manager.run_startup(sample_state)

        launch_session.assert_called_once_with(issue)
        assert sample_state.priority_queue == []


class TestStartupManagerValidationRetryRecovery:
    """Tests for validation retry state recovery."""

    @pytest.mark.asyncio
    async def test_recovers_pending_validation_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that pending validation retries are recovered from worktree state."""
        from issue_orchestrator.infra.validation_state import (
            ValidationState,
            write_validation_state,
            write_retry_prompt,
        )

        # Set up worktree path
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Create validation state in the worktree
        state = ValidationState(
            retry_count=1,
            max_retries=3,
            validation_cmd="make test",
            last_error="Test failed: assertion error",
        )
        write_validation_state(worktree, state)
        write_retry_prompt(
            worktree,
            original_prompt="Fix the login bug",
            validation_cmd="make test",
            validation_error="Test failed: assertion error",
            retry_count=1,
            max_retries=3,
        )

        # Set up issue branches to include this issue
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        # Verify the pending validation retry was recovered
        assert len(sample_state.pending_validation_retries) == 1
        retry = sample_state.pending_validation_retries[0]
        assert retry.issue_number == 42
        assert retry.retry_count == 1
        assert retry.validation_cmd == "make test"
        assert "Test failed" in retry.validation_error

    @pytest.mark.asyncio
    async def test_recovers_run_scoped_pending_validation_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Pending validation retries are recovered from current run artifacts."""
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260501-010000Z__coding-1"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({"validation_status": "retry"}))
        (run_dir / "validation-state.json").write_text(
            json.dumps(
                {
                    "retry_count": 1,
                    "max_retries": 3,
                    "validation_cmd": "make test",
                    "last_error": "Test failed from run dir",
                    "last_error_file": "validation-stderr.log",
                }
            )
        )
        (run_dir / "retry-prompt.md").write_text("retry prompt from run dir")
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_validation_retries) == 1
        retry = sample_state.pending_validation_retries[0]
        assert retry.issue_number == 42
        assert retry.retry_count == 1
        assert retry.original_prompt == "retry prompt from run dir"
        assert retry.validation_cmd == "make test"
        assert "run dir" in retry.validation_error

    @pytest.mark.asyncio
    async def test_terminal_run_scoped_validation_status_suppresses_stale_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """A later pass/fail validation result prevents stale retry recovery."""
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        stale_run = worktree / ".issue-orchestrator" / "sessions" / "20260501-010000Z__coding-1"
        stale_run.mkdir(parents=True)
        (stale_run / "manifest.json").write_text(json.dumps({"validation_status": "retry"}))
        (stale_run / "validation-state.json").write_text(
            json.dumps(
                {
                    "retry_count": 1,
                    "max_retries": 3,
                    "validation_cmd": "make test",
                    "last_error": "Old failure",
                }
            )
        )
        terminal_run = worktree / ".issue-orchestrator" / "sessions" / "20260501-010100Z__coding-2"
        terminal_run.mkdir(parents=True)
        (terminal_run / "manifest.json").write_text(json.dumps({"validation_status": "passed"}))
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        assert len(sample_state.pending_validation_retries) == 0

    @pytest.mark.asyncio
    async def test_review_only_run_scoped_retry_artifact_is_not_recovered(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """A retry artifact under a review-only run is never recovered as a coding retry.

        Regression for #6426: a validation-state.json left under a
        ``...__retrospective-review-<issue>`` run (pre-fix bug or crash boundary)
        must not relaunch as TaskKind.CODE and open a PR on an empty branch.
        """
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260501-010000Z__retrospective-review-42"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "session_name": "retrospective-review-42",
                    "validation_status": "retry",
                }
            )
        )
        (run_dir / "validation-state.json").write_text(
            json.dumps(
                {
                    "retry_count": 1,
                    "max_retries": 3,
                    "validation_cmd": "make test",
                    "last_error": "Stale review-only failure",
                }
            )
        )
        (run_dir / "retry-prompt.md").write_text("stale retry prompt")
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_validation_retries == []

    @pytest.mark.asyncio
    async def test_unrecognized_run_scoped_retry_artifact_is_not_recovered(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """A retry artifact under an unrecognized run identity is refused, not coerced to CODE.

        Regression for #6426: provenance must be explicit. An unclassified
        ``...__mystery-<issue>`` run with retry state must not be silently
        relaunched as a coding (TaskKind.CODE) validation retry.
        """
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        run_dir = (
            worktree
            / ".issue-orchestrator"
            / "sessions"
            / "20260501-010000Z__mystery-42"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"session_name": "mystery-42", "validation_status": "retry"})
        )
        (run_dir / "validation-state.json").write_text(
            json.dumps(
                {
                    "retry_count": 1,
                    "max_retries": 3,
                    "validation_cmd": "make test",
                    "last_error": "Unknown-provenance failure",
                }
            )
        )
        (run_dir / "retry-prompt.md").write_text("mystery retry prompt")
        mock_issue_branches_fn.return_value = {42: "42-fix-login"}

        await startup_manager.run_startup(sample_state)

        assert sample_state.pending_validation_retries == []

    @pytest.mark.asyncio
    async def test_no_recovery_when_no_pending_retry(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that worktrees without pending retries are not recovered."""
        # Set up worktree path without validation state
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Set up issue branches to include this issue
        mock_issue_branches_fn.return_value = {42: "42-feature"}

        await startup_manager.run_startup(sample_state)

        # Verify no pending validation retry was created
        assert len(sample_state.pending_validation_retries) == 0

    @pytest.mark.asyncio
    async def test_no_recovery_when_max_retries_exhausted(
        self,
        startup_manager,
        sample_state,
        mock_config,
        mock_issue_branches_fn,
        tmp_path,
    ):
        """Test that retries at max count are not recovered."""
        from issue_orchestrator.infra.validation_state import (
            ValidationState,
            write_validation_state,
        )

        # Set up worktree path
        mock_config.worktree_base = tmp_path
        worktree = tmp_path / f"{mock_config.repo_root.name}-42"
        worktree.mkdir()

        # Create validation state past max retries
        state = ValidationState(
            retry_count=4,
            max_retries=3,
            validation_cmd="make test",
        )
        write_validation_state(worktree, state)

        # Set up issue branches
        mock_issue_branches_fn.return_value = {42: "42-feature"}

        await startup_manager.run_startup(sample_state)

        # Verify no pending retry (max exhausted)
        assert len(sample_state.pending_validation_retries) == 0


class TestStartupGitHubCallBudget:
    """Verify startup makes only the expected number of GitHub API calls.

    This prevents regressions that silently add extra API calls to the
    startup path, which compound with agents × milestones.

    With no queue_cache_store, the startup path is:
    - Step 5: queue cache restore (no-op without store)
    - Step 6: in-progress check (cold fallback, per-agent list_issues)
    - Step 12: audit (list_issues via fetch_all_issues)

    With a warm cache store, the startup path is:
    - Step 5: queue cache restore (1 list_issues_delta call)
    - Step 6: in-progress check (filters from cache, 0 list_issues)
    - Step 12: audit (uses preloaded cache, 0 list_issues)
    """

    @pytest.mark.asyncio
    async def test_cold_start_call_count_one_agent_no_milestones(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Cold start (no store) with 1 agent, no milestones: 2 list_issues.

        - Step 6: 1 list_issues (in-progress cold fallback, 1 agent × 1 milestone)
        - Step 12: 1 list_issues (audit fetch, 1 agent × 1 milestone)
        Total: 2 list_issues, 0 get_prs_with_label
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 2
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_cold_start_call_count_two_agents_two_milestones(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Cold start (no store) with 2 agents, 2 milestones: 8 list_issues.

        - Step 6: 4 list_issues (2 agents × 2 milestones, cold fallback)
        - Step 12: 4 list_issues (2 agents × 2 milestones, audit fetch)
        Total: 8 list_issues, 0 get_prs_with_label
        """
        mock_config.agents = {
            "agent:a": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30),
            "agent:b": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30),
        }
        mock_config.filtering.milestones = ["M1", "M2"]
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 8
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_cold_start_with_code_review_adds_one_pr_call(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Code review config adds exactly 1 get_prs_with_label call."""
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_review_label = "needs-review"
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 2
        assert mock_repository_host.get_prs_with_label.call_count == 1

    @pytest.mark.asyncio
    async def test_no_agents_makes_zero_list_issues_calls(
        self, startup_manager, sample_state, mock_repository_host,
    ):
        """With no agents configured, startup should make zero list_issues calls."""
        await startup_manager.run_startup(sample_state)

        assert mock_repository_host.list_issues.call_count == 0
        assert mock_repository_host.get_prs_with_label.call_count == 0

    @pytest.mark.asyncio
    async def test_warm_start_uses_delta_only(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Warm restart with cache: 0 list_issues, 1 list_issues_delta.

        When queue_cache_store has cached issues + watermark:
        - Step 5: 1 list_issues_delta (delta sync from cache)
        - Step 6: 0 list_issues (filters in-progress from cache)
        - Step 12: 0 list_issues (audit uses preloaded cache)
        Total: 0 list_issues, 1 list_issues_delta
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        # Pre-populate queue cache store with issues + watermark
        mock_store = MagicMock()
        mock_store.load_issues.return_value = [
            Issue(number=1, title="Cached Issue", labels=["agent:test"]),
        ]
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"

        # Delta sync returns no changes
        mock_repository_host.list_issues_delta.return_value = ([], "2025-01-01T00:00:01Z")

        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=mock_store,
        )

        await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues.call_count == 0
        assert mock_repository_host.list_issues_delta.call_count == 1

    @pytest.mark.asyncio
    async def test_warm_start_hydrates_last_health_review_at(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        """Restart hydrates state.last_health_review_at from the durable store.

        Without hydration a restart would see 0.0 and re-fire the periodic
        health review immediately regardless of the interval (ADR-0031 §4).
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        mock_store = MagicMock()
        mock_store.load_issues.return_value = [
            Issue(number=1, title="Cached Issue", labels=["agent:test"]),
        ]
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"
        mock_store.load_last_health_review_at.return_value = 1750000000.25
        mock_repository_host.list_issues_delta.return_value = ([], "2025-01-01T00:00:01Z")

        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=mock_store,
        )

        state = OrchestratorState()
        await sm.run_startup(state)

        assert state.last_health_review_at == 1750000000.25

    @pytest.mark.asyncio
    async def test_warm_start_preserves_blocked_scope_issues(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn,
    ):
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        mock_store = MagicMock()
        mock_store.load_issues.return_value = [
            Issue(number=1, title="Blocked", labels=["agent:test", "publish-failed"]),
        ]
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"
        mock_repository_host.list_issues_delta.return_value = ([], "2025-01-01T00:00:01Z")

        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=mock_store,
        )

        state = OrchestratorState()
        await sm.run_startup(state)

        assert [issue.number for issue in state.cached_scope_issues] == [1]
        assert [issue.number for issue in state.cached_queue_issues] == [1]

    @pytest.mark.asyncio
    async def test_warm_start_empty_cache_with_watermark_forces_full_scan(
        self, mock_config, mock_events, mock_runner, mock_repository_host,
        mock_action_applier, mock_issue_branches_fn, caplog,
    ):
        """Empty cached issues + valid watermark is a corrupt state.

        If an earlier run wiped persisted issues but left the watermark, a
        delta sync from that watermark would miss every issue whose GitHub
        state did not change afterwards — silently stranding them. Force a
        cold full scan and log a warning so the trail is visible.
        """
        mock_config.agents = {"agent:test": AgentConfig(prompt_path=mock_config.repo_root / "prompt.md", timeout_minutes=30)}

        mock_store = MagicMock()
        mock_store.load_issues.return_value = []  # Empty despite watermark
        mock_store.load_watermark.return_value = "2025-01-01T00:00:00Z"

        update_queue_fn = MagicMock()
        sm = StartupManager(
            config=mock_config, events=mock_events, runner=mock_runner,
            repository_host=mock_repository_host, action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=MagicMock(),
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=update_queue_fn,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            queue_cache_store=mock_store,
        )

        caplog.clear()
        with caplog.at_level("WARNING", logger="issue_orchestrator.control.startup_manager"):
            await sm.run_startup(OrchestratorState())

        assert mock_repository_host.list_issues_delta.call_count == 0
        update_queue_fn.assert_called_once()
        assert any(
            "Queue cache inconsistency" in r.message for r in caplog.records
        ), caplog.text


class TestRetrospectiveRecoveryCallBudget:
    """Startup-timing regression guard for retrospective-review recovery.

    The ``recover_pending_retrospective_reviews`` startup phase once spent ~29s
    because it resolved each trigger-labeled issue's prior PR by searching for
    and hydrating every candidate PR — O(issues x PRs) serial GitHub calls. This
    drives the real ``StartupManager._recover_pending_retrospective_reviews`` and
    pins its GitHub-call budget to a single label query, independent of how many
    issues match and how many PRs each has. We assert call counts (deterministic)
    rather than wall-clock (flaky) because call count is the actual regression.
    """

    def _enable_retrospective(self, config) -> None:
        config.retrospective_review_enabled = True
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.code_review_agent = "agent:reviewer"
        config.agents = {
            "agent:web": AgentConfig(prompt_path=Path("/tmp/web.md")),
            "agent:reviewer": AgentConfig(prompt_path=Path("/tmp/reviewer.md")),
        }

    def test_recovery_call_budget_is_constant(
        self, startup_manager, mock_repository_host, sample_state
    ):
        self._enable_retrospective(startup_manager.config)
        issue_count = 25
        mock_repository_host.list_issues.return_value = [
            Issue(
                number=n,
                title=f"Issue {n}",
                labels=["agent:web", "lack-of-review-redo"],
                state="closed",
                repo="owner/repo",
            )
            for n in range(400, 400 + issue_count)
        ]

        # Exercise the private startup recovery hook directly: it is the exact
        # phase whose GitHub-call budget regressed (~29s at startup), so it is
        # the intentional regression boundary. The public run_startup() entry
        # would dilute the budget assertion across unrelated phases that also
        # call list_issues.
        startup_manager._recover_pending_retrospective_reviews(sample_state)  # noqa: SLF001

        assert len(sample_state.pending_retrospective_reviews) == issue_count
        # Discovery's source of truth is the trigger label: exactly one list call...
        mock_repository_host.list_issues.assert_called_once()
        # ...and ZERO prior-PR searches, no matter how many issues are recovered.
        mock_repository_host.search_pr_refs_for_issue.assert_not_called()
        mock_repository_host.get_prs_for_issue.assert_not_called()
        mock_repository_host.get_pr.assert_not_called()


class TestStartupSweepsThePendingWorkLedger:
    """Startup restoration must run even when discovery finds nothing (#6999 F8).

    The durable pending-work ledger holds requests that have already left their
    in-memory queue. The rows it exists for belong to runs whose terminals are
    already gone - so "no running sessions were discovered" is exactly the case
    where that row is the only remaining record of the work. Guarding the
    restoration call on a non-empty discovery is the bug this pins.
    """

    @pytest.mark.asyncio
    async def test_restoration_runs_with_no_discovered_sessions(
        self,
        mock_config,
        mock_events,
        mock_runner,
        mock_repository_host,
        mock_action_applier,
        mock_issue_branches_fn,
        mock_label_store,
        sample_state,
    ):
        mock_runner.discover_running_sessions = MagicMock(return_value=[])
        restore_sessions_fn = MagicMock()
        manager = StartupManager(
            config=mock_config,
            events=mock_events,
            runner=mock_runner,
            repository_host=mock_repository_host,
            action_applier=mock_action_applier,
            issue_branches_fn=mock_issue_branches_fn,
            session_exists_fn=lambda name: False,
            restore_sessions_fn=restore_sessions_fn,
            launch_session_fn=lambda issue: None,
            update_queue_cache_fn=lambda: None,
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            startup_worktree_reconciler=_startup_worktree_reconciler(),
            label_store=mock_label_store,
        )

        await manager.run_startup(sample_state)

        restore_sessions_fn.assert_called_once_with([])
