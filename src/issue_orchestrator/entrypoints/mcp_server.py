"""MCP server for issue-orchestrator control and data access.

Security posture (see issue #5987, F4):

- **Transport is stdio only.** The MCP server is launched by
  ``mcp.run()`` with no transport argument so it uses stdio. Exposing
  it over HTTP would expand the attack surface significantly; the
  Control API bearer token (F3) is not a substitute for a dedicated
  MCP auth story. If an HTTP transport is ever needed, gate it behind
  an explicit opt-in and add per-tool authorization before touching
  it.
- **No free-form text injection into agents.** The earlier
  ``orchestrator.session.send`` tool let any MCP client write
  arbitrary text into a running agent's session — a prompt-injection
  primitive dressed as a convenience method. It was removed as part
  of F4. If we later want a human to join a stuck session, the
  intended mechanism is a PTY attach directly to the agent terminal,
  not a synthetic MCP tool that types on the human's behalf.
- **Destructive tools require explicit confirmation.**
  ``orchestrator.shutdown`` rejects ``force=True`` unless the caller
  also passes ``confirm=True``, so accidental or drive-by invocations
  can't tear the orchestrator down.
- **``orchestrator.repos.start`` is path-guarded.** The ``repo_path``
  argument must point at an existing git repository; when the
  ``ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST`` env var is set, it must
  also resolve under one of the allowlisted roots.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, cast
import inspect
from mcp.server.fastmcp import FastMCP

from ..contracts.mcp import McpUiHintPayload
from ..contracts.repository_engine import RepositoryEngineStartPayload
from ..infra import supervisor
from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..execution.mcp_repository_engine_runtime import (
    mcp_launch_payload,
    repository_start_failure_error,
    require_identity_if_running,
    require_status_identity,
    resolve_start_result,
)
from ..execution.repository_engine_start import (
    RepositoryEngineStartRequest,
    StartRepositoryEngineCommand,
)
from ..execution.repository_engine_supervisor import build_default_supervisor_ops
from ..infra.config import Config, get_config_path
from ..infra.client_urls import resolve_client_base_url
from ..execution.orchestrator_http_api import OrchestratorAsyncHttpApi

logger = logging.getLogger(__name__)

_REPOS_ALLOWLIST_ENV = "ISSUE_ORCHESTRATOR_MCP_REPOS_ALLOWLIST"


def _mcp_repos_allowlist() -> list[Path] | None:
    """Return configured allowlist roots for ``orchestrator.repos.start``.

    When the env var is unset we return ``None`` — the caller still
    validates that the path is a real git repo, but does not gate on
    allowlisted roots. When the env var is set but empty (after
    stripping), we return an empty list, which forbids every path.
    """
    raw = os.environ.get(_REPOS_ALLOWLIST_ENV)
    if raw is None:
        return None
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        roots.append(Path(entry).expanduser().resolve())
    return roots


def _validate_repo_start_path(repo_path: str) -> str | None:
    """Return an error message if ``repo_path`` is not safe to start.

    ``None`` means the path passes static validation.
    """
    try:
        path = Path(repo_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"Invalid repo_path: {exc}"
    if not path.exists():
        return f"Repository not found: {repo_path}"
    if not path.is_dir():
        return f"Repository path is not a directory: {repo_path}"
    if not (path / ".git").exists():
        return f"Repository path is not a git checkout: {repo_path}"
    allowlist = _mcp_repos_allowlist()
    if allowlist is None:
        return None
    for root in allowlist:
        try:
            path.relative_to(root)
            return None
        except ValueError:
            continue
    return (
        f"Repository path {repo_path} is not under any configured "
        f"{_REPOS_ALLOWLIST_ENV} root"
    )


@dataclass(frozen=True)
class McpSettings:
    repo_root: Path
    config_path: Path
    instance_id: str | None
    host: str
    auto_start: bool


class RepoStartPayload(RepositoryEngineStartPayload, total=False):
    """Typed public result for the repository-start MCP operation."""


class OrchestratorHttpClient:
    def __init__(
        self,
        settings: McpSettings,
        repo_start_command: StartRepositoryEngineCommand | None = None,
    ) -> None:
        self._settings = settings
        self._cached_port: int | None = None
        self._port_override = False
        self._repo_start_command = repo_start_command or StartRepositoryEngineCommand(
            build_default_supervisor_ops()
        )

    def status(self) -> supervisor.SupervisorStatus:
        return supervisor.status(
            self._settings.repo_root, instance_id=self._settings.instance_id
        )

    def start(self) -> supervisor.SupervisorStatus:
        config = Config.load(self._settings.config_path)
        result = self._repo_start_command.execute(
            RepositoryEngineStartRequest(
                repo_root=self._settings.repo_root,
                selection=config.launch_selection,
                config_path=self._settings.config_path,
                instance_id=self._settings.instance_id,
                actor="mcp-auto-start",
            )
        )
        status = resolve_start_result(
            result,
            config,
            lambda: supervisor.status(
                self._settings.repo_root,
                instance_id=self._settings.instance_id,
            ),
        )
        self._cached_port = status.port
        return status

    def _ensure_running(self) -> supervisor.SupervisorStatus:
        status = self.status()
        config = Config.load(self._settings.config_path)
        if status.state != "running":
            if not self._settings.auto_start:
                raise RuntimeError("Orchestrator not running")
            status = self.start()
        if status.state != "running":
            raise RuntimeError(f"Orchestrator not running (state={status.state})")
        if status.port is None:
            raise RuntimeError("Orchestrator running but no port available")
        require_status_identity(status, config)
        return status

    def _api_base_url_for_port(self, port: int) -> str:
        return f"http://{self._settings.host}:{port}"

    def _client_base_url_for_port(self, port: int) -> str:
        return resolve_client_base_url(port, local_host=self._settings.host)

    def _client_url_for_path(self, port: int, path: str) -> str:
        return self._client_base_url_for_port(port).rstrip("/") + path

    def _resolve_port(self) -> int:
        cached_port = self._cached_port
        if self._port_override and cached_port is not None:
            return cached_port
        status = self._ensure_running()
        port = status.port
        if port is None:
            raise RuntimeError("Orchestrator running but no port available")
        self._cached_port = port
        return port

    def api_base_url(self) -> str:
        return self._api_base_url_for_port(self._resolve_port())

    def refresh_api_base_url(self) -> str:
        status = self._ensure_running()
        assert status.port is not None
        self._cached_port = status.port
        return self._api_base_url_for_port(status.port)

    def client_base_url(self) -> str:
        return self._client_base_url_for_port(self._resolve_port())

    def doctor_url(self) -> str | None:
        if self._port_override and self._cached_port:
            return self._client_url_for_path(self._cached_port, "/api/doctor")
        status = self.status()
        if status.state == "running" and status.port:
            require_status_identity(status, Config.load(self._settings.config_path))
            return self._client_url_for_path(status.port, "/api/doctor")
        return None

    def update_port(self, port: int) -> None:
        """Install an explicit operator override that bypasses lock discovery."""
        self._cached_port = port
        self._port_override = True

    def close(self) -> None:
        return None


# The public MCP tool surface, declared as data rather than as a sequence of
# registration calls. This is the inventory the stability doc
# (``docs/user/stability.md``) publishes and that
# ``tests/unit/test_public_api_surface_docs.py`` drift-tests, so a tool cannot
# be added or removed without the documented surface moving with it.
#
# The surface is **experimental** during 0.x: names, arguments, and return
# shapes may change in any release. See docs/user/stability.md.
MCP_TOOLS: tuple[tuple[str, str], ...] = (
    ("orchestrator.status", "tool_status"),
    ("orchestrator.start", "tool_start"),
    ("orchestrator.stop", "tool_stop"),
    ("orchestrator.pause", "tool_pause"),
    ("orchestrator.resume", "tool_resume"),
    ("orchestrator.refresh", "tool_refresh"),
    ("orchestrator.shutdown", "tool_shutdown"),
    ("orchestrator.snapshot", "tool_snapshot"),
    ("orchestrator.session.worktree", "tool_session_worktree"),
    ("orchestrator.session.manifest", "tool_session_manifest"),
    ("orchestrator.session.phases", "tool_session_phases"),
    ("orchestrator.session.claude_log", "tool_session_claude_log"),
    ("orchestrator.session.orchestrator_log", "tool_session_orchestrator_log"),
    # orchestrator.session.send intentionally omitted — see module docstring.
    # Any MCP client holding the transport could inject arbitrary text into a
    # running agent's prompt via that tool.
    ("orchestrator.session.kill", "tool_session_kill"),
    ("orchestrator.session.focus", "tool_session_focus"),
    ("orchestrator.urls", "tool_urls"),
    ("orchestrator.doctor", "tool_doctor"),
    # Unified dashboard tools
    ("orchestrator.state", "tool_state"),
    ("orchestrator.repos", "tool_repos"),
    ("orchestrator.repos.start", "tool_repos_start"),
    ("orchestrator.repos.stop", "tool_repos_stop"),
)

MCP_TOOL_NAMES: tuple[str, ...] = tuple(name for name, _ in MCP_TOOLS)


class McpApp:
    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings
        self._repo_start_command = StartRepositoryEngineCommand(
            build_default_supervisor_ops()
        )
        self._client = OrchestratorHttpClient(settings, self._repo_start_command)
        self._api = OrchestratorAsyncHttpApi(
            self._client.api_base_url,
            refresh_base_url=self._client.refresh_api_base_url,
        )

    def close(self) -> None:
        self._client.close()
        # FastMCP does not expose a shutdown hook; best-effort cleanup.
        return None

    def override_port(self, port: int) -> None:
        """Bypass supervisor detection and use a fixed port."""
        self._client.update_port(port)

    async def _safe(
        self,
        tool_name: str,
        fn: Callable[[], dict[str, Any]] | Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            result = fn()
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP tool %s failed", tool_name)
            return {
                "error": {
                    "message": str(exc),
                    "type": exc.__class__.__name__,
                }
            }

    def register(self, server: FastMCP) -> None:
        for tool_name, handler_name in MCP_TOOLS:
            # getattr fails loudly if the table names a handler that no longer
            # exists, so the declared surface cannot drift from the code.
            server.tool(name=tool_name)(getattr(self, handler_name))

    async def tool_status(self) -> dict[str, Any]:
        return await self._safe("orchestrator.status", self.status)

    async def tool_start(self) -> dict[str, Any]:
        """Start the orchestrator, pointing failures at the doctor report.

        Both failure routes land on a top-level ``error`` before they get
        here: ``_safe`` converts exceptions, and ``start`` normalises a failed
        repository-start command result. So the hint is attached by inspecting
        the result rather than by catching.

        Attaching the hint must not be able to undo that. It runs after
        ``_safe`` has already produced the structured error, so anything raised
        while building it would escape the tool and cross the protocol boundary
        as a transport-level exception — losing the very error the contract
        promises. :meth:`_doctor_ui_hint` therefore cannot raise.
        """
        result = await self._safe("orchestrator.start", self.start)
        if "error" not in result:
            return result
        return {**result, "ui_hint": self._doctor_ui_hint()}

    def _doctor_ui_hint(self) -> McpUiHintPayload:
        """Build the doctor hint for a failed start. Never raises.

        The URL is a convenience for opening the doctor panel, and resolving it
        re-reads supervisor state — the same read that may have just failed the
        start. An unreadable lock or state directory would then raise here,
        *after* the structured error was built, and destroy it.

        This is not a fallback that hides a problem: the failure being reported
        is unchanged and the exception is logged. Only the optional URL is
        dropped, and a hint without one still routes the operator to the doctor
        panel, which is where the underlying fault will be diagnosed anyway.
        """
        hint = McpUiHintPayload(kind="doctor")
        try:
            doctor_url = self._client.doctor_url()
        except Exception:
            logger.exception(
                "resolving the doctor URL failed; returning the start error "
                "with a URL-less doctor hint"
            )
            return hint
        if doctor_url:
            hint["url"] = doctor_url
        return hint

    async def tool_stop(self, force: bool = False) -> dict[str, Any]:
        return await self._safe("orchestrator.stop", lambda: self.stop(force))

    async def tool_pause(self) -> dict[str, Any]:
        return await self._safe("orchestrator.pause", self.pause)

    async def tool_resume(self) -> dict[str, Any]:
        return await self._safe("orchestrator.resume", self.resume)

    async def tool_refresh(self, inflight_stable_ids: list[str] | None = None) -> dict[str, Any]:
        return await self._safe("orchestrator.refresh", lambda: self.refresh(inflight_stable_ids))

    async def tool_shutdown(
        self, force: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Request orchestrator shutdown.

        ``force=True`` is destructive (kills running sessions mid-flight),
        so we require the caller to also pass ``confirm=True``. A
        graceful ``force=False`` shutdown does not require confirmation.
        """
        if force and not confirm:
            return {
                "error": {
                    "message": (
                        "orchestrator.shutdown(force=True) requires "
                        "confirm=True to prevent accidental tear-downs."
                    ),
                    "type": "ConfirmationRequired",
                }
            }
        return await self._safe(
            "orchestrator.shutdown",
            lambda: self.shutdown(force, reason="mcp.tool_shutdown"),
        )

    async def tool_snapshot(self) -> dict[str, Any]:
        return await self._safe("orchestrator.snapshot", self.snapshot)

    async def tool_session_worktree(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.worktree",
            lambda: self.session_worktree(issue_number),
        )

    async def tool_session_manifest(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.manifest",
            lambda: self.session_manifest(issue_number),
        )

    async def tool_session_phases(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.phases",
            lambda: self.session_phases(issue_number),
        )

    async def tool_session_claude_log(self, issue_number: int, limit: int = 200) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.claude_log",
            lambda: self.session_claude_log(issue_number, limit),
        )

    async def tool_session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.orchestrator_log",
            lambda: self.session_orchestrator_log(issue_number),
        )

    async def tool_session_kill(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.kill",
            lambda: self.session_kill(issue_number),
        )

    async def tool_session_focus(self, issue_number: int) -> dict[str, Any]:
        return await self._safe(
            "orchestrator.session.focus",
            lambda: self.session_focus(issue_number),
        )

    async def tool_urls(self) -> dict[str, Any]:
        return await self._safe("orchestrator.urls", self.urls)

    async def tool_doctor(self) -> dict[str, Any]:
        return await self._safe("orchestrator.doctor", self.doctor)

    async def tool_state(self) -> dict[str, Any]:
        """Get complete system state for the unified dashboard."""
        return await self._safe("orchestrator.state", self.get_system_state)

    async def tool_repos(self) -> dict[str, Any]:
        """List all repos with status."""
        return await self._safe("orchestrator.repos", self.list_repos)

    async def tool_repos_start(
        self,
        repo_path: str,
        config_name: str = "default.yaml",
        mode: str = "default",
    ) -> RepoStartPayload:
        """Start orchestrator for a specific repo."""
        return cast(
            RepoStartPayload,
            await self._safe(
                "orchestrator.repos.start",
                lambda: dict(self.start_repo(repo_path, config_name, mode)),
            ),
        )

    async def tool_repos_stop(self, repo_path: str, force: bool = False) -> dict[str, Any]:
        """Stop orchestrator for a specific repo."""
        return await self._safe(
            "orchestrator.repos.stop",
            lambda: self.stop_repo(repo_path, force),
        )

    def get_system_state(self) -> dict[str, Any]:
        """Get complete system state."""
        from ..observation.instance_detector import detect_system_state

        state = detect_system_state()
        return state.to_dict()

    def list_repos(self) -> dict[str, Any]:
        """List all repos with status."""
        from ..observation.instance_detector import detect_system_state

        state = detect_system_state()
        return {"repos": [r.to_dict() for r in state.repos]}

    def start_repo(
        self,
        repo_path: str,
        config_name: str = "default.yaml",
        mode: str = "default",
    ) -> RepoStartPayload:
        """Start orchestrator for a specific repo.

        The ``repo_path`` is caller-supplied and therefore untrusted —
        validate before we hand it to ``supervisor.start``. See module
        docstring for the threat model.
        """
        error = _validate_repo_start_path(repo_path)
        if error is not None:
            return {"error": error}
        path = Path(repo_path).expanduser().resolve(strict=False)

        try:
            selection = RepositoryLaunchSelection.parse(
                mode=mode,
                config_name=config_name,
            )
            result = self._repo_start_command.execute(
                RepositoryEngineStartRequest(
                    repo_root=path,
                    selection=selection,
                    actor="mcp",
                )
            )
            return cast(RepoStartPayload, dict(result.payload))
        except Exception as e:
            logger.exception("Failed to start orchestrator for %s", repo_path)
            return {"error": str(e)}

    def stop_repo(
        self,
        repo_path: str,
        force: bool = False,
        *,
        reason: str = "mcp_server.stop_repo",
    ) -> dict[str, Any]:
        """Stop orchestrator for a specific repo.

        ``reason`` is required by the underlying supervisor; the
        default identifies MCP as the source. MCP clients should
        thread their own reason when they have one (e.g. operator
        intent passed in via tool args).
        """
        path = Path(repo_path)
        stopped = supervisor.stop(path, force=force, reason=reason, actor="mcp")
        return {"status": "stopped" if stopped else "failed"}

    async def status(self) -> dict[str, Any]:
        status = self._client.status()
        result: dict[str, Any] = {"supervisor": status.to_dict()}
        if status.state == "running" and status.port is not None:
            result["status"] = await self._api.status()
            result["info"] = await self._api.info()
        return result

    def start(self) -> dict[str, Any]:
        status = self._client.status()
        config = Config.load(self._settings.config_path)
        if status.state != "running":
            command_result = self._repo_start_command.execute(
                RepositoryEngineStartRequest(
                    repo_root=self._settings.repo_root,
                    selection=config.launch_selection,
                    config_path=self._settings.config_path,
                    instance_id=self._settings.instance_id,
                    actor="mcp",
                )
            )
            result: dict[str, Any] = {"launch": mcp_launch_payload(command_result)}
            error = repository_start_failure_error(command_result)
            if error is not None:
                result["error"] = error
            return result
        require_status_identity(status, config)
        return {"supervisor": status.to_dict()}

    def stop(
        self,
        force: bool = False,
        *,
        reason: str = "mcp_server.stop",
    ) -> dict[str, Any]:
        status = self._client.status()
        require_identity_if_running(status, Config.load(self._settings.config_path))
        stopped = supervisor.stop(
            self._settings.repo_root,
            instance_id=self._settings.instance_id,
            force=force,
            reason=reason,
            actor="mcp",
        )
        return {"stopped": stopped}

    async def pause(self) -> dict[str, Any]:
        return await self._api.pause()

    async def resume(self) -> dict[str, Any]:
        return await self._api.resume()

    async def refresh(self, inflight_stable_ids: list[str] | None) -> dict[str, Any]:
        return await self._api.refresh(inflight_stable_ids or [])

    async def shutdown(
        self,
        force: bool = False,
        *,
        reason: str = "mcp_server.async_shutdown",
    ) -> dict[str, Any]:
        return await self._api.shutdown(force=force, reason=reason, actor="mcp")

    async def snapshot(self) -> dict[str, Any]:
        return {
            "status": await self._api.status(),
            "info": await self._api.info(),
            "blocked": await self._api.blocked_issues(),
            "stale": await self._api.stale_issues(),
            "dependency_problems": await self._api.dependency_problems(),
            "excluded": await self._api.excluded_issues(),
            "history": await self._api.history(),
        }

    async def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return await self._api.session_worktree(issue_number)

    async def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return await self._api.session_manifest(issue_number)

    async def session_phases(self, issue_number: int) -> dict[str, Any]:
        return await self._api.session_phases(issue_number)

    async def session_claude_log(self, issue_number: int, limit: int = 200) -> dict[str, Any]:
        return await self._api.session_claude_log(issue_number, limit)

    async def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return await self._api.session_orchestrator_log(issue_number)

    async def session_kill(self, issue_number: int) -> dict[str, Any]:
        return await self._api.kill(issue_number)

    async def session_focus(self, issue_number: int) -> dict[str, Any]:
        return await self._api.focus(issue_number)

    def urls(self) -> dict[str, Any]:
        base = self._client.client_base_url().rstrip("/")
        return {
            "base_url": base,
            "dashboard_url": f"{base}/",
            "events_url": f"{base}/api/events",
            "config_url": f"{base}/api/config",
        }

    def doctor(self) -> dict[str, Any]:
        from ..infra.doctor import run_doctor
        from ..execution.command_runner import LocalCommandRunner

        config = None
        try:
            config = Config.load(self._settings.config_path)
        except Exception:
            config = None
        result = run_doctor(
            config=config,
            config_path=self._settings.config_path,
            runner=LocalCommandRunner(),
        )
        return result.to_dict()


