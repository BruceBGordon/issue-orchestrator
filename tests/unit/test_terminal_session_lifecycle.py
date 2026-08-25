"""Typed ownership tests for terminal startup and watcher shutdown."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from issue_orchestrator.domain.terminal_session_lifecycle import (
    TerminalSessionWatcherCompleted,
    TerminalSessionWatcherPolicy,
)
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
)
from issue_orchestrator.execution.agent_runner import (
    AgentResult,
    AgentRunner,
    AgentSession,
    AgentSpec,
)
from issue_orchestrator.execution.terminal_session_lifecycle import (
    TerminalSessionWatcher,
    TerminalSessionWatcherShutdownError,
)
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.infra.env import ENV_PREFIX
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from tests.unit.terminal_session_termination_helpers import (
    RecordingTerminalSessionTerminator,
)


def _completed_agent_session(tmp_path: Path) -> AgentSession:
    return AgentRunner().start(
        AgentSpec(
            command=[sys.executable, "-c", "raise SystemExit(0)"],
            working_dir=tmp_path,
            timeout_seconds=60,
            output_dir=tmp_path,
            log_path=(tmp_path / "watcher-recording.jsonl").resolve(),
        )
    )


def test_plugin_retains_live_watcher_until_typed_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    session = _completed_agent_session(tmp_path)
    original_wait = session.wait
    watcher_entered = threading.Event()
    watcher_release = threading.Event()

    def blocked_wait(timeout: float | None = None) -> AgentResult:
        watcher_entered.set()
        PROCESS_COMPLETION_WATCHDOG.wait_for_event(
            watcher_release,
            operation="blocked terminal watcher release",
        )
        return original_wait(timeout)

    monkeypatch.setattr(session, "wait", blocked_wait)
    watcher = TerminalSessionWatcher.start("issue-903", session)
    PROCESS_COMPLETION_WATCHDOG.wait_for_event(
        watcher_entered,
        operation="terminal watcher entered its owned wait",
    )
    plugin = SubprocessPlugin(
        RecordingTerminalSessionTerminator(),
        build_process_group_supervisor(),
        TerminalSessionWatcherPolicy(0.01),
    )
    plugin._sessions["issue-903"] = session  # noqa: SLF001
    plugin._session_watchers["issue-903"] = watcher  # noqa: SLF001

    with pytest.raises(
        TerminalSessionWatcherShutdownError,
        match="remained live",
    ):
        plugin._cleanup_session("issue-903")  # noqa: SLF001

    assert plugin._sessions["issue-903"] is session  # noqa: SLF001
    assert plugin._session_watchers["issue-903"] is watcher  # noqa: SLF001

    watcher_release.set()
    assert type(
        watcher.await_completion(TerminalSessionWatcherPolicy(120.0))
    ) is TerminalSessionWatcherCompleted
    plugin._cleanup_session("issue-903")  # noqa: SLF001

    assert plugin._sessions == {}  # noqa: SLF001
    assert plugin._session_watchers == {}  # noqa: SLF001
