"""Integration tests for PR collision handling.

These tests verify behavior when:
1. A branch already has an associated closed PR (GitHub rejects new PRs)
2. A session exists in terminal but isn't tracked in active_sessions (infinite loop)

Test approach: Mock the adapters to simulate specific failure conditions
and verify the system handles them appropriately.
"""

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from issue_orchestrator.infra.config import Config, DangerousConfig
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    AgentConfig,
    PendingReview,
)
from issue_orchestrator.ports import TraceEvent
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.pr_scanner import PRScanner
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from tests.callback_endpoint_helpers import ready_callback_endpoint


class MockEventSink:
    """Mock event sink that collects events for assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def make_completion_record(
    outcome: CompletionOutcome,
    requested_actions: list[RequestedAction],
    session_id: str = "test-session",
    **kwargs
) -> CompletionRecord:
    """Helper to create a CompletionRecord with required fields."""
    return CompletionRecord(
        session_id=session_id,
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary="Test completion",
        requested_actions=requested_actions,
        **kwargs,
    )


def write_completion_to_worktree(worktree: Path, record: CompletionRecord) -> None:
    """Write completion record to worktree."""
    record_dir = worktree / ".issue-orchestrator"
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / "completion.json"
    import json
    record_path.write_text(json.dumps(record.to_dict()))


@pytest.fixture
def mock_event_sink():
    """Create a mock event sink for testing."""
    return MockEventSink()


@pytest.fixture
def mock_label_adapter():
    """Mock label adapter for CompletionProcessor."""
    adapter = MagicMock()
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


@pytest.fixture
def mock_git_adapter():
    """Mock git adapter for CompletionProcessor."""
    adapter = MagicMock()
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.push = Mock(return_value=MagicMock(success=True, message="Pushed"))
    adapter.rebase_on_branch = Mock(return_value=MagicMock(success=True, message="Rebased"))
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    return adapter


class TestPRAlreadyExistsHandling:
    """Tests for handling 'PR already exists' errors."""

    def test_pr_creation_switches_branch_for_closed_pr(
        self, mock_label_adapter, mock_git_adapter, tmp_path
    ):
        """When a closed PR exists for the branch, switch to a new branch and create a PR."""
        closed_pr = PRInfo(
            number=10,
            title="Old PR",
            url="https://github.com/owner/repo/pull/10",
            branch="issue-123",
            body="Old body",
            state="closed",
            labels=[],
        )
        new_pr = PRInfo(
            number=42,
            title="New PR",
            url="https://github.com/owner/repo/pull/42",
            branch="issue-123-r1",
            body="New body",
            state="open",
            labels=[],
        )

        mock_pr_adapter = MagicMock()
        mock_pr_adapter.get_prs_for_issue = Mock(return_value=[])
        mock_pr_adapter.get_prs_for_branch = Mock(return_value=[closed_pr])
        mock_pr_adapter.create_pr = Mock(return_value=new_pr)

        session_output = FileSystemSessionOutput()
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_assets = session_output.start_run(worktree, "issue-123", issue_number=123)
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            session_id="issue-123",
            implementation="Added feature",
        )
        write_completion_to_worktree(worktree, record)

        result = processor.process(
            worktree=worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Test Issue",
        )

        assert mock_git_adapter.push.call_count == 2
        mock_git_adapter.create_branch_from_current.assert_called_once()
        mock_pr_adapter.create_pr.assert_called_once()
        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        assert result.errors is None

    def test_pr_creation_success_no_closed_pr(
        self, mock_label_adapter, mock_git_adapter, tmp_path
    ):
        """When no closed PR exists, PR creation succeeds."""
        mock_pr_adapter = MagicMock()
        mock_pr_adapter.create_pr = Mock(
            return_value=MagicMock(number=42, url="https://github.com/owner/repo/pull/42")
        )

        session_output = FileSystemSessionOutput()
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_assets = session_output.start_run(worktree, "issue-123", issue_number=123)
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            session_id="issue-123",
            implementation="Added feature",
        )
        write_completion_to_worktree(worktree, record)

        result = processor.process(
            worktree=worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Test Issue",
        )

        assert result.success is True
        assert result.pr_url == "https://github.com/owner/repo/pull/42"
        assert result.errors is None

    def test_cleanup_happens_after_pr_creation_failure(
        self, mock_label_adapter, mock_git_adapter, tmp_path
    ):
        """Completion record should be cleaned up even if PR creation fails."""
        mock_pr_adapter = MagicMock()
        mock_pr_adapter.get_prs_for_issue = Mock(return_value=[])
        mock_pr_adapter.get_prs_for_branch = Mock(return_value=[])
        mock_pr_adapter.create_pr = Mock(
            side_effect=Exception("a pull request for branch 'issue-123' already exists")
        )

        session_output = FileSystemSessionOutput()
        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=mock_label_adapter,
            pr_adapter=mock_pr_adapter,
            git_adapter=mock_git_adapter,
            session_output=session_output,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_assets = session_output.start_run(worktree, "issue-123", issue_number=123)
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            session_id="issue-123",
        )
        write_completion_to_worktree(worktree, record)

        # Verify file exists before
        record_path = worktree / ".issue-orchestrator" / "completion.json"
        assert record_path.exists()

        # Process
        processor.process(
            worktree=worktree,
            run_assets=run_assets,
            issue_number=123,
            issue_title="Test Issue",
        )

        # Cleanup should have happened
        assert not record_path.exists()


class TestPRScannerSessionFiltering:
    """Tests for PR scanner's filtering of active sessions."""

    @pytest.fixture
    def mock_repository_scanner(self):
        """Create a mock repository scanner."""
        scanner = MagicMock()
        scanner.create_issue_key = lambda n: GitHubIssueKey(repo="test/repo", external_id=str(n))
        return scanner

    @pytest.fixture
    def test_config(self):
        """Create a test config."""
        config = Config()
        config.repo = "test/repo"
        config.code_review_agent = "agent:reviewer"
        config.code_review_label = "needs-code-review"
        return config

    def test_scanner_skips_pr_with_active_session(
        self, test_config, mock_repository_scanner, mock_event_sink
    ):
        """Scanner should skip PRs that have active review sessions."""
        # Setup: PR with needs-code-review label
        mock_repository_scanner.get_prs_with_label = Mock(
            return_value=[
                PRInfo(number=42, title="Test PR", url="https://...", branch="issue-123", body="Closes #123", state="open", labels=[])
            ]
        )

        scanner = PRScanner(
            config=test_config,
            repository=mock_repository_scanner,
            events=mock_event_sink,
        )

        # When review-42 is in active_sessions, PR #42 should be skipped
        results = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=["review-42"],  # Active session for this PR
        )

        assert len(results) == 0

    def test_scanner_finds_orphaned_pr_without_active_session(
        self, test_config, mock_repository_scanner, mock_event_sink
    ):
        """Scanner should find PRs that have no active review sessions."""
        mock_repository_scanner.get_prs_with_label = Mock(
            return_value=[
                PRInfo(number=42, title="Test PR", url="https://...", branch="issue-123", body="Closes #123", state="open", labels=[])
            ]
        )

        scanner = PRScanner(
            config=test_config,
            repository=mock_repository_scanner,
            events=mock_event_sink,
        )

        # No active sessions - PR should be found as orphaned
        results = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(results) == 1
        assert results[0].pr_number == 42

    def test_scanner_only_considers_review_sessions(
        self, test_config, mock_repository_scanner, mock_event_sink
    ):
        """Scanner should only filter by review- prefix sessions, not issue- sessions."""
        mock_repository_scanner.get_prs_with_label = Mock(
            return_value=[
                PRInfo(number=42, title="Test PR", url="https://...", branch="issue-123", body="Closes #123", state="open", labels=[])
            ]
        )

        scanner = PRScanner(
            config=test_config,
            repository=mock_repository_scanner,
            events=mock_event_sink,
        )

        # issue-123 session exists but review-42 doesn't - PR should still be found
        results = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=["issue-123"],  # Wrong prefix - not review-42
        )

        assert len(results) == 1
        assert results[0].pr_number == 42