mcp = FastMCP("Issue Orchestrator", json_response=True)


def _resolve_settings(args: argparse.Namespace) -> McpSettings:
    repo_root = Path(
        args.repo_root or os.environ.get("IO_E2E_REPO_ROOT", "") or Path.cwd()
    )
    config_path_str = args.config_path or os.environ.get("IO_E2E_CONFIG_PATH", "")
    if config_path_str:
        config_path = Path(config_path_str)
    else:
        config_path = get_config_path(repo_root, args.config_name, args.mode)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return McpSettings(
        repo_root=repo_root,
        config_path=config_path,
        instance_id=args.instance_id,
        host=args.host,
        auto_start=args.auto_start,
    )


def _resolve_repo_root(config_path: Path) -> Path:
    config = Config.load(config_path)
    if not config.repo_root:
        raise ValueError("repo_root missing in config")
    return config.repo_root


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue Orchestrator MCP server")
    parser.add_argument("--repo-root", help="Repository root path")
    parser.add_argument("--config", dest="config_path", help="Path to config file")
    parser.add_argument(
        "--config-name", default="default.yaml", help="Config filename (default.yaml)"
    )
    parser.add_argument(
        "--mode", default="default", help="Directory-backed configuration mode"
    )
    parser.add_argument(
        "--instance-id", help="Instance ID for multi-orchestrator setups"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for web API calls")
    parser.add_argument(
        "--auto-start", action="store_true", help="Start orchestrator if not running"
    )
    parser.add_argument(
        "--api-port", type=int, help="Control API port (bypasses supervisor detection)"
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.config_path and not args.repo_root:
        repo_root = _resolve_repo_root(Path(args.config_path))
        args.repo_root = str(repo_root)

    settings = _resolve_settings(args)
    app = McpApp(settings)
    # If --api-port is given, bypass supervisor detection
    api_port = args.api_port or int(os.environ.get("IO_E2E_API_PORT", "0")) or None
    if api_port:
        app.override_port(api_port)
    app.register(mcp)

    try:
        mcp.run()
    finally:
        app.close()


if __name__ == "__main__":
    main()
