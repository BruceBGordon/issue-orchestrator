"""Instance detection for the unified dashboard.

Detects running orchestrators, configured repos, and dashboard server status.
Provides a unified view of the system state for the dashboard UI.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..infra import supervisor
from ..infra.repo_registry import load_registry
from ..infra.config import list_configs, list_modes
from ..infra.client_urls import resolve_client_dashboard_url, with_client_query_params

logger = logging.getLogger(__name__)

# Global dashboard PID file location
DASHBOARD_PID_FILE = Path.home() / ".config" / "issue-orchestrator" / "dashboard.pid"


def _is_port_available(port: int) -> bool:
    """Return True when localhost:port can be bound for a new server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _find_available_dashboard_port(
    default_port: int = 19080, max_offset: int = 20
) -> int:
    """Pick the first available dashboard port, preferring default_port."""
    for candidate in range(default_port, default_port + max_offset + 1):
        if _is_port_available(candidate):
            return candidate
    # Fail-safe fallback: keep deterministic default if no free port found in window.
    return default_port


@dataclass
class DashboardStatus:
    """Status of the unified dashboard server."""

    running: bool
    pid: int | None = None
    port: int | None = None
    started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
        }


@dataclass
class RepoStatus:
    """Status of a repository and its orchestrator."""

    path: str
    name: str
    config_status: Literal["ready", "needs_setup", "legacy"]
    orchestrator_state: Literal["running", "stopped", "failed", "paused"]
    orchestrator_pid: int | None = None
    orchestrator_port: int | None = None
    configs: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    mode_configs: dict[str, list[str]] = field(default_factory=dict)
    selected_mode: str = "default"
    selected_config: str = "default.yaml"
    active_mode: str | None = None
    active_config: str | None = None
    active_config_fingerprint: str | None = None
    active_instances: list[dict[str, Any]] = field(default_factory=list)
    is_current_dir: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "path": self.path,
            "name": self.name,
            "config_status": self.config_status,
            "orchestrator_state": self.orchestrator_state,
            "orchestrator_pid": self.orchestrator_pid,
            "orchestrator_port": self.orchestrator_port,
            "configs": self.configs,
            "modes": self.modes,
            "mode_configs": self.mode_configs,
            "selected_mode": self.selected_mode,
            "selected_config": self.selected_config,
            "active_mode": self.active_mode,
            "active_config": self.active_config,
            "active_config_fingerprint": self.active_config_fingerprint,
            "active_instances": self.active_instances,
            "is_current_dir": self.is_current_dir,
        }


@dataclass
class SystemState:
    """Complete system state for the unified dashboard."""

    dashboard: DashboardStatus
    repos: list[RepoStatus]
    current_directory: str
    is_orchestrator_codebase: bool = False
    cwd_is_git_repo: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "dashboard": self.dashboard.to_dict(),
            "repos": [r.to_dict() for r in self.repos],
            "current_directory": self.current_directory,
            "is_orchestrator_codebase": self.is_orchestrator_codebase,
            "cwd_is_git_repo": self.cwd_is_git_repo,
        }


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_dashboard_pid() -> DashboardStatus:
    """Read the dashboard PID file and check if it's running."""
    if not DASHBOARD_PID_FILE.exists():
        return DashboardStatus(running=False)

    import json

    try:
        with open(DASHBOARD_PID_FILE) as f:
            data = json.load(f)
        pid = data.get("pid")
        port = data.get("port")
        started_at = data.get("started_at")

        if pid and _is_process_alive(pid):
            return DashboardStatus(
                running=True,
                pid=pid,
                port=port,
                started_at=started_at,
            )
        # Stale PID file
        return DashboardStatus(running=False)
    except (json.JSONDecodeError, OSError):
        return DashboardStatus(running=False)


def write_dashboard_pid(port: int) -> None:
    """Write the dashboard PID file.

    Called by the dashboard server on startup.
    """
    import json

    DASHBOARD_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(DASHBOARD_PID_FILE, "w") as f:
        json.dump(data, f, indent=2)


