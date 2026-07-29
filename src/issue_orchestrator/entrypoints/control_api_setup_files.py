"""Filesystem adapter for Control Center repository setup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..infra.config import get_config_path
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupFileSystemError,
    RepositorySetupPlannedFile,
)
from .setup_wizard_common import (
    FileCollector,
    render_config_yaml,
    write_missing_setup_prompts,
)


class ControlApiRepositorySetupFileSystem:
    """Plan and apply the config/prompt files produced by setup policy."""

    def plan(
        self,
        *,
        repo_root: Path,
        config_name: str,
        config: Mapping[str, Any],
        include_prompts: bool,
    ) -> RepositorySetupArtifactPlan:
        config_path = get_config_path(repo_root, config_name)
        config_yaml = render_config_yaml(config, include_header=False)
        files = [
            RepositorySetupPlannedFile(
                path=config_path,
                content=config_yaml,
                action="overwrite" if config_path.exists() else "create",
                kind="config",
            )
        ]

        if include_prompts:
            collector = FileCollector()
            write_missing_setup_prompts(
                config,
                repo_root,
                file_collector=collector,
            )
            files.extend(
                RepositorySetupPlannedFile(
                    path=write.path,
                    content=write.content,
                    action=write.action,
                    kind="prompt",
                    agent=write.agent,
                )
                for write in collector.writes
                if write.kind == "prompt"
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


__all__ = ["ControlApiRepositorySetupFileSystem"]
