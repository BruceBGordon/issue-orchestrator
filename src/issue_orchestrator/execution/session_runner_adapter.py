"""Session runner adapter wrapping pluggy terminal hooks.

This adapter implements the SessionRunner port by delegating to pluggy hooks.
It's the bridge between the core's abstract session management and the concrete
terminal implementation.

This is the ONLY place pluggy is used for session management.
"""

import logging
from typing import TYPE_CHECKING

from ..ports.session_runner import DiscoveredSession
from ..domain.terminal_launch import TerminalLaunch

if TYPE_CHECKING:
    import pluggy

logger = logging.getLogger(__name__)


class PluggySessionRunner:
    """SessionRunner implementation that delegates to pluggy terminal hooks.

    This adapter:
    - Implements the SessionRunner protocol
    - Wraps a pluggy PluginManager
    - Delegates to terminal hooks (create_session, session_exists, etc.)
    """

    def __init__(self, plugin_manager: "pluggy.PluginManager"):
        """Initialize with a configured pluggy PluginManager.

        Args:
            plugin_manager: A pluggy PluginManager with terminal hooks registered.
        """
        self._pm = plugin_manager

    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        """Create a terminal session via pluggy hook."""
        logger.info(
            "Creating session via terminal hook: id=%s name=%s title=%s cwd=%s command=%s",
            session_id,
            session_name,
            title,
            working_dir,
            launch.shell_command,
        )
        result = self._pm.hook.create_session(
            session_id=session_id,
            command=launch.shell_command,
            interaction_intent=launch.interaction_intent,
            shell=launch.shell,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )
        logger.info("Create session result: id=%s result=%s", session_id, result)
        return result if result is not None else False

    def session_exists(self, session_id: int, session_name: str) -> bool:
        """Check if session exists via pluggy hook."""
        result = self._pm.hook.session_exists(session_id=session_id, session_name=session_name)
        return result if result is not None else False

    def kill_session(self, session_id: int, session_name: str) -> None:
        """Kill session via pluggy hook."""
        self._pm.hook.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[DiscoveredSession]:
        """Discover running sessions via pluggy hook."""
        result = self._pm.hook.discover_running_sessions()
        return result if result is not None else []

    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions via pluggy hook."""
        result = self._pm.hook.cleanup_idle_sessions()
        return result if result is not None else 0

    def get_session_output(
        self,
        session_id: int,
        lines: int,
        session_name: str,  # Required - caller must provide explicit name
    ) -> str | None:
        """Get session output via pluggy hook."""
        return self._pm.hook.get_session_output(
            session_id=session_id,
            lines=lines,
            session_name=session_name,
        )

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        """Send text to a session via pluggy hook."""
        result = self._pm.hook.send_to_session(
            session_id=session_id,
            text=text,
            session_name=session_name,
        )
        return result if result is not None else False

    def session_exists_by_name(self, session_name: str) -> bool:
        """Check if session exists by name via pluggy hook."""
        result = self._pm.hook.session_exists_by_name(session_name=session_name)
        return result if result is not None else False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        """Send text to a session by name via pluggy hook."""
        result = self._pm.hook.send_to_session_by_name(session_name=session_name, text=text)
        return result if result is not None else False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        """Focus a terminal session via pluggy hook."""
        result = self._pm.hook.focus_session(session_id=session_id, session_name=session_name)
        return result if result is not None else False

    # Lifecycle hooks for terminal backend initialization and cleanup

    def on_orchestrator_startup(self) -> None:
        """Call the orchestrator startup hook.

        Terminal plugins should create their session/environment here.
        """
        logger.info("Calling on_orchestrator_startup hook")
        self._pm.hook.on_orchestrator_startup()

    def on_orchestrator_shutdown(self) -> None:
        """Call the orchestrator shutdown hook.

        Terminal plugins should clean up their session/environment here.
        """
        logger.info("Calling on_orchestrator_shutdown hook")
        self._pm.hook.on_orchestrator_shutdown()

    def terminal_health_check(self) -> dict | None:
        """Check health of the terminal backend.

        Returns:
            Dict with health status, or None if no terminal plugin.
        """
        result = self._pm.hook.terminal_health_check()
        return result