def clear_dashboard_pid() -> None:
    """Remove the dashboard PID file.

    Called by the dashboard server on shutdown.
    """
    try:
        DASHBOARD_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_orchestrator_codebase(path: Path) -> bool:
    """Check if a path is the issue-orchestrator codebase itself."""
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text()
        return 'name = "issue-orchestrator"' in content
    except OSError:
        return False


def _check_if_paused(port: int) -> bool:
    """Check if an orchestrator is paused by querying its HTTP API."""
    import json
    import urllib.request

    try:
        url = f"http://localhost:{port}/api/status"
        with urllib.request.urlopen(url, timeout=2) as response:
            data = json.loads(response.read().decode())
            return data.get("paused", False)
    except Exception:
        # If we can't reach the API, assume not paused
        return False


def get_orchestrator_details(
    repo_path: Path,
    *,
    selected_mode: str,
    selected_config: str,
) -> dict[str, Any]:
    """Return state plus actual lock identity/topology for one repository."""
    aggregate = supervisor.status_all_instances(
        repo_path,
        selected_config,
        mode=selected_mode,
    )
    instances = list(aggregate.instances)
    if not instances:
        single = supervisor.status(repo_path)
        if single.state != "stopped":
            instances.append(single)
    active = sorted(
        (instance for instance in instances if instance.state != "stopped"),
        key=lambda instance: (
            0 if instance.state == "running" else 1,
            instance.configuration_mode,
            instance.config_name,
            instance.instance_id or "",
        ),
    )
    if not active:
        return {
            "state": "stopped",
            "pid": None,
            "port": None,
            "active_mode": None,
            "active_config": None,
            "active_config_fingerprint": None,
            "active_instances": [],
        }

    primary = active[0]
    running = [instance for instance in active if instance.state == "running"]
    state: Literal["running", "stopped", "failed", "paused"] = "failed"
    if running:
        state = (
            "paused"
            if primary.port and _check_if_paused(primary.port)
            else "running"
        )
    return {
        "state": state,
        "pid": primary.pid,
        "port": primary.port,
        "active_mode": primary.configuration_mode,
        "active_config": primary.config_name,
        "active_config_fingerprint": primary.config_fingerprint,
        "active_instances": [instance.to_dict() for instance in active],
    }


def _get_config_status(
    repo_path: Path,
) -> tuple[
    Literal["ready", "needs_setup", "legacy"],
    list[str],
    dict[str, list[str]],
]:
    """Get config status for a repo.

    Returns (status, list_of_configs).
    """
    modes = list_modes(repo_path)
    mode_configs = {mode: list_configs(repo_path, mode) for mode in modes}
    if any(mode_configs.values()):
        return "ready", modes, mode_configs

    legacy_config = (repo_path / ".issue-orchestrator.yaml").exists()
    if legacy_config:
        return "legacy", [], {}

    return "needs_setup", [], {}


