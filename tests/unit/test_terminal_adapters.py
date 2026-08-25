"""Comprehensive unit tests for terminal adapters.

Tests for:
- PluggySessionRunner (execution/session_runner_adapter.py)
- SubprocessPlugin (execution/terminal_subprocess.py)

These tests mock external dependencies
and verify the adapter logic without requiring actual terminal processes.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call

import pytest

from issue_orchestrator.domain.terminal_launch import TerminalLaunch, TerminalShell


class TestPluggySessionRunner:
    """Tests for PluggySessionRunner which wraps pluggy PluginManager.

    This adapter bridges the SessionRunner protocol and pluggy hooks.
    We mock the PluginManager to test the delegation logic.
    """

    @pytest.fixture
    def mock_plugin_manager(self):
        """Create a mock pluggy PluginManager."""
        mock_pm = MagicMock()
        mock_pm.hook = MagicMock()
        return mock_pm

    @pytest.fixture
    def session_runner(self, mock_plugin_manager):
        """Create PluggySessionRunner with mocked plugin manager."""
        from issue_orchestrator.execution.session_runner_adapter import PluggySessionRunner
        return PluggySessionRunner(mock_plugin_manager)

    def test_create_session_success(self, session_runner, mock_plugin_manager, caplog):
        """create_session delegates to pluggy hook and returns result."""
        mock_plugin_manager.hook.create_session.return_value = True

        with caplog.at_level(logging.INFO):
            result = session_runner.create_session(
                session_id=42,
                launch=TerminalLaunch.classified(
                    "claude --prompt issue.md", TerminalShell.BASH
                ),
                working_dir="/tmp/worktree",
                title="Fix the bug",
                session_name="issue-42",
            )

        assert result is True
        mock_plugin_manager.hook.create_session.assert_called_once_with(
            session_id=42,
            command="claude --prompt issue.md",
            interaction_intent=TerminalLaunch.classified(
                "claude --prompt issue.md", TerminalShell.BASH
            ).interaction_intent,
            shell=TerminalShell.BASH,
            working_dir="/tmp/worktree",
            title="Fix the bug",
            session_name="issue-42",
        )
        assert "Creating session via terminal hook" in caplog.text
        assert "id=42" in caplog.text

    def test_create_session_returns_none(self, session_runner, mock_plugin_manager):
        """create_session returns False when hook returns None."""
        mock_plugin_manager.hook.create_session.return_value = None

        result = session_runner.create_session(
            session_id=42,
            launch=TerminalLaunch.classified("claude", TerminalShell.BASH),
            working_dir="/tmp/worktree",
            title=None,
            session_name="issue-42",
        )

        assert result is False

    def test_create_session_returns_false(self, session_runner, mock_plugin_manager):
        """create_session returns False when hook returns False."""
        mock_plugin_manager.hook.create_session.return_value = False

        result = session_runner.create_session(
            session_id=42,
            launch=TerminalLaunch.classified("claude", TerminalShell.BASH),
            working_dir="/tmp/worktree",
            title=None,
            session_name="issue-42",
        )

        assert result is False

    def test_session_exists_true(self, session_runner, mock_plugin_manager):
        """session_exists returns True when hook returns True."""
        mock_plugin_manager.hook.session_exists.return_value = True

        result = session_runner.session_exists(session_id=42, session_name="issue-42")

        assert result is True
        mock_plugin_manager.hook.session_exists.assert_called_once_with(session_id=42, session_name="issue-42")

    def test_session_exists_false(self, session_runner, mock_plugin_manager):
        """session_exists returns False when hook returns False."""
        mock_plugin_manager.hook.session_exists.return_value = False

        result = session_runner.session_exists(session_id=42, session_name="issue-42")

        assert result is False

    def test_session_exists_none(self, session_runner, mock_plugin_manager):
        """session_exists returns False when hook returns None."""
        mock_plugin_manager.hook.session_exists.return_value = None

        result = session_runner.session_exists(session_id=42, session_name="issue-42")

        assert result is False

    def test_kill_session(self, session_runner, mock_plugin_manager):
        """kill_session delegates to pluggy hook."""
        session_runner.kill_session(session_id=42, session_name="issue-42")

        mock_plugin_manager.hook.kill_session.assert_called_once_with(session_id=42, session_name="issue-42")

    def test_discover_running_sessions_with_results(self, session_runner, mock_plugin_manager):
        """discover_running_sessions returns list from hook."""
        mock_sessions = [
            {"issue_number": 42, "tab_name": "issue-42", "is_review": False},
            {"issue_number": 123, "tab_name": "review-123", "is_review": True},
        ]
        mock_plugin_manager.hook.discover_running_sessions.return_value = mock_sessions

        result = session_runner.discover_running_sessions()

        assert result == mock_sessions

    def test_discover_running_sessions_empty(self, session_runner, mock_plugin_manager):
        """discover_running_sessions returns empty list when hook returns None."""
        mock_plugin_manager.hook.discover_running_sessions.return_value = None

        result = session_runner.discover_running_sessions()

        assert result == []

    def test_cleanup_idle_sessions_returns_count(self, session_runner, mock_plugin_manager):
        """cleanup_idle_sessions returns count from hook."""
        mock_plugin_manager.hook.cleanup_idle_sessions.return_value = 3

        result = session_runner.cleanup_idle_sessions()

        assert result == 3

    def test_cleanup_idle_sessions_returns_none(self, session_runner, mock_plugin_manager):
        """cleanup_idle_sessions returns 0 when hook returns None."""
        mock_plugin_manager.hook.cleanup_idle_sessions.return_value = None

        result = session_runner.cleanup_idle_sessions()

        assert result == 0

    def test_get_session_output_with_result(self, session_runner, mock_plugin_manager):
        """get_session_output returns output from hook."""
        mock_plugin_manager.hook.get_session_output.return_value = "foo\nbar\nbaz"

        result = session_runner.get_session_output(session_id=42, lines=50, session_name="issue-42")

        assert result == "foo\nbar\nbaz"
        mock_plugin_manager.hook.get_session_output.assert_called_once_with(
            session_id=42, lines=50, session_name="issue-42"
        )

    def test_get_session_output_none(self, session_runner, mock_plugin_manager):
        """get_session_output returns None when hook returns None."""
        mock_plugin_manager.hook.get_session_output.return_value = None

        result = session_runner.get_session_output(session_id=42, lines=20, session_name="issue-42")

        assert result is None

    def test_get_session_output_custom_lines(self, session_runner, mock_plugin_manager):
        """get_session_output passes lines parameter correctly."""
        mock_plugin_manager.hook.get_session_output.return_value = "output"

        session_runner.get_session_output(session_id=42, lines=100, session_name="issue-42")

        mock_plugin_manager.hook.get_session_output.assert_called_once_with(
            session_id=42, lines=100, session_name="issue-42"
        )

    def test_send_to_session_success(self, session_runner, mock_plugin_manager):
        """send_to_session returns True when hook returns True."""
        mock_plugin_manager.hook.send_to_session.return_value = True

        result = session_runner.send_to_session(session_id=42, text="/exit", session_name="issue-42")

        assert result is True
        mock_plugin_manager.hook.send_to_session.assert_called_once_with(
            session_id=42, text="/exit", session_name="issue-42"
        )

    def test_send_to_session_failure(self, session_runner, mock_plugin_manager):
        """send_to_session returns False when hook returns False."""
        mock_plugin_manager.hook.send_to_session.return_value = False

        result = session_runner.send_to_session(session_id=42, text="/exit", session_name="issue-42")

        assert result is False

    def test_send_to_session_none(self, session_runner, mock_plugin_manager):
        """send_to_session returns False when hook returns None."""
        mock_plugin_manager.hook.send_to_session.return_value = None

        result = session_runner.send_to_session(session_id=42, text="/exit", session_name="issue-42")

        assert result is False

    def test_session_exists_by_name_true(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns True when hook returns True."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = True

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is True
        mock_plugin_manager.hook.session_exists_by_name.assert_called_once_with(
            session_name="review-456"
        )

    def test_session_exists_by_name_false(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns False when hook returns False."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = False

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is False

    def test_session_exists_by_name_none(self, session_runner, mock_plugin_manager):
        """session_exists_by_name returns False when hook returns None."""
        mock_plugin_manager.hook.session_exists_by_name.return_value = None

        result = session_runner.session_exists_by_name(session_name="review-456")

        assert result is False

    def test_send_to_session_by_name_success(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns True when hook returns True."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = True

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is True
        mock_plugin_manager.hook.send_to_session_by_name.assert_called_once_with(
            session_name="review-456", text="/exit"
        )

    def test_send_to_session_by_name_failure(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns False when hook returns False."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = False

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is False

    def test_send_to_session_by_name_none(self, session_runner, mock_plugin_manager):
        """send_to_session_by_name returns False when hook returns None."""
        mock_plugin_manager.hook.send_to_session_by_name.return_value = None

        result = session_runner.send_to_session_by_name(
            session_name="review-456", text="/exit"
        )

        assert result is False
