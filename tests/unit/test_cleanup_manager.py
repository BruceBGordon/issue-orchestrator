"""Unit tests for CleanupManager.

Tests focus on behavior:
- Processing deferred cleanups when PRs have review labels
- Orphaned cleanup recovery at startup
- Throttling for tech_lead issue creation failures
- Error handling in cleanup operations
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.control.cleanup_manager import CleanupManager
from issue_orchestrator.domain.models import PendingCleanup
from issue_orchestrator.ports.pull_request_tracker import PRInfo


@dataclass
class CleanupManagerBundle:
    """Bundle of CleanupManager and its injected mock dependencies."""

    manager: CleanupManager
    kill_session: MagicMock
    session_exists: MagicMock
    get_worktree_path: MagicMock
    get_session_name: MagicMock


# --- Helpers ---


def make_pending_cleanup(
    issue_number: int,
    pr_number: int,
    terminal_id: str = None,
    worktree_path: Path = None,
) -> PendingCleanup:
    """Create a PendingCleanup with a mock issue."""
    mock_issue = MagicMock()
    mock_issue.number = issue_number
    return PendingCleanup(
        issue=mock_issue,
        pr_number=pr_number,
        pr_url=f"https://github.com/test/repo/pull/{pr_number}",
        branch_name=f"{issue_number}-branch",
        terminal_id=terminal_id or f"issue-{issue_number}",
        worktree_path=worktree_path or Path(f"/tmp/worktree-{issue_number}"),
    )


# --- Fixtures ---


@pytest.fixture
def mock_config():
    """Create a mock config with reasonable defaults."""
    config = MagicMock()
    # Most tests in this file exercise the tech-lead cleanup branch; tests for
    # the no-tech-lead/code-review branch override this explicitly.
    config.tech_lead_enabled = True
    config.tech_lead_review_agent = None
    config.code_review_agent = None
    config.tech_lead_reviewed_label = "tech-lead-reviewed"
    config.code_reviewed_label = "code-reviewed"
    config.cleanup.with_tech_lead.close_ai_session_tabs = True
    config.cleanup.with_tech_lead.remove_worktrees = True
    config.cleanup.without_tech_lead.close_ai_session_tabs = True
    config.cleanup.without_tech_lead.remove_worktrees = True
    config.agents = {}
    return config


@pytest.fixture
def mock_repository_host():
    """Create a mock repository host."""
    host = MagicMock()
    host.get_prs_with_label.return_value = []
    return host


@pytest.fixture
def mock_worktree_manager():
    """Create a mock worktree manager."""
    mgr = MagicMock()
    mgr.extract_issue_number.return_value = None
    mgr.remove_checkout.return_value = None
    mgr.can_remove_without_user_changes.return_value = False
    return mgr


@pytest.fixture
def cleanup_manager_bundle(mock_config, mock_repository_host, mock_worktree_manager):
    """Create a CleanupManager with mocked dependencies."""
    kill_session = MagicMock()
    session_exists = MagicMock(return_value=False)
    get_worktree_path = MagicMock(return_value=Path("/tmp/worktree"))
    get_session_name = MagicMock(return_value="issue-123")

    manager = CleanupManager(
        config=mock_config,
        repository_host=mock_repository_host,
        worktree_manager=mock_worktree_manager,
        kill_session_fn=kill_session,
        session_exists_fn=session_exists,
        get_worktree_path_fn=get_worktree_path,
        get_session_name_fn=get_session_name,
    )

    return CleanupManagerBundle(
        manager=manager,
        kill_session=kill_session,
        session_exists=session_exists,
        get_worktree_path=get_worktree_path,
        get_session_name=get_session_name,
    )


@pytest.fixture
def cleanup_manager(cleanup_manager_bundle):
    """Convenience fixture returning just the manager."""
    return cleanup_manager_bundle.manager


# --- Test: Throttling ---


class TestTechLeadIssueThrottling:
    """Test throttling logic for tech_lead issue creation failures."""

    def test_should_retry_returns_true_initially(self, cleanup_manager):
        """First attempt always allowed before any failure."""
        assert cleanup_manager.should_retry_tech_lead_issue() is True

    def test_should_retry_returns_false_after_failure(self, cleanup_manager):
        """Should not retry immediately after failure."""
        cleanup_manager.mark_tech_lead_issue_failure()

        assert cleanup_manager.should_retry_tech_lead_issue(cooldown_seconds=60) is False

    def test_should_retry_returns_true_after_cooldown(self, cleanup_manager, monkeypatch):
        """Should allow retry after cooldown expires."""
        import time

        cleanup_manager.mark_tech_lead_issue_failure()

        # Simulate cooldown expiration by mocking time to be 2 minutes later
        original_time = time.time
        monkeypatch.setattr(time, "time", lambda: original_time() + 120)

        assert cleanup_manager.should_retry_tech_lead_issue(cooldown_seconds=60) is True


# --- Test: Process Deferred Cleanups ---


class TestProcessDeferredCleanups:
    """Test deferred cleanup processing."""

    def test_empty_list_returns_empty(self, cleanup_manager):
        """Processing empty list returns empty list."""
        result = cleanup_manager.process_deferred_cleanups([])
        assert result == []

    def test_no_review_workflow_returns_unchanged(
        self, cleanup_manager, mock_config, caplog
    ):
        """Without review workflow configured, cleanups are not processed."""
        mock_config.tech_lead_review_agent = None
        mock_config.tech_lead_enabled = False
        mock_config.code_review_agent = None

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == pending
        assert "no review workflow configured" in caplog.text

    def test_tech_lead_workflow_cleans_up_reviewed_prs(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """With tech_lead workflow, PRs with tech-lead-reviewed label are cleaned."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_reviewed_label = "tech-lead-reviewed"

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        # PR 456 has the reviewed label
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(
                number=456, url="...", title="PR", branch="123-fix",
                labels=["tech-lead-reviewed"], body="", state="open"
            )
        ]

        result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == []  # Cleanup was processed and removed
        mock_repository_host.get_prs_with_label.assert_called_once_with("tech-lead-reviewed")

    def test_code_review_workflow_cleans_up_reviewed_prs(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """With code review workflow, PRs with code-reviewed label are cleaned."""
        mock_config.tech_lead_review_agent = None
        mock_config.tech_lead_enabled = False
        mock_config.code_review_agent = "agent:reviewer"
        mock_config.code_reviewed_label = "code-reviewed"

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=789,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        # PR 789 has the code-reviewed label
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(
                number=789, url="...", title="PR", branch="123-fix",
                labels=["code-reviewed"], body="", state="open"
            )
        ]

        result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == []
        mock_repository_host.get_prs_with_label.assert_called_once_with("code-reviewed")

    def test_unreviewed_prs_remain_pending(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """PRs without the review label remain in pending list."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        # No PRs have the reviewed label
        mock_repository_host.get_prs_with_label.return_value = []

        result = cleanup_manager.process_deferred_cleanups(pending)

        assert len(result) == 1
        assert result[0].pr_number == 456

    def test_kills_session_when_configured(
        self, cleanup_manager, cleanup_manager_bundle, mock_config, mock_repository_host
    ):
        """Session is killed when close_ai_session_tabs is True."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.cleanup.with_tech_lead.close_ai_session_tabs = True

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        cleanup_manager.process_deferred_cleanups(pending)

        cleanup_manager_bundle.kill_session.assert_called_once_with("issue-123")

    def test_removes_worktree_when_configured(
        self, cleanup_manager, mock_config, mock_repository_host, mock_worktree_manager
    ):
        """Worktree is removed when remove_worktrees is True."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.cleanup.with_tech_lead.remove_worktrees = True

        worktree_path = Path("/tmp/issue-123-worktree")
        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=worktree_path,
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        cleanup_manager.process_deferred_cleanups(pending)

        mock_worktree_manager.remove_checkout.assert_called_once_with(worktree_path)

    def test_handles_kill_session_failure(
        self, cleanup_manager, cleanup_manager_bundle, mock_config, mock_repository_host, caplog
    ):
        """Session kill failure is logged and cleanup remains pending."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        cleanup_manager_bundle.kill_session.side_effect = Exception("Session not found")

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == pending
        assert "Failed to close session" in caplog.text
        assert "Cleanup incomplete" in caplog.text

    def test_handles_worktree_removal_failure(
        self, cleanup_manager, mock_config, mock_repository_host, mock_worktree_manager, caplog
    ):
        """Worktree removal failure is logged and cleanup remains pending."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_worktree_manager.remove_checkout.side_effect = Exception("Permission denied")

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == pending
        assert "Failed to remove worktree" in caplog.text
        assert "Cleanup incomplete" in caplog.text

    def test_forces_worktree_removal_for_runtime_only_dirty_state(
        self, cleanup_manager, mock_config, mock_repository_host, mock_worktree_manager
    ):
        """Runtime-only untracked artifacts do not strand deferred cleanups."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        worktree_path = Path("/tmp/worktree")
        mock_worktree_manager.remove_checkout.side_effect = [Exception("dirty"), None]
        mock_worktree_manager.can_remove_without_user_changes.return_value = True

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=worktree_path,
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == []
        assert mock_worktree_manager.remove_checkout.call_args_list == [
            call(worktree_path),
            call(worktree_path, force=True),
        ]

    def test_does_not_force_worktree_removal_when_user_changes_are_present(
        self, cleanup_manager, mock_config, mock_repository_host, mock_worktree_manager
    ):
        """Tracked or non-runtime dirty state remains pending for operator review."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        worktree_path = Path("/tmp/worktree")
        mock_worktree_manager.remove_checkout.side_effect = Exception("dirty")
        mock_worktree_manager.can_remove_without_user_changes.return_value = False

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=worktree_path,
            )
        ]
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]

        result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == pending
        mock_worktree_manager.remove_checkout.assert_called_once_with(worktree_path)

    def test_handles_pr_fetch_failure(
        self, cleanup_manager, mock_config, mock_repository_host, caplog
    ):
        """PR fetch failure returns unchanged list."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.side_effect = Exception("API error")

        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.process_deferred_cleanups(pending)

        assert result == pending
        assert "Failed to fetch PRs" in caplog.text

    def test_propagates_repository_host_pr_fetch_failure(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """Repository-host failures fail the cleanup cycle instead of looking empty."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )
        pending = [
            make_pending_cleanup(
                issue_number=123,
                pr_number=456,
                terminal_id="issue-123",
                worktree_path=Path("/tmp/worktree"),
            )
        ]

        with pytest.raises(GitHubHttpError) as exc_info:
            cleanup_manager.process_deferred_cleanups(pending)

        assert exc_info.value.status_code == 503

    def test_processes_multiple_cleanups(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """Multiple cleanups can be processed in one call."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"

        pending = [
            make_pending_cleanup(
                issue_number=123, pr_number=456,
                terminal_id="issue-123", worktree_path=Path("/tmp/wt1")
            ),
            make_pending_cleanup(
                issue_number=124, pr_number=457,
                terminal_id="issue-124", worktree_path=Path("/tmp/wt2")
            ),
            make_pending_cleanup(
                issue_number=125, pr_number=458,
                terminal_id="issue-125", worktree_path=Path("/tmp/wt3")
            ),
        ]

        # Only PRs 456 and 458 are reviewed
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR1", branch="123-fix",
                   labels=[], body="", state="open"),
            PRInfo(number=458, url="...", title="PR3", branch="125-fix",
                   labels=[], body="", state="open"),
        ]

        result = cleanup_manager.process_deferred_cleanups(pending)

        # Only PR 457 (issue 124) should remain
        assert len(result) == 1
        assert result[0].pr_number == 457


