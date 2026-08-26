"""Provider command wrapping policy for launched sessions."""

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from ..domain.models import AgentConfig
from ..ports.session_log import detect_ai_system_from_command


class ShortRetryConfig(Protocol):
    """Retry settings needed by provider_runner command wrapping."""

    max_attempts: int
    initial_backoff_seconds: int
    max_backoff_seconds: int
    jitter: bool


@dataclass(frozen=True)
class ProviderCommandWrapper:
    """Apply provider retry wrapping when an invocation is one-shot."""

    retry_config: ShortRetryConfig

    def runs_interactively(
        self,
        agent_config: AgentConfig,
        *,
        extra_provider_args: Mapping[str, str],
    ) -> bool:
        """Return whether this specific provider invocation is interactive."""
        if not agent_config.provider:
            return False

        from issue_orchestrator.agent_runner import get_provider, is_valid_provider

        if not is_valid_provider(agent_config.provider):
            return False

        kwargs: dict[str, object] = dict(agent_config.provider_args)
        kwargs.update(extra_provider_args)
        return get_provider(agent_config.provider).runs_interactively(**kwargs)

    def wrap(
        self,
        base_command: str,
        agent_config: AgentConfig,
        run_dir: Path,
        *,
        extra_provider_args: Mapping[str, str],
    ) -> str:
        """Wrap one-shot provider commands with retry/circuit reporting."""
        if self.runs_interactively(
            agent_config,
            extra_provider_args=extra_provider_args,
        ):
            return base_command

        provider = agent_config.provider or detect_ai_system_from_command(base_command)
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli_tools.provider_runner",
            "--command",
            base_command,
            "--timeout-seconds",
            str(agent_config.timeout_minutes * 60),
            "--max-attempts",
            str(self.retry_config.max_attempts),
            "--initial-backoff-seconds",
            str(self.retry_config.initial_backoff_seconds),
            "--max-backoff-seconds",
            str(self.retry_config.max_backoff_seconds),
            "--run-dir",
            str(run_dir),
        ]
        if self.retry_config.jitter:
            cmd.append("--jitter")
        else:
            cmd.append("--no-jitter")
        if provider:
            cmd.extend(["--provider", provider])
        return shlex.join(cmd)
