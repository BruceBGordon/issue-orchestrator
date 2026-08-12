"""The three ways ``issue-orchestrator start`` can run.

Split from ``cli`` so command parsing and run-mode wiring are separate
concerns, and because all three modes share one easily-missed
obligation: each either binds a Control API or must say it serves none.
Agents are launched with an environment pointing at that endpoint, so a
mode that does neither strands every callback (#6924 F7).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from rich.console import Console

if TYPE_CHECKING:
    from ..infra.config import Config

console = Console()
_T = TypeVar("_T")


async def _run_with_repo_lock_heartbeat(
    repo_root: Path,
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """Keep an in-process CLI engine's repository lock live."""
    from ..infra.repo_lock import touch_lock

    async def heartbeat() -> None:
        while True:
            touch_lock(repo_root)
            await asyncio.sleep(5.0)

    task = asyncio.create_task(heartbeat(), name="cli-repo-lock-heartbeat")
    try:
        return await operation()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def declare_no_control_api(orchestrator, api_port: int | None) -> None:
    """Answer the endpoint question when this mode binds no server.

    Running without ``--api-port`` is a valid deployment. Saying so
    explicitly is what lets the launcher tell "no Control API here" from
    "the server has not published yet" — only the second must block
    agent launch.
    """
    if api_port is None:
        orchestrator.deps.agent_callback_endpoint.declare_unavailable()


async def run_no_dashboard(orchestrator, api_port: int | None) -> None:
    """Run orchestrator without dashboard UI."""
    from .control_api_server import ControlAPIServer

    declare_no_control_api(orchestrator, api_port)

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
            from ..infra.repo_lock import set_lock_http_port

            set_lock_http_port(orchestrator.config.repo_root, control_api.port)
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        await orchestrator.startup()
        await orchestrator.run_loop()
    finally:
        if control_api:
            await control_api.stop()


async def run_web_dashboard_mode(
    orchestrator, config: "Config", args: argparse.Namespace, api_port: int | None
) -> None:
    """Run orchestrator with web dashboard."""
    import signal
    from .web import run_with_web_dashboard, trigger_server_shutdown
    from .control_api_server import ControlAPIServer

    def handle_signal():
        if orchestrator.shutdown_requested:
            orchestrator.request_shutdown(force=True)
            trigger_server_shutdown()
        else:
            orchestrator.request_shutdown()
            trigger_server_shutdown()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, handle_signal)
    loop.add_signal_handler(signal.SIGTERM, handle_signal)

    declare_no_control_api(orchestrator, api_port)

    control_api = None
    if api_port is not None:
        if api_port != 0:
            console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
            from ..infra.repo_lock import set_lock_http_port

            set_lock_http_port(orchestrator.config.repo_root, control_api.port)
            if api_port == 0:
                console.print(
                    f"[dim]Control API on http://127.0.0.1:{control_api.port}[/dim]"
                )
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        port = args.port if args.port != 8080 else config.web_port
        from ..infra.repo_lock import set_lock_http_port

        def publish_dashboard_port(actual_port: int) -> None:
            set_lock_http_port(orchestrator.config.repo_root, actual_port)

        await run_with_web_dashboard(
            orchestrator,
            port=port,
            on_server_started=publish_dashboard_port,
        )
    finally:
        if control_api:
            await control_api.stop()


async def run_tui_dashboard(
    orchestrator, config: "Config", api_port: int | None
) -> bool:
    """Run orchestrator with TUI dashboard."""
    from .control_api_server import ControlAPIServer
    from .dashboard import run_with_dashboard

    declare_no_control_api(orchestrator, api_port)

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        await control_api.start()
        from ..infra.repo_lock import set_lock_http_port

        set_lock_http_port(orchestrator.config.repo_root, control_api.port)

    try:
        await orchestrator.startup()
        return await run_with_dashboard(orchestrator, config.ui_mode)
    finally:
        if control_api:
            await control_api.stop()


@dataclass(frozen=True, slots=True)
class _CliEnginePorts:
    api: int | None
    dashboard: int
    advertised: int | None


def _resolve_cli_engine_ports(
    args: argparse.Namespace,
    config: "Config",
) -> _CliEnginePorts:
    requested_api = getattr(args, "api_port", None)
    api = requested_api if requested_api is not None else config.control_api_port
    requested_dashboard = getattr(args, "port", 8080)
    dashboard = requested_dashboard if requested_dashboard != 8080 else config.web_port
    advertised = (
        dashboard
        if not getattr(args, "no_dashboard", False) and config.ui_mode == "web"
        else api
    )
    return _CliEnginePorts(api=api, dashboard=dashboard, advertised=advertised)


