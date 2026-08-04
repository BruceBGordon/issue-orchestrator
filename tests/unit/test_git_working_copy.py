"""Unit tests for GitWorkingCopy adapter."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.ports.command_runner import OutputNewlines
from issue_orchestrator.ports.git import GitError, GitResult
from issue_orchestrator.ports.working_copy import (
    BranchPathsResult,
    BranchStatus,
    BranchTextFile,
    BranchTextFilesResult,
    CommitInfo,
    PreflightResult,
    PushResult,
    RebaseResult,
)


def git_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> GitResult:
    return GitResult(argv=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


def git_error(*_args, **kwargs) -> GitError:
    stdout = kwargs.get("stdout", "")
    stderr = kwargs.get("stderr", "")
    return GitError(git_result(stdout=stdout, stderr=stderr, returncode=1))


@pytest.fixture
def mock_git():
    """Create a mock Git client for injection."""
    return MagicMock()


@pytest.fixture
def git_wc(mock_git):
    """Create a GitWorkingCopy instance with injected mock Git client."""
    return GitWorkingCopy(git=mock_git)


@pytest.fixture
def worktree_path(tmp_path):
    """Create a temporary worktree path."""
    return tmp_path / "worktree"


class TestGetCurrentBranch:
    """Tests for get_current_branch method."""

    def test_get_current_branch_success(self, git_wc, worktree_path):
        """Test getting current branch successfully."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="feature-branch\n",
                stderr="",
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch == "feature-branch"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--abbrev-ref" in args
            assert "HEAD" in args

    def test_get_current_branch_detached_head(self, git_wc, worktree_path):
        """Test getting current branch when in detached HEAD state."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="HEAD\n",
                stderr="",
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch is None

    def test_get_current_branch_error(self, git_wc, worktree_path):
        """Test getting current branch when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="not a git repository"
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch is None


class TestGetHeadSha:
    """Tests for get_head_sha method."""

    def test_get_head_sha_success(self, git_wc, worktree_path):
        """Test getting HEAD SHA successfully."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123def456\n",
                stderr="",
            )

            sha = git_wc.get_head_sha(worktree_path)

            assert sha == "abc123def456"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "HEAD" in args

    def test_get_head_sha_error(self, git_wc, worktree_path):
        """Test getting HEAD SHA when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="not a git repository"
            )

            sha = git_wc.get_head_sha(worktree_path)

            assert sha is None


class TestGetBranchStatus:
    """Tests for get_branch_status method."""

    def test_get_branch_status_clean_with_upstream(self, git_wc, worktree_path):
        """Test getting branch status with clean state and upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # First call: get current branch
            # Second call: git status --porcelain (clean)
            # Third call: git rev-list for ahead/behind counts
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # clean
                MagicMock(returncode=0, stdout="3\t2\n", stderr=""),  # 3 ahead, 2 behind
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is not None
            assert status.branch == "main"
            assert status.clean is True
            assert status.has_remote is True
            assert status.ahead == 3
            assert status.behind == 2

    def test_get_branch_status_dirty_no_upstream(self, git_wc, worktree_path):
        """Test getting branch status with uncommitted changes and no upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout=" M file.txt\n", stderr=""),  # dirty
                git_error(1, "git", stderr="no upstream"),
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is not None
            assert status.branch == "feature-branch"
            assert status.clean is False
            assert status.has_remote is False
            assert status.ahead == 0
            assert status.behind == 0

    def test_get_branch_status_detached_head(self, git_wc, worktree_path):
        """Test getting branch status when in detached HEAD state."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            status = git_wc.get_branch_status(worktree_path)

            assert status is None

    def test_get_branch_status_error(self, git_wc, worktree_path):
        """Test getting branch status when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                git_error(1, "git", stderr="error"),
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is None


class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes method."""

    def test_has_uncommitted_changes_true(self, git_wc, worktree_path):
        """Test detecting uncommitted changes."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" M file.txt\nA  newfile.py\n",
                stderr="",
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is True

    def test_has_uncommitted_changes_false(self, git_wc, worktree_path):
        """Test clean working directory."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is False

    def test_has_uncommitted_changes_error(self, git_wc, worktree_path):
        """Test error handling - assume dirty on error for safety."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is True  # Safer to assume dirty


