"""Shared types for AI-agent hook installation and verification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


# Location of bundled hook templates (3 levels up from infra/hooks/_types.py)
TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates" / "hooks"


class AiAgentType(Enum):
    """Supported AI agent types.

    Values match ai_systems.yaml for unified configuration.
    """

    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    COPILOT = "copilot"
    CODEX = "codex"
    AIDER = "aider"
    GEMINI = "gemini"
    UNKNOWN = "unknown"


class UnsupportedAiAgentError(Exception):
    """Raised when an AI agent doesn't support required hooks."""

    def __init__(self, agent_type: AiAgentType, reason: str):
        self.agent_type = agent_type
        self.reason = reason
        super().__init__(f"Unsupported AI agent '{agent_type.value}': {reason}")


class HookVerificationError(Exception):
    """Raised when hook verification fails."""

    pass


@dataclass
class VerificationResult:
    """Result of hook verification."""

    success: bool
    meta_agent: AiAgentType
    checks_passed: list[str]
    checks_failed: list[str]
    audit_log: Optional[Path] = None

    @property
    def summary(self) -> str:
        if self.success:
            return f"✓ {self.meta_agent.value}: {len(self.checks_passed)} checks passed"
        else:
            return f"✗ {self.meta_agent.value}: {len(self.checks_failed)} checks failed"


@dataclass(frozen=True)
class ManagedHookArtifact:
    """A repo-local file owned by the hook installer."""

    path: Path
    template_path: Optional[Path] = None
    executable: bool = False


@dataclass(frozen=True)
class HookInstallationLayout:
    """Managed files and registration points for an AI agent hook install."""

    managed_files: tuple[ManagedHookArtifact, ...] = ()
    registration_files: tuple[Path, ...] = ()


class AiAgentAdapter(ABC):
    """Abstract base class for AI agent hook adapters."""

    @property
    @abstractmethod
    def agent_type(self) -> AiAgentType:
        """Return the AI agent type this adapter handles."""
        pass

    @abstractmethod
    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install hooks for this AI agent.

        Returns list of files created/modified.
        """
        pass

    @abstractmethod
    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify hooks are installed and working.

        Should test that --no-verify is actually blocked.
        """
        pass

    @abstractmethod
    def is_installed(self, project_root: Path) -> bool:
        """Check if hooks are already installed."""
        pass

    def installation_layout(self, project_root: Path) -> HookInstallationLayout:
        """Describe the repo-local files managed by this adapter."""
        return HookInstallationLayout()

    def validate_installation_target(self, project_root: Path) -> None:
        """Validate existing registration state before any setup writes."""

    def supports_ai_gate(self) -> bool:
        """Return True if this adapter supports AI gate testing."""
        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning the AI agent.

        Optional method - subclasses can override for AI gate testing.
        Default implementation returns not supported.

        Returns:
            (success, message) tuple
        """
        return False, f"AI gate test not supported for {self.agent_type.value}"

    def _managed_files(self, project_root: Path) -> tuple[ManagedHookArtifact, ...]:
        """Return the managed artifacts declared by installation_layout().

        installation_layout() is the source of truth for managed file coverage.
        install_hooks() implementations should derive template copies from this
        list so drift inspection and installation stay in sync.
        """
        return self.installation_layout(project_root).managed_files


__all__ = [
    "AiAgentAdapter",
    "AiAgentType",
    "HookInstallationLayout",
    "HookVerificationError",
    "ManagedHookArtifact",
    "TEMPLATES_DIR",
    "UnsupportedAiAgentError",
    "VerificationResult",
]
