"""Unit tests for maintenance.py - issue reset operations."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, call

from issue_orchestrator.control.maintenance import (
    _find_issue_branches,
    ResetResult,
    reset_issue,
)
from issue_orchestrator.control.actions import RemoveLabelAction, SupersedePullRequestAction
from issue_orchestrator.domain.models import SessionHistoryEntry
from issue_orchestrator.ports.pull_request_tracker import PRInfo


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_working_copy():
    """Create a mock WorkingCopy adapter."""
    wc = MagicMock()
    wc.list_remote_branches = MagicMock(return_value=[])
    wc.delete_remote_branch = MagicMock()
    return wc


@pytest.fixture
def mock_worktree_manager():
    """Create a mock WorktreeManager."""
    wm = MagicMock()
    wm.remove = MagicMock()
    return wm


@pytest.fixture
def mock_action_applier():
    """Create a mock ActionApplier."""
    aa = MagicMock()
    # By default, apply succeeds
    aa.apply = MagicMock(
        return_value=MagicMock(success=True, error=None)
    )
    return aa


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock Config."""
    config = MagicMock()
    config.repo_root = tmp_path
    config.worktree_base = tmp_path / "worktrees"
    config.worktree_base.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def mock_label_manager():
    """Create a mock LabelManager that returns current_labels as 'ours'."""
    lm = MagicMock()
    # By default, get_ours returns whatever labels are passed in
    # Tests can override this per-case
    lm.get_ours = MagicMock(side_effect=lambda labels: labels)
    return lm


@pytest.fixture
def sample_session_history_entry():
    """Create a sample SessionHistoryEntry."""
    return SessionHistoryEntry(
        issue_number=123,
        title="Test Issue",
        agent_type="agent:web",
        status="completed",
        runtime_minutes=5,
    )


# =============================================================================
# Tests for _find_issue_branches
# =============================================================================


class TestFindIssueBranches:
    """Tests for the _find_issue_branches helper."""

    def test_find_issue_branch_simple(self, mock_working_copy):
        """Test finding a simple issue branch."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/3767-fix-something",
            "origin/main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 3767)

        assert branches == ["3767-fix-something"]

    def test_find_issue_branch_strips_origin_prefix(self, mock_working_copy):
        """Test that origin/ prefix is stripped."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/123-feature-name",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 123)

        assert branches == ["123-feature-name"]

    def test_find_issue_branch_handles_no_prefix(self, mock_working_copy):
        """Test branches without origin/ prefix."""
        mock_working_copy.list_remote_branches.return_value = [
            "456-another-feature",
            "main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 456)

        assert branches == ["456-another-feature"]

    def test_find_issue_branch_not_found(self, mock_working_copy):
        """Test when branch is not found."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/main",
            "origin/develop",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 999)

        assert branches == []

    def test_find_issue_branch_with_multiple_candidates(self, mock_working_copy):
        """Test returns first matching branch."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/100-first",
            "origin/100-second",  # Same issue number, different suffix
            "origin/main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 100)

        assert branches == ["100-first", "100-second"]

    def test_find_issue_branch_skips_non_numeric_branches(self, mock_working_copy):
        """Test that branches not starting with numbers are skipped."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/feature-100",  # Doesn't start with number
            "origin/100-fix",  # Starts with number
            "origin/main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 100)

        assert branches == ["100-fix"]

    def test_find_issue_branch_with_whitespace(self, mock_working_copy):
        """Test handling of branches with leading/trailing whitespace."""
        mock_working_copy.list_remote_branches.return_value = [
            "  origin/200-feature  ",
            "origin/main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 200)

        assert branches == ["200-feature"]

    def test_find_issue_branch_complex_name(self, mock_working_copy):
        """Test finding branch with complex name."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/789-fix-bug-in-feature-X",
            "origin/main",
        ]

        branches = _find_issue_branches(mock_working_copy, Path("/repo"), 789)

        assert branches == ["789-fix-bug-in-feature-X"]

    def test_find_issue_branch_calls_list_remote_branches(self, mock_working_copy):
        """Test that list_remote_branches is called with correct repo."""
        repo_path = Path("/my/repo")
        mock_working_copy.list_remote_branches.return_value = []

        _find_issue_branches(mock_working_copy, repo_path, 123)

        mock_working_copy.list_remote_branches.assert_called_once_with(repo_path)