class TestListDirtyFiles:
    """Tests for list_dirty_files method."""

    def test_list_dirty_files_unstaged_mode(self, git_wc, worktree_path):
        """Unstaged mode should only include unstaged tracked files."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="b.txt\0a.txt\0",
                stderr="",
            )

            files = git_wc.list_dirty_files(worktree_path, "unstaged")

            assert files == ["a.txt", "b.txt"]
            mock_run.assert_called_once_with(
                worktree_path,
                ["diff", "--name-only", "-z"],
                newlines=OutputNewlines.PRESERVED,
            )

    def test_list_dirty_files_tracked_mode(self, git_wc, worktree_path):
        """Tracked mode should include unstaged and staged tracked files."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="a.txt\0", stderr=""),
                MagicMock(returncode=0, stdout="b.txt\0a.txt\0", stderr=""),
            ]

            files = git_wc.list_dirty_files(worktree_path, "tracked")

            assert files == ["a.txt", "b.txt"]

    def test_list_dirty_files_all_mode(self, git_wc, worktree_path):
        """All mode should include tracked and untracked files."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="tracked-unstaged.py\0", stderr=""),
                MagicMock(returncode=0, stdout="tracked-staged.py\0", stderr=""),
                MagicMock(returncode=0, stdout="new-untracked.txt\0", stderr=""),
            ]

            files = git_wc.list_dirty_files(worktree_path, "all")

            assert files == [
                "new-untracked.txt",
                "tracked-staged.py",
                "tracked-unstaged.py",
            ]

    def test_list_dirty_files_git_error_returns_none(self, git_wc, worktree_path):
        """Enumeration failure surfaces as ``None`` so callers can
        distinguish "everything filtered out" (``[]``) from "couldn't
        enumerate" — the latter must fail closed at the policy layer.

        Returning ``[]`` here was the original behavior and the source
        of #6159's fail-open bug: the publish gate observed
        ``dirty=True`` from the bool check + ``[]`` from this method
        and approved the push despite an unknown dirty state.
        """
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(stderr="error")

            files = git_wc.list_dirty_files(worktree_path, "all")

            assert files is None

    def test_list_dirty_files_all_mode_filters_untracked_planted(
        self, git_wc, worktree_path
    ):
        """Untracked orchestrator-planted paths are filtered in all-mode.

        ``sync_cli_tools`` copies cli_tools into every worktree. In a foreign
        target repo they appear here as untracked and must not fire the
        dirty-tree guard — otherwise coding-done fails in every worktree.
        """
        planted = "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout=f"new-real-file.txt\0{planted}\0",
                    stderr="",
                ),
            ]

            files = git_wc.list_dirty_files(worktree_path, "all")

            assert files == ["new-real-file.txt"]

    def test_list_dirty_files_tracked_mode_keeps_planted_modifications(
        self, git_wc, worktree_path
    ):
        """Tracked modifications to planted paths survive the filter.

        In the orchestrator's own repo these paths are tracked source —
        a developer edit is legitimate and must still fire the guard. The
        filter applies only to the untracked-enumeration branch, which is
        never consulted in tracked-mode.
        """
        planted = "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=f"{planted}\0", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            files = git_wc.list_dirty_files(worktree_path, "tracked")

            assert files == [planted]


class TestGetCommitsAheadOfMain:
    """Tests for get_commits_ahead_of_main method."""

    def test_get_commits_ahead_of_main_success(self, git_wc, worktree_path):
        """Test getting commits ahead of main."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "abc123|Add feature X|Alice|abc12\n"
                    "def456|Fix bug Y|Bob|def45\n"
                ),
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert len(commits) == 2
            assert commits[0].sha == "abc123"
            assert commits[0].message == "Add feature X"
            assert commits[0].author == "Alice"
            assert commits[0].short_sha == "abc12"
            assert commits[1].sha == "def456"
            assert commits[1].message == "Fix bug Y"
            assert commits[1].author == "Bob"
            assert commits[1].short_sha == "def45"

    def test_get_commits_ahead_of_main_empty(self, git_wc, worktree_path):
        """Test when no commits ahead of main."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert commits == []

    def test_get_commits_ahead_of_main_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert commits == []

    def test_get_commits_ahead_of_main_malformed(self, git_wc, worktree_path):
        """Test handling malformed output."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "abc123|Add feature X|Alice|abc12\n"
                    "malformed line\n"
                    "def456|Fix bug Y|Bob|def45\n"
                ),
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            # Should skip malformed line
            assert len(commits) == 2


