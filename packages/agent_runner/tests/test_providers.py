"""Tests for AI provider implementations."""

import pytest

from agent_runner.providers import ClaudeCodeProvider, CodexProvider


class TestClaudeCodeProvider:
    """Tests for ClaudeCodeProvider."""

    def test_name(self) -> None:
        """Test provider name."""
        provider = ClaudeCodeProvider()
        assert provider.name == "claude-code"

    def test_executable(self) -> None:
        """Test executable name."""
        provider = ClaudeCodeProvider()
        assert provider.executable == "claude"

    def test_build_command_basic(self) -> None:
        """Test basic command building."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Fix the bug",
            model="sonnet",
        )

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert "Fix the bug" in cmd

    def test_build_command_with_permission_mode(self) -> None:
        """Test command with custom permission mode."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="sonnet",
            permission_mode="acceptEdits",
        )

        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "acceptEdits"

    def test_build_command_default_permission_mode(self) -> None:
        """Test that default permission mode is bypassPermissions."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="sonnet",
        )

        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "bypassPermissions"

    def test_build_command_with_system_prompt(self) -> None:
        """Test command with system prompt."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="sonnet",
            system_prompt="You are a helpful assistant.",
        )

        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == "You are a helpful assistant."

    def test_build_command_with_max_turns(self) -> None:
        """Test command with max turns."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="sonnet",
            max_turns="10",
        )

        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "10"

    def test_build_command_prompt_is_last(self) -> None:
        """Test that prompt is the last argument."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="The task to do",
            model="opus",
            system_prompt="System prompt",
        )

        assert cmd[-1] == "The task to do"

    def test_build_command_model_aliases(self) -> None:
        """Test that model aliases are resolved."""
        provider = ClaudeCodeProvider()

        for alias in ["haiku", "sonnet", "opus"]:
            cmd = provider.build_command(prompt="Task", model=alias)
            assert alias in cmd

    def test_build_command_full_model_id(self) -> None:
        """Test that full model IDs are passed through."""
        provider = ClaudeCodeProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="claude-3-5-sonnet-20241022",
        )

        assert "claude-3-5-sonnet-20241022" in cmd


class TestCodexProvider:
    """Tests for CodexProvider."""

    def test_name(self) -> None:
        """Test provider name."""
        provider = CodexProvider()
        assert provider.name == "codex"

    def test_executable(self) -> None:
        """Test executable name."""
        provider = CodexProvider()
        assert provider.executable == "codex"

    def test_build_command_basic(self) -> None:
        """Test basic command building."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Fix the bug",
            model="gpt-5-codex",
        )

        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--model" in cmd
        assert "gpt-5-codex" in cmd
        assert "Fix the bug" in cmd

    def test_build_command_default_full_auto(self) -> None:
        """Test that full-auto maps to supported explicit CLI policies."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
        )

        assert "--full-auto" not in cmd
        assert cmd[cmd.index("--ask-for-approval") + 1] == "on-request"
        assert cmd.index("--ask-for-approval") < cmd.index("exec")
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"

    def test_build_command_yolo_mode(self) -> None:
        """Test yolo approval mode."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
            approval_mode="yolo",
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd.index("--dangerously-bypass-approvals-and-sandbox") < cmd.index(
            "exec"
        )
        assert "--full-auto" not in cmd
        assert "--ask-for-approval" not in cmd

    def test_build_command_default_approval_mode(self) -> None:
        """Test default approval mode (no flag)."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
            approval_mode="default",
        )

        assert "--full-auto" not in cmd
        assert "--ask-for-approval" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_build_command_json_output_default(self) -> None:
        """Test that JSON output is enabled by default."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
        )

        assert "--json" in cmd

    def test_build_command_json_output_disabled(self) -> None:
        """Test disabling JSON output."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
            json_output="false",
        )

        assert "--json" not in cmd

    def test_build_command_with_sandbox(self) -> None:
        """Test command with sandbox policy."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
            sandbox="workspace-write",
        )

        assert "--sandbox" in cmd
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "workspace-write"

    def test_build_command_sandbox_ignored_in_yolo(self) -> None:
        """Test that sandbox is ignored in yolo mode."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            model="gpt-5-codex",
            approval_mode="yolo",
            sandbox="workspace-write",
        )

        # yolo disables sandbox, so --sandbox shouldn't appear
        assert "--sandbox" not in cmd

    def test_build_command_prompt_is_last(self) -> None:
        """Test that prompt is the last argument."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="The task to do",
            model="gpt-5-codex",
        )

        assert cmd[-1] == "The task to do"

    def test_build_command_model_optional(self) -> None:
        """Test that model is optional (uses Codex default)."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task without model",
        )

        assert "--model" not in cmd
        assert "Task without model" in cmd
        assert cmd[-1] == "Task without model"

    def test_build_command_no_model_with_options(self) -> None:
        """Test command without model but with other options."""
        provider = CodexProvider()

        cmd = provider.build_command(
            prompt="Task",
            approval_mode="yolo",
            json_output="false",
        )

        assert "--model" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--json" not in cmd
