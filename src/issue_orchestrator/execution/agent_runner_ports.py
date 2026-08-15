"""AI provider protocol.

Defines the interface that AI agent providers (Claude, Codex, etc.) must
implement.  Providers know how to build command-line invocations for their
respective AI agents.

Previously in ``_vendor/agent_runner/ports.py``.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope
    from pathlib import Path


class AIProvider(Protocol):
    """Protocol for AI agent providers (Claude, Codex, etc.).

    Providers know how to build command-line invocations for their respective
    AI agents. They handle CLI flag differences between agents.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g., 'claude-code', 'codex')."""
        ...

    def build_command(
        self,
        prompt: str,
        model: str,
        *,
        sandbox_scope: "SandboxScope | None" = None,
        working_directory: "Path | None" = None,
        **kwargs: str,
    ) -> list[str]:
        """Build the command-line invocation for this provider."""
        ...
