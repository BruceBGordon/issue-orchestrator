"""Integration tests for AI gate verification.

These tests verify that the hook enforcement system works end-to-end by
actually spawning Claude and testing that --no-verify commands are blocked
(or not blocked when hooks are missing).

Uses static fixtures in tests/fixtures/ with local bare git repos as remotes.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.git_push_authorization import authorized_local_fixture_git_env

from .conftest import xdist_timeout

# Mark all tests in this module as integration + live.
# Most classes run hook subprocesses → xdist_group("hooks") to serialise with
# other hook tests.  Only TestAiGate actually spawns Claude and overrides to
# xdist_group("claude") at the class level.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.xdist_group("hooks"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fixtures_path() -> Path:
    """Path to the test fixtures directory."""
    return Path(__file__).parent.parent / "fixtures"


def _clean_git_env() -> dict[str, str]:
    """Return environment with git/orchestrator variables cleaned for test repos.

    Clears git internals (GIT_DIR etc.) that leak from parent processes,
    orchestrator session vars that can pollute hook scripts, and sets
    ORCHESTRATOR_HOOK_PYTHONPATH so hook scripts can import the package.

    This is xdist-safe because it builds a fresh env per subprocess call
    rather than relying on module-scope os.environ mutations.
    """
    # The fixtures push to local bare repos, so they need the shared test-only
    # push authorization (see tests/git_push_authorization.py).
    env = authorized_local_fixture_git_env(
        strip=["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"]
    )
    # Remove orchestrator session vars that leak into test subprocesses
    for key in list(env):
        if key.startswith("ISSUE_ORCHESTRATOR_"):
            del env[key]
    # Ensure hook scripts can import issue_orchestrator
    env["ORCHESTRATOR_HOOK_PYTHONPATH"] = str(_REPO_ROOT / "src")
    return env


@pytest.fixture
def test_repo_with_hooks(tmp_path: Path, fixtures_path: Path) -> Path:
    """Create a test git repo with hooks installed.

    Sets up:
    - A bare repo acting as "origin" remote
    - A working repo cloned from it with hooks installed
    - A commit ready to push

    Returns the path to the working repo.
    """
    # Clean environment to prevent git config leaking to main repo
    clean_env = _clean_git_env()

    # Create bare "remote" repo
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=remote,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create working repo
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Configure git user
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=work,
        env=clean_env,
        capture_output=True,
        check=True,
    )

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

    ClaudeCodeAdapter().install_hooks(work)

    # Create initial commit and push to establish branch
    readme = work / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create a new commit ready to push (for testing)
    test_file = work / "test.txt"
    test_file.write_text("test content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Test commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    return work


@pytest.fixture
def test_repo_without_hooks(tmp_path: Path, fixtures_path: Path) -> Path:
    """Create a test git repo WITHOUT hooks installed.

    Same setup as test_repo_with_hooks but no .claude directory.
    Used to verify that we correctly detect when hooks are missing.

    Returns the path to the working repo.
    """
    # Clean environment to prevent git config leaking to main repo
    clean_env = _clean_git_env()

    # Create bare "remote" repo
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=remote,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create working repo
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Configure git user
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Copy content from fixture (but NO .claude directory)
    src_readme = fixtures_path / "hooks-missing" / "README.md"
    dst_readme = work / "README.md"
    shutil.copy(src_readme, dst_readme)

    # Create initial commit and push
    subprocess.run(["git", "add", "."], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create a new commit ready to push
    test_file = work / "test.txt"
    test_file.write_text("test content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Test commit"],
        cwd=work,
        capture_output=True,
        env=clean_env,
        check=True,
    )

    return work


class TestHookBlocking:
    """Tests that verify hooks correctly block --no-verify commands."""

    def test_hook_script_blocks_no_verify(self, test_repo_with_hooks: Path):
        """Test that the hook script blocks --no-verify when called directly."""
        import json

        hook_script = test_repo_with_hooks / ".claude" / "hooks" / "block-no-verify.sh"

        # Simulate Claude Code's PreToolUse input
        test_input = json.dumps({
            "tool_input": {
                "command": "git push --no-verify"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2, f"Expected exit code 2 (blocked), got {result.returncode}"
        assert "BLOCKED" in result.stderr

    def test_hook_script_allows_normal_push(self, test_repo_with_hooks: Path):
        """Test that the hook script allows normal git push."""
        import json

        hook_script = test_repo_with_hooks / ".claude" / "hooks" / "block-no-verify.sh"

        test_input = json.dumps({
            "tool_input": {
                "command": "git push origin main"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit code 0 (allowed), got {result.returncode}"


@pytest.mark.provider_claude
@pytest.mark.xdist_group("claude")
class TestAiGate:
    """Tests that spawn Claude to verify end-to-end AI gate enforcement.

    These tests are slower and require Claude CLI to be installed.
    They verify the ENTIRE chain works, not just the hook script.
    """

    @pytest.fixture
    def skip_if_no_claude(self):
        """Skip test if Claude CLI is not available."""
        result = subprocess.run(
            ["which", "claude"],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("Claude CLI not installed")

    def test_ai_gate_blocks_no_verify(
        self,
        test_repo_with_hooks: Path,
        skip_if_no_claude,
    ):
        """Test that Claude is actually blocked from running --no-verify."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        success, message = adapter.test_ai_gate(test_repo_with_hooks, timeout=xdist_timeout(120))

        assert success, f"AI gate test should pass (hooks installed): {message}"
        assert "blocked" in message.lower()

    def test_ai_gate_detects_missing_hooks(
        self,
        test_repo_without_hooks: Path,
        skip_if_no_claude,
    ):
        """Test that AI gate test correctly detects when hooks are missing.

        This is the critical failure-detection test - we need to verify that
        when hooks are NOT installed, the verification reports FAILURE.
        """
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        success, message = adapter.test_ai_gate(test_repo_without_hooks, timeout=xdist_timeout(120))

        # This should FAIL because hooks are not installed
        # Claude should be able to run --no-verify without being blocked
        assert not success, (
            f"AI gate test should FAIL (no hooks): {message}\n"
            "If this passes, our failure detection is broken!"
        )