class TestFetch:
    """Tests for fetch method."""

    def test_fetch_success(self, git_wc, worktree_path):
        """Test successful fetch."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.fetch(worktree_path)

            assert result is True
            args = mock_run.call_args[0][1]
            assert "fetch" in args
            assert "origin" in args

    def test_fetch_custom_remote(self, git_wc, worktree_path):
        """Test fetch with custom remote."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.fetch(worktree_path, remote="upstream")

            assert result is True
            args = mock_run.call_args[0][1]
            assert "fetch" in args
            assert "upstream" in args

    def test_fetch_error(self, git_wc, worktree_path):
        """Test fetch failure."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="network error"
            )

            result = git_wc.fetch(worktree_path)

            assert result is False


class TestListRemoteBranches:
    """Tests for list_remote_branches method."""

    def test_list_remote_branches_success(self, git_wc, worktree_path):
        """Test listing remote branches."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="  origin/main\n  origin/feature-1\n  origin/feature-2\n",
                stderr="",
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert len(branches) == 3
            assert "origin/main" in branches
            assert "origin/feature-1" in branches
            assert "origin/feature-2" in branches

    def test_list_remote_branches_empty(self, git_wc, worktree_path):
        """Test listing remote branches when none exist."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert branches == []

    def test_list_remote_branches_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert branches == []


class TestIsGitRepo:
    """Tests for is_git_repo method."""

    def test_is_git_repo_true(self, git_wc, worktree_path):
        """Test detecting a git repository."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=".git\n",
                stderr="",
            )

            result = git_wc.is_git_repo(worktree_path)

            assert result is True
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--git-dir" in args

    def test_is_git_repo_false(self, git_wc, worktree_path):
        """Test detecting non-git directory."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                128, "git", stderr="not a git repository"
            )

            result = git_wc.is_git_repo(worktree_path)

            assert result is False


class TestGetConfigValue:
    """Tests for get_config_value method."""

    def test_get_config_value_success(self, git_wc, worktree_path):
        """Test getting config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="user@example.com\n",
                stderr="",
            )

            value = git_wc.get_config_value(worktree_path, "user.email")

            assert value == "user@example.com"
            args = mock_run.call_args[0][1]
            assert "config" in args
            assert "--get" in args
            assert "user.email" in args

    def test_get_config_value_empty(self, git_wc, worktree_path):
        """Test getting empty config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            value = git_wc.get_config_value(worktree_path, "some.key")

            assert value is None

    def test_get_config_value_not_found(self, git_wc, worktree_path):
        """Test getting non-existent config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="key not found"
            )

            value = git_wc.get_config_value(worktree_path, "nonexistent.key")

            assert value is None


class TestGetCommitsAheadCount:
    """Tests for get_commits_ahead_count method."""

    def test_get_commits_ahead_count_success(self, git_wc, worktree_path):
        """Test counting commits ahead."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="5\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 5
            args = mock_run.call_args[0][1]
            assert "rev-list" in args
            assert "--count" in args
            assert "origin/main..origin/feature-branch" in args

    def test_get_commits_ahead_count_zero(self, git_wc, worktree_path):
        """Test when no commits ahead."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="0\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0

    def test_get_commits_ahead_count_custom_base(self, git_wc, worktree_path):
        """Test with custom base branch."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="3\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(
                worktree_path, "feature-branch", base="origin/develop"
            )

            assert count == 3
            args = mock_run.call_args[0][1]
            assert "origin/develop..origin/feature-branch" in args

    def test_get_commits_ahead_count_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0

    def test_get_commits_ahead_count_invalid_output(self, git_wc, worktree_path):
        """Test handling invalid output."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not a number\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0


class TestGetLastCommitDate:
    """Tests for get_last_commit_date method."""

    def test_get_last_commit_date_success(self, git_wc, worktree_path):
        """Test getting last commit date."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="2 hours ago\n",
                stderr="",
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date == "2 hours ago"
            args = mock_run.call_args[0][1]
            assert "log" in args
            assert "-1" in args
            assert "--format=%cr" in args
            assert "origin/feature-branch" in args

    def test_get_last_commit_date_empty(self, git_wc, worktree_path):
        """Test when output is empty."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date is None

    def test_get_last_commit_date_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date is None


