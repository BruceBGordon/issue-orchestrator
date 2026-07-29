"""Repository setup command and execution owner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..domain.repository_config_name import RepositoryConfigName
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupFileSystem,
    RepositorySetupFileSystemError,
    RepositorySetupHostFactory,
    RepositorySetupPlannedFile,
)

TECH_LEAD_AGENT_LABEL = "agent:tech-lead"
TECH_LEAD_PROMPT_PATH = ".io/tech-lead.md"
WORKER_PROMPT_PATH = ".io/dev.md"

_SUPPORTED_MODELS = frozenset({"haiku", "sonnet", "opus"})
RepositorySetupLabel = tuple[str, str, str]
RepositorySetupLabelPlanner = Callable[
    [Mapping[str, Any]],
    Sequence[RepositorySetupLabel],
]


@dataclass(frozen=True)
class RepositorySetupCommand:
    """Typed choices required to preview or execute repository setup."""

    repo_root: Path
    repo_name: str
    worker_agent_label: str
    model: str
    configure_tech_lead: bool = True
    config_name: RepositoryConfigName = RepositoryConfigName("default.yaml")
    create_prompts: bool = True
    create_labels: bool = True
    replace_existing: bool = False

    def __post_init__(self) -> None:
        if not self.repo_root.is_absolute():
            raise ValueError("repo_root must be absolute")
        if not self.repo_name.strip():
            raise ValueError("repo_name is required")
        if not self.worker_agent_label.startswith("agent:"):
            raise ValueError("worker_agent_label must start with 'agent:'")
        if self.worker_agent_label == TECH_LEAD_AGENT_LABEL:
            raise ValueError("worker_agent_label must identify a worker, not the tech lead")
        if self.model not in _SUPPORTED_MODELS:
            raise ValueError(
                f"model must be one of {sorted(_SUPPORTED_MODELS)}, got {self.model!r}"
            )

    def build_config(self) -> dict[str, Any]:
        """Build the canonical setup config without touching external systems."""
        agents: dict[str, dict[str, Any]] = {
            self.worker_agent_label: {
                "prompt": WORKER_PROMPT_PATH,
                "provider": "claude-code",
                "model": self.model,
                "ai_system": "claude-code",
            }
        }
        config: dict[str, Any] = {
            "repo": {"name": self.repo_name},
            "agents": agents,
        }

        if self.configure_tech_lead:
            agents[TECH_LEAD_AGENT_LABEL] = {
                "prompt": TECH_LEAD_PROMPT_PATH,
                "provider": "claude-code",
                "model": self.model,
                "ai_system": "claude-code",
            }
            config["review"] = {
                "tech_lead_review_agent": TECH_LEAD_AGENT_LABEL,
                "tech_lead_follow_up_agent": self.worker_agent_label,
                "tech_lead_review_label": "needs-tech-lead-review",
                "tech_lead_reviewed_label": "tech-lead-reviewed",
                "tech_lead_failed_label": "tech-lead-failed",
                "tech_lead_review_threshold": 0,
                "tech_lead_review_on_failure": True,
            }

        return config


@dataclass(frozen=True, slots=True)
class RepositorySetupPreview:
    """Rendered setup output before any mutation occurs."""

    yaml: str
    files: tuple[RepositorySetupPlannedFile, ...]


@dataclass(frozen=True, slots=True)
class RepositorySetupResult:
    """Complete, successful repository setup outcome."""

    config_path: Path
    written_files: tuple[Path, ...]
    created_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositorySetupConflictError(Exception):
    """An existing config requires explicit replacement confirmation."""

    config_path: Path

    def __str__(self) -> str:
        return f"Setup would replace existing config: {self.config_path}"


@dataclass(frozen=True, slots=True)
class RepositorySetupExecutionError(Exception):
    """Setup stopped at one stage and reports every mutation already applied."""

    stage: str
    detail: str
    applied_files: tuple[Path, ...] = ()
    created_labels: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"Repository setup failed during {self.stage}: {self.detail}"


@dataclass(frozen=True, slots=True)
class _RepositorySetupLabelError(Exception):
    cause: Exception
    created_labels: tuple[str, ...]


class RepositorySetupOwner:
    """Own preview, replacement policy, file writes, and label mutations."""

    def __init__(
        self,
        *,
        file_system: RepositorySetupFileSystem,
        repository_host_factory: RepositorySetupHostFactory,
        label_planner: RepositorySetupLabelPlanner,
    ) -> None:
        self._file_system = file_system
        self._repository_host_factory = repository_host_factory
        self._label_planner = label_planner

    def preview(self, command: RepositorySetupCommand) -> RepositorySetupPreview:
        """Build the exact filesystem plan without applying it."""
        plan = self._plan(command)
        return RepositorySetupPreview(yaml=plan.config_yaml, files=plan.files)

    def execute(self, command: RepositorySetupCommand) -> RepositorySetupResult:
        """Apply one setup command or fail with a typed partial outcome."""
        config = command.build_config()
        try:
            plan = self._file_system.plan(
                repo_root=command.repo_root,
                config_name=command.config_name.value,
                config=config,
                include_prompts=command.create_prompts,
            )
        except Exception as exc:
            raise RepositorySetupExecutionError(
                stage="planning",
                detail=str(exc),
            ) from exc

        config_path = self._config_path(plan)
        config_file = next(file for file in plan.files if file.kind == "config")
        if config_file.action == "overwrite" and not command.replace_existing:
            raise RepositorySetupConflictError(config_path)

        try:
            written_files = self._file_system.apply(plan)
        except RepositorySetupFileSystemError as exc:
            raise RepositorySetupExecutionError(
                stage="files",
                detail=str(exc),
                applied_files=exc.applied_paths,
            ) from exc
        except Exception as exc:
            raise RepositorySetupExecutionError(
                stage="files",
                detail=str(exc),
            ) from exc

        created_labels: list[str] = []
        if command.create_labels:
            try:
                created_labels.extend(self._create_labels(command.repo_name, config))
            except _RepositorySetupLabelError as exc:
                raise RepositorySetupExecutionError(
                    stage="labels",
                    detail=str(exc.cause),
                    applied_files=written_files,
                    created_labels=exc.created_labels,
                ) from exc
            except Exception as exc:
                raise RepositorySetupExecutionError(
                    stage="labels",
                    detail=str(exc),
                    applied_files=written_files,
                    created_labels=tuple(created_labels),
                ) from exc

        return RepositorySetupResult(
            config_path=config_path,
            written_files=written_files,
            created_labels=tuple(created_labels),
        )

    def _plan(self, command: RepositorySetupCommand) -> RepositorySetupArtifactPlan:
        return self._file_system.plan(
            repo_root=command.repo_root,
            config_name=command.config_name.value,
            config=command.build_config(),
            include_prompts=command.create_prompts,
        )

    @staticmethod
    def _config_path(plan: RepositorySetupArtifactPlan) -> Path:
        config_paths = [file.path for file in plan.files if file.kind == "config"]
        if len(config_paths) != 1:
            raise RuntimeError(
                f"Repository setup plan requires one config file, got {config_paths}"
            )
        return config_paths[0]

    def _create_labels(
        self,
        repo_name: str,
        config: Mapping[str, Any],
    ) -> tuple[str, ...]:
        host = self._repository_host_factory(repo_name)
        existing = {
            name
            for label in host.list_labels()
            if isinstance((name := label.get("name")), str)
        }
        created: list[str] = []
        for name, color, description in self._label_planner(config):
            if name in existing:
                continue
            try:
                host.create_label(
                    name,
                    color=color,
                    description=description,
                    force=True,
                )
            except Exception as exc:
                raise _RepositorySetupLabelError(
                    cause=exc,
                    created_labels=tuple(created),
                ) from exc
            existing.add(name)
            created.append(name)
        return tuple(created)


__all__ = [
    "RepositorySetupCommand",
    "RepositorySetupConflictError",
    "RepositorySetupExecutionError",
    "RepositorySetupOwner",
    "RepositorySetupPreview",
    "RepositorySetupResult",
    "TECH_LEAD_AGENT_LABEL",
    "TECH_LEAD_PROMPT_PATH",
    "WORKER_PROMPT_PATH",
]
