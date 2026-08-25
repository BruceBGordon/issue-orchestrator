"""Plugin manager for issue-orchestrator.

Creates and configures the pluggy plugin manager, registering plugins
based on configuration and entry points.
"""

import logging
from pathlib import Path
from typing import Mapping, Optional, TypedDict

import pluggy

from ..domain.terminal_launch import TerminalLaunch
from ..domain.terminal_session_lifecycle import TerminalSessionWatcherPolicy
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.terminal_session_terminator import TerminalSessionTerminator

from ..infra.hooks.hookspec import PROJECT_NAME, TerminalSpec, LifecycleSpec

logger = logging.getLogger(__name__)

# Built-in plugin mapping (subprocess only - tmux support removed)
BUILTIN_PLUGINS = {
    "subprocess": "issue_orchestrator.execution.terminal_subprocess:SubprocessPlugin",
}

# UI mode to plugin mapping (all modes use subprocess)
UI_MODE_PLUGINS = {
    "web": "subprocess",
}


class _BuiltinPluginKwargs(TypedDict):
    session_terminator: TerminalSessionTerminator
    process_group_supervisor: ProcessGroupSupervisor
    watcher_policy: TerminalSessionWatcherPolicy
    session_interactions_enabled: bool
    worktree_base: Path | None


def _load_plugin_class(
    class_path: str,
    *,
    constructor_kwargs: Mapping[str, object] | None = None,
):
    """Load a plugin class by its dotted path.

    Args:
        class_path: Path like "package.module:ClassName"
        constructor_kwargs: Optional kwargs forwarded to the plugin constructor.

    Returns:
        Instantiated plugin object.
    """
    import importlib

    if ":" in class_path:
        module_path, class_name = class_path.rsplit(":", 1)
    else:
        module_path, class_name = class_path.rsplit(".", 1)

    module = importlib.import_module(module_path)
    plugin_class = getattr(module, class_name)
    return plugin_class(**dict(constructor_kwargs or {}))


def create_plugin_manager(
    terminal_session_terminator: TerminalSessionTerminator,
    process_group_supervisor: ProcessGroupSupervisor,
    watcher_policy: TerminalSessionWatcherPolicy,
    terminal_plugin: Optional[str] = None,
    ui_mode: str = "web",
    session_interactions_enabled: bool = False,
    worktree_base: Path | None = None,
    load_entry_points: bool = True,
) -> pluggy.PluginManager:
    """Create and configure a plugin manager.

    Args:
        terminal_session_terminator: Required behavior-level owner for stopping
            persisted terminal sessions.
        process_group_supervisor: Required child-process owner for containing
            sessions that fail before durable registry publication.
        watcher_policy: Required shutdown watchdog for the PTY completion owner.
        terminal_plugin: Explicit terminal plugin to load.
            Can be: "tmux" or a class path like "mypackage:MyPlugin"
        ui_mode: Fallback UI mode if terminal_plugin not specified.
        load_entry_points: Whether to load plugins from entry points.

    Returns:
        Configured PluginManager with hooks ready to call.
    """
    pm = pluggy.PluginManager(PROJECT_NAME)
    if not isinstance(terminal_session_terminator, TerminalSessionTerminator):
        raise ValueError(
            "create_plugin_manager.terminal_session_terminator must be a "
            "TerminalSessionTerminator"
        )
    if not isinstance(process_group_supervisor, ProcessGroupSupervisor):
        raise ValueError(
            "create_plugin_manager.process_group_supervisor must implement "
            "ProcessGroupSupervisor"
        )
    if type(watcher_policy) is not TerminalSessionWatcherPolicy:
        raise ValueError(
            "create_plugin_manager.watcher_policy must be "
            "TerminalSessionWatcherPolicy"
        )

    # Register hook specifications
    pm.add_hookspecs(TerminalSpec)
    pm.add_hookspecs(LifecycleSpec)

    # Determine which terminal plugin to load
    if terminal_plugin:
        plugin_ref = terminal_plugin
    else:
        # Fall back to UI mode mapping (default is subprocess)
        plugin_ref = UI_MODE_PLUGINS.get(ui_mode, "subprocess")

    # Load the terminal plugin
    if plugin_ref in BUILTIN_PLUGINS:
        class_path = BUILTIN_PLUGINS[plugin_ref]
    else:
        class_path = plugin_ref

    try:
        plugin_kwargs: _BuiltinPluginKwargs | None = None
        if plugin_ref in BUILTIN_PLUGINS:
            plugin_kwargs = _BuiltinPluginKwargs(
                session_terminator=terminal_session_terminator,
                process_group_supervisor=process_group_supervisor,
                watcher_policy=watcher_policy,
                session_interactions_enabled=session_interactions_enabled,
                worktree_base=worktree_base,
            )
        plugin = _load_plugin_class(class_path, constructor_kwargs=plugin_kwargs)
        pm.register(plugin, name=f"terminal_{plugin_ref}")
        logger.info("Loaded terminal plugin: %s", plugin_ref)
    except Exception as e:
        logger.error("Failed to load terminal plugin %s: %s", plugin_ref, e)
        raise

    # Load additional plugins from entry points
    if load_entry_points:
        # Entry point group: "issue_orchestrator.plugins"
        pm.load_setuptools_entrypoints(f"{PROJECT_NAME}.plugins")
        logger.debug("Loaded plugins from entry points")

    return pm