class TestVerificationResult:
    """Tests for the static (non-spawn) verification."""

    def test_verify_hooks_passes_with_hooks(self, test_repo_with_hooks: Path):
        """Test that verify_hooks passes when hooks are properly installed."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        result = adapter.verify_hooks(test_repo_with_hooks)

        assert result.success, f"Verification should pass: {result.checks_failed}"
        assert len(result.checks_passed) > 0

    def test_verify_hooks_fails_without_hooks(self, test_repo_without_hooks: Path):
        """Test that verify_hooks fails when hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        result = adapter.verify_hooks(test_repo_without_hooks)

        assert not result.success, "Verification should fail when hooks missing"
        assert len(result.checks_failed) > 0

    def test_is_installed_true_with_hooks(self, test_repo_with_hooks: Path):
        """Test that is_installed returns True when hooks are installed."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        assert adapter.is_installed(test_repo_with_hooks)

    def test_is_installed_false_without_hooks(self, test_repo_without_hooks: Path):
        """Test that is_installed returns False when hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        assert not adapter.is_installed(test_repo_without_hooks)


class TestHooksInWorktree:
    """Tests that verify hooks work correctly in git worktree context.

    This is critical: the orchestrator creates worktrees for each issue,
    and hooks must work in those worktrees, not just the base repo.
    """

    @pytest.fixture
    def base_repo_with_tracked_hooks(self, tmp_path: Path, fixtures_path: Path) -> Path:
        """Create a base git repo with hooks tracked in git (not just installed).

        This simulates a project where .claude/hooks/ is committed to the repo,
        so worktrees will inherit the hooks via git checkout.
        """
        clean_env = _clean_git_env()

        # Create bare "remote" repo
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=remote,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Create working repo (base repo)
        base = tmp_path / "base"
        base.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Configure git user
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Add remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Copy hooks from fixture and TRACK them in git
        src_claude = fixtures_path / "hooks-installed" / ".claude"
        dst_claude = base / ".claude"
        shutil.copytree(src_claude, dst_claude)

        # Ensure hook is executable
        hook_script = dst_claude / "hooks" / "block-no-verify.sh"
        hook_script.chmod(0o755)

        # Create initial commit WITH hooks tracked
        readme = base / "README.md"
        readme.write_text("# Test Repo\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit with hooks"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        return base

    def test_hooks_work_in_worktree(self, base_repo_with_tracked_hooks: Path, tmp_path: Path):
        """Test that hooks inherited from git work correctly in a worktree.

        This is the critical test: when the orchestrator creates a worktree,
        the agent runs there. The hooks must work in that context.
        """
        import json

        clean_env = _clean_git_env()
        base = base_repo_with_tracked_hooks

        # Create a worktree (simulating what the orchestrator does)
        worktree_path = tmp_path / "worktree-42"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "42-test-issue"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Verify the hooks exist in the worktree (inherited from git)
        hook_script = worktree_path / ".claude" / "hooks" / "block-no-verify.sh"
        assert hook_script.exists(), "Hook script should exist in worktree (from git)"

        parse_script = worktree_path / ".claude" / "hooks" / "parse_hook_input.py"
        assert parse_script.exists(), "parse_hook_input.py should exist in worktree"

        allow_script = worktree_path / ".claude" / "hooks" / "allow_git_push.py"
        assert allow_script.exists(), "allow_git_push.py should exist in worktree"

        # Test that the hook BLOCKS --no-verify in the worktree context
        test_input = json.dumps({
            "tool_input": {
                "command": "git push --no-verify"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            cwd=str(worktree_path),  # Run from worktree directory
        )

        assert result.returncode == 2, (
            f"Hook should block --no-verify in worktree. "
            f"Exit code: {result.returncode}, stderr: {result.stderr}"
        )
        assert "BLOCKED" in result.stderr

    def test_hooks_allow_normal_push_in_worktree(
        self, base_repo_with_tracked_hooks: Path, tmp_path: Path
    ):
        """Test that hooks allow normal commands in worktree context."""
        import json

        clean_env = _clean_git_env()
        base = base_repo_with_tracked_hooks

        # Create a worktree
        worktree_path = tmp_path / "worktree-43"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "43-another-issue"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        hook_script = worktree_path / ".claude" / "hooks" / "block-no-verify.sh"

        # Test that normal push is ALLOWED
        test_input = json.dumps({
            "tool_input": {
                "command": "git push origin main"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
        )

        assert result.returncode == 0, (
            f"Hook should allow normal push in worktree. "
            f"Exit code: {result.returncode}, stderr: {result.stderr}"
        )


class TestCursorHooksInWorktree:
    """Tests that verify Cursor hooks work correctly in git worktree context.

    Cursor uses beforeShellExecution hooks with JSON output format.
    """

    @pytest.fixture
    def base_repo_with_cursor_hooks(self, tmp_path: Path) -> Path:
        """Create a base git repo with Cursor hooks tracked in git.

        Uses the CursorAdapter to install hooks, then commits them so
        worktrees will inherit them via git checkout.
        """
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        clean_env = _clean_git_env()

        # Create bare "remote" repo
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=remote,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Create working repo (base repo)
        base = tmp_path / "base"
        base.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Configure git user
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Add remote
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Install Cursor hooks
        adapter = CursorAdapter()
        adapter.install_hooks(base)

        # Create initial commit WITH hooks tracked
        readme = base / "README.md"
        readme.write_text("# Test Repo with Cursor Hooks\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit with Cursor hooks"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        return base

    def test_cursor_hooks_work_in_worktree(self, base_repo_with_cursor_hooks: Path, tmp_path: Path):
        """Test that Cursor hooks inherited from git work correctly in a worktree."""
        import json

        clean_env = _clean_git_env()
        base = base_repo_with_cursor_hooks

        # Create a worktree
        worktree_path = tmp_path / "cursor-worktree-42"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "42-cursor-issue"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        # Verify the hooks exist in the worktree
        hook_script = worktree_path / ".cursor" / "hooks" / "block-no-verify.sh"
        assert hook_script.exists(), "Cursor hook script should exist in worktree"

        parse_script = worktree_path / ".cursor" / "hooks" / "parse_hook_input.py"
        assert parse_script.exists(), "parse_hook_input.py should exist in worktree"

        hooks_json = worktree_path / ".cursor" / "hooks.json"
        assert hooks_json.exists(), "hooks.json should exist in worktree"

        # Test that the hook BLOCKS --no-verify (Cursor format: direct command key)
        test_input = json.dumps({"command": "git push --no-verify"})

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
        )

        # Cursor hooks output JSON with permission field
        output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        assert output.get("permission") == "deny", (
            f"Cursor hook should block --no-verify in worktree. "
            f"Output: {result.stdout}, stderr: {result.stderr}"
        )

    def test_cursor_hooks_allow_normal_push_in_worktree(
        self, base_repo_with_cursor_hooks: Path, tmp_path: Path
    ):
        """Test that Cursor hooks allow normal commands in worktree context."""
        import json

        clean_env = _clean_git_env()
        base = base_repo_with_cursor_hooks

        # Create a worktree
        worktree_path = tmp_path / "cursor-worktree-43"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", "43-cursor-issue"],
            cwd=base,
            capture_output=True,
            check=True,
            env=clean_env,
        )

        hook_script = worktree_path / ".cursor" / "hooks" / "block-no-verify.sh"

        # Test that normal push is ALLOWED
        test_input = json.dumps({"command": "git push origin main"})

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
        )

        output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        assert output.get("permission") == "allow", (
            f"Cursor hook should allow normal push in worktree. "
            f"Output: {result.stdout}, stderr: {result.stderr}"
        )


class TestCursorVerificationResult:
    """Tests for Cursor static (non-spawn) verification."""

    @pytest.fixture
    def test_repo_with_cursor_hooks(self, tmp_path: Path) -> Path:
        """Create a test repo with Cursor hooks installed."""
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        work = tmp_path / "work"
        work.mkdir()

        adapter = CursorAdapter()
        adapter.install_hooks(work)

        return work

    def test_verify_hooks_passes_with_hooks(self, test_repo_with_cursor_hooks: Path):
        """Test that verify_hooks passes when Cursor hooks are properly installed."""
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        adapter = CursorAdapter()
        result = adapter.verify_hooks(test_repo_with_cursor_hooks)

        assert result.success, f"Verification should pass: {result.checks_failed}"
        assert len(result.checks_passed) > 0

    def test_verify_hooks_fails_without_hooks(self, tmp_path: Path):
        """Test that verify_hooks fails when Cursor hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        work = tmp_path / "work"
        work.mkdir()

        adapter = CursorAdapter()
        result = adapter.verify_hooks(work)

        assert not result.success, "Verification should fail when hooks missing"
        assert len(result.checks_failed) > 0

    def test_is_installed_true_with_hooks(self, test_repo_with_cursor_hooks: Path):
        """Test that is_installed returns True when Cursor hooks are installed."""
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        adapter = CursorAdapter()
        assert adapter.is_installed(test_repo_with_cursor_hooks)

    def test_is_installed_false_without_hooks(self, tmp_path: Path):
        """Test that is_installed returns False when Cursor hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import CursorAdapter

        work = tmp_path / "work"
        work.mkdir()

        adapter = CursorAdapter()
        assert not adapter.is_installed(work)