class TestRebaseOnBranch:
    """Tests for rebase_on_branch method."""

    def test_rebase_success(self, git_wc, mock_git, worktree_path):
        """Test successful rebase."""
        mock_git.rebase.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = git_wc.rebase_on_branch(worktree_path)

        assert result.success is True
        assert "origin/main" in result.message
        assert result.conflicts is None
        assert result.aborted is False

    def test_rebase_custom_target(self, git_wc, mock_git, worktree_path):
        """Test rebase with custom target."""
        mock_git.rebase.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = git_wc.rebase_on_branch(worktree_path, target="origin/develop")

        assert result.success is True
        assert "origin/develop" in result.message
        mock_git.rebase.assert_called_once_with(worktree_path, "origin/develop")

    def test_rebase_with_conflicts(self, git_wc, mock_git, worktree_path):
        """Test rebase failure with conflicts."""
        mock_git.rebase.side_effect = git_error(
            1, "git", stderr="CONFLICT (content): Merge conflict"
        )
        mock_git.run.return_value = MagicMock(
            returncode=0,
            stdout="UU conflicted_file.py\nUU another_conflict.txt\n",
            stderr="",
        )
        mock_git.rebase_abort.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = git_wc.rebase_on_branch(worktree_path)

        assert result.success is False
        assert "conflicts" in result.message.lower()
        assert result.conflicts == ["conflicted_file.py", "another_conflict.txt"]
        assert result.aborted is True

    def test_rebase_abort_fails(self, git_wc, mock_git, worktree_path):
        """Test rebase failure where abort also fails."""
        mock_git.rebase.side_effect = git_error(1, "git", stderr="CONFLICT")
        mock_git.run.return_value = MagicMock(returncode=0, stdout="UU file.py\n", stderr="")
        mock_git.rebase_abort.side_effect = git_error(1, "git", stderr="abort failed")

        result = git_wc.rebase_on_branch(worktree_path)

        assert result.success is False
        assert result.aborted is False

    def test_rebase_error_no_conflicts(self, git_wc, mock_git, worktree_path):
        """Test rebase failure without conflicts."""
        mock_git.rebase.side_effect = git_error(1, "git", stderr="fatal: some error")
        mock_git.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = git_wc.rebase_on_branch(worktree_path)

        assert result.success is False
        assert result.conflicts is None or result.conflicts == []


