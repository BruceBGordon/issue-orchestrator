"""Unified dashboard server for issue-orchestrator.

This serves the unified dashboard UI and API. It manages orchestrators for
any registered repository and provides a single entry point for the user.

Usage:
    python -m issue_orchestrator.entrypoints.control_center [--port 19080]
    # Or via CLI: issue-orchestrator (no args)

The dashboard will be available at http://127.0.0.1:19080/
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn

from ._auth_middleware import install_access_log_redaction
from .control_api import (
    configure_api_token,
    control_app,
)
from ..execution.process_control import ManagedProcess, list_processes_matching, spawn_tray_helper
from ..infra import browser_session
from ..infra.api_token import resolve_agent_callback_token, resolve_api_token
from ..infra.client_urls import resolve_client_dashboard_url
from ..infra.supervisor import ENGINE_LOG_LEVEL_ENV
from ..observation.instance_detector import write_dashboard_pid, clear_dashboard_pid

logger = logging.getLogger(__name__)

# Flag to signal reaper thread to stop
_reaper_stop = threading.Event()


def _is_loopback_host(host: str) -> bool:
    """Whether ``host`` binds only to the loopback interface.

    Accepts hostnames (``localhost``, ``ip6-localhost``) and literal
    addresses (``127.0.0.0/8``, ``::1``). Anything else — including
    ``0.0.0.0``, ``::``, or a concrete LAN interface — counts as
    non-loopback so ``--dev-no-auth`` can refuse it.
    """
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _set_default_engine_log_level(debug: bool) -> None:
    if debug:
        os.environ.setdefault(ENGINE_LOG_LEVEL_ENV, "DEBUG")


class _TrayProcessHandle:
    """Handle for tray helper subprocess lifecycle."""

    def __init__(self, process: ManagedProcess) -> None:
        self._process = process

    def stop(self) -> None:
        """Stop tray helper process with terminate then kill fallback."""
        if self._process.poll() is not None:
            return
        self._process.stop(graceful_timeout_seconds=2)


def _cleanup_stale_tray_helpers(dashboard_url: str) -> None:
    """Terminate stale tray helpers targeting the same dashboard URL."""
    current_pid = os.getpid()
    for pid, cmd in list_processes_matching("issue_orchestrator.entrypoints.tray"):
        if pid == current_pid:
            continue
        if f"--dashboard-url {dashboard_url}" not in cmd:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Terminated stale tray helper pid=%d", pid)
        except ProcessLookupError:
            continue
        except Exception:
            logger.debug("Failed to terminate stale tray helper pid=%d", pid, exc_info=True)


def _zombie_reaper(interval: float = 1.0) -> None:
    """Background thread to reap zombie orchestrator child processes.

    Only reaps PIDs that were registered via control_api.track_child_pid().
    This avoids racing with subprocess.run() for unrelated children (e.g.,
    hook verification).

    This is needed because:
    - Control center starts orchestrators as child processes
    - When orchestrators exit, they become zombies until reaped
    - We can't use SIGCHLD=SIG_IGN because it breaks subprocess.run() on macOS
    - This thread periodically reaps tracked zombie PIDs without affecting
      subprocess exit codes for unrelated children
    """
    from .control_api import get_tracked_pids, untrack_child_pid

    while not _reaper_stop.is_set():
        pids_to_check = get_tracked_pids()

        for pid in pids_to_check:
            try:
                # Try to reap this specific PID (non-blocking)
                reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
                if reaped_pid != 0:
                    logger.debug("Reaped zombie orchestrator process %d", reaped_pid)
                    untrack_child_pid(reaped_pid)
            except ChildProcessError:
                # Process doesn't exist or isn't our child - remove from tracking
                untrack_child_pid(pid)
            except Exception as e:
                logger.debug("Error reaping PID %d: %s", pid, e)

        _reaper_stop.wait(interval)


def _open_browser(url: str, delay: float = 1.0) -> None:
    """Open browser after a short delay to let server start."""
    time.sleep(delay)
    webbrowser.open(url)


def _start_tray_icon(url: str) -> Any | None:
    """Start the system tray icon, returning the icon or None on failure."""
    try:
        if sys.platform == "darwin":
            _cleanup_stale_tray_helpers(url)
            process = spawn_tray_helper(dashboard_url=url, owner_pid=os.getpid())
            time.sleep(0.15)
            if process.poll() is not None:
                raise RuntimeError(f"tray helper exited immediately with code {process.returncode}")
            logger.debug("Started tray helper process pid=%d", process.pid)
            return _TrayProcessHandle(process)

        from .tray import start_tray
        from .control_api import get_supervisor, _preferred_repo_root
        from ..execution.control_center_repo_status import build_repos_status

        def _get_engine_status() -> list[tuple[str, str]]:
            try:
                return [
                    (r["name"], r.get("status", {}).get("state", "unknown"))
                    for r in build_repos_status(
                        supervisor=get_supervisor(),
                        preferred_repo_root=_preferred_repo_root(),
                    )
                ]
            except Exception:
                return []

        icon = start_tray(dashboard_url=url, engine_status_fn=_get_engine_status)
        logger.debug("System tray icon started")
        return icon
    except Exception as exc:
        logger.warning(
            "System tray icon unavailable: %s",
            exc,
            exc_info=True,
        )
        return None


def _setup_control_center_file_log(log_level: int) -> None:
    """Persist the Control Center's log to a file — not just stderr / the
    launching terminal — so its supervisor records (starting, stopping, and
    killing orchestrator instances) survive the terminal closing and stay
    attributable after the fact.
    """
    from ..infra.logging_config import (
        add_rotating_file_handler,
        get_control_center_log_path,
    )

    try:
        cc_log_path = get_control_center_log_path()
        if add_rotating_file_handler(cc_log_path, level=log_level):
            logger.info("Control Center logging to %s", cc_log_path)
    except OSError as exc:
        logger.warning("Could not set up Control Center file log: %s", exc)


def main() -> int:
    """Run the standalone control center server."""
    # NOTE: Cannot use signal.signal(signal.SIGCHLD, signal.SIG_IGN) to auto-reap
    # zombie child processes because on macOS it breaks subprocess.run() by causing
    # waitpid() to return exit code 0 instead of the actual exit code. Instead, we
    # use a background thread (_zombie_reaper) that periodically calls waitpid()
    # with WNOHANG to reap zombies without affecting subprocess exit codes.

    parser = argparse.ArgumentParser(
        description="Issue Orchestrator Control Center - manage orchestrators",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=19080,
        help="Port to listen on (default: 19080)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Enable HTTP access logs",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open browser automatically",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Don't show system tray icon",
    )
    parser.add_argument(
        "--dev-no-auth",
        action="store_true",
        help=(
            "DEVELOPMENT ONLY: disable Control API authentication. "
            "Any local process can mutate state. Also triggered by "
            "ISSUE_ORCHESTRATOR_DEV_NO_AUTH=1. Never use on a shared "
            "host or in production."
        ),
    )

    args = parser.parse_args()
    if os.environ.get("ISSUE_ORCHESTRATOR_DEV_NO_AUTH") == "1":
        args.dev_no_auth = True

    # Validate the --dev-no-auth + --host combination before any
    # startup side effect runs. ``write_dashboard_pid``, the browser
    # auto-open thread, and the reaper all touch observable state
    # (PID file on disk, a browser tab, a background thread); if we
    # refused the combination after them we would leave behind a
    # stale dashboard-detection PID file and potentially open the
    # operator's browser to a server that never bound. Tracked as
    # #6017 re-review-4 P2.
    if args.dev_no_auth and not _is_loopback_host(args.host):
        # Use ``print`` + no logger: ``logging.basicConfig`` has not
        # been called yet in this pre-side-effects branch, so a
        # logger call would go to the root default handler. Keep
        # the refusal message visible on stderr for scripts.
        print(
            f"Refusing --dev-no-auth with --host {args.host}. "
            "Only loopback binds are allowed in dev-no-auth mode.",
            flush=True,
        )
        return 2

    # Default engine target repo to where Control Center was launched from.
    os.environ.setdefault("ISSUE_ORCHESTRATOR_CC_REPO_ROOT", str(Path.cwd().resolve()))
    _set_default_engine_log_level(args.debug)

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Suppress httpx INFO logging (polls orchestrator status every 3s)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _setup_control_center_file_log(log_level)

    # Clean up stale repos from registry (e.g., deleted directories, old test paths)
    from ..infra.repo_registry import cleanup_stale_repos
    stale_count = cleanup_stale_repos()
    if stale_count > 0:
        logger.info("Cleaned up %d stale repo(s) from registry", stale_count)

    url = resolve_client_dashboard_url(args.port, local_host=args.host)
    print(f"Starting Issue Orchestrator Dashboard on {url}")
    print("Press Ctrl+C to stop")

    # Write PID file for detection by CLI
    write_dashboard_pid(args.port)

    # Open browser in background thread after server starts
    if not args.no_browser:
        thread = threading.Thread(target=_open_browser, args=(url,), daemon=True)
        thread.start()

    # Start zombie reaper thread to clean up exited orchestrator processes
    # Only on Unix-like systems where waitpid is available
    if hasattr(os, "waitpid") and os.name != "nt":
        _reaper_stop.clear()
        reaper_thread = threading.Thread(target=_zombie_reaper, daemon=True)
        reaper_thread.start()

    # Activate bearer-token auth on the Control API before binding the
    # port. ``control_center`` serves ``control_app`` directly (outside
    # of ``ControlAPIServer.start``), so if we skip this call every
    # request lands on an unauthenticated endpoint — the exact hole
    # flagged in #6017 review (P1 on #6011).
    if args.dev_no_auth:
        # The non-loopback refusal already ran up-top, before any
        # startup side effects. By the time we get here we know the
        # operator asked for a loopback dev-no-auth mode; just log,
        # banner, and wire the middleware to pass-through.
        logger.error(
            "⚠  Control Center running with --dev-no-auth: "
            "authentication is DISABLED. Any local process can mutate "
            "state. DO NOT use on a shared host or in production. "
            "(set via %s)",
            "ISSUE_ORCHESTRATOR_DEV_NO_AUTH=1"
            if os.environ.get("ISSUE_ORCHESTRATOR_DEV_NO_AUTH") == "1"
            else "--dev-no-auth",
        )
        print(
            "\n\033[1;31m"
            "⚠  AUTH DISABLED (--dev-no-auth). Any local process can "
            "mutate Control API state. Dev only."
            "\033[0m\n",
            flush=True,
        )
        # Explicitly clear any preexisting auth state. If the module
        # globals were seeded from an earlier server run, or if the
        # operator exported ``ISSUE_ORCHESTRATOR_API_TOKEN`` /
        # ``ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN`` before launching
        # this CC, the middleware would still enforce against them.
        # ``--dev-no-auth`` must be an authoritative OFF, not a hope
        # that state is clean.
        configure_api_token(None, agent_callback=None)
        os.environ.pop("ISSUE_ORCHESTRATOR_API_TOKEN", None)
        os.environ.pop("ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", None)
        browser_session.initialize()
    else:
        admin_token = resolve_api_token()
        agent_callback_token = resolve_agent_callback_token()
        configure_api_token(admin_token, agent_callback=agent_callback_token)
        # Derive the HMAC secret from the admin token so the dashboard
        # process accepts the same session cookie this Control Center
        # mints. Without this, the operator would have to log in twice
        # — once on 19080, once on 8080.
        browser_session.initialize(admin_token=admin_token)
        os.environ.setdefault("ISSUE_ORCHESTRATOR_API_TOKEN", admin_token)
        os.environ["ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN"] = agent_callback_token
    # Strip SSE tokens from uvicorn access-log lines so a query param
    # that's still valid for a few seconds doesn't persist in log
    # storage (#6017 re-review-3 P2). Applies in both auth and
    # dev-no-auth modes since the log format is identical.
    install_access_log_redaction()

    # Start system tray icon (menu bar on macOS)
    tray_icon = _start_tray_icon(url) if not args.no_tray else None

    try:
        uvicorn.run(
            control_app,
            host=args.host,
            port=args.port,
            log_level="info" if not args.debug else "debug",
            access_log=args.debug_http,
        )
        return 0
    except KeyboardInterrupt:
        print("\nShutting down...")
        return 0
    except Exception as e:
        logger.exception("Control center failed: %s", e)
        return 1
    finally:
        # Stop the system tray icon
        if tray_icon is not None:
            tray_icon.stop()
        # Stop the reaper thread
        _reaper_stop.set()
        # Clear PID file
        clear_dashboard_pid()


if __name__ == "__main__":
    sys.exit(main())
