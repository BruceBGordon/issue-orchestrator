"""Repository setup command and execution owner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Literal, Mapping, Sequence

from ..domain.repository_config_name import RepositoryConfigName
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupConfigTarget,
    RepositorySetupFileSystem,
    RepositorySetupFileSystemError,
    RepositorySetupHostFactory,
    RepositorySetupNamedConfig,
    RepositorySetupPlannedFile,
)

TECH_LEAD_AGENT_LABEL = "agent:tech-lead"
TECH_LEAD_PROMPT_PATH = ".io/tech-lead.md"
WORKER_PROMPT_PATH = ".io/dev.md"

_SUPPORTED_MODELS = frozenset({"haiku", "sonnet", "opus"})
_WORKER_AGENT_LABEL_PATTERN = re.compile(r"agent:(?!tech-lead$).+")
RepositorySetupLabel = tuple[str, str, str]
RepositorySetupStage = Literal["planning", "files", "labels"]
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
        if _WORKER_AGENT_LABEL_PATTERN.fullmatch(self.worker_agent_label) is None:
            raise ValueError(
                "worker_agent_label must match 'agent:<worker>' and cannot be "
                "'agent:tech-lead'"
            )
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

    def to_request(self) -> RepositorySetupRequest:
        """Translate simplified setup choices into the shared owner request."""
        return RepositorySetupRequest(
            repo_root=self.repo_root,
            repo_name=self.repo_name,
            config=self.build_config(),
            config_target=RepositorySetupNamedConfig(self.config_name),
            create_prompts=self.create_prompts,
            create_labels=self.create_labels,
            replace_existing=self.replace_existing,
        )


@dataclass(frozen=True)
class RepositorySetupRequest:
    """A complete config plus the mutation choices owned by repository setup."""

    repo_root: Path
    repo_name: str
    config: Mapping[str, Any]
    config_target: RepositorySetupConfigTarget = RepositorySetupNamedConfig(
        RepositoryConfigName.default()
    )
    create_prompts: bool = True
    create_labels: bool = True
    replace_existing: bool = False

    def __post_init__(self) -> None:
        if not self.repo_root.is_absolute():
            raise ValueError("repo_root must be absolute")
        if not self.repo_name.strip():
            raise ValueError("repo_name is required")
        object.__setattr__(self, "config", deepcopy(dict(self.config)))


@dataclass(frozen=True, slots=True)
class RepositorySetupPreview:
    """Rendered setup output before any mutation occurs."""

    yaml: str
    files: tuple[RepositorySetupPlannedFile, ...]
    labels: tuple[RepositorySetupLabel, ...]


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

    stage: RepositorySetupStage
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

    def preview(self, request: RepositorySetupRequest) -> RepositorySetupPreview:
        """Build the exact filesystem plan without applying it."""
        plan = self._plan(request)
        return RepositorySetupPreview(
            yaml=plan.config_yaml,
            files=plan.files,
            labels=tuple(self._label_planner(request.config)),
        )

    def execute(self, request: RepositorySetupRequest) -> RepositorySetupResult:
        """Apply one setup command or fail with a typed partial outcome."""
        config = request.config
        try:
            plan = self._file_system.plan(
                repo_root=request.repo_root,
                config_target=request.config_target,
                config=config,
                include_prompts=request.create_prompts,
            )
        except Exception as exc:
            raise RepositorySetupExecutionError(
                stage="planning",
                detail=str(exc),
            ) from exc

        config_path = self._config_path(plan)
        config_file = next(file for file in plan.files if file.kind == "config")
        if config_file.action == "overwrite" and not request.replace_existing:
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
        if request.create_labels:
            try:
                created_labels.extend(self._create_labels(request.repo_name, config))
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

    def _plan(self, request: RepositorySetupRequest) -> RepositorySetupArtifactPlan:
        return self._file_system.plan(
            repo_root=request.repo_root,
            config_target=request.config_target,
            config=request.config,
            include_prompts=request.create_prompts,
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
    "RepositorySetupRequest",
    "RepositorySetupResult",
    "RepositorySetupStage",
    "TECH_LEAD_AGENT_LABEL",
    "TECH_LEAD_PROMPT_PATH",
    "WORKER_PROMPT_PATH",
]
