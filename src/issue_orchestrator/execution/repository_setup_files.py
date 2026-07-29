"""Filesystem adapter for repository setup execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..domain.repository_config_name import RepositoryConfigName
from ..infra.atomic_io import atomic_write_bytes
from ..infra.config import get_config_path
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupConfigTarget,
    RepositorySetupExplicitConfig,
    RepositorySetupFileSystemError,
    RepositorySetupNamedConfig,
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
        config_target: RepositorySetupConfigTarget,
        config: Mapping[str, Any],
        include_prompts: bool,
    ) -> RepositorySetupArtifactPlan:
        config_path = self._config_path(repo_root, config_target)
        config_yaml = render_setup_config_yaml(config)
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

    @staticmethod
    def _config_path(
        repo_root: Path,
        config_target: RepositorySetupConfigTarget,
    ) -> Path:
        if isinstance(config_target, RepositorySetupNamedConfig):
            validated_name = RepositoryConfigName(config_target.name.value)
            return get_config_path(repo_root, validated_name.value)
        if isinstance(config_target, RepositorySetupExplicitConfig):
            return RepositorySetupExplicitConfig(config_target.path).path
        raise TypeError(f"Unsupported repository setup config target: {config_target!r}")

    def apply(self, plan: RepositorySetupArtifactPlan) -> tuple[Path, ...]:
        applied_paths: list[Path] = []
        for planned_file in plan.files:
            try:
                planned_file.path.parent.mkdir(parents=True, exist_ok=True)
                if planned_file.action == "create":
                    with planned_file.path.open("x", encoding="utf-8") as file:
                        file.write(planned_file.content)
                elif planned_file.action == "overwrite":
                    atomic_write_bytes(
                        planned_file.path,
                        planned_file.content.encode("utf-8"),
                    )
                else:
                    raise ValueError(
                        f"Unsupported repository setup action: {planned_file.action}"
                    )
            except Exception as exc:
                raise RepositorySetupFileSystemError(
                    operation=f"write {planned_file.kind} file {planned_file.path}",
                    applied_paths=tuple(applied_paths),
                    cause=exc,
                ) from exc
            applied_paths.append(planned_file.path)
        return tuple(applied_paths)


__all__ = ["RepositorySetupFileSystemAdapter"]