class TestPush:
    """Tests for push method."""

    def test_push_success_with_existing_remote(self, git_wc, worktree_path):
        """Test successful push when remote branch exists (subsequent push)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch succeeds
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path)

            assert result.success is True
            assert result.branch == "feature-branch"
            assert result.remote == "origin"
            assert "Pushed" in result.message

            # Check fetch command
            fetch_args = mock_run.call_args_list[1][0][1]
            assert "fetch" in fetch_args

            # Check push command
            push_args = mock_run.call_args_list[2][0][1]
            assert "push" in push_args
            assert "--force-with-lease" in push_args

    def test_push_uses_injected_git_auth_env(self, mock_git, worktree_path):
        """Authenticated publish injects env without putting tokens in argv."""
        class _GitAuth:
            def git_env_overrides(self, *, remote: str) -> dict[str, str]:
                assert remote == "origin"
                return {
                    "GIT_CONFIG_COUNT": "3",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": (
                        "Authorization: Basic "
                        "eC1hY2Nlc3MtdG9rZW46aW5zdGFsbGF0aW9uLXRva2Vu"
                    ),
                    "GIT_CONFIG_KEY_1": "remote.origin.url",
                    "GIT_CONFIG_VALUE_1": "https://github.com/owner/repo.git",
                    "GIT_CONFIG_KEY_2": "remote.origin.pushurl",
                    "GIT_CONFIG_VALUE_2": "https://github.com/owner/repo.git",
                }

        git_wc = GitWorkingCopy(git=mock_git, git_auth=_GitAuth())
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is True
            fetch_env = mock_run.call_args_list[1].kwargs["env"]
            push_env = mock_run.call_args_list[2].kwargs["env"]
            assert fetch_env["GIT_CONFIG_VALUE_0"] == (
                "Authorization: Basic "
                "eC1hY2Nlc3MtdG9rZW46aW5zdGFsbGF0aW9uLXRva2Vu"
            )
            assert push_env["GIT_CONFIG_VALUE_0"] == (
                "Authorization: Basic "
                "eC1hY2Nlc3MtdG9rZW46aW5zdGFsbGF0aW9uLXRva2Vu"
            )
            push_args = " ".join(mock_run.call_args_list[2].args[1])
            assert "installation-token" not in push_args
            assert "installation-token" not in push_env["GIT_CONFIG_VALUE_1"]

    def test_push_first_push_no_remote(self, git_wc, worktree_path):
        """Test first push when remote branch doesn't exist yet."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (fails with "couldn't find remote ref"), clear ref, push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(
                    returncode=128, stdout="",
                    stderr="fatal: couldn't find remote ref feature-branch"
                ),  # first push - branch doesn't exist on remote
                MagicMock(returncode=0, stdout="", stderr=""),  # clear stale ref
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path)

            assert result.success is True
            assert mock_run.call_count == 4
            push_args = mock_run.call_args_list[3][0][1]
            assert "push" in push_args

    def test_push_fetch_failure_network(self, git_wc, worktree_path):
        """Test push fails early when fetch fails with network error."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Fetch returns an error that is NOT "couldn't find remote ref"
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(
                    returncode=1, stdout="",
                    stderr="fatal: unable to access: Connection refused"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Failed to update tracking refs" in result.message
            assert result.retryable is True
            # Should NOT have called push
            assert mock_run.call_count == 2

    def test_push_fetch_failure_exception(self, git_wc, worktree_path):
        """Test push fails early when fetch throws an exception."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                Exception("Network timeout"),  # fetch throws
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Failed to update tracking refs" in result.message
            assert result.retryable is True

    def test_push_no_branch(self, git_wc, worktree_path):
        """Test push when current branch cannot be determined."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.branch == ""
            assert "Could not determine current branch" in result.message
            assert result.retryable is False

    def test_push_with_skip_hooks(self, git_wc, worktree_path):
        """Test push with skip_hooks enabled."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, skip_hooks=True)

            assert result.success is True
            push_args = mock_run.call_args_list[2][0][1]
            assert "--no-verify" in push_args

    def test_push_without_set_upstream(self, git_wc, worktree_path):
        """Test push without setting upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, set_upstream=False)

            assert result.success is True
            push_args = mock_run.call_args_list[2][0][1]
            assert "-u" not in push_args

    def test_push_non_fast_forward(self, git_wc, worktree_path):
        """Test push failure due to non-fast-forward."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push (fails)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="error: failed to push some refs (non-fast-forward)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.branch == "feature-branch"
            assert "non-fast-forward" in result.message
            assert result.retryable is False  # Needs force or rebase

    def test_push_rejected(self, git_wc, worktree_path):
        """Test push failure due to rejected refs."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push (fails)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="error: failed to push (rejected)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "rejected" in result.message
            assert result.retryable is False

    def test_push_failure_preserves_hook_stdout_with_git_stderr(self, git_wc, worktree_path):
        """Pre-push hooks often print actionable diagnostics to stdout."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                git_error(
                    stdout=(
                        "ERROR: Test-skipping patterns detected\n"
                        "+import org.junit.jupiter.api.Assumptions.assumeTrue"
                    ),
                    stderr="error: failed to push some refs",
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Test-skipping patterns detected" in result.message
            assert "assumeTrue" in result.message
            assert "failed to push some refs" in result.message

    def test_push_permission_denied(self, git_wc, worktree_path):
        """Test push failure due to permission denied."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push (fails)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="Permission denied (publickey)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Permission denied" in result.message
            assert result.retryable is False  # Auth issue

    def test_push_network_error(self, git_wc, worktree_path):
        """Test push failure due to network error (retryable)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push (fails)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="Could not resolve host: github.com"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.retryable is True  # Network issues are retryable

    def test_push_custom_remote(self, git_wc, worktree_path):
        """Test push to custom remote."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (success), push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, remote="upstream")

            assert result.success is True
            assert result.remote == "upstream"
            # Check fetch uses custom remote
            fetch_args = mock_run.call_args_list[1][0][1]
            assert "fetch" in fetch_args
            assert "upstream" in fetch_args
            # Check push uses custom remote
            push_args = mock_run.call_args_list[2][0][1]
            assert "upstream" in push_args


class TestGetIssueNumberFromBranch:
    """Tests for get_issue_number_from_branch method."""

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_canonical_format(
        self, mock_extract, git_wc, worktree_path
    ):
        """Test extracting issue number from canonical format."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="328-add-feature\n", stderr=""
            )
            mock_extract.return_value = 328

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 328
            mock_extract.assert_called_once_with("328-add-feature")

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_legacy_format(
        self, mock_extract, git_wc, worktree_path
    ):
        """Test fallback to legacy format (issue-123)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="issue-123\n", stderr=""
            )
            mock_extract.return_value = None  # Canonical format doesn't match

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 123

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_slash_format(self, mock_extract, git_wc, worktree_path):
        """Test fallback to slash format (feature/456-thing)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="feature/456-thing\n", stderr=""
            )
            mock_extract.return_value = None

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 456

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_no_match(self, mock_extract, git_wc, worktree_path):
        """Test when branch name doesn't contain issue number."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="main\n", stderr=""
            )
            mock_extract.return_value = None

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num is None

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_no_branch(self, mock_extract, git_wc, worktree_path):
        """Test when current branch cannot be determined."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num is None
            mock_extract.assert_not_called()


