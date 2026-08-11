"""Shared rendering and prompt planning for repository setup artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..infra.config_value_rules import resolve_tech_lead_watch_label
from .setup_wizard_prompts import (
    build_code_review_prompt_text,
    build_internal_review_instructions_text,
    build_starter_prompt_text,
    build_tech_lead_review_prompt_text,
)

CONFIG_HEADER = """\
# Issue Orchestrator Configuration
#
# Template variables for initial_prompt and command:
#   {issue_number}    - GitHub issue number
#   {issue_title}     - Issue title
#   {prompt}          - Path to prompt file
#   {worktree}        - Path to worktree
#   {model}           - Model name from agent config
#   {permission_mode} - Claude permission mode
#   {pr_number}       - PR number (review/rework sessions only)
#
# See: https://github.com/anthropics/issue-orchestrator

"""


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that disables anchors and aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PlannedSetupPrompt:
    """One missing prompt and its exact repository setup content."""

    path: Path
    content: str
    agent: str


def _plan_internal_review_prompt(
    review_config: Mapping[str, Any],
    repo_root: Path,
    planned_paths: set[Path],
) -> PlannedSetupPrompt | None:
    """Plan the optional internal-review artifact within the repository."""
    internal_review = review_config.get("internal", {}) or {}
    if not isinstance(internal_review, Mapping):
        raise ValueError("review.internal must be a mapping")
    if internal_review.get("enabled") is not True:
        return None

    instructions_rel = internal_review.get(
        "instructions",
        ".io/internal-review.md",
    )
    if not isinstance(instructions_rel, str) or not instructions_rel.strip():
        raise ValueError("review.internal.instructions must be non-empty")
    instructions_rel = instructions_rel.strip()
    configured_path = Path(instructions_rel)
    if configured_path.is_absolute():
        raise ValueError("review.internal.instructions must be repository-relative")
    repo_root_resolved = repo_root.resolve()
    instructions_path = (repo_root_resolved / configured_path).resolve()
    try:
        instructions_path.relative_to(repo_root_resolved)
    except ValueError as exc:
        raise ValueError(
            "review.internal.instructions must stay inside the repository"
        ) from exc
    if instructions_path.exists():
        if not instructions_path.is_file():
            raise ValueError(
                "review.internal.instructions must reference a file: "
                f"{instructions_path}"
            )
        return None
    if instructions_path in planned_paths:
        return None
    return PlannedSetupPrompt(
        path=instructions_path,
        content=build_internal_review_instructions_text(),
        agent="internal-review",
    )


def render_setup_config_yaml(
    config: Mapping[str, Any],
    *,
    include_header: bool = True,
) -> str:
    """Render setup config YAML with stable formatting."""
    buffer = io.StringIO()
    yaml.dump(
        dict(config),
        buffer,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    content = buffer.getvalue()
    return CONFIG_HEADER + content if include_header else content


def plan_missing_setup_prompts(
    config: Mapping[str, Any],
    repo_root: Path,
) -> tuple[PlannedSetupPrompt, ...]:
    """Plan every missing prompt through one cross-entrypoint policy."""
    review_config = config.get("review", {}) or {}
    code_review_agent = review_config.get("default")
    code_review_label = review_config.get("code_review_label", "needs-code-review")
    code_reviewed_label = review_config.get("code_reviewed_label", "code-reviewed")
    tech_lead_review_agent = review_config.get("tech_lead_review_agent")
    tech_lead_reviewed_label = review_config.get(
        "tech_lead_reviewed_label",
        "tech-lead-reviewed",
    )
    tech_lead_watch_label = resolve_tech_lead_watch_label(
        review_config.get("tech_lead_review_label"),
        code_reviewed_label,
    )

    planned: list[PlannedSetupPrompt] = []
    planned_paths: set[Path] = set()
    for agent_name, agent_config in (config.get("agents", {}) or {}).items():
        if not isinstance(agent_name, str) or not isinstance(agent_config, Mapping):
            continue
        prompt_rel = agent_config.get("prompt", "")
        if not isinstance(prompt_rel, str) or not prompt_rel:
            continue

        prompt_path = Path(prompt_rel)
        if not prompt_path.is_absolute():
            prompt_path = repo_root / prompt_path
        prompt_path = prompt_path.resolve()
        if prompt_path.exists() or prompt_path in planned_paths:
            continue

        if agent_name == code_review_agent or agent_name.lower() == "agent:reviewer":
            content = build_code_review_prompt_text(
                code_review_label,
                code_reviewed_label,
            )
        elif (
            agent_name == tech_lead_review_agent
            or "tech_lead" in agent_name.lower()
        ):
            content = build_tech_lead_review_prompt_text(
                tech_lead_watch_label,
                tech_lead_reviewed_label,
            )
        else:
            content = build_starter_prompt_text(agent_name.split(":")[-1])

        planned.append(
            PlannedSetupPrompt(
                path=prompt_path,
                content=content,
                agent=agent_name,
            )
        )
        planned_paths.add(prompt_path)

    internal_review_prompt = _plan_internal_review_prompt(
        review_config,
        repo_root,
        planned_paths,
    )
    if internal_review_prompt is not None:
        planned.append(internal_review_prompt)

    return tuple(planned)


__all__ = [
    "CONFIG_HEADER",
    "PlannedSetupPrompt",
    "plan_missing_setup_prompts",
    "render_setup_config_yaml",
]
