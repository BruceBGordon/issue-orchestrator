"""Base class for CLI-based AI providers.

This module provides a base class that implements common functionality
for AI providers that are invoked via command-line interface.

Previously in ``_vendor/agent_runner/providers/base.py``.
"""

import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from issue_orchestrator.ports.provider_readiness import ProviderReadiness
from issue_orchestrator.ports.provider_resilience import ProviderErrorType

from ..agent_runner_errors import classify_provider_output

if TYPE_CHECKING:
    from pathlib import Path

    from issue_orchestrator.domain.sandbox_scope import SandboxScope
    from issue_orchestrator.ports.command_runner import CommandRunner


class CLIProvider(ABC):
    """Base class for CLI-based AI agent providers.

    Subclasses must implement:
    - name: Provider identifier
    - executable: The CLI executable name
    - build_command: Build the full command argv

    Subclasses may override:
    - check_readiness: Run the CLI's own cheap auth probe (default: no probe)
    - description: Human-readable description

    Provider adapters are the **only** place raw CLI text is interpreted
    (#6999). ``check_readiness`` and ``classify_output`` return typed values;
    control never sees a banner string or an exit code.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'claude-code', 'codex')."""
        ...

    @property
    @abstractmethod
    def executable(self) -> str:
        """The CLI executable name (e.g., 'claude', 'codex')."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description of this provider."""
        return f"{self.name} CLI"

    @property
    def interactive(self) -> bool:
        """Whether this provider runs as an interactive TUI session.

        Interactive providers:
        - Are NOT wrapped in provider_runner (no retry/circuit wrapper)
        - May seed the initial prompt through argv and accept follow-up prompts via PTY
        - Stay alive for follow-up prompts (review feedback, rework)
        """
        return False

    def runs_interactively(self, **kwargs: object) -> bool:
        """Whether this invocation should be treated as an interactive session.

        Most providers have a single execution mode and can rely on
        :attr:`interactive`. Providers with both TUI and one-shot modes can
        inspect provider args and choose per invocation.
        """
        return self.interactive

    def needs_fresh_prompt_process(self, **kwargs: object) -> bool:
        """Whether a completed prompt turn should respawn before follow-up input.

        Interactive providers normally keep one process alive across review
        exchange turns. Providers that expose an interactive UI but cannot
        reliably accept a second orchestrator-driven prompt should override
        this capability.
        """
        return False

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        working_directory: "Path | None" = None,
        **kwargs: str,
    ) -> list[str]:
        """Build the command-line invocation for this provider.

        Args:
            prompt: The task/prompt to send to the agent
            model: Model identifier (provider-specific), None for default
            sandbox_scope: When set, the bounded sandbox the orchestrator
                computed for this session. ``None`` (the default) preserves the
                provider's existing unsandboxed command exactly.
            working_directory: Worktree where the provider process will run.
            **kwargs: Provider-specific options (provider_args from YAML)

        Returns:
            Command as argv list
        """
        ...

    def apply_scope(self, scope: "SandboxScope") -> list[str]:
        """Translate a :class:`SandboxScope` into this provider's CLI argv fragment.

        Default: not supported. Providers that can enforce a sandbox override
        this; a provider that cannot yet raises :class:`NotImplementedError`
        rather than silently launching unsandboxed.
        """
        raise NotImplementedError(
            f"{self.name} does not support sandbox-scope translation"
        )

    def is_available(self) -> bool:
        """Check if the CLI executable is installed and in PATH."""
        return shutil.which(self.executable) is not None

    def check_readiness(self, runner: "CommandRunner") -> ProviderReadiness:
        """Run this CLI's cheapest non-interactive credential probe.

        Default: report installation only. A provider that ships no auth probe
        answers ``UNKNOWN`` rather than ``READY`` — "I could not tell" must
        never be recorded as "credentials confirmed" — and ``UNKNOWN`` is still
        launchable, so an unprobeable provider behaves exactly as it did before
        this boundary existed.

        Subclasses override this (not ``is_authenticated``) so there is one
        implementation of "is this provider logged in" per provider.
        """
        del runner  # the default probe runs nothing
        if not self.is_available():
            return ProviderReadiness.not_installed(
                self.name, f"{self.executable} not found in PATH"
            )
        return ProviderReadiness.unknown(
            self.name, f"{self.executable} has no non-interactive auth probe"
        )

    def classify_output(self, output: str) -> ProviderErrorType | None:
        """Classify raw provider output through the one classification table.

        Providers may override to add a provider-specific pre-pass, but must
        delegate the token matching itself so no second table appears.
        """
        return classify_provider_output(output)

    def is_authenticated(self, runner: "CommandRunner | None" = None) -> bool:
        """Whether a probe positively confirmed working credentials.

        Delegates to :meth:`check_readiness` so the probe has exactly one
        implementation. Without a ``runner`` no probe can be executed, so this
        degrades to the availability answer it has always given.
        """
        if runner is None:
            return self.is_available()
        return self.check_readiness(runner).authenticated

    # Credential probes must fail fast: they run on the launch path, and a
    # hung probe would reintroduce the very stall this boundary removes.
    AUTH_PROBE_TIMEOUT_SECONDS = 15

    def _run_auth_probe(
        self, runner: "CommandRunner", argv: list[str]
    ) -> tuple[str, int | None, bool]:
        """Run one credential probe, returning (combined output, exit code, timed out)."""
        result = runner.run(argv, timeout_seconds=self.AUTH_PROBE_TIMEOUT_SECONDS)
        combined = f"{result.stdout}\n{result.stderr}"
        return combined, result.returncode, result.timed_out

    def check_version(self) -> str | None:
        """Get the CLI version string, if available."""
        if not self.is_available():
            return None
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip() or result.stderr.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
