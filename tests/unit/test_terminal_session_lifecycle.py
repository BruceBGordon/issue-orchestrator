"""Public-boundary ownership tests for terminal watcher shutdown."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
    TerminalRunDestination,
    TerminalShell,
)
from issue_orchestrator.domain.terminal_session_lifecycle import (
    TerminalSessionWatcherCompleted,
    TerminalSessionWatcherPolicy,
)
from issue_orchestrator.execution.agent_runner import AgentResult, AgentSession
from issue_orchestrator.execution.retained_thread import (
    MaskedThreadStartPrimitive,
    ThreadingRetainedThreadFactory,
)
from issue_orchestrator.execution.terminal_session_lifecycle import (
    TerminalSessionWatcher,
    TerminalSessionWatcherShutdownError,
)
from issue_orchestrator.execution.terminal_session_registry import (
    SqliteTerminalSessionRegistry,
)
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from tests.unit.terminal_session_owner_helpers import RecordingTerminalSessionOwner
from tests.unit.terminal_session_termination_helpers import (
    RecordingTerminalSessionTerminator,
)


class BlockingTerminalSessionWatcherFactory:
    """Start a real watcher whose public session wait has an explicit handoff."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.entered = threading.Event()
        self.release = threading.Event()
        self._watchers: list[TerminalSessionWatcher] = []

    def create(
        self,
        session_name: str,
        session: AgentSession,
    ) -> TerminalSessionWatcher:
        original_wait = session.wait

        def blocked_wait(timeout: float | None = None) -> AgentResult:
            self.entered.set()
            PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                self.release,
                operation="blocked terminal watcher release",
            )
            return original_wait(timeout)

        self._monkeypatch.setattr(session, "wait", blocked_wait)
        watcher = TerminalSessionWatcher(
            session_name,
            session,
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive()),
        )
        self._watchers.append(watcher)
        return watcher

    def require_completed(self) -> None:
        if len(self._watchers) != 1:
            raise AssertionError(
                "blocking watcher factory must own exactly one watcher"
            )
        outcome = self._watchers[0].await_completion(
            TerminalSessionWatcherPolicy(PROCESS_COMPLETION_WATCHDOG.timeout_seconds)
        )
        if type(outcome) is not TerminalSessionWatcherCompleted:
            raise AssertionError(f"terminal watcher did not complete: {outcome!r}")


def test_plugin_retains_session_until_watcher_proves_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = (tmp_path / "repo").resolve()
    worktree = (repo_root / "wt").resolve()
    run_dir = (worktree / ".issue-orchestrator/sessions/issue-903").resolve()
    run_dir.mkdir(parents=True)
    registry = SqliteTerminalSessionRegistry(repo_root)
    watcher_factory = BlockingTerminalSessionWatcherFactory(monkeypatch)
    plugin = SubprocessPlugin(
        RecordingTerminalSessionTerminator(),
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        TerminalSessionWatcherPolicy(0.01),
        watcher_factory,
    )
    launch = TerminalLaunch(
        shell_command=f"{sys.executable} -c 'raise SystemExit(0)'",
        shell=TerminalShell.BASH,
        interaction_intent=TerminalInteractionIntent.NONE,
        destination=TerminalRunDestination(
            run_dir,
            (run_dir / "terminal-recording.jsonl").resolve(),
        ),
    )

    assert (
        plugin.create_session(
            session_id=903,
            launch=launch,
            working_dir=str(worktree),
            title="Watcher lifecycle",
            session_name="issue-903",
        )
        is True
    )
    PROCESS_COMPLETION_WATCHDOG.wait_for_event(
        watcher_factory.entered,
        operation="terminal watcher entered its owned wait",
    )

    try:
        with pytest.raises(
            TerminalSessionWatcherShutdownError,
            match="remained live",
        ):
            plugin.kill_session(903, "issue-903")
        assert tuple(registry.load()) == ("issue-903",)
    finally:
        watcher_factory.release.set()

    watcher_factory.require_completed()
    assert plugin.kill_session(903, "issue-903") is True
    assert registry.load() == {}
