"""Unit tests for the worktree module."""

import os
import pytest
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import subprocess

from issue_orchestrator.adapters.worktree.api import (
    slugify,
    generate_branch_name,
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    can_remove_without_user_changes,
    get_worktree_branch,
    next_branch_name,
    install_hooks,
    find_worktree_for_branch,
    install_claude_settings,
    sync_cli_tools,
    WorktreeError,
)
from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions
from tests.unit.worktree_git_helpers import (
    block_worktree_config_writes,
    effective_hooks_path,
    make_git_worktree,
)


@pytest.fixture(autouse=True)
def clear_worktree_session_env(monkeypatch):
    """Keep unit expectations stable when tests run inside agent worktree sessions."""
    monkeypatch.delenv("ORCHESTRATOR_DISABLE_WORKTREE_REUSE", raising=False)


class TestSlugify:
    """Test the slugify function."""

    def test_slugify_basic(self):
        """Test basic slugification."""
        assert slugify("Add user authentication") == "add-user-authentication"

    def test_slugify_special_characters(self):
        """Test slugifying text with special characters."""
        assert slugify("Fix bug in @user's profile!") == "fix-bug-in-user-s-profile"
        assert slugify("Support 100% coverage") == "support-100-coverage"
        assert slugify("Update README.md file") == "update-readme-md-file"

    def test_slugify_multiple_spaces(self):
        """Test slugifying text with multiple consecutive spaces."""
        assert slugify("Add    multiple   spaces") == "add-multiple-spaces"

    def test_slugify_leading_trailing_special_chars(self):
        """Test that leading/trailing hyphens are removed."""
        assert slugify("!!!Important!!!") == "important"
        assert slugify("---dashes---") == "dashes"

    def test_slugify_max_length(self):
        """Test max length truncation."""
        long_text = "This is a very long title that should be truncated"
        result = slugify(long_text, max_length=20)
        assert len(result) <= 20
        assert result == "this-is-a-very-long"

    def test_slugify_max_length_no_trailing_hyphen(self):
        """Test that truncation doesn't leave trailing hyphens."""
        # If truncation happens mid-word, ensure no trailing hyphen
        text = "Add feature for users"
        result = slugify(text, max_length=15)
        assert not result.endswith("-")

    def test_slugify_unicode_characters(self):
        """Test slugifying unicode characters."""
        assert slugify("Café résumé") == "caf-r-sum"
        assert slugify("日本語タイトル") == ""  # Non-latin chars removed
        assert slugify("Fix émoji 🎉 support") == "fix-moji-support"

    def test_slugify_numbers(self):
        """Test slugifying text with numbers."""
        assert slugify("Issue 123 fix") == "issue-123-fix"
        assert slugify("v2.0 release") == "v2-0-release"

    def test_slugify_empty_string(self):
        """Test slugifying empty string."""
        assert slugify("") == ""
        assert slugify("   ") == ""
        assert slugify("!!!") == ""

    def test_slugify_only_special_chars(self):
        """Test text with only special characters."""
        assert slugify("@#$%^&*()") == ""

    def test_slugify_very_long_title(self):
        """Test extremely long title truncation."""
        very_long = "a" * 100
        result = slugify(very_long, max_length=40)
        assert len(result) == 40
        assert result == "a" * 40


class TestGenerateBranchName:
    """Test the generate_branch_name function."""

    def test_generate_branch_name_basic(self):
        """Test basic branch name generation."""
        result = generate_branch_name(123, "Add user authentication")
        assert result == "123-add-user-authentication"

    def test_generate_branch_name_with_special_chars(self):
        """Test branch name with special characters."""
        result = generate_branch_name(456, "Fix bug in @user's profile!")
        assert result == "456-fix-bug-in-user-s-profile"

    def test_generate_branch_name_long_title(self):
        """Test branch name with very long title."""
        long_title = "This is a very long issue title that should be truncated to fit"
        result = generate_branch_name(789, long_title)
        assert result.startswith("789-")
        # Check that the slug part is truncated to max 50 chars
        slug_part = result[4:]  # Remove "789-"
        assert len(slug_part) <= 50

    def test_generate_branch_name_unicode(self):
        """Test branch name with unicode characters."""
        result = generate_branch_name(42, "Add café résumé feature")
        assert result == "42-add-caf-r-sum-feature"

    def test_generate_branch_name_number_formatting(self):
        """Test that issue number is preserved correctly."""
        result = generate_branch_name(1, "Test")
        assert result.startswith("1-")

        result = generate_branch_name(99999, "Test")
        assert result.startswith("99999-")


class TestBranchSuffix:
    """Test branch suffix generation for recreated worktrees."""

    def test_next_branch_name_increments_suffix(self, monkeypatch, tmp_path):
        """Select next available -rN suffix."""
        monkeypatch.setattr(
            "issue_orchestrator.adapters.worktree._worktree._list_branch_names",
            lambda _repo: ["123-fix", "123-fix-r1", "123-fix-r3"],
        )
        assert next_branch_name(tmp_path, "123-fix") == "123-fix-r4"

    def test_next_branch_name_strips_existing_suffix(self, monkeypatch, tmp_path):
        """Avoid stacking suffixes when branch already has -rN."""
        monkeypatch.setattr(
            "issue_orchestrator.adapters.worktree._worktree._list_branch_names",
            lambda _repo: ["123-fix", "123-fix-r1"],
        )
        assert next_branch_name(tmp_path, "123-fix-r1") == "123-fix-r2"


