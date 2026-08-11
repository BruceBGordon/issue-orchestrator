"""Repository setup command and execution owner."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Literal, Mapping, Sequence

from ..domain.repository_setup_auth import (
    RepositorySetupGitHubAuthorization,
)
from ..domain.repository_config_name import RepositoryConfigName
from ..domain.worktree_paths import (
    default_worktree_base_config,
    resolve_worktree_base,
)
from ..ports.repository_setup import (
    RepositorySetupArtifactPlan,
    RepositorySetupConfigTarget,
    RepositorySetupFileSystem,
    RepositorySetupFileSystemError,
    RepositorySetupGitHubAuthorizationCodec,
    RepositorySetupGitHubVerification,
    RepositorySetupGitHubVerifier,
    RepositorySetupHostFactory,
    RepositorySetupNamedConfig,
    RepositorySetupPlannedFile,
)

TECH_LEAD_AGENT_LABEL = "agent:tech-lead"
TECH_LEAD_PROMPT_PATH = ".io/tech-lead.md"
REVIEWER_AGENT_LABEL = "agent:reviewer"
REVIEWER_PROMPT_PATH = ".io/reviewer.md"
WORKER_PROMPT_PATH = ".io/dev.md"
INTERNAL_REVIEW_PROMPT_PATH = ".io/internal-review.md"

_SUPPORTED_MODELS = frozenset({"haiku", "sonnet", "opus"})
_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_WORKER_AGENT_LABEL_PATTERN = re.compile(r"agent:(?!(?:reviewer|tech-lead)$).+")
RepositorySetupLabel = tuple[str, str, str]
RepositorySetupStage = Literal["authorization", "planning", "files", "labels"]
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
    validation_quick_command: str
    validation_publish_command: str
    effort: str = "high"
    configure_reviewer: bool = True
    reviewer_model: str = "sonnet"
    reviewer_effort: str = "high"
    configure_internal_reviewer: bool = False
    internal_review_max_rounds: int = 5
    internal_review_instructions: str = INTERNAL_REVIEW_PROMPT_PATH
    worktree_base: str | None = None
    github_authorization: RepositorySetupGitHubAuthorization = (
        RepositorySetupGitHubAuthorization(kind="detected")
    )
    configure_tech_lead: bool = True
    tech_lead_model: str = "sonnet"
    tech_lead_effort: str = "high"
    tech_lead_review_threshold: int = 1
    config_name: RepositoryConfigName = RepositoryConfigName("default.yaml")
    create_prompts: bool = True
    create_labels: bool = True
    replace_existing: bool = False

    def __post_init__(self) -> None:
        self._validate_repository_choices()
        self._validate_agent_choices()
        self._validate_pipeline_choices()

    def _validate_repository_choices(self) -> None:
        """Validate repository identity and filesystem selections."""
        if not self.repo_root.is_absolute():
            raise ValueError("repo_root must be absolute")
        if not self.repo_name.strip():
            raise ValueError("repo_name is required")
        if _WORKER_AGENT_LABEL_PATTERN.fullmatch(self.worker_agent_label) is None:
            raise ValueError(
                "worker_agent_label must match 'agent:<worker>' and cannot be "
                "'agent:reviewer' or 'agent:tech-lead'"
            )
        if self.worktree_base is not None and not self.worktree_base.strip():
            raise ValueError("worktree_base is required")

    def _validate_agent_choices(self) -> None:
        """Validate every generated Claude role profile."""
        for role, model in (
            ("worker", self.model),
            ("reviewer", self.reviewer_model),
            ("tech lead", self.tech_lead_model),
        ):
            if model not in _SUPPORTED_MODELS:
                raise ValueError(
                    f"{role} model must be one of {sorted(_SUPPORTED_MODELS)}, "
                    f"got {model!r}"
                )
        for role, effort in (
            ("worker", self.effort),
            ("reviewer", self.reviewer_effort),
            ("tech lead", self.tech_lead_effort),
        ):
            if effort not in _SUPPORTED_EFFORTS:
                raise ValueError(
                    f"{role} effort must be one of {sorted(_SUPPORTED_EFFORTS)}, "
                    f"got {effort!r}"
                )

    def _validate_pipeline_choices(self) -> None:
        """Validate review cadence and its required validation gates."""
        if not 0 <= self.tech_lead_review_threshold <= 50:
            raise ValueError("tech_lead_review_threshold must be between 0 and 50")
        if not 1 <= self.internal_review_max_rounds <= 50:
            raise ValueError("internal_review_max_rounds must be between 1 and 50")
        instructions = self.internal_review_instructions.strip()
        configured_path = Path(instructions)
        if (
            not instructions
            or configured_path.is_absolute()
            or ".." in configured_path.parts
            or configured_path == Path(".")
        ):
            raise ValueError(
                "internal_review_instructions must be a contained repository-relative path"
            )
        for field, command in (
            ("validation_quick_command", self.validation_quick_command),
            ("validation_publish_command", self.validation_publish_command),
        ):
            if not command.strip():
                raise ValueError(f"{field} is required")

    def build_config(
        self,
        authorization_codec: RepositorySetupGitHubAuthorizationCodec,
    ) -> dict[str, Any]:
        """Build the canonical setup config without touching external systems."""
        agents: dict[str, dict[str, Any]] = {
            self.worker_agent_label: self._agent_config(
                prompt=WORKER_PROMPT_PATH,
                model=self.model,
                effort=self.effort,
            ),
        }
        repo: dict[str, Any] = {"name": self.repo_name}
        github = authorization_codec.to_config(self.github_authorization)
        if github:
            repo["github"] = github
        config: dict[str, Any] = {
            "repo": repo,
            "worktrees": {
                "base": self.worktree_base.strip()
                if self.worktree_base is not None
                else default_worktree_base_config(self.repo_root),
            },
            "agents": agents,
            "validation": {
                "quick": {
                    "cmd": self.validation_quick_command.strip(),
                    "timeout_seconds": 300,
                },
                "publish": {
                    "cmd": self.validation_publish_command.strip(),
                    "timeout_seconds": 1800,
                    "dirty_check": "tracked",
                },
            },
        }

        review: dict[str, Any] = {
            "enabled": self.configure_reviewer,
            "code_review_label": "needs-code-review",
            "code_reviewed_label": "code-reviewed",
            "max_rework_cycles": 5,
            "nits": {
                "default_policy": "surface",
                "by_agent": {},
            },
            "exchange": {
                "mode": "via-local-loop",
                "loop": {
                    "max_rounds": 10,
                    "max_no_progress": 2,
                    "require_validation": True,
                },
            },
            "internal": {
                "enabled": self.configure_internal_reviewer,
                "max_rounds": self.internal_review_max_rounds,
                "instructions": self.internal_review_instructions.strip(),
            },
        }
        config["review"] = review

        if self.configure_reviewer:
            agents[REVIEWER_AGENT_LABEL] = self._agent_config(
                prompt=REVIEWER_PROMPT_PATH,
                model=self.reviewer_model,
                effort=self.reviewer_effort,
            )
            review["default"] = REVIEWER_AGENT_LABEL

        if self.configure_tech_lead:
            config["tech_lead"] = {"enabled": True}
            agents[TECH_LEAD_AGENT_LABEL] = self._agent_config(
                prompt=TECH_LEAD_PROMPT_PATH,
                model=self.tech_lead_model,
                effort=self.tech_lead_effort,
            )
            review.update(
                {
                    "tech_lead_review_agent": TECH_LEAD_AGENT_LABEL,
                    "tech_lead_follow_up_agent": self.worker_agent_label,
                    "tech_lead_review_label": "needs-tech-lead-review",
                    "tech_lead_reviewed_label": "tech-lead-reviewed",
                    "tech_lead_failed_label": "tech-lead-failed",
                    "tech_lead_review_threshold": self.tech_lead_review_threshold,
                    "tech_lead_review_on_failure": True,
                }
            )

        return config

    @staticmethod
    def _agent_config(*, prompt: str, model: str, effort: str) -> dict[str, Any]:
        """Build one provider-correct Claude agent configuration."""
        return {
            "prompt": prompt,
            "provider": "claude-code",
            "model": model,
            "provider_args": {"effort": effort},
            "ai_system": "claude-code",
            "sandbox": True,
        }

    def to_request(
        self,
        authorization_codec: RepositorySetupGitHubAuthorizationCodec,
    ) -> RepositorySetupRequest:
        """Translate simplified setup choices into the shared owner request."""
        return RepositorySetupRequest(
            repo_root=self.repo_root,
            repo_name=self.repo_name,
            config=self.build_config(authorization_codec),
            github_authorization=self.github_authorization,
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
    github_authorization: RepositorySetupGitHubAuthorization
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
    worktree_base: Path
    github_authorization: RepositorySetupGitHubVerification
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
        github_verifier: RepositorySetupGitHubVerifier,
        github_authorization_codec: RepositorySetupGitHubAuthorizationCodec,
        label_planner: RepositorySetupLabelPlanner,
    ) -> None:
        self._file_system = file_system
        self._repository_host_factory = repository_host_factory
        self._github_verifier = github_verifier
        self._github_authorization_codec = github_authorization_codec
        self._label_planner = label_planner

    def request_from_command(
        self,
        command: RepositorySetupCommand,
    ) -> RepositorySetupRequest:
        """Encode one typed setup command through the owned authorization codec."""
        return command.to_request(self._github_authorization_codec)

    def authorization_from_config(
        self,
        config: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization:
        """Decode YAML-shaped authorization through the owner boundary."""
        return self._github_authorization_codec.from_config(config)

    def authorization_from_public(
        self,
        payload: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization:
        """Decode a browser authorization payload through the owner boundary."""
        return self._github_authorization_codec.from_public(payload)

    def authorization_to_public(
        self,
        authorization: RepositorySetupGitHubAuthorization,
        *,
        redact_inline_token: bool = False,
    ) -> dict[str, Any]:
        """Encode a browser-safe authorization payload through the owner boundary."""
        return self._github_authorization_codec.to_public(
            authorization,
            redact_inline_token=redact_inline_token,
        )

    def preview(self, request: RepositorySetupRequest) -> RepositorySetupPreview:
        """Build the exact filesystem plan without applying it."""
        verification = self._verify_github_authorization(request)
        plan = self._plan(request)
        return RepositorySetupPreview(
            yaml=plan.config_yaml,
            worktree_base=self._resolve_worktree_base(request),
            github_authorization=verification,
            files=plan.files,
            labels=tuple(self._label_planner(request.config)),
        )

    def verify_github_authorization(
        self,
        repo_name: str,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> RepositorySetupGitHubVerification:
        """Verify one explicit setup choice without planning or writing files."""
        if not repo_name.strip():
            raise ValueError("repo_name is required")
        return self._github_verifier(repo_name, authorization)

    def execute(self, request: RepositorySetupRequest) -> RepositorySetupResult:
        """Apply one setup command or fail with a typed partial outcome."""
        config = request.config
        try:
            self._verify_github_authorization(request)
        except Exception as exc:
            raise RepositorySetupExecutionError(
                stage="authorization",
                detail=str(exc),
            ) from exc
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
                authorization = request.github_authorization
                created_labels.extend(
                    self._create_labels(request.repo_name, config, authorization)
                )
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

    @staticmethod
    def _resolve_worktree_base(request: RepositorySetupRequest) -> Path:
        worktrees = request.config.get("worktrees")
        raw_base = worktrees.get("base") if isinstance(worktrees, Mapping) else None
        if raw_base is not None and not isinstance(raw_base, (str, Path)):
            raise ValueError("worktrees.base must be a path string")
        return resolve_worktree_base(raw_base, request.repo_root)

    def _create_labels(
        self,
        repo_name: str,
        config: Mapping[str, Any],
        authorization: RepositorySetupGitHubAuthorization,
    ) -> tuple[str, ...]:
        host = self._repository_host_factory(repo_name, authorization)
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

    def _verify_github_authorization(
        self,
        request: RepositorySetupRequest,
    ) -> RepositorySetupGitHubVerification:
        return self._github_verifier(
            request.repo_name,
            request.github_authorization,
        )


__all__ = [
    "RepositorySetupCommand",
    "RepositorySetupConflictError",
    "RepositorySetupExecutionError",
    "RepositorySetupOwner",
    "RepositorySetupPreview",
    "RepositorySetupRequest",
    "RepositorySetupResult",
    "RepositorySetupStage",
    "REVIEWER_AGENT_LABEL",
    "REVIEWER_PROMPT_PATH",
    "TECH_LEAD_AGENT_LABEL",
    "TECH_LEAD_PROMPT_PATH",
    "WORKER_PROMPT_PATH",
]