def detect_system_state(cwd: Path | None = None) -> SystemState:
    """Detect the complete system state.

    Args:
        cwd: Current working directory (defaults to actual cwd)

    Returns:
        SystemState with dashboard status, repos, and context info
    """
    if cwd is None:
        cwd = Path.cwd()
    cwd = cwd.resolve()

    # Dashboard status
    dashboard = _read_dashboard_pid()

    # Check if cwd is a git repo
    cwd_is_git = (cwd / ".git").exists()

    # Check if cwd is the orchestrator codebase
    is_orch_codebase = _is_orchestrator_codebase(cwd)

    # Load registered repos
    registry = load_registry()
    repos: list[RepoStatus] = []

    # Process registered repos
    for reg_repo in registry.repos:
        repo_path = Path(reg_repo.path)
        if not repo_path.exists():
            continue

        selection = reg_repo.launch_selection
        config_status, modes, mode_configs = _get_config_status(repo_path)
        runtime = get_orchestrator_details(
            repo_path,
            selected_mode=selection.mode.value,
            selected_config=selection.config.value,
        )

        repos.append(
            RepoStatus(
                path=reg_repo.path,
                name=reg_repo.name,
                config_status=config_status,
                orchestrator_state=runtime["state"],
                orchestrator_pid=runtime["pid"],
                orchestrator_port=runtime["port"],
                configs=mode_configs.get(selection.mode.value, []),
                modes=modes,
                mode_configs=mode_configs,
                selected_mode=selection.mode.value,
                selected_config=selection.config.value,
                active_mode=runtime["active_mode"],
                active_config=runtime["active_config"],
                active_config_fingerprint=runtime["active_config_fingerprint"],
                active_instances=runtime["active_instances"],
                is_current_dir=(repo_path == cwd),
            )
        )

    # If cwd is a git repo but not registered, add it
    if cwd_is_git and not is_orch_codebase:
        cwd_str = str(cwd)
        if not any(r.path == cwd_str for r in repos):
            config_status, modes, mode_configs = _get_config_status(cwd)
            runtime = get_orchestrator_details(
                cwd,
                selected_mode="default",
                selected_config="default.yaml",
            )
            repos.insert(
                0,
                RepoStatus(
                    path=cwd_str,
                    name=cwd.name,
                    config_status=config_status,
                    orchestrator_state=runtime["state"],
                    orchestrator_pid=runtime["pid"],
                    orchestrator_port=runtime["port"],
                    configs=mode_configs.get("default", []),
                    modes=modes,
                    mode_configs=mode_configs,
                    selected_mode="default",
                    selected_config="default.yaml",
                    active_mode=runtime["active_mode"],
                    active_config=runtime["active_config"],
                    active_config_fingerprint=runtime["active_config_fingerprint"],
                    active_instances=runtime["active_instances"],
                    is_current_dir=True,
                ),
            )

    return SystemState(
        dashboard=dashboard,
        repos=repos,
        current_directory=str(cwd),
        is_orchestrator_codebase=is_orch_codebase,
        cwd_is_git_repo=cwd_is_git,
    )


def _canonical_path(path: Path) -> str:
    """Return a stable absolute key for comparing filesystem paths."""
    return str(path.expanduser().resolve())


def _path_is_within_any(path: Path, roots: Iterable[Path]) -> bool:
    """Return whether a canonical absolute path is covered by any canonical root."""
    for root in roots:
        if path == root or path.is_relative_to(root):
            return True
    return False


def _normalize_search_paths(paths: Iterable[Path]) -> list[Path]:
    """Expand, resolve, and remove duplicate search roots."""
    normalized: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = Path(_canonical_path(path))
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(resolved)
    return normalized


