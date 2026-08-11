"""Tests for the file-backed coder internal-review prompt addendum."""

from pathlib import Path

import pytest

from issue_orchestrator.execution.internal_review_prompt import (
    FileInternalReviewPromptAddendum,
    build_coder_prompt_addendum_provider,
)
from issue_orchestrator.infra.config import Config


def _provider(
    *,
    enabled: bool = True,
    max_rounds: int = 5,
    instructions_path: str = ".io/internal-review.md",
) -> FileInternalReviewPromptAddendum:
    return FileInternalReviewPromptAddendum(
        enabled=enabled,
        max_rounds=max_rounds,
        instructions_path=instructions_path,
    )


def test_disabled_provider_does_not_require_instruction_file(tmp_path: Path) -> None:
    assert _provider(enabled=False).for_worktree(tmp_path) is None


def test_enabled_provider_wraps_repository_instructions(tmp_path: Path) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("Spawn one fast reviewer.", encoding="utf-8")

    addendum = _provider(max_rounds=3).for_worktree(tmp_path)

    assert addendum is not None
    assert "Spawn one fast reviewer." in addendum
    assert "Spawn exactly one internal reviewer" in addendum
    assert "at most 3 internal reviewer verdict(s)" in addendum
    assert "independent external reviewer" in addendum
    assert "blocked instead of reporting successful completion" in addendum


def test_loaded_config_normalizes_instruction_path_before_runtime_read(
    tmp_path: Path,
) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("Review the coder's work.", encoding="utf-8")
    config_path = tmp_path / ".issue-orchestrator.yaml"
    config_path.write_text(
        'review:\n  internal:\n    enabled: true\n'
        '    instructions: " .io/internal-review.md "\n',
        encoding="utf-8",
    )
    config = Config.load(config_path)

    addendum = build_coder_prompt_addendum_provider(config).for_worktree(tmp_path)

    assert addendum is not None
    assert "Review the coder's work." in addendum


def test_enabled_provider_fails_when_instruction_file_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="file not found"):
        _provider().for_worktree(tmp_path)


def test_enabled_provider_rejects_empty_instruction_file(tmp_path: Path) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("  \n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty file"):
        _provider().for_worktree(tmp_path)


@pytest.mark.parametrize(
    "configured_path",
    ["../outside.md", "/tmp/outside.md"],
)
def test_enabled_provider_rejects_paths_outside_worktree(
    tmp_path: Path,
    configured_path: str,
) -> None:
    with pytest.raises(ValueError, match="repository-relative|inside"):
        _provider(instructions_path=configured_path).for_worktree(tmp_path)


def test_enabled_provider_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-internal-review.md"
    outside.write_text("Do not load me.", encoding="utf-8")
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.symlink_to(outside)

    with pytest.raises(ValueError, match="inside the coder worktree"):
        _provider().for_worktree(tmp_path)
