"""Session runner port for terminal session management.

This port defines the interface for managing terminal sessions where AI agents
run. The orchestrator calls these methods without knowing the implementation
details (currently subprocess-based).

This abstraction keeps terminal backend details out of the core.
"""

from typing import NotRequired, Protocol, TypedDict

from ..domain.terminal_launch import TerminalLaunch


class DiscoveredSession(TypedDict):
    """Session info discovered during orchestrator restart recovery."""

    issue_number: int
    tab_name: str
    is_review: bool
    run_dir: str
    session_name: NotRequired[str]


class SessionRunner(Protocol):
    """Port for managing terminal sessions.

    The orchestrator doesn't know or care about the specific backend.

    Note: session_name is required - callers must compute the name explicitly
    (e.g., "issue-123" or "review-456"). No fallback logic in implementations.
    """

    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        """Create a new terminal session for an agent.

        Args:
            session_id: Numeric ID (typically issue number)
            launch: Typed shell command and interaction semantics
            working_dir: Working directory path
            title: Optional human-readable title
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if created successfully, False otherwise.
        """
        ...

    def session_exists(self, session_id: int, session_name: str) -> bool:
        """Check if a session exists and is running.

        Args:
            session_id: Numeric ID to check
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if session exists and is running.
        """
        ...

    def kill_session(self, session_id: int, session_name: str) -> None:
        """Kill/close a terminal session.

        Args:
            session_id: Numeric ID to kill
            session_name: Full session name (e.g., "issue-123", "review-456")
        """
        ...

    def discover_running_sessions(self) -> list[DiscoveredSession]:
        """Discover sessions that survived an orchestrator restart.

        Returns:
            List of session info dicts (issue_number, tab_name, is_review).
        """
        ...

    def cleanup_idle_sessions(self) -> int:
        """Clean up sessions where the agent has exited.

        Returns:
            Number of sessions cleaned up.
        """
        ...

    def get_session_output(
        self,
        session_id: int,
        lines: int,
        session_name: str,  # Required - caller must provide explicit name
    ) -> str | None:
        """Get recent output from a session.

        Args:
            session_id: Numeric ID
            lines: Number of lines to retrieve
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            Terminal output string, or None if not available.
        """
        ...

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        """Send text to a running session.

        Args:
            session_id: Numeric ID
            text: Text to send (e.g., "/exit")
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if sent successfully, False otherwise.
        """
        ...

    def session_exists_by_name(self, session_name: str) -> bool:
        """Check if a session exists by its full name.

        Args:
            session_name: Full session name (e.g., 'issue-123', 'review-456')

        Returns:
            True if session exists and is running.
        """
        ...

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        """Send text to a running session by name.

        Args:
            session_name: Full session name (e.g., 'issue-123', 'review-456')
            text: Text to send (e.g., "/exit")

        Returns:
            True if sent successfully, False otherwise.
        """
        ...

    def focus_session(self, session_id: int, session_name: str) -> bool:
        """Focus/select a terminal session to bring it to the foreground.

        Args:
            session_id: Numeric ID (typically issue number)
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if focused successfully, False otherwise.
        """
        ...

    # Lifecycle hooks for terminal backend initialization and cleanup

    def on_orchestrator_startup(self) -> None:
        """Called when the orchestrator starts up.

        Terminal backends should create their session/environment here.
        """
        ...

    def on_orchestrator_shutdown(self) -> None:
        """Called when the orchestrator shuts down.

        Terminal backends should clean up their session/environment here.
        """
        ...

    def terminal_health_check(self) -> dict[str, object] | None:
        """Check health of the terminal backend.

        Returns:
            Dict with health status, or None if not implemented.
            Expected keys: healthy, server_running, session_exists, error, backend
        """
        ...


class NullSessionRunner:
    """No-op session runner for testing."""

    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,
    ) -> bool:
        return True

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return False

    def kill_session(self, session_id: int, session_name: str) -> None:
        pass

    def discover_running_sessions(self) -> list[DiscoveredSession]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(
        self,
        session_id: int,
        lines: int,
        session_name: str,
    ) -> str | None:
        return None

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        pass

    def on_orchestrator_shutdown(self) -> None:
        pass

    def terminal_health_check(self) -> dict[str, object] | None:
        return None