class TestCreateWorktree:
    """Test the create_worktree function."""

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_success(self, mock_run, mock_install_hooks, tmp_path):
        """Test successful worktree creation."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-add-user-auth"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:3] == ["fetch", "origin", "123-add-user-auth"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                return MagicMock(returncode=0, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        # Execute
        worktree_path, branch_name, *_ = create_worktree(
            repo_root, 123, "Add user auth", worktree_base
        )

        # Verify
        assert branch_name == "123-add-user-auth"
        assert worktree_path == worktree_base / "repo-123"

        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        assert any(cmd[3:5] == ["worktree", "prune"] for cmd in calls)
        assert any(cmd[3:5] == ["worktree", "list"] for cmd in calls)
        assert any(cmd[3:6] == ["fetch", "origin", "main"] for cmd in calls)
        assert any(cmd[3:6] == ["rev-parse", "--verify", "origin/main"] for cmd in calls)

        # Create worktree with new branch (-b flag) from default branch
        worktree_cmd = next(cmd for cmd in calls if cmd[3:5] == ["worktree", "add"])
        assert "-b" in worktree_cmd  # New branch flag
        assert "123-add-user-auth" in worktree_cmd
        assert "origin/main" in worktree_cmd  # Should branch from origin/main

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_uses_seed_ref_override_for_fresh_branch(self, mock_run, mock_install_hooks, tmp_path):
        """Fresh worktrees can be seeded from an explicit local ref without changing PR base."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "HEAD"]:
                return MagicMock(returncode=0, stdout="seedsha\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        worktree_path, branch_name, *_ = create_worktree(
            repo_root,
            123,
            "Test",
            worktree_base=worktree_base,
            base_branch="main",
            seed_ref="HEAD",
        )

        assert worktree_path == worktree_base / "repo-123"
        assert branch_name == "123-test"

        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        worktree_cmd = next(cmd for cmd in calls if cmd[3:5] == ["worktree", "add"])
        assert worktree_cmd[-1] == "HEAD"
        assert "origin/main" not in worktree_cmd
        assert not any(cmd[3:6] == ["fetch", "origin", "main"] for cmd in calls)

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_stack_successor_branches_from_predecessor(self, mock_run, mock_install_hooks, tmp_path):
        """A stack successor's fresh branch is created from origin/<predecessor>.

        With no local/remote issue branch and no seed ref, passing the
        predecessor branch as base makes the new branch descend from the
        predecessor head, so the publish ancestry gate is satisfied without any
        manual rebase (#6596).
        """
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/20-base"]:
                return MagicMock(returncode=0, stdout="basesha\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        worktree_path, branch_name, *_ = create_worktree(
            repo_root,
            123,
            "Test",
            worktree_base=worktree_base,
            base_branch="20-base",
            seed_ref=None,
        )

        assert branch_name == "123-test"
        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        worktree_cmd = next(cmd for cmd in calls if cmd[3:5] == ["worktree", "add"])
        assert worktree_cmd[-3:] == ["-b", "123-test", "origin/20-base"]
        assert "origin/main" not in worktree_cmd

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_default_base(self, mock_run, mock_install_hooks, tmp_path):
        """Test worktree creation with default base directory."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute (no worktree_base specified)
        worktree_path, branch_name, *_ = create_worktree(repo_root, 456, "Fix bug")

        # Verify - should use parent of repo_root as base
        expected_path = tmp_path / "repo-456"
        assert worktree_path == expected_path

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_not_a_git_repo(self, mock_run, tmp_path):
        """Test error when path is not a git repository."""
        # Setup - directory without .git
        repo_root = tmp_path / "not-a-repo"
        repo_root.mkdir()

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Not a git repository"):
            create_worktree(repo_root, 123, "Test")

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_already_exists(self, mock_run, mock_install_hooks, tmp_path):
        """Test that existing worktree is reused when it passes validation."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        # Create existing worktree directory with valid .git file
        existing_worktree = worktree_base / "repo-123"
        existing_worktree.mkdir()
        gitdir = tmp_path / "gitdir"
        gitdir.mkdir()
        # Create .git file to make it look like a valid worktree
        (existing_worktree / ".git").write_text(f"gitdir: {gitdir}")

        # Mock subprocess calls for worktree reuse validation:
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            # Prune stale worktrees
            if "prune" in cmd:
                return MagicMock(returncode=0, stderr="")
            # Get current branch
            if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                return MagicMock(returncode=0, stdout="existing-branch\n")
            # Validation: MERGE_HEAD check - return 1 (no merge in progress)
            if "rev-parse" in cmd and "MERGE_HEAD" in cmd:
                return MagicMock(returncode=1, stderr="")
            # Validation: diff --check (no conflicts)
            if "diff" in cmd and "--check" in cmd:
                return MagicMock(returncode=0, stdout="")
            # Pull --rebase to update
            if "pull" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "ls-files" in cmd and "--error-unmatch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "update-index" in cmd and "--skip-worktree" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            # Default success for other commands
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = mock_subprocess

        # Execute - should reuse existing worktree instead of raising error
        path, branch, *_ = create_worktree(repo_root, 123, "Test", worktree_base)

        # Verify it returned the existing worktree
        assert path == existing_worktree
        assert branch == "existing-branch"
        # Verify hooks were reinstalled on reuse
        mock_install_hooks.assert_called_once_with(existing_worktree, None)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_git_command_fails(self, mock_run, tmp_path):
        """Test error when git command fails."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock failed worktree add command
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch (no match)
            MagicMock(returncode=1, stderr=""),  # branch doesn't exist
            MagicMock(returncode=1, stderr=""),  # fetch fails (branch not on remote)
            MagicMock(returncode=1, stderr=""),  # symbolic-ref fails (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # rev-parse main succeeds (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # fetch origin/main succeeds
            MagicMock(returncode=1, stderr="fatal: invalid reference"),  # worktree create fails
        ]

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to create worktree"):
            create_worktree(repo_root, 123, "Test")

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_recovers_from_stale_branch_registration(self, mock_run, mock_install_hooks, tmp_path):
        """Prune stale worktree metadata and retry when branch is bound to missing path."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        stale_path = tmp_path / "missing-worktree-123"

        worktree_list_output = (
            f"worktree {stale_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        state = {"worktree_add_calls": 0}

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                state["worktree_add_calls"] += 1
                if state["worktree_add_calls"] == 1:
                    return MagicMock(
                        returncode=1,
                        stdout="",
                        stderr=(
                            "Preparing worktree (checking out '123-test')\n"
                            f"fatal: '123-test' is already used by worktree at '{stale_path}'\n"
                        ),
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        worktree_path, branch_name, *_ = create_worktree(
            repo_root, 123, "Test", worktree_base=worktree_base
        )

        assert worktree_path == worktree_base / "repo-123"
        assert branch_name == "123-test"
        assert state["worktree_add_calls"] == 2

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_detaches_when_branch_in_use(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        tmp_path,
        monkeypatch,
    ):
        """Detach existing worktree branch when reuse is disabled."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        existing_worktree = tmp_path / "worktrees" / "issue-123" / "repo-123"
        existing_worktree.mkdir(parents=True)

        monkeypatch.setenv("ORCHESTRATOR_DISABLE_WORKTREE_REUSE", "1")

        worktree_list_output = (
            f"worktree {existing_worktree}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        # Use a function-based mock that handles various git commands
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "prune" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
            if "checkout" in cmd and "--detach" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "push" in cmd and "--delete" in cmd:
                return MagicMock(returncode=0, stderr="")
            # Default success for all other commands (worktree add, branch checks, etc)
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = mock_subprocess

        create_worktree(repo_root, 123, "Test")

        assert any(
            call_args[0][0][:4] == ["git", "-C", str(existing_worktree), "checkout"]
            and "--detach" in call_args[0][0]
            for call_args in mock_run.call_args_list
        )
        assert any(
            "worktree" in call_args[0][0]
            for call_args in mock_run.call_args_list
        )

        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_removes_existing_path_when_reuse_disabled(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        tmp_path,
        monkeypatch,
    ):
        """Remove existing worktree path when reuse is disabled."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        existing_path = worktree_base / "repo-123"
        existing_path.mkdir()
        gitdir = repo_root / ".git" / "worktrees" / "repo-123"

        monkeypatch.setenv("ORCHESTRATOR_DISABLE_WORKTREE_REUSE", "1")

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "remove"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                worktree_path = worktree_base / "repo-123"
                worktree_path.mkdir(parents=True, exist_ok=True)
                gitdir.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".git").write_text(f"gitdir: {gitdir}")
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ls-files", "--error-unmatch"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["update-index", "--skip-worktree"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        create_worktree(repo_root, 123, "Test", worktree_base=worktree_base)

        assert any(
            call_args[0][0][:5] == ["git", "-C", str(repo_root), "worktree", "remove"]
            for call_args in mock_run.call_args_list
        )

        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_does_not_create_setup_commit(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        mock_sync_cli_tools,
        tmp_path,
    ):
        """Worktree setup must not mutate the issue branch with a synthetic commit."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        gitdir = repo_root / ".git" / "worktrees" / "repo-123"
        exclude_path = gitdir / "info" / "exclude"
        mock_sync_cli_tools.return_value = [
            Path("src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"),
            Path("src/issue_orchestrator/entrypoints/cli_tools/validate_runner.py"),
            Path("src/issue_orchestrator/entrypoints/cli_tools/_runtime_models.py"),
        ]

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                worktree_path = worktree_base / "repo-123"
                worktree_path.mkdir(parents=True, exist_ok=True)
                gitdir.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".git").write_text(f"gitdir: {gitdir}")
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ls-files", "--error-unmatch"]:
                return MagicMock(returncode=0, stdout=f"{argv[-1]}\n", stderr="")
            if "commit" in argv:
                pytest.fail(f"unexpected setup commit command: {argv}")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        create_worktree(repo_root, 123, "Test", worktree_base=worktree_base)

        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()
        mock_sync_cli_tools.assert_called_once()
        commit_calls = [
            call_args[0][0][3:]
            for call_args in mock_run.call_args_list
            if len(call_args[0][0]) > 3 and call_args[0][0][3] == "commit"
        ]
        assert commit_calls == []
        skip_worktree_calls = [
            call_args[0][0][3:]
            for call_args in mock_run.call_args_list
            if call_args[0][0][3:5] == ["update-index", "--skip-worktree"]
        ]
        assert ["update-index", "--skip-worktree", "--", ".claude/settings.json"] in skip_worktree_calls
        assert ["update-index", "--skip-worktree", "--", ".issue-orchestrator/session-latest.json"] in skip_worktree_calls
        assert [
            "update-index",
            "--skip-worktree",
            "--",
            "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        ] in skip_worktree_calls
        assert [
            "update-index",
            "--skip-worktree",
            "--",
            "src/issue_orchestrator/entrypoints/cli_tools/validate_runner.py",
        ] in skip_worktree_calls
        assert [
            "update-index",
            "--skip-worktree",
            "--",
            "src/issue_orchestrator/entrypoints/cli_tools/_runtime_models.py",
        ] in skip_worktree_calls
        exclude_text = exclude_path.read_text()
        assert ".agent-done-marker" in exclude_text
        assert ".venv" in exclude_text
        assert ".claude/settings.json" in exclude_text
        assert ".issue-orchestrator/session-latest.json" in exclude_text
        assert ".issue-orchestrator/sessions" in exclude_text
        assert ".issue-orchestrator/validation" in exclude_text
        assert ".issue-orchestrator/worktree-id" in exclude_text
        assert "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py" in exclude_text

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_writes_excludes_to_common_git_dir_for_linked_worktree(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        mock_sync_cli_tools,
        tmp_path,
    ):
        """Linked worktrees should write ignore entries where git actually reads them."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"
        gitdir = repo_root / ".git" / "worktrees" / "repo-123"
        common_exclude = repo_root / ".git" / "info" / "exclude"
        mock_sync_cli_tools.return_value = [
            Path("src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"),
        ]

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                worktree_path = worktree_base / "repo-123"
                worktree_path.mkdir(parents=True, exist_ok=True)
                gitdir.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".git").write_text(f"gitdir: {gitdir}")
                (gitdir / "commondir").write_text(str(repo_root / ".git"))
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ls-files", "--error-unmatch"]:
                return MagicMock(returncode=0, stdout=f"{argv[-1]}\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        create_worktree(repo_root, 123, "Test", worktree_base=worktree_base)

        assert ".issue-orchestrator/sessions" in common_exclude.read_text()
        assert "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py" in common_exclude.read_text()
        assert ".issue-orchestrator/sessions" in (gitdir / "info" / "exclude").read_text()

    def test_sync_cli_tools_copies_runtime_support_files(self, tmp_path):
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        synced_paths = sync_cli_tools(worktree_path)

        assert Path("src/issue_orchestrator/entrypoints/cli_tools/coding_done.py") in synced_paths
        assert Path("src/issue_orchestrator/entrypoints/cli_tools/_runtime_models.py") in synced_paths
        synced_models = (
            worktree_path
            / "src"
            / "issue_orchestrator"
            / "entrypoints"
            / "cli_tools"
            / "_runtime_models.py"
        )
        assert synced_models.exists()
        assert "class ProposedFollowUpIssue" in synced_models.read_text()

    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_links_repo_venv_into_issue_worktree(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        mock_sync_cli_tools,
        tmp_path,
    ):
        """Issue worktrees should reuse the repo venv so validate-quick can run there."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        (repo_root / ".venv" / "bin").mkdir(parents=True)
        (repo_root / ".venv" / "bin" / "python").write_text("")

        worktree_base = tmp_path / "worktrees"
        gitdir = repo_root / ".git" / "worktrees" / "repo-123"
        mock_sync_cli_tools.return_value = []

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["fetch", "origin", "123-test"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                worktree_path = worktree_base / "repo-123"
                worktree_path.mkdir(parents=True, exist_ok=True)
                gitdir.mkdir(parents=True, exist_ok=True)
                (worktree_path / ".git").write_text(f"gitdir: {gitdir}")
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ls-files", "--error-unmatch"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        worktree_path, *_ = create_worktree(repo_root, 123, "Test", worktree_base=worktree_base)

        linked_venv = worktree_path / ".venv"
        assert linked_venv.is_symlink()
        assert linked_venv.resolve() == (repo_root / ".venv").resolve()
        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_creates_base_directory(self, mock_run, mock_install_hooks, tmp_path):
        """Test that worktree base directory is created if it doesn't exist."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "new" / "nested" / "worktrees"

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute
        create_worktree(repo_root, 123, "Test", worktree_base)

        # Verify base directory was created
        assert worktree_base.exists()
        assert worktree_base.is_dir()

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_with_complex_title(self, mock_run, mock_install_hooks, tmp_path):
        """Test worktree creation with complex issue title."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute with complex title
        complex_title = "Fix bug in @user's profile (100% coverage) 🎉"
        worktree_path, branch_name, *_ = create_worktree(repo_root, 999, complex_title)

        # Verify branch name is properly slugified
        assert branch_name == "999-fix-bug-in-user-s-profile-100-coverage"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_subprocess_exception(self, mock_run, tmp_path):
        """Test handling of subprocess exceptions."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock: prune succeeds, find worktree (no match), then exception on branch check
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch (no match)
            OSError("Command not found"),  # exception on branch check
        ]

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Error creating worktree"):
            create_worktree(repo_root, 123, "Test")


