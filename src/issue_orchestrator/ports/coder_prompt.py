"""Behavior-level port for repository-owned coder prompt addenda."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class CoderPromptAddendumProvider(Protocol):
    """Resolve the optional instructions appended to one coder prompt."""

    def for_worktree(self, worktree: Path) -> str | None:
        """Return the coder addendum for ``worktree``, or ``None`` when disabled."""


class NoCoderPromptAddendum:
    """Explicit null implementation used when no coder addendum is configured."""

    def for_worktree(self, worktree: Path) -> str | None:
        _ = worktree
        return None


NO_CODER_PROMPT_ADDENDUM = NoCoderPromptAddendum()