def _existing_engine_blocks_cli_start(config: "Config") -> bool:
    from ..execution.control_center_runtime import (
        inspect_repository_orchestrator_ownership,
    )
    from ..infra import supervisor
    from ..infra.repo_lock import is_locked, read_lock

    ownership = inspect_repository_orchestrator_ownership(
        config.repo_root,
        config.launch_selection,
    )
    if ownership.conflicting:
        active = ownership.conflicting[0]
        console.print(
            "[red]Start aborted: a Repository Engine is already running under "
            f"{active['active_selection']['mode']}/"
            f"{active['active_selection']['config_name']} "
            f"on port {active['port']}.[/red]"
        )
        return True
    if ownership.matching and not is_locked(config.repo_root):
        console.print(
            "[red]Start aborted: an untracked Repository Engine is already "
            f"running on port {ownership.matching[0]['port']}.[/red]"
        )
        return True
    if not is_locked(config.repo_root):
        return False
    info = read_lock(config.repo_root)
    if info:
        console.print(
            f"[yellow]Orchestrator already running (pid={info.pid}, "
            f"port={info.http_port}).[/yellow]"
        )
    if not sys.stdin.isatty():
        console.print(
            "[red]Non-interactive start aborted "
            "(orchestrator already running).[/red]"
        )
        return True
    choice = console.input("Abort start? [Y/n]: ").strip().lower() or "y"
    if choice in {"y", "yes"}:
        return True
    console.print("[yellow]Stopping existing orchestrator...[/yellow]")
    stopped = supervisor.stop(
        config.repo_root,
        force=True,
        reason="cli start: replacing existing orchestrator at user prompt",
        actor="cli.start",
    )
    if not stopped:
        console.print("[red]Failed to stop existing orchestrator.[/red]")
    return not stopped


def _publish_cli_engine_ownership(config: "Config", port: int | None) -> None:
    from ..execution.repository_engine_start import record_repository_engine_launch
    from ..infra.repo_lock import (
        acquire_lock,
        release_lock,
        repository_lifecycle_mutation,
    )

    with repository_lifecycle_mutation(config.repo_root):
        acquire_lock(
            config.repo_root,
            port,
            configuration_mode=config.configuration_mode,
            config_name=config.config_name,
            config_fingerprint=config.config_fingerprint,
        )
        try:
            record_repository_engine_launch(config.repo_root, config.launch_selection)
        except Exception:
            release_lock(config.repo_root)
            raise


def _cli_engine_operation(
    args: argparse.Namespace,
    config: "Config",
    orchestrator: Any,
    ports: _CliEnginePorts,
) -> Callable[[], Awaitable[Any]]:
    from .cli_support import client_dashboard_link

    if getattr(args, "no_dashboard", False):
        console.print("[dim]Running without dashboard UI[/dim]")
        if ports.api and ports.api != 0:
            console.print(f"[dim]Control API on http://127.0.0.1:{ports.api}[/dim]")
        return lambda: run_no_dashboard(orchestrator, ports.api)
    if config.ui_mode == "web":
        if ports.dashboard != 0:
            console.print(
                "[green]Dashboard will open at "
                f"{client_dashboard_link(ports.dashboard)}[/green]"
            )
        return lambda: run_web_dashboard_mode(orchestrator, config, args, ports.api)
    if ports.api and ports.api != 0:
        console.print(f"[dim]Control API on http://127.0.0.1:{ports.api}[/dim]")
    return lambda: run_tui_dashboard(orchestrator, config, ports.api)


def run_locked_cli_engine(
    args: argparse.Namespace,
    config: "Config",
    build_orchestrator: Callable[..., Any],
) -> int:
    """Own CLI Repository Engine conflict checks, lock lifetime, and run mode."""
    from ..infra.repo_lock import (
        AlreadyRunning,
        RepositoryLifecycleBusy,
        release_lock,
    )

    if _existing_engine_blocks_cli_start(config):
        return 1
    ports = _resolve_cli_engine_ports(args, config)

    lock_acquired = False
    try:
        _publish_cli_engine_ownership(config, ports.advertised)
        lock_acquired = True
        orchestrator = build_orchestrator(config=config)
        operation = _cli_engine_operation(args, config, orchestrator, ports)
        asyncio.run(_run_with_repo_lock_heartbeat(config.repo_root, operation))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
    except AlreadyRunning as exc:
        console.print(
            "[red]Start aborted: repository lifecycle ownership was acquired "
            f"concurrently (pid={exc.pid}).[/red]"
        )
        return 1
    except RepositoryLifecycleBusy:
        console.print(
            "[red]Start aborted: another repository lifecycle change is in "
            "progress.[/red]"
        )
        return 1
    finally:
        if lock_acquired:
            release_lock(config.repo_root)
    return 0