class TestRemoveWorktree:
    """Test the remove_worktree function."""

    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_success(self, mock_run, mock_get_branch, tmp_path):
        """Test successful worktree removal."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        # Mock branch name
        mock_get_branch.return_value = "123-test-branch"

        # Mock successful git commands; git worktree remove deletes the path.
        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "worktree" in cmd and "remove" in cmd:
                shutil.rmtree(worktree_path)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = mock_git_command

        # Execute
        remove_worktree(worktree_path)

        # Verify git commands were called
        assert mock_run.call_count == 2

        # First call: remove worktree
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[0] == "git"
        assert first_call[1] == "-C"
        assert first_call[2] == str(repo_root)
        assert first_call[3] == "worktree"
        assert first_call[4] == "remove"
        assert first_call[5] == str(worktree_path)

        # Second call: delete branch
        second_call = mock_run.call_args_list[1][0][0]
        assert second_call[0] == "git"
        assert second_call[1] == "-C"
        assert second_call[2] == str(repo_root)
        assert second_call[3] == "branch"
        assert second_call[4] == "-D"
        assert second_call[5] == "123-test-branch"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist (non-forced)."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            remove_worktree(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_forced_is_idempotent_when_absent(self, mock_run, tmp_path):
        """R3 (#6824): a FORCED removal of an already-absent path is a no-op, not
        an error — so a partial-success cleanup retry cannot loop forever."""
        worktree_path = tmp_path / "already-gone"

        # No exception, and git is not invoked (nothing to remove).
        remove_worktree(worktree_path, force=True)
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_git_fails(self, mock_run, mock_get_branch, tmp_path):
        """Test default cleanup raises when git worktree remove fails."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        mock_get_branch.return_value = "123-test-branch"

        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="fatal: worktree is locked"),
        ]

        with pytest.raises(WorktreeError, match="Failed to remove worktree"):
            remove_worktree(worktree_path)

        assert worktree_path.exists()
        assert mock_run.call_count == 1

    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_force_falls_back_to_rmtree(
        self,
        mock_run,
        mock_get_branch,
        tmp_path,
    ):
        """Test force cleanup falls back to directory deletion."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )
        mock_get_branch.return_value = "123-test-branch"
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="fatal: worktree is locked"),
            MagicMock(returncode=0, stderr=""),
        ]

        remove_worktree(worktree_path, force=True)

        assert not worktree_path.exists()
        assert mock_run.call_count == 2
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[3:7] == [
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
        ]

    @patch("issue_orchestrator.adapters.worktree._worktree.shutil.rmtree")
    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_git_and_fallback_fail(
        self, mock_run, mock_get_branch, mock_rmtree, tmp_path
    ):
        """Test error when neither git nor fallback removal deletes the path."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )
        mock_get_branch.return_value = "123-test-branch"
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: worktree is locked"
        )

        with pytest.raises(WorktreeError, match="Failed to remove worktree path"):
            remove_worktree(worktree_path, force=True)

        mock_rmtree.assert_called_once_with(worktree_path, ignore_errors=True)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_force_removes_orphaned_directory_without_git(
        self,
        mock_run,
        tmp_path,
    ):
        """Force cleanup deletes stale directories that are no longer git worktrees."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / "leftover.txt").write_text("stale")

        remove_worktree(worktree_path, force=True)

        assert not worktree_path.exists()
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_branch_deletion_fails_silently(
        self, mock_run, mock_get_branch, tmp_path
    ):
        """Test that branch deletion failures don't raise errors."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        mock_get_branch.return_value = "123-test-branch"

        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "worktree" in cmd and "remove" in cmd:
                shutil.rmtree(worktree_path)
                return MagicMock(returncode=0, stderr="")
            if "branch" in cmd and "-D" in cmd:
                return MagicMock(returncode=1, stderr="error: branch not found")
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = mock_git_command

        # Execute - should not raise
        remove_worktree(worktree_path)

        # Verify both commands were attempted
        assert mock_run.call_count == 2

    @patch("issue_orchestrator.adapters.worktree._worktree.get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_no_branch_name(self, mock_run, mock_get_branch, tmp_path):
        """Test removal when branch name cannot be determined."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        mock_get_branch.return_value = None

        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "worktree" in cmd and "remove" in cmd:
                shutil.rmtree(worktree_path)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = mock_git_command

        # Execute
        remove_worktree(worktree_path)

        # Verify only worktree removal was called (not branch deletion)
        assert mock_run.call_count == 1


class TestFindWorktreeForBranch:
    """Test the find_worktree_for_branch function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_found(self, mock_run, tmp_path):
        """Test finding an existing worktree for a branch."""

        mock_output = """worktree /path/to/main
HEAD abc123
branch refs/heads/main

worktree /path/to/worktree-128
HEAD def456
branch refs/heads/128-m9-ios-styling

"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        result = find_worktree_for_branch(tmp_path, "128-m9-ios-styling")
        assert result == Path("/path/to/worktree-128")

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_not_found(self, mock_run, tmp_path):
        """Test when branch is not checked out in any worktree."""

        mock_output = """worktree /path/to/main
HEAD abc123
branch refs/heads/main

"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        result = find_worktree_for_branch(tmp_path, "nonexistent-branch")
        assert result is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_git_fails(self, mock_run, tmp_path):
        """Test when git command fails."""

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = find_worktree_for_branch(tmp_path, "some-branch")
        assert result is None


class TestListWorktrees:
    """Test the list_worktrees function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_success(self, mock_run):
        """Test successful listing of worktrees."""
        # Mock git output
        mock_output = """worktree /path/to/main
branch refs/heads/main

worktree /path/to/worktree-123
branch refs/heads/123-feature

worktree /path/to/worktree-456
branch refs/heads/456-bugfix
"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        worktrees = list_worktrees(Path("/tmp/repo"))

        # Verify
        assert len(worktrees) == 3
        assert Path("/path/to/main") in worktrees
        assert Path("/path/to/worktree-123") in worktrees
        assert Path("/path/to/worktree-456") in worktrees

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_empty(self, mock_run):
        """Test listing when only main worktree exists."""
        # Mock git output with only main worktree
        mock_output = """worktree /path/to/main
branch refs/heads/main
"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        worktrees = list_worktrees(Path("/tmp/repo"))

        # Verify
        assert len(worktrees) == 1
        assert Path("/path/to/main") in worktrees

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_git_fails(self, mock_run):
        """Test error when git command fails."""
        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list worktrees"):
            list_worktrees(Path("/tmp/repo"))

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_subprocess_exception(self, mock_run):
        """Test handling of subprocess exceptions."""
        # Mock subprocess exception
        mock_run.side_effect = OSError("Command not found")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Error listing worktrees"):
            list_worktrees(Path("/tmp/repo"))


class TestWorktreeExists:
    """Test the worktree_exists function."""

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_true(self, mock_list):
        """Test checking existing worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-123"), Path("/tmp/repo"))

        # Verify
        assert result is True

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_false(self, mock_list):
        """Test checking non-existent worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-999"), Path("/tmp/repo"))

        # Verify
        assert result is False

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_error_propagates(self, mock_list):
        """Test that errors from list_worktrees propagate."""
        # Mock error from list_worktrees
        mock_list.side_effect = WorktreeError("Failed to list")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list"):
            worktree_exists(Path("/path/to/worktree"), Path("/tmp/repo"))


class TestHasUncommittedChanges:
    """Test the has_uncommitted_changes function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_true(self, mock_run, tmp_path):
        """Test detecting uncommitted changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with changes
        mock_output = " M file1.txt\n?? file2.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify
        assert result is True

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_false(self, mock_run, tmp_path):
        """Test no uncommitted changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with no changes
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify
        assert result is False

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            has_uncommitted_changes(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_git_fails(self, mock_run, tmp_path):
        """Test error when git status fails."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to check worktree status"):
            has_uncommitted_changes(worktree_path)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_staged_only(self, mock_run, tmp_path):
        """Test detecting staged changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with staged changes
        mock_output = "M  file1.txt\nA  file2.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify - staged changes count as uncommitted
        assert result is True

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_untracked_only(self, mock_run, tmp_path):
        """Test detecting untracked files."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with untracked files
        mock_output = "?? new_file.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify - untracked files count as uncommitted
        assert result is True


class TestCanRemoveWithoutUserChanges:
    """Test worktree cleanup safety checks."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_clean_worktree_can_be_forced(self, mock_run, tmp_path):
        """Clean worktrees can be removed through forced cleanup fallback."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = can_remove_without_user_changes(worktree_path)

        assert result is True
        assert "--untracked-files=all" in mock_run.call_args[0][0]

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_runtime_only_untracked_paths_can_be_forced(self, mock_run, tmp_path):
        """Known orchestrator/runtime artifacts are safe to discard."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "?? .agent-done-marker\n"
                "?? .issue-orchestrator/persistent-pairs/\n"
                "?? .issue-orchestrator/review-response.json\n"
                "?? .issue-orchestrator/validation/abc123.json\n"
                "?? .githooks/pre-push.log\n"
                "?? packages/vscode/node_modules\n"
            ),
            stderr="",
        )

        result = can_remove_without_user_changes(worktree_path)

        assert result is True

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_tracked_changes_cannot_be_forced(self, mock_run, tmp_path):
        """Tracked changes are treated as user changes."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=" M src/app.py\n",
            stderr="",
        )

        result = can_remove_without_user_changes(worktree_path)

        assert result is False

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_unknown_untracked_paths_cannot_be_forced(self, mock_run, tmp_path):
        """Unknown untracked files require operator review."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="?? notes.md\n",
            stderr="",
        )

        result = can_remove_without_user_changes(worktree_path)

        assert result is False

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_path_prefix_false_positives_cannot_be_forced(self, mock_run, tmp_path):
        """Runtime roots must match exact paths or path-boundary subpaths."""
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "?? .issue-orchestrator/stateful-notes\n"
                "?? .issue-orchestrator/review-report.md.backup\n"
            ),
            stderr="",
        )

        result = can_remove_without_user_changes(worktree_path)

        assert result is False


