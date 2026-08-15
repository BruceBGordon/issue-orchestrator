"""Unit tests for AI gate test CLI integration.

These tests verify the setup-hooks and doctor commands properly integrate
with the AI gate test feature. They mock adapters to avoid spawning
actual AI agents, making them fast and reliable for CI.

Moved from integration tests since they mock adapter methods.
"""

import argparse
from datetime import datetime, timezone

import pytest


class TestAiGateCLI:
    """Unit tests for AI gate test in CLI commands."""

    @pytest.fixture
    def repo_with_config(self, tmp_path):
        """Create a minimal repo with config for testing."""
        # Create config directory
        config_dir = tmp_path / ".issue-orchestrator/config/modes/default"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Create config file
        config_file = config_dir / "default.yaml"
        config_file.write_text(f"""
repo:
  name: test/test-repo

worktrees:
  base: {tmp_path / "worktrees"}

agents:
  agent:test:
    prompt: {tmp_path / "prompt.txt"}
    provider: claude-code
    model: sonnet

hooks:
  ai_gate:
    interval_days: 7
""")

        # Create required directories/files
        (tmp_path / "prompt.txt").write_text("Test prompt")
        (tmp_path / "worktrees").mkdir(exist_ok=True)

        return tmp_path, config_file

    def test_setup_hooks_runs_ai_gate(self, repo_with_config, monkeypatch):
        """Test that setup-hooks command runs AI gate tests and shows results."""
        from issue_orchestrator.infra.hooks.hooks import (
            ClaudeCodeAdapter,
            VerificationResult,
            AiAgentType,
        )
        from issue_orchestrator.entrypoints.cli import cmd_setup_hooks

        repo_path, config_file = repo_with_config

        # Mock all adapter methods to avoid real hook operations
        monkeypatch.setattr(
            ClaudeCodeAdapter, "is_installed", lambda self, path: True
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter, "install_hooks", lambda self, path: []
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "verify_hooks",
            lambda self, path: VerificationResult(
                success=True,
                meta_agent=AiAgentType.CLAUDE_CODE,
                checks_passed=["hook_script"],
                checks_failed=[],
            ),
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "test_ai_gate",
            lambda self, path, timeout=30: (True, "blocked git push --no-verify"),
        )

        # Create args namespace
        args = argparse.Namespace(
            config=str(config_file),
            target=str(repo_path),
        )

        # Run command
        exit_code = cmd_setup_hooks(args)

        assert exit_code == 0

        # Verify AI gate state was saved
        from issue_orchestrator.infra.ai_gate_state import load_ai_gate_state

        state = load_ai_gate_state(repo_path)
        assert state.last_check is not None
        assert "claude-code" in state.last_results
        assert state.last_results["claude-code"].success is True

    def test_setup_hooks_fails_on_ai_gate_failure(
        self, repo_with_config, monkeypatch
    ):
        """Test that setup-hooks returns non-zero when AI gate test fails."""
        from issue_orchestrator.infra.hooks.hooks import (
            ClaudeCodeAdapter,
            VerificationResult,
            AiAgentType,
        )
        from issue_orchestrator.entrypoints.cli import cmd_setup_hooks

        repo_path, config_file = repo_with_config

        # Mock adapter methods - AI gate test fails
        monkeypatch.setattr(
            ClaudeCodeAdapter, "is_installed", lambda self, path: True
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter, "install_hooks", lambda self, path: []
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "verify_hooks",
            lambda self, path: VerificationResult(
                success=True,
                meta_agent=AiAgentType.CLAUDE_CODE,
                checks_passed=["hook_script"],
                checks_failed=[],
            ),
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "test_ai_gate",
            lambda self, path, timeout=30: (False, "command was not blocked"),
        )

        args = argparse.Namespace(
            config=str(config_file),
            target=str(repo_path),
        )

        exit_code = cmd_setup_hooks(args)

        # Should fail because AI gate test failed
        assert exit_code == 1

    @pytest.mark.parametrize(
        ("gate_message", "expected_output"),
        [
            (
                "Claude is not authenticated; run 'claude auth login'\n"
                "Verify with 'claude auth status', then retry startup.\n"
                "The AI gate did not reach a result, so startup remains blocked.\n"
                "Stderr (72 chars): Failed to authenticate: OAuth session expired",
                (
                    "Claude is not authenticated; run 'claude auth login'",
                    "Verify with 'claude auth status', then retry startup.",
                    "Failed to authenticate: OAuth session expired",
                ),
            ),
            (
                "Claude AI gate could not run (exit 1)\n"
                "Resolve the Claude CLI error below, then retry startup.\n"
                "The AI gate did not reach a result, so startup remains blocked.\n"
                "Stderr (42 chars): Cannot connect to the Anthropic API",
                (
                    "Claude AI gate could not run (exit 1)",
                    "Resolve the Claude CLI error below, then retry startup.",
                    "Cannot connect to the Anthropic API",
                ),
            ),
        ],
    )
    def test_setup_hooks_prints_actionable_ai_gate_failure_details(
        self,
        repo_with_config,
        monkeypatch,
        capsys,
        gate_message,
        expected_output,
    ):
        """setup-hooks keeps remediation and provider diagnostics visible."""
        from issue_orchestrator.entrypoints.cli import cmd_setup_hooks
        from issue_orchestrator.infra.hooks.hooks import (
            AiAgentType,
            ClaudeCodeAdapter,
            VerificationResult,
        )

        repo_path, config_file = repo_with_config
        monkeypatch.setattr(ClaudeCodeAdapter, "install_hooks", lambda self, path: [])
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "verify_hooks",
            lambda self, path: VerificationResult(
                success=True,
                meta_agent=AiAgentType.CLAUDE_CODE,
                checks_passed=["hook_script"],
                checks_failed=[],
            ),
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "test_ai_gate",
            lambda self, path, timeout=30: (False, gate_message),
        )

        exit_code = cmd_setup_hooks(
            argparse.Namespace(config=str(config_file), target=str(repo_path))
        )
        output = capsys.readouterr().out

        assert exit_code == 1
        for expected in expected_output:
            assert expected in output

    def test_doctor_shows_ai_gate_with_cached_results(
        self, repo_with_config, monkeypatch
    ):
        """Test that doctor shows AI Gate with cached results."""
        from unittest.mock import MagicMock

        from issue_orchestrator.infra.ai_gate_state import (
            AiGateState,
            AiGateResult,
            save_ai_gate_state,
        )
        from issue_orchestrator.infra.doctor.checks.hooks import check_hook_verification
        from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
        from issue_orchestrator.infra.hooks.hooks import AiAgentType, VerificationResult
        from issue_orchestrator.infra.config import Config

        repo_path, config_file = repo_with_config

        # Mock adapter to make hooks appear installed
        mock_adapter = MagicMock()
        mock_adapter.is_installed.return_value = True
        mock_adapter.verify_hooks.return_value = VerificationResult(
            success=True,
            meta_agent=AiAgentType.CLAUDE_CODE,
            checks_passed=["hook_script"],
            checks_failed=[],
        )
        monkeypatch.setattr(hook_checks, "get_adapter", lambda _: mock_adapter)

        # Pre-populate AI gate state with a recent successful check
        state = AiGateState(
            last_check=datetime.now(timezone.utc),
            last_results={
                "claude-code": AiGateResult(
                    success=True,
                    message="blocked git push --no-verify",
                    timestamp=datetime.now(timezone.utc),
                ),
            },
        )
        save_ai_gate_state(repo_path, state)

        # Load config and run doctor hook checks
        config = Config.load(config_file)
        checks = check_hook_verification(config)

        # Find the AI Gate
        ai_gate = next((c for c in checks if c.name == "AI Gate"), None)

        assert ai_gate is not None, "AI Gate should be in doctor output"
        assert ai_gate.status == "ok"
        assert "Passed" in ai_gate.detail
        assert ai_gate.expandable is not None
        assert ai_gate.expandable["ran"] is False  # Used cached results

    def test_doctor_retries_cached_failure(self, repo_with_config, monkeypatch):
        """Cached failures trigger a re-run instead of blocking with stale error."""
        from unittest.mock import MagicMock

        from issue_orchestrator.infra.ai_gate_state import (
            AiGateState,
            AiGateResult,
            save_ai_gate_state,
        )
        from issue_orchestrator.infra.doctor.checks.hooks import check_hook_verification
        from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
        from issue_orchestrator.infra.hooks.hooks import AiAgentType, VerificationResult
        from issue_orchestrator.infra.config import Config

        repo_path, config_file = repo_with_config

        # Mock adapter: hooks installed, gate test passes on re-run
        mock_adapter = MagicMock()
        mock_adapter.is_installed.return_value = True
        mock_adapter.verify_hooks.return_value = VerificationResult(
            success=True,
            meta_agent=AiAgentType.CLAUDE_CODE,
            checks_passed=["hook_script"],
            checks_failed=[],
        )
        mock_adapter.test_ai_gate.return_value = (True, "AI gate test passed")
        monkeypatch.setattr(hook_checks, "get_adapter", lambda _: mock_adapter)

        # Pre-populate AI gate state with a recent FAILED check
        state = AiGateState(
            last_check=datetime.now(timezone.utc),
            last_results={
                "claude-code": AiGateResult(
                    success=False,
                    message="command was not blocked",
                    timestamp=datetime.now(timezone.utc),
                ),
            },
        )
        save_ai_gate_state(repo_path, state)

        config = Config.load(config_file)
        checks = check_hook_verification(config)

        ai_gate = next((c for c in checks if c.name == "AI Gate"), None)

        assert ai_gate is not None
        # Cached failure triggered re-run; re-run passed → ok
        assert ai_gate.status == "ok"
        assert ai_gate.expandable["ran"] is True
        assert ai_gate.expandable["triggered_by"] == "cached failure retry (claude-code)"
