"""Behavior-level port for repository-owned coder prompt addenda."""

from __future__ import annotations

from typing import Protocol

from ..domain.coder_prompt import (
    CoderPromptAddendumPreparation,
    PreparedCoderPromptAddendum,
)
from ..domain.session_key import TaskKind


class CoderPromptAddendumProvider(Protocol):
    """Prepare the optional instructions for one role- and task-aware prompt."""

    def prepare(
        self,
        *,
        task: TaskKind,
        agent_label: str,
    ) -> CoderPromptAddendumPreparation:
        """Resolve trusted addendum I/O before the caller mutates launch state."""
        ...


class NoCoderPromptAddendum:
    """Explicit null implementation used when no coder addendum is configured."""

    def prepare(
        self,
        *,
        task: TaskKind,
        agent_label: str,
    ) -> PreparedCoderPromptAddendum:
        _ = (task, agent_label)
        return PreparedCoderPromptAddendum(None)


NO_CODER_PROMPT_ADDENDUM = NoCoderPromptAddendum()
