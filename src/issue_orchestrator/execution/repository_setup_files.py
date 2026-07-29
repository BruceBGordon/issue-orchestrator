"""Filesystem adapter for repository setup execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..domain.repository_config_name import RepositoryConfigName
from ..infra.config import get_config_path
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupFileSystemError,
    RepositorySetupPlannedFile,
)
from .repository_setup_artifacts import (
    plan_missing_setup_prompts,
    render_setup_config_yaml,
)


class RepositorySetupFileSystemAdapter:
    """Plan and apply the config/prompt files produced by setup policy."""

    def plan(
        self,
        *,
        repo_root: Path,
        config_name: RepositoryConfigName,
        config: Mapping[str, Any],
        include_prompts: bool,
    ) -> RepositorySetupArtifactPlan:
        validated_name = RepositoryConfigName(config_name.value)
        config_path = get_config_path(repo_root, validated_name.value)
        config_yaml = render_setup_config_yaml(config, include_header=False)
        files = [
            RepositorySetupPlannedFile(
                path=config_path,
                content=config_yaml,
                action="overwrite" if config_path.exists() else "create",
                kind="config",
            )
        ]

        if include_prompts:
            files.extend(
                RepositorySetupPlannedFile(
                    path=prompt.path,
                    content=prompt.content,
                    action="create",
                    kind="prompt",
                    agent=prompt.agent,
                )
                for prompt in plan_missing_setup_prompts(config, repo_root)
            )

        return RepositorySetupArtifactPlan(
            config_yaml=config_yaml,
            files=tuple(files),
        )

    def apply(self, plan: RepositorySetupArtifactPlan) -> tuple[Path, ...]:
        applied_paths: list[Path] = []
        for planned_file in plan.files:
            try:
                planned_file.path.parent.mkdir(parents=True, exist_ok=True)
                planned_file.path.write_text(planned_file.content, encoding="utf-8")
            except Exception as exc:
                raise RepositorySetupFileSystemError(
                    operation=f"write {planned_file.kind} file {planned_file.path}",
                    applied_paths=tuple(applied_paths),
                    cause=exc,
                ) from exc
            applied_paths.append(planned_file.path)
        return tuple(applied_paths)


__all__ = ["RepositorySetupFileSystemAdapter"]
