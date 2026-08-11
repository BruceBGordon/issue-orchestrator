"""Tests for the file-backed coder internal-review prompt addendum."""

from pathlib import Path

import pytest

from issue_orchestrator.domain.coder_prompt import (
    CoderPromptAddendumUnavailable,
    PreparedCoderPromptAddendum,
)
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.execution.internal_review_prompt import (
    FileInternalReviewPromptAddendum,
    build_coder_prompt_addendum_provider,
)
from issue_orchestrator.infra.config import Config


def _provider(
    repository_root: Path,
    *,
    enabled: bool = True,
    max_rounds: int = 5,
    instructions_path: str = ".io/internal-review.md",
) -> FileInternalReviewPromptAddendum:
    return FileInternalReviewPromptAddendum(
        repository_root=repository_root,
        enabled=enabled,
        max_rounds=max_rounds,
        instructions_path=instructions_path,
        tech_lead_agent_label_supplier=lambda: None,
    )


def _prepare(
    provider: FileInternalReviewPromptAddendum,
    *,
    task: TaskKind = TaskKind.CODE,
    agent_label: str = "agent:dev",
) -> PreparedCoderPromptAddendum | CoderPromptAddendumUnavailable:
    return provider.prepare(task=task, agent_label=agent_label)


def test_disabled_provider_does_not_require_instruction_file(tmp_path: Path) -> None:
    assert _prepare(_provider(tmp_path, enabled=False)) == PreparedCoderPromptAddendum(
        None
    )


def test_enabled_provider_wraps_repository_instructions(tmp_path: Path) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("Spawn one fast reviewer.", encoding="utf-8")

    preparation = _prepare(_provider(tmp_path, max_rounds=3))

    assert isinstance(preparation, PreparedCoderPromptAddendum)
    addendum = preparation.addendum
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
    config.repo_root = tmp_path

    preparation = build_coder_prompt_addendum_provider(config).prepare(
        task=TaskKind.CODE,
        agent_label="agent:dev",
    )

    assert isinstance(preparation, PreparedCoderPromptAddendum)
    addendum = preparation.addendum
    assert addendum is not None
    assert "Review the coder's work." in addendum


def test_built_provider_reads_live_tech_lead_agent_label(tmp_path: Path) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("Review the coder's work.", encoding="utf-8")
    config_path = tmp_path / ".issue-orchestrator.yaml"
    config_path.write_text(
        "review:\n"
        "  tech_lead_review_agent: agent:old-tech-lead\n"
        "  internal:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    config = Config.load(config_path)
    config.repo_root = tmp_path
    provider = build_coder_prompt_addendum_provider(config)

    config.tech_lead_review_agent = "agent:new-tech-lead"

    assert provider.prepare(
        task=TaskKind.CODE,
        agent_label="agent:new-tech-lead",
    ) == PreparedCoderPromptAddendum(None)
    old_label_preparation = provider.prepare(
        task=TaskKind.CODE,
        agent_label="agent:old-tech-lead",
    )
    assert isinstance(old_label_preparation, PreparedCoderPromptAddendum)
    assert old_label_preparation.addendum is not None


def test_enabled_provider_fails_when_instruction_file_is_missing(
    tmp_path: Path,
) -> None:
    preparation = _prepare(_provider(tmp_path))

    assert isinstance(preparation, CoderPromptAddendumUnavailable)
    assert "file not found" in preparation.reason


def test_enabled_provider_rejects_empty_instruction_file(tmp_path: Path) -> None:
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.write_text("  \n", encoding="utf-8")

    preparation = _prepare(_provider(tmp_path))

    assert isinstance(preparation, CoderPromptAddendumUnavailable)
    assert "non-empty file" in preparation.reason


@pytest.mark.parametrize(
    "configured_path",
    ["../outside.md", "/tmp/outside.md"],
)
def test_enabled_provider_rejects_paths_outside_repository_root(
    tmp_path: Path,
    configured_path: str,
) -> None:
    preparation = _prepare(
        _provider(tmp_path, instructions_path=configured_path),
    )

    assert isinstance(preparation, CoderPromptAddendumUnavailable)
    assert "repository-relative" in preparation.reason or "inside" in preparation.reason


def test_enabled_provider_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-internal-review.md"
    outside.write_text("Do not load me.", encoding="utf-8")
    instructions = tmp_path / ".io" / "internal-review.md"
    instructions.parent.mkdir()
    instructions.symlink_to(outside)

    preparation = _prepare(_provider(tmp_path))

    assert isinstance(preparation, CoderPromptAddendumUnavailable)
    assert "inside the repository root" in preparation.reason


@pytest.mark.parametrize(
    ("task", "agent_label"),
    [
        (TaskKind.REVIEW, "agent:reviewer"),
        (TaskKind.RETROSPECTIVE_REVIEW, "agent:reviewer"),
        (TaskKind.TECH_LEAD, "agent:tech-lead"),
        (TaskKind.CODE, "agent:tech-lead"),
        (TaskKind.CODE, "agent:architecture"),
    ],
)
def test_non_coder_roles_do_not_read_instruction_file(
    tmp_path: Path,
    task: TaskKind,
    agent_label: str,
) -> None:
    provider = _provider(tmp_path)
    if agent_label == "agent:architecture":
        provider = FileInternalReviewPromptAddendum(
            repository_root=tmp_path,
            enabled=True,
            max_rounds=5,
            instructions_path=".io/internal-review.md",
            tech_lead_agent_label_supplier=lambda: "agent:architecture",
        )

    assert _prepare(
        provider,
        task=task,
        agent_label=agent_label,
    ) == PreparedCoderPromptAddendum(None)