# --- Test: Orphaned Cleanup Recovery ---


class TestRecoverOrphanedCleanups:
    """Test orphaned cleanup recovery at startup."""

    def test_returns_zero_without_review_workflow(self, cleanup_manager, mock_config):
        """Without review workflow, returns 0 (no cleanup needed)."""
        mock_config.tech_lead_review_agent = None
        mock_config.tech_lead_enabled = False
        mock_config.code_review_agent = None

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0

    def test_returns_zero_without_cleanup_label(self, cleanup_manager, mock_config):
        """Without cleanup label configured, returns 0."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.tech_lead_reviewed_label = None

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0

    def test_returns_zero_when_no_reviewed_prs(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """With no reviewed PRs, returns 0."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.return_value = []

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0

    def test_skips_running_sessions(
        self, cleanup_manager, cleanup_manager_bundle, mock_config, mock_repository_host, mock_worktree_manager
    ):
        """Sessions still running are not cleaned up."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]
        mock_worktree_manager.extract_issue_number.return_value = 123

        # Session is still running
        cleanup_manager_bundle.session_exists.return_value = True

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0
        mock_worktree_manager.remove_checkout.assert_not_called()

    def test_cleans_up_orphaned_worktrees(
        self, cleanup_manager, cleanup_manager_bundle, mock_config, mock_repository_host, mock_worktree_manager, tmp_path
    ):
        """Orphaned worktrees are cleaned up."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.agents = {"agent:tech-lead": MagicMock()}

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]
        mock_worktree_manager.extract_issue_number.return_value = 123

        # Session is not running
        cleanup_manager_bundle.session_exists.return_value = False

        # Worktree exists
        worktree = tmp_path / "issue-123"
        worktree.mkdir()
        cleanup_manager_bundle.get_worktree_path.return_value = worktree

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 1
        mock_worktree_manager.remove_checkout.assert_called_once()

    def test_handles_pr_fetch_failure(
        self, cleanup_manager, mock_config, mock_repository_host, caplog
    ):
        """PR fetch failure during recovery is logged, returns 0."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.side_effect = Exception("Network error")

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0
        assert "Failed to fetch reviewed PRs" in caplog.text

    def test_recovery_propagates_repository_host_pr_fetch_failure(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """Repository-host failures fail recovery instead of looking empty."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            cleanup_manager.recover_orphaned_cleanups()

        assert exc_info.value.status_code == 503

    def test_calls_startup_message_callback(
        self, cleanup_manager, mock_config, mock_repository_host
    ):
        """Startup message callback is invoked if provided."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_repository_host.get_prs_with_label.return_value = []

        callback = MagicMock()
        cleanup_manager.recover_orphaned_cleanups(set_startup_message=callback)

        callback.assert_called_once_with("Checking for orphaned cleanups...")

    def test_does_not_count_worktree_removal_failure_during_recovery(
        self, cleanup_manager, cleanup_manager_bundle, mock_config, mock_repository_host, mock_worktree_manager, tmp_path, caplog
    ):
        """Worktree removal failure is logged and not counted as cleaned."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.agents = {"agent:tech-lead": MagicMock()}

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="123-fix",
                   labels=[], body="", state="open")
        ]
        mock_worktree_manager.extract_issue_number.return_value = 123
        mock_worktree_manager.remove_checkout.side_effect = Exception("Cannot remove")

        cleanup_manager_bundle.session_exists.return_value = False

        worktree = tmp_path / "issue-123"
        worktree.mkdir()
        cleanup_manager_bundle.get_worktree_path.return_value = worktree

        with caplog.at_level(logging.WARNING):
            result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0
        assert "Failed to remove worktree" in caplog.text

    def test_skips_prs_without_extractable_issue_number(
        self, cleanup_manager, mock_config, mock_repository_host, mock_worktree_manager
    ):
        """PRs where issue number can't be extracted are skipped."""
        mock_config.tech_lead_review_agent = "agent:tech-lead"
        mock_config.agents = {"agent:tech-lead": MagicMock()}

        mock_repository_host.get_prs_with_label.return_value = [
            PRInfo(number=456, url="...", title="PR", branch="weird-branch-name",
                   labels=[], body="", state="open")
        ]
        # Can't extract issue number from branch
        mock_worktree_manager.extract_issue_number.return_value = None

        result = cleanup_manager.recover_orphaned_cleanups()

        assert result == 0
