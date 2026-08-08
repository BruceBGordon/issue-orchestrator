"""Tests to verify git config isolation - prevents regression of config leaking bugs.

These tests verify that:
1. Worktree config changes don't leak to other worktrees or the main repo
2. Test fixture git commands don't affect the main repo even with GIT_DIR set
3. Worktree's core.worktree config points to the correct path (not stale temp dirs)
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import install_hooks

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_git_env() -> dict[str, str]:
    """Return environment with git variables cleared to prevent leaking to test repos."""
    env = os.environ.copy()
    for var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"]:
        env.pop(var, None)
    return env


class TestWorktreeConfigIsolation:
    """Verify that worktree config changes don't leak to other repos."""

    def test_install_hooks_does_not_affect_main_repo_config(self, tmp_path: Path):
        """Installing hooks in a worktree should not modify the main repo's config.

        This is a regression test for a bug where symlink resolution issues
        (/tmp vs /private/tmp on macOS) caused git to write config to the wrong repo.
        """
        # Create a main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "main@test.com"],
            cwd=main_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Main User"],
            cwd=main_repo, capture_output=True, check=True
        )

        # Record the main repo's config before
        config_before = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=main_repo, capture_output=True, text=True, check=True
        ).stdout

        # Create a worktree
        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
            cwd=main_repo, capture_output=True, check=True
        )

        # Install hooks in the worktree (this was leaking config before the fix)
        install_hooks(worktree_path)

        # Verify main repo's config is unchanged (except extensions.worktreeConfig
        # which is required in main repo for per-worktree configs to work)
        config_after = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=main_repo, capture_output=True, text=True, check=True
        ).stdout

        # Filter out extensions.worktreeconfig since it's expected to be added
        def filter_config(config: str) -> str:
            return "\n".join(
                line for line in config.strip().split("\n")
                if not line.startswith("extensions.worktreeconfig")
            )

        assert filter_config(config_before) == filter_config(config_after), (
            f"Main repo config changed after installing hooks in worktree!\n"
            f"Before:\n{config_before}\nAfter:\n{config_after}"
        )

        # Verify dangerous configs did NOT leak (these were the actual bugs)
        dangerous_configs = ["core.worktree", "core.bare", "core.hooksPath", "remote.origin"]
        for config_key in dangerous_configs:
            result = subprocess.run(
                ["git", "config", "--local", "--get-regexp", f"^{config_key}"],
                cwd=main_repo, capture_output=True, text=True, check=False
            )
            # These should not exist in the main repo after worktree hook install
            if config_key == "core.bare":
                # core.bare=false is fine, core.bare=true would be bad
                if result.returncode == 0 and "true" in result.stdout:
                    pytest.fail(f"Dangerous config leaked to main repo: {config_key}=true")
            elif result.returncode == 0 and result.stdout.strip():
                pytest.fail(f"Dangerous config leaked to main repo: {result.stdout.strip()}")

    def test_worktree_config_points_to_correct_path(self, tmp_path: Path):
        """Worktree's core.worktree config should point to the actual worktree path.

        This is a regression test for a bug where symlink resolution issues
        (/tmp vs /private/tmp on macOS) caused core.worktree to point to the
        wrong directory, making git see unrelated files as changes.
        """
        # Create a main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=main_repo, capture_output=True, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
        )

        # Create a worktree
        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "test-branch"],
            cwd=main_repo, capture_output=True, check=True
        )

        # Install hooks (this sets core.worktree in the worktree config)
        install_hooks(worktree_path)

        # Get the worktree's core.worktree config
        result = subprocess.run(
            ["git", "config", "--worktree", "--get", "core.worktree"],
            cwd=worktree_path, capture_output=True, text=True, check=False
        )

        if result.returncode == 0 and result.stdout.strip():
            configured_path = Path(result.stdout.strip())
            # Resolve both paths to handle symlinks (/tmp vs /private/tmp)
            actual_resolved = worktree_path.resolve()
            configured_resolved = configured_path.resolve()

            assert configured_resolved == actual_resolved, (
                f"Worktree core.worktree points to wrong path!\n"
                f"Expected: {actual_resolved}\n"
                f"Got: {configured_resolved}\n"
                f"Raw config value: {result.stdout.strip()}"
            )

    def test_worktree_config_stays_in_worktree(self, tmp_path: Path):
        """Config set in a worktree should not appear in another worktree."""
        # Create main repo
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        subprocess.run(["git", "init"], cwd=main_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=main_repo, capture_output=True, check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
        )

        # Create two worktrees
        worktree1 = tmp_path / "wt1"
        worktree2 = tmp_path / "wt2"
        subprocess.run(
            ["git", "worktree", "add", str(worktree1), "-b", "branch1"],
            cwd=main_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "worktree", "add", str(worktree2), "-b", "branch2"],
            cwd=main_repo, capture_output=True, check=True
        )

        # Install hooks in worktree1 only
        install_hooks(worktree1)

        # Verify worktree2 does not have the hooks config
        wt2_hooks_path = subprocess.run(
            ["git", "config", "--worktree", "--get", "core.hooksPath"],
            cwd=worktree2, capture_output=True, text=True, check=False
        )
        # Should either fail (no config) or return empty
        assert wt2_hooks_path.returncode != 0 or not wt2_hooks_path.stdout.strip(), (
            f"Worktree2 unexpectedly has core.hooksPath: {wt2_hooks_path.stdout}"
        )

    def test_install_hooks_preserves_prepare_commit_msg_for_worktree_commits(
        self,
        tmp_path: Path,
    ):
        """Agent worktrees keep the tracked DCO hook after hooksPath override."""
        env = _clean_git_env()
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Agent User"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "config", "user.email", "agent@example.com"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        githooks = main_repo / ".githooks"
        githooks.mkdir()
        shutil.copy2(REPO_ROOT / ".githooks" / "prepare-commit-msg", githooks)
        shutil.copy2(REPO_ROOT / ".githooks" / "applypatch-msg", githooks)
        (githooks / "prepare-commit-msg").chmod(0o755)
        (githooks / "applypatch-msg").chmod(0o755)
        subprocess.run(
            ["git", "config", "--local", "core.hooksPath", ".githooks"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        (main_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "seed.txt"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )

        worktree_path = tmp_path / "worktree"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "agent-branch"],
            cwd=main_repo,
            capture_output=True,
            check=True,
            env=env,
        )
        install_hooks(worktree_path)
        hooks_path_result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        installed_hooks = Path(hooks_path_result.stdout.strip())
        if not installed_hooks.is_absolute():
            installed_hooks = worktree_path / installed_hooks
        assert (installed_hooks / "prepare-commit-msg").exists()

        (worktree_path / "agent.txt").write_text("agent\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "agent.txt"],
            cwd=worktree_path,
            capture_output=True,
            check=True,
            env=env,
        )
        result = subprocess.run(
            ["git", "commit", "-m", "agent change"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        message = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout
        assert "Signed-off-by: Agent User <agent@example.com>" in message


class TestGitEnvIsolation:
    """Verify that git commands respect isolation even with GIT_DIR set."""

    def test_git_commands_with_cwd_ignore_git_dir_env(self, tmp_path: Path):
        """Git commands using clean env should not be affected by GIT_DIR.

        This is a regression test for a bug where integration tests using
        cwd=work wrote config to the main repo because GIT_DIR was set.
        """
        # Create two separate repos
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        # Initialize repo_a with specific config
        subprocess.run(["git", "init"], cwd=repo_a, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "repo_a@test.com"],
            cwd=repo_a, capture_output=True, check=True
        )

        # Initialize repo_b
        subprocess.run(["git", "init"], cwd=repo_b, capture_output=True, check=True)

        # Record repo_a's config
        config_before = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=repo_a, capture_output=True, text=True, check=True
        ).stdout

        # Now try to set config in repo_b with GIT_DIR pointing to repo_a
        # This simulates the bug where test fixture config leaked to main repo
        bad_env = os.environ.copy()
        bad_env["GIT_DIR"] = str(repo_a / ".git")

        # Without the fix, this would modify repo_a's config!
        # With fix: we clean GIT_DIR from env before running git commands
        clean_env = os.environ.copy()
        for var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]:
            clean_env.pop(var, None)

        subprocess.run(
            ["git", "config", "user.email", "repo_b@test.com"],
            cwd=repo_b, capture_output=True, check=True, env=clean_env
        )

        # Verify repo_a's config is unchanged
        config_after = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=repo_a, capture_output=True, text=True, check=True
        ).stdout

        assert config_before == config_after, (
            f"Repo A config changed when setting config in Repo B!\n"
            f"Before:\n{config_before}\nAfter:\n{config_after}"
        )

        # Verify repo_b got the config
        repo_b_email = subprocess.run(
            ["git", "config", "--get", "user.email"],
            cwd=repo_b, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert repo_b_email == "repo_b@test.com"

    def test_git_dir_env_causes_leak_without_clean_env(self, tmp_path: Path):
        """Demonstrate that GIT_DIR env DOES cause config to leak (the bug).

        This test proves the bug exists when not using clean env,
        validating that our fix is necessary.
        """
        # Create two repos
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        clean_env = _clean_git_env()
        subprocess.run(["git", "init"], cwd=repo_a, capture_output=True, check=True, env=clean_env)
        subprocess.run(["git", "init"], cwd=repo_b, capture_output=True, check=True, env=clean_env)

        # Set GIT_DIR to repo_a, but run command with cwd=repo_b
        bad_env = dict(clean_env)
        bad_env["GIT_DIR"] = str(repo_a / ".git")

        # This WILL write to repo_a despite cwd=repo_b (demonstrating the bug)
        subprocess.run(
            ["git", "config", "test.leaked", "true"],
            cwd=repo_b, capture_output=True, check=True, env=bad_env
        )

        # Verify the config leaked to repo_a (not repo_b)
        leaked = subprocess.run(
            ["git", "config", "--get", "test.leaked"],
            cwd=repo_a, capture_output=True, text=True, check=False, env=clean_env
        )
        assert leaked.returncode == 0 and leaked.stdout.strip() == "true", (
            "Expected GIT_DIR to cause config leak - this test validates the bug exists"
        )

        # repo_b should NOT have the config
        not_leaked = subprocess.run(
            ["git", "config", "--get", "test.leaked"],
            cwd=repo_b, capture_output=True, text=True, check=False, env=clean_env
        )
        assert not_leaked.returncode != 0, (
            "Config should NOT be in repo_b when GIT_DIR points elsewhere"
        )


class TestConftestGitIsolation:
    """Verify that the conftest isolate_git_env fixture prevents pollution."""

    def test_git_env_vars_stripped_by_conftest(self):
        """Verify that GIT_DIR etc. are NOT in the environment during tests.

        The conftest.py autouse fixture should strip these vars to prevent
        test git commands from polluting the main repo.
        """
        git_env_vars = [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
        ]
        for var in git_env_vars:
            assert var not in os.environ, (
                f"{var} should be stripped by conftest isolate_git_env fixture"
            )

    def test_temp_repo_config_does_not_leak_to_main(self, tmp_path: Path):
        """Verify that git config in temp repo doesn't affect main repo.

        This is a regression test for the bug where tests running
        `git config user.email test@test.com` polluted the main repo.
        """
        # Get the main repo path (parent of tests directory)
        main_repo = Path(__file__).parent.parent.parent

        # Record main repo config before
        main_config_before = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=main_repo, capture_output=True, text=True, check=False
        ).stdout

        # Create a temp repo and set config (simulating what tests do)
        temp_repo = tmp_path / "temp_repo"
        temp_repo.mkdir()
        subprocess.run(["git", "init"], cwd=temp_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "pollution-test@test.com"],
            cwd=temp_repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Pollution Test User"],
            cwd=temp_repo, capture_output=True, check=True
        )

        # Verify main repo config is unchanged
        main_config_after = subprocess.run(
            ["git", "config", "--local", "--list"],
            cwd=main_repo, capture_output=True, text=True, check=False
        ).stdout

        assert main_config_before == main_config_after, (
            f"Main repo config changed after setting config in temp repo!\\n"
            f"This means GIT_DIR was set and leaked config.\\n"
            f"Before:\\n{main_config_before}\\nAfter:\\n{main_config_after}"
        )

        # Double-check: main repo should NOT have test email
        main_email = subprocess.run(
            ["git", "config", "--local", "--get", "user.email"],
            cwd=main_repo, capture_output=True, text=True, check=False
        ).stdout.strip()

        assert main_email != "pollution-test@test.com", (
            "Test email leaked to main repo - conftest fixture may not be working"
        )
