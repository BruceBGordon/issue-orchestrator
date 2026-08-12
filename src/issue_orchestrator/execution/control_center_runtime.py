"""Shared Control Center runtime and identity helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.repository_launch_selection import (
    RepositoryConfigurationIdentity,
    RepositoryLaunchSelection,
)
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.orchestrator_http_api import probe_orchestrator_json
from ..infra.repo_identity import (
    RepoIdentity,
    build_repo_identity_with_status,
    diff_repo_identity,
)

LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS = 45

if TYPE_CHECKING:
    from ..ports.repository_engine_supervisor import SupervisorOps, SupervisorStatus


@dataclass(frozen=True, slots=True)
class RepositoryOrchestratorOwnership:
    """Live port probes partitioned against one requested selection."""

    requested: RepositoryLaunchSelection
    matching: tuple[dict[str, Any], ...]
    conflicting: tuple[dict[str, Any], ...]

    @property
    def all(self) -> tuple[dict[str, Any], ...]:
        return self.matching + self.conflicting


def build_repo_identity(repo_root: Path) -> RepoIdentity:
    """Build repo identity with execution-layer git status resolution."""
    git = GitWorkingCopy()

    def _resolve_repo_status(root: Path) -> tuple[str | None, list[str]]:
        branch: str | None = None
        try:
            branch = git.get_current_branch(root)
        except Exception:
            branch = None
        dirty_lines = git.get_status_porcelain_lines(root)
        return branch, dirty_lines

    return build_repo_identity_with_status(
        repo_root, status_resolver=_resolve_repo_status
    )


def get_selected_launch_selection(repo_root: Path) -> RepositoryLaunchSelection:
    """Return the complete desired launch selection for a repository."""
    from ..infra.repo_registry import load_registry

    registry = load_registry()
    normalized = str(repo_root.resolve())
    for repo in registry.repos:
        if repo.path == normalized:
            return repo.launch_selection
    return RepositoryLaunchSelection.default()


def get_selected_config(repo_root: Path) -> str:
    """Return the selected config name for compatibility call sites."""
    return get_selected_launch_selection(repo_root).config.value


def get_effective_launch_selection(
    repo_root: Path,
    supervisor: SupervisorOps,
) -> RepositoryLaunchSelection:
    """Resolve the live engine selection, or desired selection when stopped."""
    live = _get_live_configuration_identity(repo_root, supervisor)
    if live is not None:
        return live.selection
    return get_selected_launch_selection(repo_root)


def get_effective_configuration_identity(
    repo_root: Path,
    supervisor: SupervisorOps,
) -> RepositoryConfigurationIdentity:
    """Resolve one active mode/config/fingerprint, or desired file identity."""
    live = _get_live_configuration_identity(repo_root, supervisor)
    if live is not None:
        return live
    desired = get_selected_launch_selection(repo_root)
    config = load_config_for_selection(repo_root, desired)
    return RepositoryConfigurationIdentity(
        selection=desired,
        fingerprint=config.config_fingerprint,
    )


def _get_live_configuration_identity(
    repo_root: Path,
    supervisor: SupervisorOps,
) -> RepositoryConfigurationIdentity | None:
    """Return the exact active identity without interpreting stopped config files."""
    desired = get_selected_launch_selection(repo_root)
    live = list(live_repository_engine_statuses(repo_root, supervisor, desired))
    if live:
        selection = _selection_from_live_statuses(live)
        fingerprints = {status.config_fingerprint for status in live}
        if len(fingerprints) != 1:
            raise RuntimeError("Conflicting live repository config fingerprints")
        return RepositoryConfigurationIdentity(
            selection=selection,
            fingerprint=fingerprints.pop(),
        )

    detected = detect_repository_orchestrators(repo_root)
    if not detected:
        return None
    identities = {
        (
            str(item.get("info", {}).get("configuration_mode", "")),
            str(item.get("info", {}).get("config_name", "")),
        )
        for item in detected
    }
    if len(identities) != 1:
        raise RuntimeError("Conflicting live repository configuration identities")
    mode, config_name = identities.pop()
    fingerprints = {
        str(item.get("info", {}).get("config_fingerprint", "")) for item in detected
    }
    if len(fingerprints) != 1:
        raise RuntimeError("Conflicting live repository config fingerprints")
    return RepositoryConfigurationIdentity(
        selection=RepositoryLaunchSelection.parse(
            mode=mode,
            config_name=config_name,
        ),
        fingerprint=fingerprints.pop(),
    )


def live_repository_engine_statuses(
    repo_root: Path,
    supervisor: SupervisorOps,
    selection: RepositoryLaunchSelection,
) -> tuple[SupervisorStatus, ...]:
    """Return authoritative live lock statuses before any port discovery."""
    aggregate = supervisor.status_all_instances(
        repo_root,
        selection.config.value,
        mode=selection.mode.value,
    )
    live = tuple(
        instance
        for instance in aggregate.instances
        if instance.state not in {"stopped", "failed"}
    )
    if live:
        return live
    single = supervisor.status(repo_root)
    return (single,) if single.state not in {"stopped", "failed"} else ()


def _selection_from_live_statuses(
    statuses: list[SupervisorStatus],
) -> RepositoryLaunchSelection:
    identities = {
        (status.configuration_mode, status.config_name) for status in statuses
    }
    if len(identities) != 1:
        raise RuntimeError("Conflicting live repository configuration identities")
    mode, config_name = identities.pop()
    return RepositoryLaunchSelection.parse(mode=mode, config_name=config_name)


def load_config_for_selection(
    repo_root: Path,
    selection: RepositoryLaunchSelection,
):
    """Load exactly one typed repository mode/config selection."""
    from ..infra.config import Config, get_config_path

    return Config.load(
        get_config_path(
            repo_root,
            selection.config.value,
            selection.mode,
        )
    )


def load_selected_config(repo_root: Path):
    """Load the registry-owned desired configuration for a repository."""
    return load_config_for_selection(
        repo_root,
        get_selected_launch_selection(repo_root),
    )


def _load_config_port(
    repo_root: Path,
    config_name: str | None,
    mode: str | None,
) -> int | None:
    """Load the web port from a repo config."""
    from ..infra.config import Config, get_config_path

    selection = RepositoryLaunchSelection.parse(mode=mode, config_name=config_name)
    config_path = get_config_path(
        repo_root,
        selection.config.value,
        selection.mode,
    )
    if not config_path.exists():
        return None
    try:
        config = Config.load(config_path)
    except Exception:
        return None
    return config.web_port


def client_dashboard_url(port: int | None) -> str | None:
    """Resolve the browser-facing dashboard URL for a repo engine port."""
    if port is None or port == 0:
        return None

    from ..infra.client_urls import resolve_client_dashboard_url

    return resolve_client_dashboard_url(port)


def detect_orchestrator_by_port(
    repo_root: Path,
    config_name: str | None,
    *,
    mode: str | None = None,
    expected_identity: RepoIdentity | None = None,
) -> dict[str, Any] | None:
    """Detect an orchestrator by probing the configured port."""
    port = _load_config_port(repo_root, config_name, mode)
    if not port:
        return None

    details = inspect_orchestrator_at_port(
        repo_root,
        port,
        expected_identity=expected_identity,
    )
    if details is None or details["info"].get("repo_root") != str(repo_root):
        return None
    return details


def inspect_orchestrator_at_port(
    repo_root: Path,
    port: int,
    *,
    expected_identity: RepoIdentity | None = None,
) -> dict[str, Any] | None:
    """Inspect one known runtime port without inferring lifecycle ownership."""

    base_url = f"http://127.0.0.1:{port}"
    info = _read_json(f"{base_url}/api/info", timeout=0.6)
    if info is None:
        return None

    details: dict[str, Any] = {"port": port, "info": info}
    observed_root = info.get("repo_root")
    if observed_root != str(repo_root):
        details["identity_mismatch"] = {
            "repo_root": {
                "expected": str(repo_root),
                "observed": observed_root,
            }
        }
    annotate_identity_mismatch(details, info, expected_identity)
    _annotate_orchestrator_health(details, base_url)
    return details


def detect_repository_orchestrators(repo_root: Path) -> list[dict[str, Any]]:
    """Probe every launchable repo config and return distinct live engines.

    Registry selection is desired state, not runtime ownership.  A lifecycle
    guard therefore has to inspect every configured port rather than only the
    currently selected mode/config pair.
    """
    from ..infra.config import list_configs, list_modes

    detected_by_port: dict[int, dict[str, Any]] = {}
    for mode in list_modes(repo_root):
        for config_name in list_configs(repo_root, mode):
            detected = detect_orchestrator_by_port(
                repo_root,
                config_name,
                mode=mode,
            )
            if detected is None:
                continue
            port = detected["port"]
            detected.setdefault(
                "probed_selection",
                RepositoryLaunchSelection.parse(
                    mode=mode,
                    config_name=config_name,
                ).to_dict(),
            )
            detected_by_port.setdefault(port, detected)
    return [detected_by_port[port] for port in sorted(detected_by_port)]


def inspect_repository_orchestrator_ownership(
    repo_root: Path,
    requested: RepositoryLaunchSelection,
) -> RepositoryOrchestratorOwnership:
    """Classify every live engine as matching or conflicting ownership."""
    matching: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []
    for detected in detect_repository_orchestrators(repo_root):
        info = detected.get("info", {})
        probed = detected.get("probed_selection", {})
        active = RepositoryLaunchSelection.parse(
            mode=info.get("configuration_mode") or probed.get("mode"),
            config_name=info.get("config_name") or probed.get("config_name"),
        )
        detected["active_selection"] = active.to_dict()
        (matching if active == requested else conflicting).append(detected)
    return RepositoryOrchestratorOwnership(
        requested=requested,
        matching=tuple(matching),
        conflicting=tuple(conflicting),
    )


def annotate_identity_mismatch(
    details: dict[str, Any],
    info: dict[str, Any],
    expected_identity: RepoIdentity | None,
) -> None:
    """Attach identity drift details when the observed engine differs."""
    if expected_identity is None:
        return
    observed_identity_payload = info.get("repo_identity")
    if not isinstance(observed_identity_payload, dict):
        return
    observed_identity = RepoIdentity(
        repo_root=str(observed_identity_payload.get("repo_root", "")),
        commit_sha=(
            str(observed_identity_payload["commit_sha"])
            if observed_identity_payload.get("commit_sha")
            else None
        ),
        branch=(
            str(observed_identity_payload["branch"])
            if observed_identity_payload.get("branch")
            else None
        ),
        working_tree_dirty=bool(
            observed_identity_payload.get("working_tree_dirty", False)
        ),
        dirty_fingerprint=(
            str(observed_identity_payload["dirty_fingerprint"])
            if observed_identity_payload.get("dirty_fingerprint")
            else None
        ),
        source_root=(
            str(observed_identity_payload["source_root"])
            if observed_identity_payload.get("source_root")
            else None
        ),
    )
    identity_mismatch = diff_repo_identity(expected_identity, observed_identity)
    for volatile_field in ("working_tree_dirty", "dirty_fingerprint"):
        identity_mismatch.pop(volatile_field, None)
    if identity_mismatch:
        details["identity_mismatch"] = identity_mismatch
        details["observed_identity"] = observed_identity.to_dict()
        details["expected_identity"] = expected_identity.to_dict()


def _annotate_orchestrator_health(details: dict[str, Any], base_url: str) -> None:
    status_data = _read_json(f"{base_url}/api/status", timeout=0.6)
    if status_data is None:
        details.setdefault("health", "unknown")
        return

    details["status"] = status_data
    last_tick = status_data.get("last_tick_time")
    if not isinstance(last_tick, (int, float)) or last_tick <= 0:
        return
    tick_age = time.time() - last_tick
    details["tick_age_seconds"] = tick_age
    details["health"] = "stale" if tick_age > 120 else "ok"


def confirm_orchestrator_at_port(repo_root: Path, port: int) -> bool:
    """Confirm the orchestrator at a port belongs to the repo_root."""
    info = _read_json(f"http://127.0.0.1:{port}/api/info", timeout=0.6)
    return info is not None and info.get("repo_root") == str(repo_root)


def is_shutdown_complete(port: int | None) -> bool:
    """Check if an orchestrator is in shutdown-complete state."""
    if not port:
        return False
    data = _read_json(f"http://127.0.0.1:{port}/api/status", timeout=2.0)
    if data is None:
        return False
    shutdown_requested = data.get("shutdown_requested", False)
    active_sessions = data.get("active_sessions", [])
    return shutdown_requested and len(active_sessions) == 0


def _read_json(url: str, *, timeout: float) -> dict[str, Any] | None:
    """Probe a local orchestrator endpoint via the execution-owned HTTP adapter."""
    return probe_orchestrator_json(url, timeout_seconds=timeout)


def heartbeat_age_seconds(iso_timestamp: str | None) -> int | None:
    """Return heartbeat age in seconds for an ISO timestamp."""
    if not iso_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def enrich_runtime_health(
    repo_path: Path,
    status_payload: dict[str, Any] | None,
    *,
    orphaned: bool = False,
    instance_id: str | None = None,
) -> dict[str, Any] | None:
    """Annotate runtime payloads with lock heartbeat and health state."""
    if status_payload is None:
        return None

    from ..infra.repo_lock import read_lock

    lock_info = read_lock(repo_path, instance_id=instance_id)
    last_heartbeat_at = lock_info.last_heartbeat_at if lock_info is not None else None
    heartbeat_age = heartbeat_age_seconds(last_heartbeat_at)
    status_payload["last_heartbeat_at"] = last_heartbeat_at
    status_payload["heartbeat_age_seconds"] = heartbeat_age

    if orphaned:
        status_payload["runtime_health"] = "orphaned"
        return status_payload

    state = status_payload.get("state")
    if state == "failed":
        status_payload["runtime_health"] = "stale_lock"
        return status_payload
    if (
        state == "running"
        and heartbeat_age is not None
        and heartbeat_age > LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS
    ):
        status_payload["runtime_health"] = "unresponsive"
        status_payload["unresponsive"] = True
        return status_payload
    if state == "running":
        status_payload["runtime_health"] = "healthy"
        status_payload["unresponsive"] = False
        return status_payload
    status_payload["runtime_health"] = "not_running"
    return status_payload


__all__ = [
    "LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS",
    "annotate_identity_mismatch",
    "build_repo_identity",
    "client_dashboard_url",
    "confirm_orchestrator_at_port",
    "detect_orchestrator_by_port",
    "detect_repository_orchestrators",
    "enrich_runtime_health",
    "get_effective_launch_selection",
    "get_effective_configuration_identity",
    "get_selected_config",
    "heartbeat_age_seconds",
    "is_shutdown_complete",
    "inspect_orchestrator_at_port",
    "inspect_repository_orchestrator_ownership",
    "live_repository_engine_statuses",
    "RepositoryOrchestratorOwnership",
]