class PluginManager:
    """High-level wrapper around pluggy for issue-orchestrator.

    Provides a cleaner interface for the orchestrator to call hooks.
    """

    def __init__(
        self,
        terminal_session_terminator: TerminalSessionTerminator,
        process_group_supervisor: ProcessGroupSupervisor,
        watcher_policy: TerminalSessionWatcherPolicy,
        terminal_plugin: Optional[str] = None,
        ui_mode: str = "web",
        session_interactions_enabled: bool = False,
        worktree_base: Path | None = None,
    ):
        """Initialize the plugin manager.

        Args:
            terminal_session_terminator: Required behavior-level owner for
                stopping persisted terminal sessions.
            terminal_plugin: Explicit terminal plugin to load.
            ui_mode: Fallback UI mode for backwards compatibility.
        """
        self._pm = create_plugin_manager(
            terminal_session_terminator=terminal_session_terminator,
            process_group_supervisor=process_group_supervisor,
            watcher_policy=watcher_policy,
            terminal_plugin=terminal_plugin,
            ui_mode=ui_mode,
            session_interactions_enabled=session_interactions_enabled,
            worktree_base=worktree_base,
        )

    @property
    def hook(self):
        """Access the hook caller for direct hook invocation."""
        return self._pm.hook

    # Convenience methods that wrap hook calls with sensible defaults

    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        """Create a terminal session."""
        result = self._pm.hook.create_session(
            session_id=session_id,
            launch=launch,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )
        return result if result is not None else False

    def session_exists(self, session_id: int, session_name: str) -> bool:
        """Check if a session exists."""
        result = self._pm.hook.session_exists(session_id=session_id, session_name=session_name)
        return result if result is not None else False

    def kill_session(self, session_id: int, session_name: str) -> None:
        """Kill a session."""
        self._pm.hook.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[dict]:
        """Discover running sessions."""
        result = self._pm.hook.discover_running_sessions()
        return result if result is not None else []

    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions."""
        result = self._pm.hook.cleanup_idle_sessions()
        return result if result is not None else 0

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        """Get session output."""
        return self._pm.hook.get_session_output(
            session_id=session_id,
            lines=lines,
            session_name=session_name,
        )

    def register_plugin(self, plugin: object, name: str | None = None) -> None:
        """Register an additional plugin.

        Args:
            plugin: Plugin instance implementing hook methods
            name: Optional name for the plugin
        """
        self._pm.register(plugin, name=name)
        logger.info("Registered plugin: %s", name or type(plugin).__name__)

    # Trace event emission

    def emit(self, event: str, data: dict | None = None) -> None:
        """Emit a trace event to all registered plugins.

        This is the single entry point for all lifecycle notifications.
        Events are broadcast to all plugins implementing on_trace_event
        (SSE, IPC, logging, metrics, etc.).

        Event naming convention: {domain}.{action}
            - session.started, session.completed, session.failed
            - issue.claimed, issue.blocked, issue.needs_human
            - pr.created
            - review.requested, review.completed, review.escalated
            - orchestrator.ready, orchestrator.paused, orchestrator.resumed

        Args:
            event: Event name (e.g., "session.started")
            data: Event-specific data dictionary
        """
        self._pm.hook.on_trace_event(event=event, data=data or {})