class TestReviewLaunchLoopPrevention:
    """Tests for preventing the infinite review launch loop.

    The bug: Scanner sees PR as "orphaned" (not in active_sessions),
    Session launcher sees existing terminal session and rejects,
    Next tick: Scanner sees it as orphaned again → infinite loop.

    The fix: When session launcher skips due to "already exists",
    it should add a session to active_sessions OR the scanner should
    check actual terminal state, not just active_sessions.
    """

    @pytest.fixture
    def mock_repository_scanner(self):
        """Create a mock repository scanner."""
        scanner = MagicMock()
        scanner.create_issue_key = lambda n: GitHubIssueKey(repo="test/repo", external_id=str(n))
        return scanner

    @pytest.fixture
    def test_config(self):
        """Create a test config."""
        config = Config()
        config.repo = "test/repo"
        config.code_review_agent = "agent:reviewer"
        config.code_review_label = "needs-code-review"
        return config

    def test_scanner_with_empty_active_sessions_finds_pr(
        self, test_config, mock_repository_scanner, mock_event_sink
    ):
        """
        Demonstrates the bug: if active_sessions is empty but terminal session exists,
        scanner will keep finding the PR as orphaned.

        This documents the current behavior that leads to the infinite loop.
        """
        mock_repository_scanner.get_prs_with_label = Mock(
            return_value=[
                PRInfo(number=42, title="Test PR", url="https://...", branch="issue-123", body="Closes #123", state="open", labels=[])
            ]
        )

        scanner = PRScanner(
            config=test_config,
            repository=mock_repository_scanner,
            events=mock_event_sink,
        )

        # First scan: no active sessions
        results1 = scanner.scan_for_reviews(already_queued=[], active_sessions=[])

        # Second scan: still no active sessions (simulating the bug)
        results2 = scanner.scan_for_reviews(already_queued=[], active_sessions=[])

        # Third scan: still no active sessions
        results3 = scanner.scan_for_reviews(already_queued=[], active_sessions=[])

        # All three scans find the same PR - this is the bug behavior
        assert len(results1) == 1
        assert len(results2) == 1
        assert len(results3) == 1

    def test_scanner_stops_finding_pr_when_in_queued(
        self, test_config, mock_repository_scanner, mock_event_sink
    ):
        """
        The already_queued parameter should prevent re-scanning the same PR.
        """
        mock_repository_scanner.get_prs_with_label = Mock(
            return_value=[
                PRInfo(number=42, title="Test PR", url="https://...", branch="issue-123", body="Closes #123", state="open", labels=[])
            ]
        )

        scanner = PRScanner(
            config=test_config,
            repository=mock_repository_scanner,
            events=mock_event_sink,
        )

        # First scan: finds the PR
        results1 = scanner.scan_for_reviews(already_queued=[], active_sessions=[])
        assert len(results1) == 1

        # Second scan: PR is now in already_queued
        queued = [results1[0]]
        results2 = scanner.scan_for_reviews(already_queued=queued, active_sessions=[])

        # Should not find the PR again
        assert len(results2) == 0