# =============================================================================
# Tests for ResetResult
# =============================================================================


class TestResetResult:
    """Tests for the ResetResult dataclass."""

    def test_reset_result_success_creation(self):
        """Test creating a successful ResetResult."""
        result = ResetResult(
            success=True,
            issue_number=123,
            deleted_worktree="/path/to/worktree",
            deleted_branch="123-feature",
            labels_removed=["blocked", "in-progress"],
        )

        assert result.success is True
        assert result.issue_number == 123
        assert result.deleted_worktree == "/path/to/worktree"
        assert result.deleted_branch == "123-feature"
        assert result.labels_removed == ["blocked", "in-progress"]
        assert result.error is None

    def test_reset_result_failure_creation(self):
        """Test creating a failed ResetResult."""
        result = ResetResult(
            success=False,
            issue_number=456,
            error="Permission denied",
        )

        assert result.success is False
        assert result.issue_number == 456
        assert result.error == "Permission denied"
        assert result.deleted_worktree is None
        assert result.deleted_branch is None
        assert result.labels_removed is None

    def test_reset_result_minimal_creation(self):
        """Test creating ResetResult with minimal fields."""
        result = ResetResult(success=True, issue_number=789)

        assert result.success is True
        assert result.issue_number == 789
        assert result.deleted_worktree is None
        assert result.deleted_branch is None
        assert result.labels_removed is None
        assert result.error is None


# =============================================================================
# Tests for reset_issue
# =============================================================================


