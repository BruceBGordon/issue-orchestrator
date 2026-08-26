"""Hook specifications for the issue-orchestrator plugin system.

This module defines the interfaces (hook specifications) that plugins can implement.
Plugins register implementations using the @hookimpl decorator.

Usage:
    from issue_orchestrator.infra.hooks.hookspec import hookimpl

    class MyTerminalPlugin:
        @hookimpl
        def create_session(self, session_id, command, working_dir, title):
            # Implementation
            ...

Entry points are registered in pyproject.toml:
    [project.entry-points."issue_orchestrator.plugins"]
    my_plugin = "my_package:MyPlugin"
"""

import pluggy

from ...domain.terminal_launch import TerminalLaunch

# Project name for hook markers
PROJECT_NAME = "issue_orchestrator"

# Create hook specification and implementation markers
hookspec = pluggy.HookspecMarker(PROJECT_NAME)
hookimpl = pluggy.HookimplMarker(PROJECT_NAME)


class TerminalSpec:
    """Hook specifications for terminal/session management.

    Terminal plugins manage the execution environment where AI agents run.
    Examples: tmux windows, Wezterm panes, Kitty windows.

    All hooks use firstresult=True, meaning the first plugin to return
    a non-None value wins. This allows plugin priority ordering.
    """

    @hookspec(firstresult=True)
    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool | None:
        """Create a new terminal session for an agent.

        Args:
            session_id: Numeric ID (typically issue number)
            launch: Complete typed terminal launch contract
            working_dir: Working directory path
            title: Optional human-readable title
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if created, False if failed, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def session_exists(self, session_id: int, session_name: str) -> bool | None:
        """Check if a session exists and is running.

        Args:
            session_id: Numeric ID to check
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if exists, False if not, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def kill_session(self, session_id: int, session_name: str) -> bool | None:
        """Kill/close a terminal session.

        Args:
            session_id: Numeric ID to kill
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if killed, False if not found, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def discover_running_sessions(self) -> list[dict] | None:
        """Discover sessions that survived an orchestrator restart.

        Returns:
            List of dicts with {issue_number, tab_name, is_review},
            or None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def cleanup_idle_sessions(self) -> int | None:
        """Clean up sessions where the agent has exited.

        Returns:
            Number of sessions cleaned up, or None to defer.
        """

    @hookspec(firstresult=True)
    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        """Get recent output from a session.

        Args:
            session_id: Numeric ID
            lines: Number of lines to retrieve
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            Terminal output string, or None if not available/supported.
        """

    @hookspec(firstresult=True)
    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool | None:
        """Send text to a running session.

        Args:
            session_id: Numeric ID
            text: Text to send (e.g., "/exit")
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if sent, False if failed, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def session_exists_by_name(self, session_name: str) -> bool | None:
        """Check if a session exists by its full name.

        Args:
            session_name: Full session name (e.g., 'issue-123', 'review-456')

        Returns:
            True if exists, False if not, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def send_to_session_by_name(self, session_name: str, text: str) -> bool | None:
        """Send text to a running session by name.

        Args:
            session_name: Full session name (e.g., 'issue-123', 'review-456')
            text: Text to send (e.g., "/exit")

        Returns:
            True if sent, False if failed, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def focus_session(self, session_id: int, session_name: str) -> bool | None:
        """Focus/select a terminal session to bring it to the foreground.

        Args:
            session_id: Numeric ID (typically issue number)
            session_name: Full session name (e.g., "issue-123", "review-456")

        Returns:
            True if focused, False if not found, None to defer to next plugin.
        """

    # Lifecycle hooks for terminal backend initialization and cleanup

    @hookspec
    def on_orchestrator_startup(self) -> None:
        """Called when the orchestrator starts up.

        Terminal plugins should create their session/environment here.
        For tmux: creates the tmux session.
        """

    @hookspec
    def on_orchestrator_shutdown(self) -> None:
        """Called when the orchestrator shuts down.

        Terminal plugins should clean up their session/environment here.
        For tmux: kills the entire session (atomic cleanup of all windows).
        """

    @hookspec(firstresult=True)
    def terminal_health_check(self) -> dict | None:
        """Check health of the terminal backend.

        Returns a dict with health status:
        {
            "healthy": bool,
            "server_running": bool,
            "session_exists": bool,
            "error": str | None,
        }

        Returns None if not implemented.
        """


class TraceEventSpec:
    """Hook specification for trace event broadcasting.

    This is the ONLY lifecycle hook. All orchestrator events are broadcast
    through this single hook, keeping the plugin API stable.

    Event naming convention:
        {domain}.{action}

    Domains:
        - orchestrator: orchestrator.ready, orchestrator.paused, orchestrator.resumed
        - session: session.started, session.completed, session.failed
        - issue: issue.claimed, issue.blocked, issue.needs_human
        - pr: pr.created
        - review: review.requested, review.completed, review.escalated

    Plugins (SSE, IPC, logging, metrics) implement this one hook and
    filter/react to events by name. New events can be added without
    changing the hookspec.

    This is for NOTIFICATIONS only. For extension points where plugins
    can contribute/veto/alter behavior, use dedicated hooks.
    """

    @hookspec
    def on_trace_event(
        self,
        event: str,
        data: dict,
    ) -> None:
        """Broadcast a trace event to all registered sinks.

        Args:
            event: Event name (e.g., "session.started", "review.escalated")
            data: Event-specific data dictionary

        Common data fields by event type:
            session.started:
                issue_number, session_id, worktree_path, branch_name
            session.completed:
                issue_number, session_id, pr_url, runtime_minutes
            session.failed:
                issue_number, session_id, error, runtime_minutes
            issue.claimed:
                issue_number, title, agent_type
            issue.blocked:
                issue_number, reason
            issue.needs_human:
                issue_number, reason
            pr.created:
                issue_number, pr_number, pr_url, title
            review.requested:
                pr_number, issue_number, review_type
            review.completed:
                pr_number, issue_number, result, rework_count
            review.escalated:
                pr_number, issue_number, rework_count, max_rework_cycles
            orchestrator.ready:
                {}
            orchestrator.paused:
                {}
            orchestrator.resumed:
                {}
            orchestrator.state_changed:
                active_count, paused, completed_today
        """


# Keep LifecycleSpec as alias for backwards compatibility during transition
LifecycleSpec = TraceEventSpec


# Future hook specs can be added here:
#
# class AISpec:
#     """Hook specifications for AI/LLM backends."""
#
#     @hookspec(firstresult=True)
#     def build_command(self, model, prompt_path, initial_prompt, permission_mode): ...
#
#
# class IssueTrackerSpec:
#     """Hook specifications for issue tracking systems."""
#
#     @hookspec(firstresult=True)
#     def list_issues(self, labels, milestone): ...
