"""File-backed internal-review instructions for coder prompt composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..domain.coder_prompt import (
    CoderPromptAddendumPreparation,
    CoderPromptAddendumUnavailable,
    PreparedCoderPromptAddendum,
    build_internal_review_addendum,
)
from ..domain.session_key import TaskKind
from ..ports.coder_prompt import CoderPromptAddendumProvider

if TYPE_CHECKING:
    from ..infra.config import Config


@dataclass(frozen=True, slots=True)
class FileInternalReviewPromptAddendum:
    """Load trusted repository instructions and render the coder-side contract."""

    repository_root: Path
    enabled: bool
    max_rounds: int
    instructions_path: str
    tech_lead_agent_label_supplier: Callable[[], str | None]

    def prepare(
        self,
        *,
        task: TaskKind,
        agent_label: str,
    ) -> CoderPromptAddendumPreparation:
        """Resolve the addendum once, centrally excluding all non-coder roles."""
        if not self._applies_to(task=task, agent_label=agent_label):
            return PreparedCoderPromptAddendum(None)
        try:
            instructions_path = self._contained_instructions_path()
            instructions = instructions_path.read_text(encoding="utf-8").strip()
            if not instructions:
                raise ValueError(
                    "review.internal.instructions must reference a non-empty file: "
                    f"{instructions_path}"
                )
            addendum = build_internal_review_addendum(
                instructions=instructions,
                max_rounds=self.max_rounds,
                source=self.instructions_path,
            )
        except (OSError, ValueError) as exc:
            return CoderPromptAddendumUnavailable(str(exc))
        return PreparedCoderPromptAddendum(addendum)

    def _applies_to(self, *, task: TaskKind, agent_label: str) -> bool:
        """Own the complete internal-review role policy in one place."""
        if not self.enabled or task not in {TaskKind.CODE, TaskKind.REWORK}:
            return False
        tech_lead_labels = {"agent:tech-lead"}
        configured_tech_lead_label = self.tech_lead_agent_label_supplier()
        if configured_tech_lead_label is not None:
            tech_lead_labels.add(configured_tech_lead_label)
        return agent_label not in tech_lead_labels

    def _contained_instructions_path(self) -> Path:
        """Resolve instructions from the trusted, non-mutating repository root."""
        repository_root = self.repository_root.resolve()
        configured = Path(self.instructions_path)
        if configured.is_absolute():
            raise ValueError("review.internal.instructions must be repository-relative")
        candidate = (repository_root / configured).resolve()
        try:
            candidate.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError(
                "review.internal.instructions must stay inside the repository root"
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(
                "review.internal.instructions file not found in repository root: "
                f"{candidate}"
            )
        return candidate


def build_coder_prompt_addendum_provider(
    config: "Config",
) -> CoderPromptAddendumProvider:
    """Build the process-scoped provider from validated runtime configuration."""
    return FileInternalReviewPromptAddendum(
        repository_root=config.repo_root,
        enabled=config.internal_review_enabled,
        max_rounds=config.internal_review_max_rounds,
        instructions_path=config.internal_review_instructions,
        tech_lead_agent_label_supplier=lambda: config.tech_lead_review_agent,
    )
