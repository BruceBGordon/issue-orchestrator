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
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from ..infra.config import Config

console = Console()


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
    from .control_api import ControlAPIServer

    declare_no_control_api(orchestrator, api_port)

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
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
    from .control_api import ControlAPIServer

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
            if api_port == 0:
                console.print(
                    f"[dim]Control API on http://127.0.0.1:{control_api.port}[/dim]"
                )
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        port = args.port if args.port != 8080 else config.web_port
        await run_with_web_dashboard(orchestrator, port=port)
    finally:
        if control_api:
            await control_api.stop()


async def run_tui_dashboard(
    orchestrator, config: "Config", api_port: int | None
) -> bool:
    """Run orchestrator with TUI dashboard."""
    from .control_api import ControlAPIServer
    from .dashboard import run_with_dashboard

    declare_no_control_api(orchestrator, api_port)

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        await control_api.start()

    try:
        await orchestrator.startup()
        return await run_with_dashboard(orchestrator, config.ui_mode)
    finally:
        if control_api:
            await control_api.stop()