class TestGetWorktreeRoot:
    """Tests for get_worktree_root method."""

    def test_get_worktree_root_success(self, git_wc, worktree_path):
        """Test getting worktree root."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/path/to/repo\n",
                stderr="",
            )

            root = git_wc.get_worktree_root(worktree_path)

            assert root == Path("/path/to/repo")
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--show-toplevel" in args

    def test_get_worktree_root_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                128, "git", stderr="not a git repository"
            )

            root = git_wc.get_worktree_root(worktree_path)

            assert root is None


class TestCommitAll:
    """Tests for commit_all method."""

    def test_commit_all_success(self, git_wc, worktree_path):
        """Test successful commit."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # First call: git add -A
            # Second call: git commit
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is True

            # Check git add command
            add_args = mock_run.call_args_list[0][0][1]
            assert "add" in add_args
            assert "-A" in add_args

            # Check git commit command
            commit_args = mock_run.call_args_list[1][0][1]
            assert "commit" in commit_args
            assert "-m" in commit_args
            assert "Test commit" in commit_args

    def test_commit_all_with_allow_empty(self, git_wc, worktree_path):
        """Test commit with allow_empty flag."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.commit_all(worktree_path, "Empty commit", allow_empty=True)

            assert result is True
            commit_args = mock_run.call_args_list[1][0][1]
            assert "--allow-empty" in commit_args

    def test_commit_all_nothing_to_commit(self, git_wc, worktree_path):
        """Test when there's nothing to commit."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Create an error with stdout set on the result
            error = git_error(stdout="nothing to commit, working tree clean", stderr="")

            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                error,
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            # Should return True for "nothing to commit" - not an error
            assert result is True

    def test_commit_all_add_fails(self, git_wc, worktree_path):
        """Test when git add fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error adding files"
            )

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is False

    def test_commit_all_commit_fails(self, git_wc, worktree_path):
        """Test when git commit fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                git_error(
                    1, "git", stderr="Author identity unknown"
                ),
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is False


