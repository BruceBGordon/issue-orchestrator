"""File-backed internal-review instructions for coder prompt composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.coder_prompt import build_internal_review_addendum
from ..ports.coder_prompt import CoderPromptAddendumProvider

if TYPE_CHECKING:
    from ..infra.config import Config


@dataclass(frozen=True, slots=True)
class FileInternalReviewPromptAddendum:
    """Load trusted repository instructions and render the coder-side contract."""

    enabled: bool
    max_rounds: int
    instructions_path: str

    def for_worktree(self, worktree: Path) -> str | None:
        if not self.enabled:
            return None
        instructions_path = self._contained_instructions_path(worktree)
        instructions = instructions_path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise ValueError(
                "review.internal.instructions must reference a non-empty file: "
                f"{instructions_path}"
            )
        return build_internal_review_addendum(
            instructions=instructions,
            max_rounds=self.max_rounds,
            source=self.instructions_path,
        )

    def _contained_instructions_path(self, worktree: Path) -> Path:
        worktree_root = worktree.resolve()
        configured = Path(self.instructions_path)
        if configured.is_absolute():
            raise ValueError("review.internal.instructions must be repository-relative")
        candidate = (worktree_root / configured).resolve()
        try:
            candidate.relative_to(worktree_root)
        except ValueError as exc:
            raise ValueError(
                "review.internal.instructions must stay inside the coder worktree"
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(
                "review.internal.instructions file not found in coder worktree: "
                f"{candidate}"
            )
        return candidate


def build_coder_prompt_addendum_provider(
    config: "Config",
) -> CoderPromptAddendumProvider:
    """Build the process-scoped provider from validated runtime configuration."""
    return FileInternalReviewPromptAddendum(
        enabled=config.internal_review_enabled,
        max_rounds=config.internal_review_max_rounds,
        instructions_path=config.internal_review_instructions,
    )