class TestGetWorktreeBranch:
    """Test the get_worktree_branch helper function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_success(self, mock_run, tmp_path):
        """Test successfully getting branch name."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock git output
        mock_run.return_value = MagicMock(
            returncode=0, stdout="123-feature-branch\n", stderr=""
        )

        # Execute
        branch_name = get_worktree_branch(worktree_path)

        # Verify
        assert branch_name == "123-feature-branch"

        # Check git command
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert cmd[1] == "-C"
        assert cmd[2] == str(worktree_path)
        assert cmd[3] == "rev-parse"
        assert cmd[4] == "--abbrev-ref"
        assert cmd[5] == "HEAD"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_git_fails(self, mock_run, tmp_path):
        """Test when git command fails."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute
        branch_name = get_worktree_branch(worktree_path)

        # Verify - should return None on failure
        assert branch_name is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_empty_output(self, mock_run, tmp_path):
        """Test when git returns empty output."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock empty git output
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Execute
        branch_name = get_worktree_branch(worktree_path)

        # Verify
        assert branch_name is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_subprocess_exception(self, mock_run, tmp_path):
        """Test handling of subprocess exceptions."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock subprocess exception
        mock_run.side_effect = OSError("Command not found")

        # Execute
        branch_name = get_worktree_branch(worktree_path)

        # Verify - should return None on exception
        assert branch_name is None


class TestIntegrationScenarios:
    """Test integration scenarios with multiple operations."""

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_full_lifecycle(self, mock_run, mock_install_hooks, tmp_path):
        """Test complete lifecycle: create, check, remove."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"

        # Mock all git commands to succeed
        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "worktree" in cmd and "add" in cmd:
                return MagicMock(returncode=0, stderr="")
            elif "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-parse" in cmd:
                return MagicMock(returncode=0, stdout="123-test-feature\n", stderr="")
            elif "worktree" in cmd and "remove" in cmd:
                shutil.rmtree(worktree_path)
                return MagicMock(returncode=0, stderr="")
            elif "branch" in cmd and "-D" in cmd:
                return MagicMock(returncode=0, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_git_command

        # Create worktree
        worktree_path, branch_name, *_ = create_worktree(
            repo_root, 123, "Test feature", worktree_base
        )

        assert branch_name == "123-test-feature"
        assert worktree_path == worktree_base / "repo-123"

        # Check for uncommitted changes (should be clean)
        worktree_path.mkdir(parents=True, exist_ok=True)  # Create for existence check
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / worktree_path.name}"
        )
        result = has_uncommitted_changes(worktree_path)
        assert result is False

        # Remove worktree
        remove_worktree(worktree_path)

    @patch(
        "issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks",
        return_value=True,
    )
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_edge_case_titles(self, mock_run, mock_install_hooks, tmp_path):
        """Test various edge case issue titles."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        mock_run.return_value = MagicMock(returncode=0, stderr="")

        edge_cases = [
            (1, "!!!URGENT!!!", "1-urgent"),
            (2, "日本語タイトル", "2-"),  # Non-latin chars
            (3, "Add @mention & #hashtag", "3-add-mention-hashtag"),
            (4, "Fix 100% coverage", "4-fix-100-coverage"),
            (5, "a" * 100, "5-" + "a" * 50),  # Very long
            (6, "   spaces   ", "6-spaces"),
            (7, "kebab-case-title", "7-kebab-case-title"),
            (8, "CamelCaseTitle", "8-camelcasetitle"),
        ]

        for issue_num, title, expected_branch in edge_cases:
            worktree_path, branch_name, *_ = create_worktree(
                repo_root, issue_num, title, tmp_path / "worktrees"
            )
            assert branch_name == expected_branch


class TestInstallHooks:
    """Test the install_hooks function including hook chaining."""

    def test_install_hooks_no_git_file(self, tmp_path):
        """Test that install_hooks does nothing if .git file doesn't exist."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        # No .git file

        # Should not raise, and must report that nothing was installed so
        # callers enforcing guardrails can fail instead of assuming success.
        assert install_hooks(worktree_path) is False

    def test_install_hooks_invalid_git_file(self, tmp_path):
        """Test that install_hooks handles invalid .git file content."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text("invalid content")

        assert install_hooks(worktree_path) is False

    def test_install_hooks_no_project_hook(self, tmp_path, monkeypatch):
        """Test installing hooks when project has no pre-push hook."""
        # Clear any ambient operator override so the expected baked path is
        # sys.executable (not whatever the caller's shell has configured).
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_PYTHON", raising=False)

        # A real repo, because "installed" means git will run it — which only a
        # real repo can be asked.
        wt = make_git_worktree(tmp_path)

        assert install_hooks(wt.worktree_path) is True

        # Should have installed orchestrator's hook directly (no chaining)
        pre_push = wt.hooks_dir / "pre-push"
        assert pre_push.exists()
        # Should NOT have project or orchestrator suffixed hooks
        assert not (wt.hooks_dir / "pre-push.project").exists()
        assert not (wt.hooks_dir / "pre-push.orchestrator").exists()
        # ...and git must actually be pointed at the directory holding it.
        assert effective_hooks_path(wt.worktree_path) == str(wt.hooks_dir)

        # Regression: the bundled hook ships with an ``@@ORCHESTRATOR_PYTHON@@``
        # placeholder; install must substitute it with the orchestrator's
        # interpreter path so the hook does not depend on env-var
        # propagation (which broke pushes in target repos with no .venv —
        # see the issue-307 failure that motivated this).
        installed = pre_push.read_text()
        assert "@@ORCHESTRATOR_PYTHON@@" not in installed, (
            "Install must substitute @@ORCHESTRATOR_PYTHON@@ placeholder"
        )
        assert sys.executable in installed, (
            f"Install should bake sys.executable={sys.executable} into the hook"
        )

    def test_install_hooks_chains_with_project_hook(self, tmp_path, monkeypatch):
        """Test that hooks are chained when project has a pre-push hook."""
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_PYTHON", raising=False)
        wt = make_git_worktree(tmp_path)

        # Create a project pre-push hook
        project_hook = wt.main_repo / ".git" / "hooks" / "pre-push"
        project_hook.write_text("#!/bin/bash\necho 'Project hook'\nexit 0\n")
        project_hook.chmod(0o755)

        assert install_hooks(wt.worktree_path) is True

        # Verify chained hooks were created
        pre_push = wt.hooks_dir / "pre-push"
        pre_push_project = wt.hooks_dir / "pre-push.project"
        pre_push_orchestrator = wt.hooks_dir / "pre-push.orchestrator"

        assert pre_push.exists(), "Wrapper hook should exist"
        assert pre_push_project.exists(), "Project hook copy should exist"
        assert pre_push_orchestrator.exists(), "Orchestrator hook copy should exist"
        assert effective_hooks_path(wt.worktree_path) == str(wt.hooks_dir)

        # Verify wrapper content chains both hooks
        wrapper_content = pre_push.read_text()
        assert "pre-push.project" in wrapper_content, "Wrapper should call project hook"
        assert "pre-push.orchestrator" in wrapper_content, "Wrapper should call orchestrator hook"
        assert "set -e" in wrapper_content, "Wrapper should fail on error"

        # Verify project hook was copied correctly
        assert "Project hook" in pre_push_project.read_text()

        # Verify all hooks are executable
        assert pre_push.stat().st_mode & 0o111, "Wrapper should be executable"
        assert pre_push_project.stat().st_mode & 0o111, "Project hook should be executable"
        assert pre_push_orchestrator.stat().st_mode & 0o111, "Orchestrator hook should be executable"

        # Regression: see test_install_hooks_no_project_hook for context.
        installed = pre_push_orchestrator.read_text()
        assert "@@ORCHESTRATOR_PYTHON@@" not in installed
        assert sys.executable in installed

    def test_install_hooks_reports_failure_when_supplied_hook_is_missing(
        self, tmp_path, monkeypatch
    ):
        """A chained wrapper without the orchestrator hook is a guardrail loss.

        The project's own gate still runs, so the push "works" and nothing looks
        wrong — which is exactly why the return value has to say it didn't.
        """
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_PYTHON", raising=False)
        wt = make_git_worktree(tmp_path)
        project_hook = wt.main_repo / ".git" / "hooks" / "pre-push"
        project_hook.write_text("#!/bin/bash\nexit 0\n")
        project_hook.chmod(0o755)

        installed = install_hooks(wt.worktree_path, tmp_path / "nonexistent-pre-push")

        assert installed is False
        assert not (wt.hooks_dir / "pre-push.orchestrator").exists()

    def test_install_hooks_reports_failure_when_worktree_config_cannot_be_written(
        self, tmp_path
    ):
        """A copied hook in a directory git never consults is not a guardrail.

        This is the bypass the return value exists to close: the hooks directory
        stays perfectly writable, so the file lands and everything *looks*
        installed, while git keeps using its previous hooks path and the
        orchestrator's pre-push check never runs.
        """
        wt = make_git_worktree(tmp_path)
        block_worktree_config_writes(wt.gitdir)

        installed = install_hooks(wt.worktree_path)

        assert installed is False
        assert not (wt.hooks_dir / "pre-push").exists()
        assert effective_hooks_path(wt.worktree_path) != str(wt.hooks_dir)

    def test_install_hooks_reports_failure_when_repo_hooks_path_is_unreadable(
        self, tmp_path
    ):
        """A config-read error must not be read as "this repo has no hooksPath".

        Treating the two the same would resolve project hooks against the wrong
        directory and silently drop the repo's own pre-push gate from the chain.
        """
        wt = make_git_worktree(tmp_path)
        (wt.main_repo / ".git" / "config").write_text("[core\nthis is not valid\n")

        assert install_hooks(wt.worktree_path) is False
        assert not (wt.hooks_dir / "pre-push").exists()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_install_hooks_with_custom_hooks_path(self, mock_run, tmp_path):
        """Test hook installation when project uses core.hooksPath (e.g., .githooks/)."""
        # Setup fake git structure
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        main_hooks = main_git / "hooks"
        main_hooks.mkdir()

        worktrees_dir = main_git / "worktrees" / "test-worktree"
        worktrees_dir.mkdir(parents=True)
        hooks_dir = worktrees_dir / "hooks"

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(f"gitdir: {worktrees_dir}")

        # Create .githooks directory with project hook in MAIN REPO (simulating version-controlled hooks)
        # The hook should be in the main repo, not the worktree - worktrees share the main repo's hooks
        custom_hooks_dir = main_repo / ".githooks"
        custom_hooks_dir.mkdir()
        project_hook = custom_hooks_dir / "pre-push"
        project_hook.write_text("#!/bin/bash\necho 'Custom hooks path hook'\nexit 0\n")
        project_hook.chmod(0o755)

        # Mock a coherent git: the main repo reports .githooks, the worktree
        # config writes succeed, and reading core.hooksPath back afterwards
        # returns the worktree hooks dir those writes just set.
        def mock_git_command(*args, **kwargs):
            argv = args[0][3:]  # strip ["git", "-C", <repo>]
            if argv[:4] == ["config", "--local", "--get", "core.hooksPath"]:
                return MagicMock(returncode=0, stdout=".githooks\n", stderr="")
            if argv[:3] == ["config", "--get", "core.hooksPath"]:
                return MagicMock(returncode=0, stdout=f"{hooks_dir}\n", stderr="")
            if argv[:1] == ["config"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="")

        mock_run.side_effect = mock_git_command

        assert install_hooks(worktree_path) is True

        # Verify chained hooks were created in gitdir/hooks (not .githooks)
        pre_push = hooks_dir / "pre-push"
        pre_push_project = hooks_dir / "pre-push.project"
        pre_push_orchestrator = hooks_dir / "pre-push.orchestrator"

        assert pre_push.exists(), "Wrapper hook should exist in gitdir/hooks"
        assert pre_push_project.exists(), "Project hook copy should exist"
        assert pre_push_orchestrator.exists(), "Orchestrator hook copy should exist"

        # Verify git config was called to enable worktreeConfig extension
        worktree_config_calls = [call for call in mock_run.call_args_list
                                 if "config" in str(call) and "extensions.worktreeConfig" in str(call)]
        assert len(worktree_config_calls) >= 1, "Should have enabled worktreeConfig extension"

        # Verify git config was called to override hooksPath for this worktree only
        config_calls = [call for call in mock_run.call_args_list
                        if "config" in str(call) and "--worktree" in str(call)]
        assert len(config_calls) >= 1, "Should have set worktree-specific hooksPath config"

        # Verify project hook was copied from custom hooks path
        assert "Custom hooks path hook" in pre_push_project.read_text()

    def test_baked_python_honors_env_override(self, tmp_path, monkeypatch):
        """Render path respects ISSUE_ORCHESTRATOR_PYTHON when it is valid.

        The reviewer flagged that unconditionally baking ``sys.executable``
        conflicted with the rest of the resolution contract: an operator or
        test that set ``ISSUE_ORCHESTRATOR_PYTHON`` to a specific interpreter
        would be overridden by the install step, defeating the override in
        the exact failure mode the PR was designed for (env var not reaching
        the later push).
        """
        from issue_orchestrator.adapters.worktree._worktree_hooks import (
            HOOKS_DIR,
            _render_orchestrator_pre_push,
        )

        # Fake interpreter that exists and is executable.
        override = tmp_path / "fake-python"
        override.write_text("#!/bin/bash\nexec /usr/bin/true\n")
        override.chmod(0o755)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_PYTHON", str(override))

        rendered = _render_orchestrator_pre_push(HOOKS_DIR / "pre-push")

        assert str(override) in rendered, (
            "Rendered hook should bake the operator override, not sys.executable"
        )

    def test_baked_python_ignores_invalid_env_override(self, tmp_path, monkeypatch):
        """Invalid override (missing file) falls back to sys.executable."""
        from issue_orchestrator.adapters.worktree._worktree_hooks import (
            HOOKS_DIR,
            _render_orchestrator_pre_push,
        )

        monkeypatch.setenv("ISSUE_ORCHESTRATOR_PYTHON", str(tmp_path / "does-not-exist"))

        rendered = _render_orchestrator_pre_push(HOOKS_DIR / "pre-push")

        assert sys.executable in rendered

    def test_baked_python_shell_quotes_metacharacters(self, tmp_path, monkeypatch):
        """Interpreter paths with shell metacharacters round-trip safely.

        A raw string-replace inside a double-quoted shell context would let
        ``$``, backticks, or quotes expand or break the hook. The renderer
        must shell-quote the substitution so the rendered hook parses back
        to the exact installed path when executed.
        """
        import subprocess

        from issue_orchestrator.adapters.worktree._worktree_hooks import (
            HOOKS_DIR,
            _render_orchestrator_pre_push,
        )

        # Path containing every shell metacharacter we care about.
        mean_dir = tmp_path / "with $dollars `ticks` 'single' and spaces"
        mean_dir.mkdir()
        override = mean_dir / "python"
        override.write_text('#!/bin/bash\necho "ran: $0"\n')
        override.chmod(0o755)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_PYTHON", str(override))

        rendered = _render_orchestrator_pre_push(HOOKS_DIR / "pre-push")

        # Extract the resolution block and ask bash to evaluate PYTHON_BIN
        # exactly as the hook would, using an unset runtime env var so the
        # baked path branch is exercised.
        probe = tmp_path / "probe.sh"
        probe.write_text(
            "unset ISSUE_ORCHESTRATOR_PYTHON\n"
            + rendered.split("if ! ")[0]  # everything up to the hook body
            + 'printf "%s" "$PYTHON_BIN"\n'
        )
        result = subprocess.run(
            ["bash", str(probe)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == str(override), (
            f"Shell-evaluated PYTHON_BIN={result.stdout!r} should equal the "
            f"installed path {str(override)!r}; render likely failed to quote "
            f"metacharacters. Hook excerpt:\n{rendered}"
        )


class TestInstallClaudeSettings:
    """Tests for install_claude_settings function."""

    def test_install_claude_settings_creates_file(self, tmp_path):
        """Test that install_claude_settings creates .claude/settings.json."""

        install_claude_settings(tmp_path)

        settings_file = tmp_path / ".claude" / "settings.json"
        assert settings_file.exists()

    def test_install_claude_settings_has_stop_hook(self, tmp_path):
        """Test that the settings contain a Stop hook."""
        import json

        install_claude_settings(tmp_path)

        settings_file = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())

        assert "hooks" in settings
        assert "Stop" in settings["hooks"]
        assert len(settings["hooks"]["Stop"]) > 0

    def test_install_claude_settings_merges_with_existing(self, tmp_path):
        """Test that install_claude_settings merges with existing settings."""
        import json

        # Create existing settings
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_file = claude_dir / "settings.json"
        existing = {"some_key": "some_value", "hooks": {"PreToolUse": []}}
        settings_file.write_text(json.dumps(existing))

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())

        # Original content preserved
        assert settings["some_key"] == "some_value"
        assert "PreToolUse" in settings["hooks"]
        # New hook added
        assert "Stop" in settings["hooks"]


class TestCreateWorktreeReuse:
    """Test reuse flow via create_worktree (public API)."""

    def _policy(self):
        from issue_orchestrator.ports.worktree_policy import ValidationResult, SyncResult

        class AlwaysReusePolicy:
            def validate_for_reuse(self, worktree_path, expected_branch, repo_root):
                return ValidationResult(can_reuse=True, reason="ok")

            def sync_remote_refs(self, worktree_path, branch_name):
                return SyncResult(success=True)

            def delete_worktree(self, worktree_path, repo_root):
                return True

        return AlwaysReusePolicy()

    def test_reuse_rebases_onto_origin_main(self, tmp_path):
        """Verify reuse path rebases onto origin/main."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings"),

            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["reset", "--hard"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            worktree_path_out, branch_name, reuse_status, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert reuse_status == "reused"
            assert worktree_path_out == worktree_path
            assert branch_name == "123-test"
            assert uncommitted == 0
            assert commits == 0
            rebase_call = next(
                call_args[0][0]
                for call_args in mock_run.call_args_list
                if call_args[0][0][3:5] == ["rebase", "origin/main"]
            )
            assert "rebase" in rebase_call
            assert "origin/main" in rebase_call

    def test_reuse_rebase_conflict_discards_commits(self, tmp_path):
        """Verify reuse path resets to origin/main on rebase failure."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings"),

            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=1, stdout="", stderr="CONFLICT")
                if argv[:2] == ["rebase", "--abort"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-list", "--count", "origin/main..HEAD"]:
                    return MagicMock(returncode=0, stdout="2\n", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "origin/main":
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            _, _, _, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert uncommitted == 0
            assert commits == 2

    def test_reuse_counts_uncommitted_changes(self, tmp_path):
        """Verify reuse path counts discarded uncommitted changes."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings"),

            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="M file1.txt\nM file2.txt\n", stderr="")
                if argv[:2] == ["reset", "--hard"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            _, _, _, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert uncommitted == 2
            assert commits == 0

    def test_reuse_preserve_branch_skips_rebase_and_reset(self, tmp_path):
        """preserve_branch=True must NOT rebase or hard-reset a diverged branch.

        Models a tech_lead investigation reusing a stranded worktree whose branch
        is ahead of origin/main: the branch (and its unpushed commits) must
        survive untouched — no ``rebase`` and no ``reset --hard origin/main``.
        """
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                # If the reset path ran, rebase would fail here and force a hard
                # reset that discards the stranded commits. It must never run.
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=1, stdout="", stderr="CONFLICT")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            (
                worktree_path_out,
                branch_name,
                reuse_status,
                _,
                rebase_failed,
                uncommitted,
                commits,
            ) = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(
                    reuse_push_preflight=False, preserve_branch=True
                ),
                policy=self._policy(),
            )

            assert reuse_status == "reused"
            assert worktree_path_out == worktree_path
            assert branch_name == "123-test"
            assert rebase_failed is False
            assert uncommitted == 0
            assert commits == 0
            issued = [call_args[0][0][3:] for call_args in mock_run.call_args_list]
            assert not any(a[:1] == ["rebase"] for a in issued), issued
            assert not any(
                a[:3] == ["reset", "--hard", "origin/main"] for a in issued
            ), issued
            # The rebase/reset prep fetches origin/main; skipping it means no
            # such fetch is issued during reuse.
            assert not any(
                a[:3] == ["fetch", "origin", "main"] for a in issued
            ), issued

    def test_reuse_without_preserve_branch_still_resets(self, tmp_path):
        """Default (preserve_branch=False) still rebases and discards on conflict.

        Regression guard: the preserve-branch skip must be strictly opt-in — the
        ordinary reuse path keeps freshening the branch onto the base branch.
        """
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.install_claude_settings"),
            patch("issue_orchestrator.adapters.worktree._worktree_runtime_setup.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=1, stdout="", stderr="CONFLICT")
                if argv[:2] == ["rebase", "--abort"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-list", "--count", "origin/main..HEAD"]:
                    return MagicMock(returncode=0, stdout="2\n", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "origin/main":
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            _, _, _, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(
                    reuse_push_preflight=False, preserve_branch=False
                ),
                policy=self._policy(),
            )

            assert commits == 2
            issued = [call_args[0][0][3:] for call_args in mock_run.call_args_list]
            assert any(a[:2] == ["rebase", "origin/main"] for a in issued), issued
            assert any(
                a[:3] == ["reset", "--hard", "origin/main"] for a in issued
            ), issued


# =============================================================================
# Worktree Preparation Tests (control/worktree.py)
# =============================================================================

from issue_orchestrator.control.worktree import Worktree, WorktreePreparationError
from issue_orchestrator.ports.session_output import SessionOutput
import json


@pytest.fixture
def worktree_dir(tmp_path: Path) -> Path:
    """Create a temporary worktree directory with orchestrator dir."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    orchestrator_dir = worktree / ".issue-orchestrator"
    orchestrator_dir.mkdir()
    return worktree


@pytest.fixture
def mock_session_output() -> MagicMock:
    """Create a mock SessionOutput for testing."""
    return MagicMock(spec=SessionOutput)


@pytest.fixture
def worktree(worktree_dir: Path, mock_session_output: MagicMock) -> Worktree:
    """Create a Worktree instance for testing."""
    return Worktree(worktree_dir, issue_number=123, session_output=mock_session_output)


class TestWorktreePrepareForSession:
    """Tests for Worktree.prepare_for_session()."""

    def test_removes_completion_files(self, worktree: Worktree, worktree_dir: Path):
        """Removes completion.json files."""
        completion = worktree_dir / ".issue-orchestrator" / "completion.json"
        completion.write_text(json.dumps({
            "session_id": "old-session",
            "timestamp": "2026-01-01T00:00:00",
            "outcome": "completed",
            "summary": "Old",
        }))

        assert completion.exists()
        worktree.prepare_for_session("new-session")
        assert not completion.exists()

    def test_keeps_session_identity_files(self, worktree: Worktree, worktree_dir: Path):
        """Keeps per-session identity files for recent runs."""
        identity = worktree_dir / ".issue-orchestrator" / "sessions" / "new-session" / "identity.json"
        identity.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text(json.dumps({"session_name": "old"}))

        assert identity.exists()
        worktree.prepare_for_session("new-session")
        assert identity.exists()

    def test_removes_multiple_completion_files(self, worktree: Worktree, worktree_dir: Path):
        """Removes all completion*.json files."""
        orch_dir = worktree_dir / ".issue-orchestrator"

        files = [
            "completion.json",
            "completion-agent_backend.json",
            "completion-agent_e2e-test.json",
        ]
        for name in files:
            (orch_dir / name).write_text(json.dumps({
                "session_id": "old",
                "timestamp": "2026-01-01T00:00:00",
                "outcome": "completed",
                "summary": "Test",
            }))

        worktree.prepare_for_session("new-session")

        for name in files:
            assert not (orch_dir / name).exists()

    def test_keeps_pane_log(self, worktree: Worktree, worktree_dir: Path):
        """Keeps pane.log for recent runs."""
        pane_log = worktree_dir / ".issue-orchestrator" / "sessions" / "new-session" / "pane.log"
        pane_log.parent.mkdir(parents=True, exist_ok=True)
        pane_log.write_text("Old session output from Claude Code\n")

        assert pane_log.exists()
        worktree.prepare_for_session("new-session")
        assert pane_log.exists()

    def test_no_error_when_orchestrator_dir_missing(self, tmp_path: Path, mock_session_output: MagicMock):
        """No error when .issue-orchestrator dir doesn't exist."""
        worktree = Worktree(tmp_path, issue_number=123, session_output=mock_session_output)
        worktree.prepare_for_session("new-session")  # Should not raise

    def test_prunes_old_session_runs(self, tmp_path: Path, mock_session_output: MagicMock):
        """Calls session_output.prune_runs with correct arguments."""
        # Configure mock to return empty list (no pruning needed)
        mock_session_output.prune_runs.return_value = []

        worktree = Worktree(tmp_path, issue_number=123, retain_runs=2, session_output=mock_session_output)
        worktree.prepare_for_session("issue-1")

        # Verify prune_runs was called with correct path and retention
        mock_session_output.prune_runs.assert_called_once_with(tmp_path, 2)

    def test_raises_worktree_preparation_error_on_delete_failure(
        self, worktree: Worktree, worktree_dir: Path, monkeypatch
    ):
        """Raises WorktreePreparationError if file cannot be deleted."""
        orch_dir = worktree_dir / ".issue-orchestrator"
        completion = orch_dir / "completion.json"
        completion.write_text("{}")

        def _raise_unlink(_self):
            raise OSError("Permission denied")

        monkeypatch.setattr(Path, "unlink", _raise_unlink)

        with pytest.raises(WorktreePreparationError) as exc_info:
            worktree.prepare_for_session("new-session")
        # Verify exception properties
        assert exc_info.value.path == worktree_dir
        assert exc_info.value.issue_number == 123
        assert "Cannot delete stale files" in str(exc_info.value)
        # Verify OSError is chained
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, OSError)


class TestWorktreePreparationError:
    """Tests for the WorktreePreparationError exception."""

    def test_exception_properties(self, tmp_path: Path):
        """Test that exception stores path and issue_number."""
        error = WorktreePreparationError(
            tmp_path, 456, "Test error message"
        )
        assert error.path == tmp_path
        assert error.issue_number == 456
        assert str(error) == "Test error message"

    def test_exception_is_raised_with_oserror_cause(self, tmp_path: Path):
        """Test exception can be chained with OSError."""
        original = OSError("Permission denied")
        error = WorktreePreparationError(
            tmp_path, 789, "Cannot delete file"
        )
        error.__cause__ = original

        assert error.__cause__ is original