class TestPushPreflight:
    """Tests for push_preflight method."""

    def test_push_preflight_success_with_tracking(self, git_wc, worktree_path):
        """Test successful preflight when remote tracking branch exists."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch succeeds
                    MagicMock(returncode=0, stdout="", stderr=""),  # push --dry-run
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert result.error is None
                assert mock_run.call_count == 2

    def test_push_preflight_success_first_push(self, git_wc, worktree_path):
        """Test successful preflight when no remote tracking (first push)."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (fails with "couldn't find remote ref"), clear ref, push --dry-run
                mock_run.side_effect = [
                    MagicMock(
                        returncode=128, stdout="",
                        stderr="fatal: couldn't find remote ref feature-branch"
                    ),  # first push
                    MagicMock(returncode=0, stdout="", stderr=""),  # clear stale ref
                    MagicMock(returncode=0, stdout="", stderr=""),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert mock_run.call_count == 3

    def test_push_preflight_fetch_failure(self, git_wc, worktree_path):
        """Test preflight fails early when fetch fails with network error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Fetch fails with an error other than "couldn't find remote ref"
                mock_run.side_effect = [
                    MagicMock(
                        returncode=1, stdout="",
                        stderr="fatal: unable to access: Connection refused"
                    ),
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "Failed to update tracking refs" in result.error
                assert result.fix_hint is not None
                assert mock_run.call_count == 1  # No push attempted

    def test_push_preflight_no_branch(self, git_wc, worktree_path):
        """Test when current branch cannot be determined."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = None

            result = git_wc.push_preflight(worktree_path)

            assert result.would_succeed is False
            assert "branch" in result.error.lower()
            assert result.fix_hint is not None

    def test_push_preflight_uses_metadata_branch(self, git_wc, worktree_path):
        """Branch metadata should satisfy push_preflight when HEAD is detached."""
        meta_dir = worktree_path / ".issue-orchestrator"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "worktree.json").write_text(json.dumps({"branch_name": "feature-meta"}))

        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = None

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    MagicMock(returncode=0, stdout="", stderr=""),  # push dry-run
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert mock_run.call_count == 2

    def test_push_preflight_stale_info_error(self, git_wc, worktree_path):
        """Test handling stale info error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run (fails)
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="stale info detected"),  # push fails
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "stale info" in result.error
                assert result.fix_hint is not None
                assert "rebase" in result.fix_hint.lower()

    def test_push_preflight_rejected_error(self, git_wc, worktree_path):
        """Test handling rejected push error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run (fails)
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="! [rejected] non-fast-forward"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "rejected" in result.error
                assert result.fix_hint is not None

    def test_push_preflight_permission_denied(self, git_wc, worktree_path):
        """Test handling permission denied error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run (fails)
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="Permission denied (publickey)"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "permission denied" in result.error.lower()
                assert "authentication" in result.fix_hint.lower()

    def test_push_preflight_timeout(self, git_wc, worktree_path):
        """Test handling timeout error on push."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run (timeout)
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    Exception("command timed out"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "timed out" in result.error.lower()
                assert result.fix_hint is not None

    def test_push_preflight_custom_remote(self, git_wc, worktree_path):
        """Test with custom remote."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: fetch (success), push --dry-run
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    MagicMock(returncode=0, stdout="", stderr=""),  # push
                ]

                result = git_wc.push_preflight(worktree_path, remote="upstream")

                assert result.would_succeed is True
                # Check fetch uses custom remote
                fetch_args = mock_run.call_args_list[0][0][1]
                assert "fetch" in fetch_args
                assert "upstream" in fetch_args


class TestPushFetchFailureStopsProcessing:
    """Test that fetch failures stop processing early."""

    def test_push_fetch_failure_prevents_push(self, git_wc, worktree_path):
        """Verify that when fetch fails with network error, push is never attempted."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, fetch (fails with network error)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),  # get_branch
                MagicMock(
                    returncode=1, stdout="",
                    stderr="fatal: unable to access: Connection refused"
                ),  # fetch fails
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Failed to update tracking refs" in result.message
            # Only 2 calls - push was never attempted
            assert mock_run.call_count == 2

    def test_preflight_fetch_failure_prevents_dry_run(self, git_wc, worktree_path):
        """Verify that when fetch fails in preflight, dry-run is never attempted."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Fetch fails with network error (not "couldn't find remote ref")
                mock_run.side_effect = [
                    MagicMock(
                        returncode=1, stdout="",
                        stderr="fatal: unable to access: Connection refused"
                    ),
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "Failed to update tracking refs" in result.error
                # Only 1 call - dry-run push was never attempted
                assert mock_run.call_count == 1


class TestPushClearsStaleRemoteTrackingRef:
    """Ensure stale remote-tracking refs are cleared for first-push cases."""

    def test_push_clears_stale_remote_ref_on_missing_remote(self, git_wc, worktree_path):
        """Delete stale remote-tracking ref when remote branch is missing."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=1, stdout="", stderr="fatal: couldn't find remote ref feature-branch"),
                    MagicMock(returncode=0, stdout="", stderr=""),
                    MagicMock(returncode=0, stdout="", stderr=""),
                ]

                result = git_wc.push(worktree_path)

                assert result.success is True
                assert mock_run.call_args_list[1][0][1] == [
                    "update-ref",
                    "-d",
                    "refs/remotes/origin/feature-branch",
                ]

    def test_preflight_clears_stale_remote_ref_on_missing_remote(self, git_wc, worktree_path):
        """Delete stale remote-tracking ref during preflight when remote branch is missing."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=1, stdout="", stderr="fatal: couldn't find remote ref feature-branch"),
                    MagicMock(returncode=0, stdout="", stderr=""),
                    MagicMock(returncode=0, stdout="", stderr=""),
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert mock_run.call_args_list[1][0][1] == [
                    "update-ref",
                    "-d",
                    "refs/remotes/origin/feature-branch",
                ]


def _run_git_cmd(cwd: Path, *args: str) -> str:
    """Run a real git command in a test repo, returning stdout."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class TestReadBranchTextFiles:
    def test_reads_exact_head_objects_in_requested_order(
        self, git_wc, worktree_path
    ):
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                git_result(stdout="first\n"),
                git_result(stdout="second\n"),
            ]

            result = git_wc.read_branch_text_files(
                worktree_path, ("tests/test_first.py", "tests/test_second.py")
            )

        assert result == BranchTextFilesResult(
            success=True,
            files=(
                BranchTextFile(path="tests/test_first.py", content="first\n"),
                BranchTextFile(path="tests/test_second.py", content="second\n"),
            ),
        )
        assert mock_run.call_args_list == [
            call(
                worktree_path,
                ["show", "HEAD:tests/test_first.py"],
                newlines=OutputNewlines.PRESERVED,
            ),
            call(
                worktree_path,
                ["show", "HEAD:tests/test_second.py"],
                newlines=OutputNewlines.PRESERVED,
            ),
        ]

    def test_git_error_fails_complete_request(self, git_wc, worktree_path):
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(stderr="fatal: path missing")

            result = git_wc.read_branch_text_files(
                worktree_path, ("tests/test_guard.py",)
            )

        assert result.success is False
        assert result.files == ()
        assert result.error == "fatal: path missing"


