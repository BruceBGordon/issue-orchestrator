"""Shared setup-wizard helpers for CLI and Control Center entrypoints."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping
from urllib.parse import urlparse

import yaml

from ..control.label_manager import (
    TECH_LEAD_NEEDS_HUMAN_LABEL,
    tech_lead_issue_label_metadata,
)
from ..execution.repository_setup_artifacts import (
    CONFIG_HEADER,
    plan_missing_setup_prompts,
    render_setup_config_yaml,
)
from ..infra.config_value_rules import resolve_tech_lead_watch_label
from ..execution.setup_wizard_prompts import (
    build_code_review_prompt_text,
    build_starter_prompt_text,
    build_tech_lead_review_prompt_text,
)

if TYPE_CHECKING:
    from ..infra.config import Config


@dataclass
class PlannedWrite:
    """A file write that would be performed."""

    path: Path
    content: str
    action: Literal["create", "overwrite"]
    kind: str | None = None
    agent: str | None = None

    def size_display(self) -> str:
        """Return a human-readable size."""
        size = len(self.content.encode("utf-8"))
        if size < 1024:
            return f"{size} B"
        return f"{size / 1024:.1f} KB"


class FileCollector:
    """Collect planned writes and label mutations for dry-run workflows."""

    def __init__(self) -> None:
        self.writes: list[PlannedWrite] = []
        self.labels: list[tuple[str, str, str]] = []

    def add_write(
        self,
        path: Path,
        content: str,
        action: Literal["create", "overwrite"] = "create",
        *,
        kind: str | None = None,
        agent: str | None = None,
    ) -> None:
        """Record a planned file write."""
        self.writes.append(
            PlannedWrite(
                path=path,
                content=content,
                action=action,
                kind=kind,
                agent=agent,
            )
        )

    def add_label(self, name: str, color: str, description: str) -> None:
        """Record a planned GitHub label creation."""
        self.labels.append((name, color, description))


def get_repository_host(repo_name: str):
    """Get a RepositoryHost for the given repo."""
    from ..execution.providers import create_repository_host

    return create_repository_host(repo=repo_name)


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout_s: int = 10,
) -> tuple[bool, str]:
    """Run a git command and return ``(success, output)``."""
    from ..execution.git_tools import run_git as run_git_impl

    return run_git_impl(args, cwd=cwd, timeout_s=timeout_s)


def detect_repo(cwd: Path | None = None) -> str | None:
    """Detect ``owner/repo`` from the current git origin remote."""
    ok, output = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if not ok:
        return None

    url = output.strip()
    return _repo_path_from_github_remote(url)


def _repo_path_from_github_remote(url: str) -> str | None:
    """Extract ``owner/repo`` from GitHub remote URL formats."""
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1].removesuffix(".git")

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        if parsed.hostname != "github.com":
            return None
        repo_path = parsed.path.lstrip("/")
        return repo_path.removesuffix(".git") or None

    if "github.com" not in url:
        return None
    repo_path = "/".join(url.split("/")[-2:])
    return repo_path.removesuffix(".git") or None


def fetch_github_labels(repo: str) -> list[str]:
    """Fetch label names from GitHub for the given repository."""
    try:
        labels = get_repository_host(repo).list_labels()
    except Exception:
        return []

    names: list[str] = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        name = label.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def find_existing_config(
    start_path: Path | None = None,
) -> tuple[Path | None, dict | None]:
    """Find an existing orchestrator config starting at ``start_path``."""
    from ..infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME

    if start_path is None:
        start_path = Path.cwd()

    candidates = [
        f"{CONFIG_DIR}/modes/default/{DEFAULT_CONFIG_NAME}",
        f"{CONFIG_DIR}/modes/default/*.yaml",
        f"{CONFIG_DIR}/{DEFAULT_CONFIG_NAME}",
        f"{CONFIG_DIR}/*.yaml",
    ]

    current = start_path
    while current != current.parent:
        for candidate in candidates:
            if "*" in candidate:
                matches = list(current.glob(candidate))
                if not matches:
                    continue
                config_path = matches[0]
            else:
                config_path = current / candidate
                if not config_path.exists():
                    continue
            try:
                with open(config_path) as handle:
                    return config_path, yaml.safe_load(handle)
            except yaml.YAMLError:
                continue
        current = current.parent

    return None, None


def find_existing_default_config(
    start_path: Path | None = None,
) -> tuple[Path | None, dict | None]:
    """Find the legacy default config used by the Control Center setup API."""
    from ..infra.config import find_config_file

    config_path = find_config_file(start_path)
    if config_path is None:
        return None, None

    try:
        with open(config_path) as handle:
            return config_path, yaml.safe_load(handle)
    except Exception:
        return config_path, None


def find_prompt_candidates(start_path: Path | None = None) -> list[Path]:
    """Find likely prompt markdown files in a repository."""
    if start_path is None:
        start_path = Path.cwd()

    candidates: list[Path] = []
    high_priority_patterns = [
        ".prompts/**/*.md",
        "**/prompts/*.md",
        ".issue-orchestrator/prompts/**/*.md",
        "**/*orchestrator*.md",
        "**/*-agent*.md",
        "**/*_agent*.md",
    ]

    for pattern in high_priority_patterns:
        for path in start_path.glob(pattern):
            if not path.is_file() or path in candidates:
                continue
            if any(part.startswith(".") or part == "node_modules" for part in path.parts):
                continue
            candidates.append(path)

    if candidates:
        return sorted(set(candidates))

    docs_patterns = [
        "docs/**/*ai*.md",
        "docs/**/*agent*.md",
        "docs/**/*claude*.md",
    ]
    for pattern in docs_patterns:
        for path in start_path.glob(pattern):
            if path.is_file() and path not in candidates:
                candidates.append(path)

    return sorted(set(candidates))


def render_config_yaml(
    config: Mapping[str, Any],
    *,
    include_header: bool = True,
) -> str:
    """Render config to YAML with stable formatting."""
    return render_setup_config_yaml(config, include_header=include_header)


def write_config(
    config: Mapping[str, Any],
    path: Path,
    file_collector: FileCollector | None = None,
    *,
    include_header: bool = True,
) -> None:
    """Write a config file or add it to a dry-run collector."""
    content = render_config_yaml(config, include_header=include_header)

    if file_collector is not None:
        action = "overwrite" if path.exists() else "create"
        file_collector.add_write(path, content, action, kind="config")
        return

    with open(path, "w") as handle:
        handle.write(content)


def create_starter_prompt(
    agent_name: str,
    path: Path,
    file_collector: FileCollector | None = None,
) -> None:
    """Create a starter prompt file for a work agent."""
    content = build_starter_prompt_text(agent_name.split(":")[-1])
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_code_review_prompt(
    path: Path,
    code_review_label: str,
    code_reviewed_label: str,
    file_collector: FileCollector | None = None,
    *,
    agent_name: str | None = None,
) -> None:
    """Create a code-review prompt file."""
    content = build_code_review_prompt_text(code_review_label, code_reviewed_label)
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_tech_lead_review_prompt(
    path: Path,
    review_label: str,
    reviewed_label: str,
    file_collector: FileCollector | None = None,
    *,
    agent_name: str | None = None,
) -> None:
    """Create a tech-lead-review prompt file."""
    content = build_tech_lead_review_prompt_text(review_label, reviewed_label)
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_missing_setup_prompts(
    config: Mapping[str, Any],
    repo_root: Path,
    file_collector: FileCollector | None = None,
) -> list[Path]:
    """Create or collect any missing prompt files referenced by the config."""
    planned = plan_missing_setup_prompts(config, repo_root)
    for prompt in planned:
        if file_collector is not None:
            file_collector.add_write(
                prompt.path,
                prompt.content,
                "create",
                kind="prompt",
                agent=prompt.agent,
            )
            continue
        prompt.path.parent.mkdir(parents=True, exist_ok=True)
        prompt.path.write_text(prompt.content)
    return [prompt.path for prompt in planned]


def plan_setup_labels(
    config: Mapping[str, Any],
    *,
    include_priority_labels: bool = True,
    include_review_labels_without_default: bool = False,
) -> list[tuple[str, str, str]]:
    """Return the label set for a setup-wizard config."""
    labels_config = config.get("labels", {}) or {}
    review_config = config.get("review", {}) or {}
    return _plan_setup_labels(
        labels_config=labels_config,
        review_config=review_config,
        agent_names=(config.get("agents", {}) or {}).keys(),
        include_priority_labels=include_priority_labels,
        include_review_labels_without_default=include_review_labels_without_default,
    )


def _plan_setup_labels(
    *,
    labels_config: Mapping[str, Any],
    review_config: Mapping[str, Any],
    agent_names: Iterable[str],
    include_priority_labels: bool = True,
    include_review_labels_without_default: bool = False,
) -> list[tuple[str, str, str]]:
    """Build setup labels for CLI and Control Center surfaces."""
    label_prefix = labels_config.get("prefix", "")

    def prefixed(label: str) -> str:
        return f"{label_prefix}:{label}" if label_prefix else label

    agent_labels = [
        (agent_name, "1D76DB", f"Issues for {agent_name.split(':')[-1]} agent")
        for agent_name in agent_names
    ]
    priority_labels = [
        ("priority:high", "D93F0B", "Urgent - do first"),
        ("priority:medium", "FBCA04", "Normal priority"),
        ("priority:low", "0E8A16", "Nice to have"),
    ]
    status_labels = [
        (
            prefixed(labels_config.get("in_progress", "in-progress")),
            "5319E7",
            "Agent is working on this",
        ),
        (
            prefixed(labels_config.get("blocked", "blocked")),
            "B60205",
            "Agent is blocked",
        ),
        (
            prefixed(labels_config.get("needs_human", "needs-human")),
            "FBCA04",
            "Agent needs human input",
        ),
        (
            prefixed(TECH_LEAD_NEEDS_HUMAN_LABEL),
            "D4C5F9",
            "Orchestrator-owned tech_lead needs-human provenance",
        ),
    ]

    all_labels = agent_labels + status_labels
    if include_priority_labels:
        all_labels.extend(priority_labels)

    code_review_agent = review_config.get("default")
    review_enabled = bool(review_config.get("enabled"))
    if code_review_agent or (include_review_labels_without_default and review_enabled):
        all_labels.extend(
            [
                (
                    review_config.get("code_review_label", "needs-code-review"),
                    "7057FF",
                    "PR needs code review",
                ),
                (
                    review_config.get("code_reviewed_label", "code-reviewed"),
                    "0E8A16",
                    "PR has been code reviewed",
                ),
            ]
        )

    tech_lead_review_agent = review_config.get("tech_lead_review_agent")
    if tech_lead_review_agent:
        from ..domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
            PROPOSED_TECH_LEAD_LABEL,
            TECH_LEAD_OBSERVATION_LABEL,
        )
        observation_color, observation_description = tech_lead_issue_label_metadata(
            TECH_LEAD_OBSERVATION_LABEL
        )
        tech_lead_watch_label = resolve_tech_lead_watch_label(
            review_config.get("tech_lead_review_label"),
            review_config.get("code_reviewed_label"),
        )

        all_labels.extend(
            [
                (
                    tech_lead_watch_label,
                    "7057FF",
                    "PR needs tech-lead review",
                ),
                (
                    review_config.get("tech_lead_reviewed_label", "tech-lead-reviewed"),
                    "1D76DB",
                    "PR has been tech_lead reviewed",
                ),
                (
                    review_config.get("tech_lead_failed_label", "tech-lead-failed"),
                    "B60205",
                    "Tech-lead review failed",
                ),
                # Gate label for act-level tech_lead proposals (#6779 R3): a fresh
                # install must provision it, else a proposal issue is created
                # with the tech_lead/filter labels but no blocking gate and becomes
                # schedulable as ordinary tech_lead work.
                (
                    PROPOSED_TECH_LEAD_LABEL,
                    "B60205",
                    "Tech Lead proposal awaiting operator approval",
                ),
                # Health-review anchor marker (ADR-0031 §4): same class of
                # orchestrator-managed workflow label; a fresh install needs it
                # so the marker is not silently dropped at anchor creation.
                (
                    HEALTH_REVIEW_MARKER_LABEL,
                    "C5DEF5",
                    "Periodic health-review anchor",
                ),
                (
                    TECH_LEAD_OBSERVATION_LABEL,
                    observation_color,
                    observation_description,
                ),
            ]
        )

    unique_labels: dict[str, tuple[str, str, str]] = {}
    for label in all_labels:
        unique_labels.setdefault(label[0], label)
    return list(unique_labels.values())


def required_repo_labels(config: "Config") -> list[str]:
    """The full label set the CLI ``init`` command provisions (single owner).

    Base workflow labels (including the orchestrator-owned ``tech-lead-needs-human``
    marker from the #6771 redesign) + priority tiers + configured worker agents
    + tech_lead workflow labels (the #6779 R3 proposal gate, the tech-lead-agent
    label, the health-review marker, and the #6781 observation marker) when
    tech_lead is configured. De-duped so labels appearing in more than one source
    (e.g. an agent that is also the tech lead agent) are not provisioned twice.
    """
    from ..control.label_manager import LabelManager

    lm = LabelManager(config)
    labels: list[str] = [
        lm.in_progress,
        lm.blocked,
        lm.needs_human,
        lm.tech_lead_needs_human,
        "priority:high",
        "priority:medium",
        "priority:low",
    ]
    labels.extend(config.agents.keys())
    if config.tech_lead_review_agent:
        from ..domain.tech_lead_session import (
            HEALTH_REVIEW_MARKER_LABEL,
            PROPOSED_TECH_LEAD_LABEL,
            TECH_LEAD_OBSERVATION_LABEL,
        )

        labels.extend(
            label
            for label in (
                config.tech_lead_review_agent,
                config.tech_lead_watch_label,
                config.tech_lead_reviewed_label,
                config.tech_lead_failed_label,
                PROPOSED_TECH_LEAD_LABEL,
                HEALTH_REVIEW_MARKER_LABEL,
                TECH_LEAD_OBSERVATION_LABEL,
            )
            if label
        )
    return list(dict.fromkeys(labels))


def load_config_for_repo(repo_root: Path | None) -> "Config | None":
    """Load the default config for a repo when one exists."""
    from ..infra.config import Config, DEFAULT_CONFIG_NAME, get_config_path, list_configs

    if repo_root is None:
        return None
    available = list_configs(repo_root)
    if not available:
        return None
    config_name = DEFAULT_CONFIG_NAME if DEFAULT_CONFIG_NAME in available else available[0]
    config_path = get_config_path(repo_root, config_name)
    try:
        return Config.load(config_path)
    except Exception:
        return None


def _probe_cli_version(executable: str, *, fallback: str) -> str:
    """Return ``<cli> --version`` output when available."""
    from ..execution.command_runner import LocalCommandRunner

    result = LocalCommandRunner().run([executable, "--version"], timeout_seconds=5)
    if result.returncode == 0:
        detail = result.stdout.strip() or result.stderr.strip()
        if detail:
            return detail
    return fallback


def _provider_cli_display(provider_name: str, executable: str) -> str:
    if executable == provider_name:
        return provider_name
    return f"{provider_name} via {executable}"


def _provider_cli_detail(detail: str, provider_name: str, executable: str) -> str:
    if executable == provider_name:
        return detail
    return f"{detail} (executable: {executable})"


def build_any_ai_provider_check() -> dict[str, Any]:
    """Check whether at least one registered AI provider CLI is available."""
    from issue_orchestrator.agent_runner import get_provider, list_providers

    providers = list_providers()
    available: list[str] = []
    for provider_name in providers:
        provider = get_provider(provider_name)
        executable = getattr(provider, "executable", provider_name)
        if provider.is_available():
            available.append(_provider_cli_display(provider_name, executable))

    if available:
        return {
            "ok": True,
            "detail": "Available: " + ", ".join(available),
        }
    return {
        "ok": False,
        "detail": "No AI provider CLIs found. Install one of: " + ", ".join(providers),
    }


def _build_provider_agent_check(
    *,
    label: str,
    provider_name: str,
    seen_executables: set[str],
) -> dict[str, Any] | None:
    from issue_orchestrator.agent_runner import get_provider

    from ..infra.provider_cli_diagnostics import provider_cli_missing_detail

    try:
        provider = get_provider(provider_name)
    except ValueError:
        return {
            "name": f"{provider_name} CLI",
            "ok": False,
            "detail": f"Unknown provider configured for {label}: {provider_name}",
        }

    executable = getattr(provider, "executable", provider_name)
    if executable in seen_executables:
        return None
    seen_executables.add(executable)
    if not provider.is_available():
        return {
            "name": f"{provider_name} CLI",
            "ok": False,
            "detail": provider_cli_missing_detail(provider_name, executable),
        }
    path = shutil.which(executable) or executable
    detail = provider.check_version() or path
    return {
        "name": f"{provider_name} CLI",
        "ok": True,
        "detail": _provider_cli_detail(detail, provider_name, executable),
    }


def build_agent_checks(config: "Config | None") -> list[dict[str, Any]]:
    """Check agent CLI availability for the supplied config."""
    if config is None:
        return [{
            "name": "Agent CLI",
            "ok": True,
            "detail": "Config not detected yet",
        }]

    checks: list[dict[str, Any]] = []
    seen_executables: set[str] = set()
    for label, agent_config in config.agents.items():
        provider_name = getattr(agent_config, "provider", None)
        default_agent = getattr(config, "default_agent", None)
        if provider_name is None and default_agent is not None:
            provider_name = getattr(default_agent, "provider", None)
        if provider_name:
            check = _build_provider_agent_check(
                label=label,
                provider_name=provider_name,
                seen_executables=seen_executables,
            )
            if check:
                checks.append(check)
            continue

        command = getattr(agent_config, "command", None) or ""
        executable = command.strip().split()[0] if command.strip() else ""
        if not executable:
            checks.append({
                "name": f"{label} CLI",
                "ok": False,
                "detail": "No command configured",
            })
            continue
        exec_name = executable.rsplit("/", 1)[-1]
        if exec_name in seen_executables:
            continue
        seen_executables.add(exec_name)
        path = shutil.which(exec_name)
        if not path:
            checks.append({
                "name": f"{exec_name} CLI",
                "ok": False,
                "detail": "Not found on PATH",
            })
            continue
        checks.append({
            "name": f"{exec_name} CLI",
            "ok": True,
            "detail": _probe_cli_version(path, fallback=path),
        })
    return checks


def build_github_auth_check(config: "Config | None") -> dict[str, Any]:
    """Validate GitHub auth using repo-specific config when available."""
    from ..execution.providers import validate_github_token

    auth_kwargs = config.github_auth_kwargs() if config else {}
    token_result = validate_github_token(
        **auth_kwargs,
        repo=getattr(config, "repo", None) if config else None,
        api_url=getattr(config, "github_api_url", "https://api.github.com")
        if config
        else "https://api.github.com",
    )
    if token_result.valid:
        detail = f"Authenticated as {token_result.username}"
        if config and getattr(config, "repo", None):
            detail += f" with access to {config.repo}"
        return {"ok": True, "detail": detail}
    return {"ok": False, "detail": token_result.error or "Unknown error"}


__all__ = [
    "CONFIG_HEADER",
    "FileCollector",
    "PlannedWrite",
    "build_agent_checks",
    "build_any_ai_provider_check",
    "build_code_review_prompt_text",
    "build_github_auth_check",
    "build_starter_prompt_text",
    "build_tech_lead_review_prompt_text",
    "create_code_review_prompt",
    "create_starter_prompt",
    "create_tech_lead_review_prompt",
    "detect_repo",
    "fetch_github_labels",
    "find_existing_config",
    "find_existing_default_config",
    "find_prompt_candidates",
    "get_repository_host",
    "load_config_for_repo",
    "plan_setup_labels",
    "render_config_yaml",
    "run_git",
    "write_config",
    "write_missing_setup_prompts",
]
