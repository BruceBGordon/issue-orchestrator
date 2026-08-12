"""Build Control Center repository status payloads."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..execution.control_center_runtime import (
    client_dashboard_url,
    detect_repository_orchestrators,
    enrich_runtime_health,
)
from ..execution.orchestrator_http_api import probe_orchestrator_json
from ..ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorOps,
    SupervisorStatus,
)

if TYPE_CHECKING:
    from ..infra.repo_registry import RegisteredRepo

logger = logging.getLogger(__name__)


def build_repos_status(
    *,
    supervisor: SupervisorOps,
    preferred_repo_root: Path | None,
) -> list[dict[str, Any]]:
    """Build status payloads for registered repositories."""
    from ..infra import repo_registry
    from ..infra.config import list_configs, list_modes

    preferred_repo = str(preferred_repo_root) if preferred_repo_root else None
    repos = repo_registry.list_repos()
    if preferred_repo and all(repo.path != preferred_repo for repo in repos):
        try:
            repo_registry.add_repo(preferred_repo)
            repos = repo_registry.list_repos()
        except ValueError:
            repos = repo_registry.list_repos()

    if preferred_repo:
        repos = sorted(repos, key=lambda repo: 0 if repo.path == preferred_repo else 1)

    cwd = Path.cwd().resolve()
    result: list[dict[str, Any]] = []

    for repo in repos:
        path = Path(repo.path)
        path_resolved = path.resolve() if path.exists() else path
        selection = repo.launch_selection
        desired_instances = _expected_instances_for_repo(
            path,
            selection.config.value,
            selection.mode.value,
        )
        available_modes = list_modes(path) if path.exists() else []
        mode_configs = {mode: list_configs(path, mode) for mode in available_modes}
        available_configs = mode_configs.get(selection.mode.value, [])
        repo_data: dict[str, Any] = {
            "path": repo.path,
            "name": repo.name,
            "added_at": repo.added_at,
            "exists": path.exists(),
            "is_current_dir": path_resolved == cwd,
            "configs": available_configs,
            "modes": available_modes,
            "mode_configs": mode_configs,
            "selected_config": selection.config.value,
            "selected_mode": selection.mode.value,
            "desired_instances": desired_instances,
        }

        all_status = _all_repository_statuses(
            supervisor=supervisor,
            repo_path=path,
            selected_config=selection.config.value,
            selected_mode=selection.mode.value,
        )
        active_engines = sorted(
            (
                status
                for status in all_status.instances
                if status.state != "stopped"
            ),
            key=lambda status: (
                status.configuration_mode,
                status.config_name,
                status.config_fingerprint,
                status.instance_id or "",
            ),
        )
        _attach_active_configuration(repo_data, active_engines)
        expected_instances = _expected_instances_for_active_configuration(
            path,
            active_engines,
            fallback=desired_instances,
        )
        repo_data["expected_instances"] = expected_instances
        is_multi_instance = expected_instances > 1 or any(
            engine.instance_id is not None for engine in active_engines
        )

        if is_multi_instance and path.exists():
            _populate_multi_instance_status(
                repo_data=repo_data,
                repo_path=path,
                expected_instances=expected_instances,
                multi_status=all_status,
            )
        else:
            _populate_single_instance_status(
                repo_data=repo_data,
                repo=repo,
                repo_path=path,
                status_info=(active_engines[0] if active_engines else None),
            )

        repo_data["dashboard_url"] = client_dashboard_url(
            (repo_data.get("status") or {}).get("port")
        )
        repo_data["health"] = repo.health.to_dict() if repo.health else None
        result.append(repo_data)

    return result


def _all_repository_statuses(
    *,
    supervisor: SupervisorOps,
    repo_path: Path,
    selected_config: str,
    selected_mode: str,
) -> MultiInstanceStatus:
    """Read every lock so active topology never depends on desired selection."""
    if not repo_path.exists():
        return MultiInstanceStatus(repo_root=str(repo_path))
    aggregate = supervisor.status_all_instances(
        repo_path,
        config_name=selected_config,
        mode=selected_mode,
    )
    if aggregate.instances:
        return aggregate
    single = supervisor.status(repo_path)
    if single.state != "stopped":
        aggregate.instances.append(single)
    return aggregate


def _attach_active_configuration(
    repo_data: dict[str, Any],
    active_engines: list[SupervisorStatus],
) -> None:
    """Expose actual lock identity separately from the desired registry selection."""
    if not active_engines:
        return
    identities = {
        (
            status.configuration_mode,
            status.config_name,
            status.config_fingerprint,
        )
        for status in active_engines
    }
    # Conflicting identities should be impossible at acquisition time. Sort for
    # deterministic diagnostics if old/corrupt state nevertheless contains one.
    active_mode, active_config, active_fingerprint = sorted(identities)[0]
    repo_data.update(
        {
            "active_mode": active_mode,
            "active_config": active_config,
            "active_config_fingerprint": active_fingerprint,
            "configuration_identity_conflict": len(identities) > 1,
        }
    )


def _expected_instances_for_active_configuration(
    repo_path: Path,
    active_engines: list[SupervisorStatus],
    *,
    fallback: int,
) -> int:
    """Use the active config only when its current bytes match the lock fingerprint."""
    if not active_engines:
        return fallback
    first = active_engines[0]
    from ..infra.config import Config, get_config_path

    try:
        config = Config.load(
            get_config_path(repo_path, first.config_name, first.configuration_mode)
        )
    except Exception:
        return max(1, len(active_engines))
    if config.config_fingerprint != first.config_fingerprint:
        return max(1, len(active_engines))
    return config.instances


def _expected_instances_for_repo(
    repo_path: Path,
    selected_config: str | None,
    selected_mode: str | None,
) -> int:
    """Return configured instance count, falling back to single-instance mode."""
    from ..infra.config import Config, get_config_path

    if not repo_path.exists() or not selected_config:
        return 1
    try:
        config_path = get_config_path(
            repo_path,
            selected_config,
            selected_mode or "default",
        )
        if not config_path.exists():
            return 1
        config = Config.load(config_path)
    except Exception:
        logger.debug(
            "Falling back to single-instance mode for repo=%s config=%s",
            repo_path,
            selected_config,
            exc_info=True,
        )
        return 1
    return config.instances


def _populate_multi_instance_status(
    *,
    repo_data: dict[str, Any],
    repo_path: Path,
    expected_instances: int,
    multi_status: MultiInstanceStatus,
) -> None:
    """Attach status payloads for a multi-instance repository."""
    repo_data["instances"] = []

    for instance_status in multi_status.instances:
        instance_data = instance_status.to_dict()
        if instance_status.state == "running" and instance_status.port:
            _apply_internal_runtime_state(instance_data, instance_status.port)

        enriched_instance = enrich_runtime_health(
            repo_path,
            instance_data,
            instance_id=instance_data.get("instance_id"),
        )
        resolved_instance = enriched_instance or instance_data
        resolved_instance["dashboard_url"] = client_dashboard_url(
            resolved_instance.get("port")
        )
        repo_data["instances"].append(resolved_instance)

    running_count = sum(
        1 for status in multi_status.instances if status.state == "running"
    )
    if running_count == expected_instances:
        repo_data["status"] = {"state": "running", "running_count": running_count}
    elif running_count > 0:
        repo_data["status"] = {"state": "partial", "running_count": running_count}
    else:
        repo_data["status"] = {"state": "stopped", "running_count": 0}
    if repo_data.get("active_mode"):
        repo_data["status"].update(
            {
                "configuration_mode": repo_data["active_mode"],
                "config_name": repo_data["active_config"],
                "config_fingerprint": repo_data["active_config_fingerprint"],
            }
        )


def _populate_single_instance_status(
    *,
    repo_data: dict[str, Any],
    repo: RegisteredRepo,
    repo_path: Path,
    status_info: SupervisorStatus | None,
) -> None:
    """Attach status payloads for a single-instance repository."""
    repo_data["status"] = enrich_runtime_health(
        repo_path,
        status_info.to_dict() if status_info else None,
    )

    if (
        (status_info is None or status_info.state != "running")
        and repo_path.exists()
    ):
        detected_engines = detect_repository_orchestrators(repo_path)
        if detected_engines:
            detected = detected_engines[0]
            status_data = detected.get("status", {})
            orphaned_status = {
                "state": "running",
                "pid": None,
                "port": detected["port"],
                "started_at": None,
                "recovered": False,
                "error": None,
                "orphaned": True,
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds"),
                "shutdown_requested": status_data.get("shutdown_requested", False),
                "active_session_count": len(status_data.get("active_sessions", [])),
            }
            repo_data["status"] = enrich_runtime_health(
                repo_path,
                orphaned_status,
                orphaned=True,
            )
            info = detected.get("info", {})
            active_mode = info.get("configuration_mode")
            active_config = info.get("config_name")
            active_fingerprint = info.get("config_fingerprint")
            if active_mode and active_config and active_fingerprint:
                repo_data.update(
                    {
                        "active_mode": active_mode,
                        "active_config": active_config,
                        "active_config_fingerprint": active_fingerprint,
                        "configuration_identity_conflict": (
                            len(detected_engines) > 1
                        ),
                    }
                )

    if status_info and status_info.state == "running" and status_info.port:
        status_payload = repo_data.get("status")
        if isinstance(status_payload, dict):
            _apply_internal_runtime_state(status_payload, status_info.port)


def _apply_internal_runtime_state(status_payload: dict[str, Any], port: int) -> None:
    """Attach best-effort internal runtime fields from the orchestrator API."""
    internal = probe_orchestrator_json(
        f"http://127.0.0.1:{port}/api/status",
        timeout_seconds=2.0,
    )
    if internal is None:
        return

    status_payload["paused"] = internal.get("paused", False)
    status_payload["shutdown_requested"] = internal.get("shutdown_requested", False)
    active_sessions = internal.get("active_sessions", [])
    status_payload["active_session_count"] = len(active_sessions)
    status_payload["e2e_role"] = internal.get("e2e_role")
    # The CC frontend uses startup_status to keep the Open button in an
    # "Initializing…" state until the engine has finished its first
    # GitHub fetch + reconcile. Without it, opening mid-startup shows
    # SSE-driven UI updates as visible flashes.
    status_payload["startup_status"] = internal.get("startup_status")


__all__ = ["build_repos_status"]