class TestBranchPostImagePathsAgainstBase:
    """Path-oriented branch-tip query (#6659).

    The runtime-artifact guard relies on this to see *every* branch-tip change
    shape — including no-hunk additions (empty files), binary blobs, and
    rename/copy destinations — and to exclude deletions. A unified-diff text
    parser misses the no-hunk and binary shapes, which is the bypass this
    method closes.
    """

    @staticmethod
    def _init_repo(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        _run_git_cmd(root, "init", "-q")
        _run_git_cmd(root, "config", "user.email", "test@example.com")
        _run_git_cmd(root, "config", "user.name", "Test User")
        _run_git_cmd(root, "config", "commit.gpgsign", "false")

    def test_captures_empty_binary_and_rename_excludes_deletion(self, tmp_path):
        # Real git so we exercise the actual diff output shapes, not a stub.
        root = tmp_path / "repo"
        self._init_repo(root)

        # Base commit: a file we will later rename, plus one we will delete.
        (root / "to_rename.txt").write_text("hello\n")
        (root / "to_delete.txt").write_text("bye\n")
        _run_git_cmd(root, "add", "-A")
        _run_git_cmd(root, "commit", "-q", "-m", "base")
        base = _run_git_cmd(root, "rev-parse", "HEAD")

        # Feature branch carrying the runtime-artifact shapes a text parser
        # cannot see.
        _run_git_cmd(root, "checkout", "-q", "-b", "feature")
        io_dir = root / ".issue-orchestrator"
        (io_dir / "tool-homes").mkdir(parents=True)
        (io_dir / "review-response.json").write_text("")  # empty: no diff hunk
        (io_dir / "tool-homes" / "blob.bin").write_bytes(bytes(range(256)))  # binary
        os.replace(root / "to_rename.txt", io_dir / "renamed.txt")  # rename into IO dir
        (root / "to_delete.txt").unlink()  # deletion: must be excluded
        _run_git_cmd(root, "add", "-A")
        _run_git_cmd(root, "commit", "-q", "-m", "feature")

        result = GitWorkingCopy().branch_post_image_paths_against_base(root, base)

        assert result.success
        paths = set(result.paths)
        # No-hunk empty addition and binary addition are both captured.
        assert ".issue-orchestrator/review-response.json" in paths
        assert ".issue-orchestrator/tool-homes/blob.bin" in paths
        # Rename destination (post-image) is captured; its pre-image is not.
        assert ".issue-orchestrator/renamed.txt" in paths
        assert "to_rename.txt" not in paths
        # A pure deletion leaves nothing in the branch tip, so it is excluded.
        assert "to_delete.txt" not in paths

    def test_uses_path_oriented_command_and_parses_nul_output(
        self, git_wc, worktree_path
    ):
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=".issue-orchestrator/review-response.json\0src/app.py\0",
                stderr="",
            )

            result = git_wc.branch_post_image_paths_against_base(
                worktree_path, "origin/main"
            )

            assert result == BranchPathsResult(
                success=True,
                paths=(".issue-orchestrator/review-response.json", "src/app.py"),
            )
            mock_run.assert_called_once_with(
                worktree_path,
                [
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-ext-diff",
                    "--diff-filter=ACMRT",
                    "origin/main...HEAD",
                ],
                newlines=OutputNewlines.PRESERVED,
            )

    def test_git_error_fails_closed(self, git_wc, worktree_path):
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(stderr="fatal: bad revision")

            result = git_wc.branch_post_image_paths_against_base(
                worktree_path, "origin/main"
            )

            assert result.success is False
            assert result.paths == ()
            assert result.error
