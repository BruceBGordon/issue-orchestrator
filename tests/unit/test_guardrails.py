"""E2E tests for security guardrails.

These tests verify that the gh and git wrappers correctly block
unauthorized commands while allowing read-only operations.
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests.git_push_authorization import (
    GH_AUTH_ENV_VAR,
    GH_AUTH_TOKEN,
    authorized_local_fixture_git_env,
    unauthorized_git_env,
)

# Path to the wrapper scripts
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "scripts"


@pytest.mark.parametrize("wrapper", ["git", "gh"])
def test_wrapper_scripts_accept_the_shared_authorization_token(wrapper: str) -> None:
    """The shared test helper must authorize against what the wrappers check.

    ``tests/git_push_authorization`` is the single owner of the privileged
    fixture bypass. If its constants drifted from the wrapper sources, every
    fixture would silently lose its authorization and the failure would show
    up as an unrelated "BLOCKED" push somewhere far away.
    """
    source = (SCRIPTS_DIR / wrapper).read_text(encoding="utf-8")

    assert GH_AUTH_ENV_VAR in source
    assert GH_AUTH_TOKEN in source


class TestGhWrapper:
    """Test the gh CLI wrapper guardrails."""

    @pytest.fixture
    def wrapper_env(self, tmp_path):
        """Environment with wrapper directory prepended to PATH."""
        fake_gh = tmp_path / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 0\n")
        fake_gh.chmod(0o755)
        env = unauthorized_git_env()  # Ensure not authorized
        env["PATH"] = f"{SCRIPTS_DIR}:{env.get('PATH', '')}"
        env["ORCHESTRATOR_REAL_GH"] = str(fake_gh)
        return env

    @pytest.fixture
    def authorized_env(self, wrapper_env):
        """Environment with authorization token set."""
        return authorized_local_fixture_git_env(wrapper_env)

    def test_blocks_pr_create(self, wrapper_env):
        """gh pr create should be blocked without authorization."""
        result = subprocess.run(
            ["gh", "pr", "create", "--title", "test", "--body", "test"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_blocks_pr_review(self, wrapper_env):
        """gh pr review should be blocked (not in whitelist)."""
        result = subprocess.run(
            ["gh", "pr", "review", "1", "--approve"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_blocks_pr_merge(self, wrapper_env):
        """gh pr merge should be blocked (not in whitelist)."""
        result = subprocess.run(
            ["gh", "pr", "merge", "1"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_blocks_pr_comment(self, wrapper_env):
        """gh pr comment should be blocked (not in whitelist)."""
        result = subprocess.run(
            ["gh", "pr", "comment", "1", "--body", "test"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_blocks_issue_create(self, wrapper_env):
        """gh issue create should be blocked (not in whitelist)."""
        result = subprocess.run(
            ["gh", "issue", "create", "--title", "test", "--body", "test"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_blocks_api(self, wrapper_env):
        """gh api should be blocked (not in whitelist)."""
        result = subprocess.run(
            ["gh", "api", "/user"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_allows_pr_view(self, wrapper_env):
        """gh pr view should be allowed (read-only)."""
        # This will fail because we're not in a repo, but it shouldn't be BLOCKED
        result = subprocess.run(
            ["gh", "pr", "view", "1"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        # Should NOT be blocked - might fail for other reasons (not in repo, etc)
        assert "BLOCKED" not in result.stderr

    def test_allows_pr_list(self, wrapper_env):
        """gh pr list should be allowed (read-only)."""
        result = subprocess.run(
            ["gh", "pr", "list"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr

    def test_allows_issue_view(self, wrapper_env):
        """gh issue view should be allowed (read-only)."""
        result = subprocess.run(
            ["gh", "issue", "view", "1"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr

    def test_allows_issue_list(self, wrapper_env):
        """gh issue list should be allowed (read-only)."""
        result = subprocess.run(
            ["gh", "issue", "list"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr

    def test_allows_help(self, wrapper_env):
        """gh help should be allowed."""
        result = subprocess.run(
            ["gh", "help"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr
        assert result.returncode == 0

    def test_allows_version(self, wrapper_env):
        """gh --version should be allowed."""
        result = subprocess.run(
            ["gh", "--version"],
            env=wrapper_env,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr
        assert result.returncode == 0

    def test_authorization_bypasses_block(self, authorized_env):
        """With ORCHESTRATOR_GH_AUTH, blocked commands should pass through."""
        # This will fail because we're not in a repo, but shouldn't be BLOCKED
        result = subprocess.run(
            ["gh", "pr", "create", "--title", "test", "--body", "test"],
            env=authorized_env,
            capture_output=True,
            text=True,
        )
        # Should NOT be blocked (may fail for other reasons)
        assert "BLOCKED" not in result.stderr


class TestGitWrapper:
    """Test the git wrapper guardrails."""

    @pytest.fixture
    def wrapper_env(self):
        """Environment with wrapper directory prepended to PATH."""
        env = unauthorized_git_env()
        env["PATH"] = f"{SCRIPTS_DIR}:{env.get('PATH', '')}"
        return env

    @pytest.fixture
    def authorized_env(self, wrapper_env):
        """Environment with authorization token set."""
        return authorized_local_fixture_git_env(wrapper_env)

    def test_blocks_push(self, wrapper_env, tmp_path):
        """git push should be blocked without authorization."""
        # Create a git repo to test in
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "push"],
            env=wrapper_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_allows_commit(self, wrapper_env, tmp_path):
        """git commit should be allowed."""
        # Create a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)

        # Create a file and stage it
        (tmp_path / "test.txt").write_text("test")
        subprocess.run(["git", "add", "test.txt"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "commit", "-m", "test commit"],
            env=wrapper_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        # Should NOT be blocked
        assert "BLOCKED" not in result.stderr

    def test_allows_status(self, wrapper_env, tmp_path):
        """git status should be allowed."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "status"],
            env=wrapper_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr
        assert result.returncode == 0

    def test_allows_branch(self, wrapper_env, tmp_path):
        """git branch should be allowed."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "branch"],
            env=wrapper_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr

    def test_allows_log(self, wrapper_env, tmp_path):
        """git log should be allowed."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "log"],
            env=wrapper_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert "BLOCKED" not in result.stderr

    def test_authorization_allows_push(self, authorized_env, tmp_path):
        """With ORCHESTRATOR_GH_AUTH, push should be allowed through."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

        result = subprocess.run(
            ["git", "push"],
            env=authorized_env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        # Should NOT be blocked (may fail for no remote, but not BLOCKED)
        assert "BLOCKED" not in result.stderr


class TestCredentialScrubbing:
    """Test that credential scrubbing works correctly."""

    def test_isolation_prefix_scrubs_tokens(self):
        """build_isolation_prefix should unset GitHub tokens."""
        from issue_orchestrator.control.isolation import build_isolation_prefix

        isolation = build_isolation_prefix(
            worktree=Path("/tmp"),
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,
        )

        # Should contain unset commands for tokens
        assert "unset GH_TOKEN" in isolation
        assert "unset GITHUB_TOKEN" in isolation
        assert "unset ISSUE_ORCH_GITHUB_TOKEN" in isolation

    def test_forbidden_env_vars_includes_tokens(self):
        """FORBIDDEN_ENV_VARS should include all GitHub tokens."""
        from issue_orchestrator.control.isolation import FORBIDDEN_ENV_VARS

        assert "GH_TOKEN" in FORBIDDEN_ENV_VARS
        assert "GITHUB_TOKEN" in FORBIDDEN_ENV_VARS
        assert "ISSUE_ORCH_GITHUB_TOKEN" in FORBIDDEN_ENV_VARS
        assert "GH_ENTERPRISE_TOKEN" in FORBIDDEN_ENV_VARS

    def test_scrubbing_removes_tokens_from_env(self):
        """Tokens should be removed after running isolation commands."""
        from issue_orchestrator.control.isolation import build_isolation_prefix

        isolation = build_isolation_prefix(
            worktree=Path("/tmp"),
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,
        )

        # Set a fake token and verify it gets scrubbed
        env = os.environ.copy()
        env["GH_TOKEN"] = "fake-token-for-test"

        # Run a script that applies isolation and checks for token
        # Note: isolation already ends with && so we don't add another
        check_script = f'{isolation} python3 -c "import os; exit(0 if os.environ.get(\'GH_TOKEN\') is None else 1)"'
        result = subprocess.run(
            ["bash", "-c", check_script],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"GH_TOKEN should be scrubbed. stderr: {result.stderr}"