class TestResetIssue:
    """Tests for the reset_issue function."""

    def test_reset_issue_success_all_steps(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test successful reset with all cleanup operations."""
        # Setup - compute worktree path the same way get_worktree_path does
        worktree_path = mock_config.worktree_base / f"{mock_config.repo_root.name}-123"
        worktree_path.mkdir(parents=True)

        mock_working_copy.list_remote_branches.return_value = [
            "origin/123-feature",
        ]

        session_history = []
        completed_today = []

        # Execute
        result = reset_issue(
            issue_number=123,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=["blocked"],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Assert result
        assert result.success is True
        assert result.issue_number == 123
        assert result.deleted_worktree == str(worktree_path)
        assert result.deleted_branch == "123-feature"
        assert result.labels_removed == ["blocked"]

        # Assert worktree was removed
        mock_worktree_manager.remove_checkout.assert_called_once_with(worktree_path)

        # Assert branch was deleted
        mock_working_copy.delete_remote_branch.assert_called_once_with(
            mock_config.repo_root, "123-feature"
        )

        # Assert label was removed
        assert mock_action_applier.apply.call_count == 1
        call_args = mock_action_applier.apply.call_args[0][0]
        assert isinstance(call_args, RemoveLabelAction)
        assert call_args.issue_number == 123
        assert call_args.label == "blocked"

    def test_reset_issue_deletes_all_matching_remote_branches(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        mock_working_copy.list_remote_branches.return_value = [
            "origin/123-old-branch",
            "origin/123-fresh-branch",
            "origin/main",
        ]

        result = reset_issue(
            issue_number=123,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
        )

        assert result.success is True
        assert mock_working_copy.delete_remote_branch.call_args_list == [
            call(mock_config.repo_root, "123-old-branch"),
            call(mock_config.repo_root, "123-fresh-branch"),
        ]

    def test_reset_issue_from_scratch_clears_validation_cache_via_worktree_removal(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Reset-and-retry **from scratch** must invalidate the validation
        cache, otherwise a fresh attempt at the same head SHA can
        silently reuse the previous run's pass result. This was the
        regression observed in production where cached validation was
        reused erroneously after a reset.

        The runner stores per-SHA validation records inside the
        worktree at ``.issue-orchestrator/validation/<sha>.json``.
        ``reset_issue(from_scratch=True)`` calls
        ``worktree_manager.remove_checkout_and_branch(path, force=True)`` (the
        scratch path — distinct from ordinary reset's checkout-only removal),
        which deletes that directory and its records. This test pins
        the scratch-specific invariant: ``from_scratch=True`` must
        invoke the force-remove call. Without it, a second attempt at
        the same SHA would hit the cache and skip validation.
        """
        worktree_path = mock_config.worktree_base / f"{mock_config.repo_root.name}-789"
        worktree_path.mkdir(parents=True)
        # Seed a passing validation record so a regression that didn't
        # delete the worktree would leave the cache reusable.
        validation_dir = worktree_path / ".issue-orchestrator" / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        cache_record = validation_dir / "deadbeef.json"
        cache_record.write_text('{"passed": true, "head_sha": "deadbeef"}')

        # Make the mock worktree_manager actually remove the directory
        # so the test verifies the contract end-to-end. The scratch
        # path passes ``force=True``; the kwarg-accepting signature
        # mirrors the destructive WorktreeManager operation.
        def _remove(path, force: bool = False):  # noqa: ARG001 - kwarg shape only
            import shutil
            shutil.rmtree(path, ignore_errors=True)

        mock_worktree_manager.remove_checkout_and_branch.side_effect = _remove
        mock_working_copy.list_remote_branches.return_value = []
        # Scratch reset requires a repository_host for PR supersession.
        mock_repository_host = MagicMock()
        mock_repository_host.list_open_prs_for_branch = MagicMock(return_value=[])
        mock_repository_host.list_open_prs = MagicMock(return_value=[])

        result = reset_issue(
            issue_number=789,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            from_scratch=True,
            repository_host=mock_repository_host,
        )

        assert result.success is True
        assert result.deleted_worktree == str(worktree_path)
        # Pin the scratch-specific call shape — ``force=True`` is what
        # the from_scratch branch passes; ordinary reset passes no
        # ``force`` kwarg. Asserting on the kwarg is what catches a
        # regression that lets scratch reset fall back to the soft
        # remove (which can leave the worktree behind on git lock
        # errors and thus leave the validation cache intact).
        mock_worktree_manager.remove_checkout_and_branch.assert_called_once_with(
            worktree_path, force=True,
        )
        # Worktree directory and its validation cache must both be gone.
        assert not worktree_path.exists(), (
            "reset_issue should delete the worktree so the "
            "per-SHA validation cache is implicitly cleared"
        )
        assert not cache_record.exists(), (
            "validation/<sha>.json must not survive reset_issue; "
            "a stale cache entry would let a second attempt at the "
            "same head SHA silently skip validation"
        )

    def test_reset_issue_no_worktree(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test reset when no worktree exists."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/456-feature",
        ]

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=456,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=["blocked"],
            session_history=session_history,
            completed_today=completed_today,
        )

        assert result.success is True
        assert result.deleted_worktree is None
        # Worktree manager should not be called
        mock_worktree_manager.remove_checkout.assert_not_called()
        mock_worktree_manager.remove_checkout_and_branch.assert_not_called()

    def test_reset_issue_no_branch(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
        tmp_path,
    ):
        """Test reset when branch doesn't exist."""
        # Setup worktree
        worktree_path = tmp_path / "worktrees" / "789"
        worktree_path.mkdir(parents=True)

        # No branches found
        mock_working_copy.list_remote_branches.return_value = []

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=789,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=["blocked"],
            session_history=session_history,
            completed_today=completed_today,
        )

        assert result.success is True
        assert result.deleted_branch is None
        # Branch deletion should not be called
        mock_working_copy.delete_remote_branch.assert_not_called()

    def test_reset_issue_no_labels(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test reset with no blocking labels."""
        mock_working_copy.list_remote_branches.return_value = [
            "origin/100-feature",
        ]

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=100,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        assert result.success is True
        assert result.labels_removed == []
        # Action applier should not be called for label removal
        mock_action_applier.apply.assert_not_called()

    def test_reset_issue_multiple_labels(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test reset with multiple blocking labels."""
        mock_working_copy.list_remote_branches.return_value = []

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=111,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=["blocked", "in-progress", "needs-review"],
            session_history=session_history,
            completed_today=completed_today,
        )

        assert result.success is True
        assert result.labels_removed == ["blocked", "in-progress", "needs-review"]
        assert mock_action_applier.apply.call_count == 3

    def test_reset_issue_removes_from_session_history(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test that issue is removed from session history."""
        mock_working_copy.list_remote_branches.return_value = []

        # Create session history with entries including the one we're resetting
        session_history = [
            SessionHistoryEntry(
                issue_number=222,
                title="Other Issue",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
            SessionHistoryEntry(
                issue_number=123,
                title="Issue to Reset",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
            SessionHistoryEntry(
                issue_number=333,
                title="Another Issue",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]

        completed_today = []

        reset_issue(
            issue_number=123,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Should have removed the entry for issue 123
        assert len(session_history) == 2
        assert all(e.issue_number != 123 for e in session_history)
        assert session_history[0].issue_number == 222
        assert session_history[1].issue_number == 333

    def test_reset_issue_removes_from_completed_today(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test that issue is removed from completed_today list."""
        mock_working_copy.list_remote_branches.return_value = []

        session_history = []
        completed_today = [100, 123, 456]

        reset_issue(
            issue_number=123,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        assert 123 not in completed_today
        assert completed_today == [100, 456]

    def test_reset_issue_completed_today_not_present(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test reset when issue is not in completed_today."""
        mock_working_copy.list_remote_branches.return_value = []

        session_history = []
        completed_today = [100, 456]

        result = reset_issue(
            issue_number=123,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Should still succeed
        assert result.success is True
        # completed_today should be unchanged
        assert completed_today == [100, 456]

    def test_reset_issue_worktree_removal_failure(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
        tmp_path,
    ):
        """Test that failure to remove worktree is logged but doesn't stop reset."""
        worktree_path = tmp_path / "worktrees" / "555"
        worktree_path.mkdir(parents=True)

        mock_worktree_manager.remove_checkout.side_effect = Exception("Permission denied")
        mock_working_copy.list_remote_branches.return_value = [
            "origin/555-feature",
        ]

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=555,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Reset should still succeed overall
        assert result.success is True
        # But worktree deletion should be None
        assert result.deleted_worktree is None
        # Branch deletion should still happen
        assert result.deleted_branch == "555-feature"

    def test_reset_issue_branch_deletion_failure(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test that failure to delete branch is logged but doesn't stop reset."""
        # Compute worktree path the same way get_worktree_path does
        worktree_path = mock_config.worktree_base / f"{mock_config.repo_root.name}-666"
        worktree_path.mkdir(parents=True)

        mock_working_copy.delete_remote_branch.side_effect = Exception("API error")
        mock_working_copy.list_remote_branches.return_value = [
            "origin/666-feature",
        ]

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=666,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Reset should still succeed overall
        assert result.success is True
        # Worktree deletion should succeed
        assert result.deleted_worktree == str(worktree_path)
        # But branch deletion should be None
        assert result.deleted_branch is None

    def test_reset_issue_label_removal_failure(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test that failure to remove label is logged but doesn't stop reset."""
        mock_working_copy.list_remote_branches.return_value = []

        # First label succeeds, second fails
        mock_action_applier.apply.side_effect = [
            MagicMock(success=True, error=None),
            MagicMock(success=False, error="API error"),
        ]

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=777,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=["blocked", "in-progress"],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Reset should still succeed overall
        assert result.success is True
        # Only successful label removal should be in the list
        assert result.labels_removed == ["blocked"]

    def test_reset_issue_unforeseen_exception(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Test handling of unexpected exceptions during reset."""
        mock_working_copy.list_remote_branches.side_effect = RuntimeError("Unexpected error")

        session_history = []
        completed_today = []

        result = reset_issue(
            issue_number=888,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=session_history,
            completed_today=completed_today,
        )

        # Reset should fail with the error
        assert result.success is False
        assert result.issue_number == 888
        assert result.error is not None
        assert "Unexpected error" in result.error

    def test_reset_issue_clears_timeline_when_store_provided(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Timeline data is cleared when timeline_store is passed (from-scratch reset)."""
        from unittest.mock import MagicMock

        timeline_store = MagicMock()
        timeline_store.delete.return_value = 42

        mock_working_copy.list_remote_branches.return_value = []

        result = reset_issue(
            issue_number=555,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            timeline_store=timeline_store,
        )

        assert result.success is True
        timeline_store.delete.assert_called_once_with(555)

    def test_reset_issue_from_scratch_closes_prs_and_reports_full_cleanup(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        worktree_path = mock_config.worktree_base / f"{mock_config.repo_root.name}-559"
        worktree_path.mkdir(parents=True)
        mock_worktree_manager.remove_checkout_and_branch.side_effect = (
            lambda path, *, force=False: path.rmdir()
        )
        mock_working_copy.list_remote_branches.return_value = ["origin/559-scratch-old"]
        mock_working_copy.delete_remote_branch.return_value = True
        timeline_store = MagicMock()
        timeline_store.delete.return_value = 9
        repository_host = MagicMock()
        repository_host.get_prs_for_issue.return_value = [
            PRInfo(
                number=376,
                title="#559: old attempt",
                url="https://github.com/owner/repo/pull/376",
                branch="559-scratch-old",
                body="",
                state="open",
                labels=[],
            )
        ]

        result = reset_issue(
            issue_number=559,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            timeline_store=timeline_store,
            from_scratch=True,
            repository_host=repository_host,
        )

        assert result.success is True
        assert result.deleted_worktree == str(worktree_path)
        assert result.deleted_branch == "559-scratch-old"
        assert result.deleted_branches == ["559-scratch-old"]
        assert result.superseded_prs == [376]
        assert result.timeline_events_deleted == 9
        mock_worktree_manager.remove_checkout_and_branch.assert_called_once_with(
            worktree_path, force=True
        )
        supersede_actions = [
            applier_call.args[0]
            for applier_call in mock_action_applier.apply.call_args_list
            if isinstance(applier_call.args[0], SupersedePullRequestAction)
        ]
        assert len(supersede_actions) == 1
        assert supersede_actions[0].issue_number == 559
        assert supersede_actions[0].pr_number == 376
        assert "Superseded by reset and retry from scratch" in supersede_actions[0].comment

    def test_reset_issue_from_scratch_reports_partial_superseded_prs_on_failure(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        repository_host = MagicMock()
        repository_host.get_prs_for_issue.return_value = [
            PRInfo(
                number=376,
                title="#559: old attempt",
                url="https://github.com/owner/repo/pull/376",
                branch="559-scratch-old",
                body="",
                state="open",
                labels=[],
            ),
            PRInfo(
                number=377,
                title="#559: second old attempt",
                url="https://github.com/owner/repo/pull/377",
                branch="559-scratch-second",
                body="",
                state="open",
                labels=[],
            ),
        ]
        mock_action_applier.apply.side_effect = [
            MagicMock(success=True, error=None),
            MagicMock(success=False, error="permission denied"),
        ]

        result = reset_issue(
            issue_number=559,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            from_scratch=True,
            repository_host=repository_host,
        )

        assert result.success is False
        assert result.superseded_prs == [376]
        assert "failed to supersede PR #377" in result.error
        mock_working_copy.delete_remote_branch.assert_not_called()

    def test_reset_issue_from_scratch_fails_when_worktree_survives(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        worktree_path = mock_config.worktree_base / f"{mock_config.repo_root.name}-560"
        worktree_path.mkdir(parents=True)
        mock_working_copy.list_remote_branches.return_value = []

        result = reset_issue(
            issue_number=560,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            from_scratch=True,
            repository_host=MagicMock(),
        )

        assert result.success is False
        assert "Worktree still exists after removal" in result.error

    def test_reset_issue_from_scratch_fails_when_remote_branch_delete_returns_false(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        mock_working_copy.list_remote_branches.return_value = ["origin/561-old"]
        mock_working_copy.delete_remote_branch.return_value = False
        repository_host = MagicMock()
        repository_host.get_prs_for_issue.return_value = []

        result = reset_issue(
            issue_number=561,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            from_scratch=True,
            repository_host=repository_host,
        )

        assert result.success is False
        assert "Scratch reset failed to delete remote branch 561-old" in result.error

    def test_reset_issue_skips_timeline_when_store_is_none(
        self,
        mock_worktree_manager,
        mock_working_copy,
        mock_action_applier,
        mock_config,
        mock_label_manager,
    ):
        """Timeline data is NOT cleared for regular resets (timeline_store=None)."""
        mock_working_copy.list_remote_branches.return_value = []

        result = reset_issue(
            issue_number=556,
            config=mock_config,
            worktree_manager=mock_worktree_manager,
            working_copy=mock_working_copy,
            action_applier=mock_action_applier,
            label_manager=mock_label_manager,
            current_labels=[],
            session_history=[],
            completed_today=[],
            timeline_store=None,
        )

        assert result.success is True
