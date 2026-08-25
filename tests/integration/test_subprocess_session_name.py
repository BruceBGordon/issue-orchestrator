"""Integration test for session_name parameter flow through pluggy hooks.

This test verifies that when a review session is created with session_name="review-3865",
the subprocess plugin correctly registers it under that name, not "issue-3865".

This reproduces the bug from issue #3861 where review sessions were registered
under the wrong name, causing session_exists checks to fail.
"""

import shlex
import sys
from pathlib import Path

import pluggy
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)
import pytest

# Run PTY tests sequentially in one worker to avoid Python 3.14 forkpty warning
# (forkpty() in multi-threaded processes can deadlock)
pytestmark = pytest.mark.xdist_group("pty")

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_terminal_session_terminator,
)
from issue_orchestrator.infra.hooks.hookspec import PROJECT_NAME, TerminalSpec
from tests.unit.session_run_helpers import make_session_run_assets


def _long_running_command(worktree: Path, session_name: str) -> str:
    """Keep subprocess sessions alive long enough to assert registry state."""
    run_assets = make_session_run_assets(worktree, session_name=session_name)
    command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"
    return f"export ISSUE_ORCHESTRATOR_RUN_DIR='{run_assets.run_dir}' && {command}"


@pytest.fixture
def temp_repo_root(tmp_path: Path) -> Path:
    """Create a temporary repo root with required structure."""
    repo_root = tmp_path / "test-repo"
    repo_root.mkdir()

    # Create state directory for subprocess registry
    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True)

    return repo_root


@pytest.fixture
def plugin_manager(temp_repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> pluggy.PluginManager:
    """Create a pluggy plugin manager with the subprocess plugin."""
    # Set REPO_ROOT so SubprocessPlugin uses our temp directory
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(temp_repo_root))

    pm = pluggy.PluginManager(PROJECT_NAME)
    pm.add_hookspecs(TerminalSpec)

    # Create and register the subprocess plugin
    plugin = SubprocessPlugin(build_terminal_session_terminator())
    pm.register(plugin, name="terminal_subprocess")

    return pm


class TestSessionNameFlow:
    """Test that session_name flows correctly through the pluggy hook system."""

    def test_review_session_uses_correct_name(
        self, plugin_manager: pluggy.PluginManager, temp_repo_root: Path
    ):
        """Verify review session is registered under 'review-3865', not 'issue-3865'.

        This is the exact bug from issue #3861.
        """
        worktree = temp_repo_root / "test-worktree"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Call create_session with explicit session_name="review-3865"
        # This is what the orchestrator does for review sessions
        result = plugin_manager.hook.create_session(
            session_id=3865,
            command=_long_running_command(worktree, "review-3865"),
            interaction_intent=TerminalInteractionIntent.NONE,
            shell=TerminalShell.BASH,
            working_dir=str(worktree),
            title="Review PR #3865",
            session_name="review-3865",  # This is the key parameter!
        )

        try:
            # Session should be created
            assert result is True, "Session creation should succeed"

            # NOW CHECK: session_exists_by_name("review-3865") should return True
            exists = plugin_manager.hook.session_exists_by_name(session_name="review-3865")
            assert exists is True, (
                "Session should exist under name 'review-3865', "
                "but session_exists_by_name returned False. "
                "This means the session was registered under a different name (likely 'issue-3865')."
            )

            # And it should NOT exist under "issue-3865"
            wrong_exists = plugin_manager.hook.session_exists_by_name(session_name="issue-3865")
            assert wrong_exists is False, (
                "Session should NOT exist under name 'issue-3865', "
                "but it does. The session_name parameter is being ignored!"
            )
        finally:
            plugin_manager.hook.kill_session(session_id=3865, session_name="review-3865")

    def test_issue_session_uses_explicit_name(
        self, plugin_manager: pluggy.PluginManager, temp_repo_root: Path
    ):
        """Verify issue session is registered under explicit 'issue-123' name."""
        worktree = temp_repo_root / "test-worktree-2"
        worktree.mkdir()
        sessions_dir = worktree / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Caller provides explicit session name (no fallbacks)
        result = plugin_manager.hook.create_session(
            session_id=123,
            command=_long_running_command(worktree, "issue-123"),
            interaction_intent=TerminalInteractionIntent.NONE,
            shell=TerminalShell.BASH,
            working_dir=str(worktree),
            title="Issue #123",
            session_name="issue-123",  # Caller computes name
        )

        try:
            assert result is True, "Session creation should succeed"

            # Should exist under "issue-123"
            exists = plugin_manager.hook.session_exists_by_name(session_name="issue-123")
            assert exists is True, "Session should exist under name 'issue-123'"
        finally:
            plugin_manager.hook.kill_session(session_id=123, session_name="issue-123")


class TestPluggyKwargHandling:
    """Test that pluggy correctly passes keyword arguments with defaults."""

    def test_hookspec_has_session_name_param(self):
        """Verify the hookspec includes session_name parameter."""
        import inspect
        sig = inspect.signature(TerminalSpec.create_session)
        params = list(sig.parameters.keys())

        assert "session_name" in params, (
            f"TerminalSpec.create_session should have session_name parameter. "
            f"Found params: {params}"
        )

    def test_subprocess_plugin_has_session_name_param(self):
        """Verify the plugin implementation includes session_name parameter."""
        import inspect
        sig = inspect.signature(SubprocessPlugin.create_session)
        params = list(sig.parameters.keys())

        assert "session_name" in params, (
            f"SubprocessPlugin.create_session should have session_name parameter. "
            f"Found params: {params}"
        )