class TestLaunchDispositionRetainsPendingWork:
    """The typed disposition decides whether a launch consumes its pending item.

    A launch can fail without the work having failed: the terminal is already
    running, or the provider refused. Both must leave the pending item alone,
    otherwise the scanner keeps rediscovering work it has already dropped — and
    for a tech-lead failure investigation, whose queue entry is the only record,
    dropping it loses the investigation outright (#6999 F10).
    """

    def test_an_unannotated_failure_is_permanent(self):
        """The default is unchanged: a plain failure means the launcher gave up."""
        from issue_orchestrator.control.session_launch_types import (
            LaunchDisposition,
            LaunchResult,
        )

        result = LaunchResult(session=None, success=False, reason="Some error")

        assert result.disposition is LaunchDisposition.PERMANENT_FAILURE
        assert not result.defers_to_provider

    def test_a_successful_launch_is_always_launched(self):
        """Success has exactly one disposition; it cannot contradict itself."""
        from issue_orchestrator.control.session_launch_types import (
            LaunchDisposition,
            LaunchResult,
        )

        result = LaunchResult(
            session=None,
            success=True,
            disposition=LaunchDisposition.PERMANENT_FAILURE,
        )

        assert result.disposition is LaunchDisposition.LAUNCHED

    @pytest.mark.parametrize(
        "disposition,retained",
        [
            ("EXISTING_TERMINAL", True),
            ("PROVIDER_DEFERRED", True),
            ("INPUT_RETRY", True),
            ("PERMANENT_FAILURE", False),
        ],
    )
    def test_only_a_permanent_failure_consumes_the_pending_item(
        self, disposition: str, retained: bool
    ):
        """The queue owner's contract, stated as a table.

        Mirrors ``_PendingQueueOwner.settle``: exactly one disposition drops the
        work, and a provider refusal is not it.
        """
        from issue_orchestrator.control.session_launch_types import (
            LaunchDisposition,
            LaunchResult,
        )

        result = LaunchResult(
            session=None,
            success=False,
            reason="launch did not start a session",
            disposition=LaunchDisposition[disposition],
        )

        pending_reviews = ["review_42"]
        if result.disposition is LaunchDisposition.PERMANENT_FAILURE:
            pending_reviews = [r for r in pending_reviews if r != "review_42"]

        assert ("review_42" in pending_reviews) is retained
