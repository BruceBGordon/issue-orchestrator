"""Unit tests for plugin manager loading."""

from pathlib import Path

from issue_orchestrator.execution.manager import create_plugin_manager
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
    terminal_session_watcher_policy,
)
from tests.unit.terminal_session_termination_helpers import (
    RecordingTerminalSessionTerminator,
)


def test_subprocess_plugin_can_load():
    """Ensure subprocess plugin mapping resolves to a valid class."""
    pm = create_plugin_manager(
        RecordingTerminalSessionTerminator(),
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        terminal_plugin="subprocess",
        load_entry_points=False,
    )
    assert pm is not None


def test_subprocess_plugin_receives_session_interaction_kwargs(tmp_path):
    session_terminator = RecordingTerminalSessionTerminator()
    pm = create_plugin_manager(
        session_terminator,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        terminal_plugin="subprocess",
        session_interactions_enabled=True,
        worktree_base=tmp_path,
        load_entry_points=False,
    )

    plugin = next(plugin for plugin in pm.get_plugins() if type(plugin).__name__ == "SubprocessPlugin")

    assert plugin._session_interactions_enabled is True  # noqa: SLF001
    assert plugin._worktree_base == Path(tmp_path).resolve()  # noqa: SLF001
    assert plugin._session_terminator is session_terminator  # noqa: SLF001
