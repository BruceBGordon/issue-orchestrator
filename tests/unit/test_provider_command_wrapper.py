"""Tests for provider command wrapping policy."""

import shlex
from pathlib import Path

from issue_orchestrator.control.provider_command_wrapper import ProviderCommandWrapper
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config_models import ProviderShortRetryConfig


def _wrapper() -> ProviderCommandWrapper:
    return ProviderCommandWrapper(
        ProviderShortRetryConfig(
            max_attempts=2,
            initial_backoff_seconds=1,
            max_backoff_seconds=3,
            jitter=False,
        )
    )


def test_codex_default_interactive_skips_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md", provider="codex")

    wrapped = _wrapper().wrap(
        "codex 'task'",
        agent,
        tmp_path,
        extra_provider_args={},
    )

    assert wrapped == "codex 'task'"


def test_codex_exec_mode_uses_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(
        prompt_path=tmp_path / "prompt.md",
        provider="codex",
        provider_args={"execution_mode": "exec"},
    )

    wrapped = _wrapper().wrap(
        "codex exec 'task'",
        agent,
        tmp_path,
        extra_provider_args={},
    )

    argv = shlex.split(wrapped)
    assert argv[0]
    assert "issue_orchestrator.entrypoints.cli_tools.provider_runner" in argv
    assert "--provider" in argv
    assert argv[argv.index("--provider") + 1] == "codex"
    assert "--no-jitter" in argv


def test_codex_extra_exec_mode_uses_provider_runner(tmp_path: Path) -> None:
    agent = AgentConfig(prompt_path=tmp_path / "prompt.md", provider="codex")

    wrapped = _wrapper().wrap(
        "codex exec 'task'",
        agent,
        tmp_path,
        extra_provider_args={"execution_mode": "exec"},
    )

    assert "issue_orchestrator.entrypoints.cli_tools.provider_runner" in shlex.split(wrapped)