def default_repo_search_paths(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> list[Path]:
    """Return rationalized default directories to search for repos.

    The Control Center can run from ``$HOME`` while managing repos under
    ``~/dev``. Scanning both ``~/dev`` and ``$HOME`` rediscovers the same
    repos through nested roots, so defaults stay focused on common repo
    containers and only add a current-directory context when it is outside
    those containers.
    """
    home = Path(_canonical_path(home or Path.home()))
    cwd = Path(_canonical_path(cwd or Path.cwd()))
    common_roots = [
        home / "dev",
        home / "projects",
        home / "code",
        home / "repos",
        home / "src",
        home / "work",
        home / "github",
    ]
    candidates = list(common_roots)

    if cwd != home:
        cwd_context = cwd.parent if (cwd / ".git").exists() else cwd
        if not _path_is_within_any(cwd_context, common_roots):
            candidates.append(cwd_context)

    return _normalize_search_paths(candidates)


def _default_search_paths() -> list[Path]:
    """Return default directories to search for repos."""
    return default_repo_search_paths()


def _dedupe_discovered_repos(
    discovered: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate discovered repos by canonical repository path."""
    by_path: dict[str, dict[str, Any]] = {}
    for repo in discovered:
        repo_path = repo.get("path")
        if not repo_path:
            continue
        key = _canonical_path(Path(str(repo_path)))
        if key in by_path:
            continue
        normalized = dict(repo)
        normalized["path"] = key
        by_path[key] = normalized
    return list(by_path.values())


def _try_add_discovered_repo(
    entry_path: Path,
    registered_paths: set[str],
    discovered: list[dict[str, Any]],
) -> bool:
    """Try to add a git repo to the discovered list.

    Returns True if the repo was added or should be skipped (not recursed into).
    Returns False if the directory should be recursed into.
    """
    git_path = entry_path / ".git"
    if not git_path.exists():
        return False  # Not a git repo, recurse

    # Skip worktrees (.git is a file, not a directory)
    if git_path.is_file():
        return True

    resolved = _canonical_path(entry_path)
    if resolved in registered_paths:
        return True

    if _is_orchestrator_codebase(entry_path):
        return True

    config_status, modes, mode_configs = _get_config_status(entry_path)
    discovered.append(
        {
            "path": resolved,
            "name": entry_path.name,
            "configs": mode_configs.get("default", []),
            "modes": modes,
            "mode_configs": mode_configs,
            "status": config_status,
        }
    )
    return True


def discover_repos(
    search_paths: list[Path] | None = None,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    """Discover git repositories that could be configured.

    Args:
        search_paths: Paths to search (defaults to common dev directories)
        max_depth: Maximum directory depth to search

    Returns:
        List of discovered repos with path, name, status, configs
    """
    if search_paths is None:
        search_paths = _default_search_paths()
    else:
        search_paths = _normalize_search_paths(search_paths)

    registry = load_registry()
    registered_paths = {_canonical_path(Path(r.path)) for r in registry.repos}
    discovered: list[dict[str, Any]] = []
    scanned_dirs: set[str] = set()

    def scan_directory(base: Path, depth: int) -> None:
        if depth > max_depth or not base.exists() or not base.is_dir():
            return

        base_key = _canonical_path(base)
        if base_key in scanned_dirs:
            return
        scanned_dirs.add(base_key)

        try:
            for entry in os.scandir(base):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                entry_path = Path(entry.path)
                if not _try_add_discovered_repo(
                    entry_path, registered_paths, discovered
                ):
                    scan_directory(entry_path, depth + 1)
        except PermissionError:
            pass

    for search_path in search_paths:
        scan_directory(search_path, 0)

    discovered = _dedupe_discovered_repos(discovered)
    discovered.sort(key=lambda x: x["name"].lower())
    return discovered


def get_best_entry_point(state: SystemState) -> dict[str, Any]:
    """Determine the best entry point based on current state.

    Always opens the unified dashboard. If an orchestrator is running for
    the current directory, deep-links to that repo's activity view.

    Returns a dict with:
    - action: "open_dashboard" | "start_dashboard"
    - url: URL to open (if applicable)
    - port: Port to use
    - repo_path: Path to deep-link to (if applicable)
    """
    # Find if current directory has a running orchestrator (for deep-linking)
    active_repo_path: str | None = None
    for repo in state.repos:
        if repo.is_current_dir and repo.orchestrator_state == "running":
            active_repo_path = repo.path
            break

    # Build URL with optional deep-link
    def build_url(port: int) -> str:
        base = resolve_client_dashboard_url(port)
        return with_client_query_params(base, repo=active_repo_path)

    # If dashboard is running, open it
    if state.dashboard.running and state.dashboard.port:
        return {
            "action": "open_dashboard",
            "url": build_url(state.dashboard.port),
            "port": state.dashboard.port,
            "repo_path": active_repo_path,
        }

    # Need to start dashboard
    start_port = _find_available_dashboard_port(default_port=19080)
    return {
        "action": "start_dashboard",
        "port": start_port,
        "repo_path": active_repo_path,
    }
