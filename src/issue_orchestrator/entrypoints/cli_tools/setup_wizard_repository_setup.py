"""CLI adapter for the shared repository setup owner."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ...control.repository_setup import (
    RepositorySetupConflictError,
    RepositorySetupExecutionError,
    RepositorySetupOwner,
    RepositorySetupPreview,
    RepositorySetupRequest,
    RepositorySetupResult,
)
from ...domain.repository_config_name import RepositoryConfigName
from ...infra.config import get_config_path
from ...ports.repository_setup import (
    RepositorySetupConfigTarget,
    RepositorySetupExplicitConfig,
    RepositorySetupNamedConfig,
)
from ..setup_wizard_common import FileCollector
from .setup_wizard_support import Prompter


def build_cli_repository_setup_request(
    *,
    owner: RepositorySetupOwner,
    config: Mapping[str, Any],
    repo_root: Path,
    config_path: Path,
) -> RepositorySetupRequest:
    """Translate a completed CLI questionnaire into the shared owner request."""
    repo_config = config.get("repo") or {}
    repo_name = (
        repo_config.get("name") if isinstance(repo_config, Mapping) else repo_config
    )
    if not isinstance(repo_name, str) or not repo_name.strip():
        raise ValueError("Completed setup config requires repo.name")

    return RepositorySetupRequest(
        repo_root=repo_root.resolve(),
        repo_name=repo_name,
        config=config,
        github_authorization=owner.authorization_from_config(config),
        config_target=_config_target(repo_root, config_path),
    )


def setup_preview_collector(preview: RepositorySetupPreview) -> FileCollector:
    """Adapt an owner preview to the existing CLI summary renderer."""
    collector = FileCollector()
    for planned_file in preview.files:
        collector.add_write(
            planned_file.path,
            planned_file.content,
            planned_file.action,
            kind=planned_file.kind,
            agent=planned_file.agent,
        )
    for name, color, description in preview.labels:
        collector.add_label(name, color, description)
    return collector


def authorize_repository_setup(
    request: RepositorySetupRequest,
    preview: RepositorySetupPreview,
) -> RepositorySetupRequest:
    """Authorize only the config replacement shown in the confirmed preview."""
    replaces_config = any(
        planned_file.kind == "config" and planned_file.action == "overwrite"
        for planned_file in preview.files
    )
    return replace(request, replace_existing=replaces_config)


def apply_cli_repository_setup(
    *,
    owner: RepositorySetupOwner,
    request: RepositorySetupRequest,
    prompter: Prompter,
) -> RepositorySetupResult:
    """Execute through the shared owner and present its typed outcome."""
    try:
        result = owner.execute(request)
    except RepositorySetupConflictError as exc:
        prompter.print("\n✗ Repository setup changed after preview.")
        prompter.print(f"  Detail: {exc}")
        prompter.print("  Rerun setup to review the current file plan.")
        raise SystemExit(1) from exc
    except RepositorySetupExecutionError as exc:
        prompter.print(f"\n✗ Repository setup failed during {exc.stage}.")
        prompter.print(f"  Detail: {exc.detail}")
        for path in exc.applied_files:
            prompter.print(f"  Already written: {path}")
        for label in exc.created_labels:
            prompter.print(f"  Already created label: {label}")
        if exc.stage == "labels":
            prompter.print(
                "  Verify repository authentication, then rerun "
                "`issue-orchestrator doctor`."
            )
        raise SystemExit(1) from exc

    for path in result.written_files:
        prompter.print(f"  ✓ Wrote {path}")
    if result.created_labels:
        prompter.print(f"  ✓ Created {len(result.created_labels)} GitHub labels")
    return result


def _config_target(
    repo_root: Path,
    config_path: Path,
) -> RepositorySetupConfigTarget:
    resolved_root = repo_root.resolve()
    resolved_path = config_path.resolve()
    try:
        name = RepositoryConfigName(resolved_path.name)
    except ValueError:
        pass
    else:
        if get_config_path(resolved_root, name.value).resolve() == resolved_path:
            return RepositorySetupNamedConfig(name)
    return RepositorySetupExplicitConfig(resolved_path)


__all__ = [
    "apply_cli_repository_setup",
    "authorize_repository_setup",
    "build_cli_repository_setup_request",
    "setup_preview_collector",
]
