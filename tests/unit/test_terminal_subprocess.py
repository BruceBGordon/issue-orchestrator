from __future__ import annotations

import json
import time
import base64
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
    TerminalRunDestination,
    TerminalShell,
)
from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
)
from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessIdentityObservation,
)
from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationOutcome,
)
from issue_orchestrator.domain.terminal_session_registry import (
    PendingTerminalSessionRecord,
    TerminalSessionRecord,
)
from issue_orchestrator.execution.terminal_subprocess import (
    SubprocessPlugin,
)
from issue_orchestrator.execution.terminal_session_lifecycle import (
    ThreadTerminalSessionWatcherFactory,
)
from issue_orchestrator.execution.retained_thread import (
    MaskedThreadStartPrimitive,
    ThreadingRetainedThreadFactory,
)
from issue_orchestrator.execution.terminal_session_registry import (
    SqliteTerminalSessionRegistry,
    TerminalSessionRegistryError,
)
from issue_orchestrator.entrypoints.bootstrap import (
    build_terminal_session_terminator,
)
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_process_group_supervisor,
    build_terminal_session_owner,
    terminal_session_watcher_policy,
)
from issue_orchestrator.infra.env import ENV_PREFIX
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.unit.terminal_session_termination_helpers import (
    RecordingTerminalSessionTerminator,
)
from tests.unit.terminal_session_owner_helpers import (
    RecordingTerminalSessionOwner,
    TerminalSessionSentinelCohort,
)


def _plugin(
    repo_root: Path,
    *,
    session_interactions_enabled: bool = False,
    worktree_base: Path | None = None,
    terminal_status: TerminalSessionStatus = TerminalSessionStatus.CONTAINED,
) -> SubprocessPlugin:
    return SubprocessPlugin(
        RecordingTerminalSessionTerminator(status=terminal_status),
        RecordingTerminalSessionOwner(),
        SqliteTerminalSessionRegistry(repo_root.resolve()),
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
        session_interactions_enabled=session_interactions_enabled,
        worktree_base=worktree_base,
    )


def _session_record(
    *,
    session_name: str,
    issue_number: int,
    worktree: Path,
    run_dir: Path,
    process_id: int,
    tab_name: str,
    is_review: bool,
) -> TerminalSessionRecord:
    return TerminalSessionRecord(
        session_name=session_name,
        issue_number=issue_number,
        worktree_path=worktree.resolve(),
        process=TerminalSessionProcess(
            process_id=process_id,
            birth_identity=ProcessBirthIdentity("darwin-timeval:1700000000:100"),
            terminal_cancellation=(
                TerminalSessionOwnerCancellation.for_run_dir(run_dir.resolve())
            ),
            executor_cancellation=(
                ExecutorInteractiveSessionCancellation.for_run_dir(run_dir.resolve())
            ),
        ),
        registered_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        recording_path=(run_dir / "terminal-recording.jsonl").resolve(),
        tab_name=tab_name,
        is_review=is_review,
    )


def _read_recording_output(path):
    output_chunks: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            output_chunks.append(
                base64.b64decode(data_b64).decode("utf-8", errors="ignore")
            )
    return "".join(output_chunks)


def _launch_with_run_dir(
    worktree: Path,
    session_name: str,
    command: str,
    *,
    interaction_intent: TerminalInteractionIntent = TerminalInteractionIntent.NONE,
) -> tuple[TerminalLaunch, Path]:
    run_dir = worktree / ".issue-orchestrator" / "sessions" / session_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return (
        TerminalLaunch(
            shell_command=command,
            shell=TerminalShell.BASH,
            interaction_intent=interaction_intent,
            destination=TerminalRunDestination(
                run_dir=run_dir.resolve(),
                recording_path=(run_dir / "terminal-recording.jsonl").resolve(),
            ),
        ),
        run_dir,
    )


def _term_resistant_launch(
    worktree: Path,
    session_name: str,
    descendant_pid_path: Path,
) -> tuple[TerminalLaunch, Path]:
    program = CooperativeTermResistantProcessTreeProgram(
        descendant_pid_path=descendant_pid_path,
        descendant_lifetime_seconds=300,
        readiness_lines=(),
    )
    return _launch_with_run_dir(
        worktree,
        session_name,
        shlex.join((sys.executable, "-c", program.python_source())),
    )


class _IdentityObservationFailureTerminalSessionTerminator(
    RecordingTerminalSessionTerminator
):
    def __init__(self, descendant_pid_path: Path) -> None:
        super().__init__()
        self._descendant_pid_path = descendant_pid_path

    def identify(self, process_id: int) -> ProcessIdentityObservation:
        del process_id
        PROCESS_COMPLETION_WATCHDOG.wait_for_path(
            self._descendant_pid_path,
            operation="TERM-resistant terminal descendant readiness",
        )
        raise RuntimeError("injected process identity observation failure")


class _CommitFailureTerminalSessionRegistry:
    """Typed fault adapter that fails only the pending-to-identified commit."""

    def __init__(
        self,
        delegate: SqliteTerminalSessionRegistry,
        descendant_pid_path: Path,
    ) -> None:
        self._delegate = delegate
        self._descendant_pid_path = descendant_pid_path

    def load(self) -> dict[str, TerminalSessionRecord]:
        return self._delegate.load()

    def load_pending(self) -> tuple[PendingTerminalSessionRecord, ...]:
        return self._delegate.load_pending()

    def begin_launch(self, record: PendingTerminalSessionRecord) -> None:
        self._delegate.begin_launch(record)

    def upsert(self, record: TerminalSessionRecord) -> None:
        self._delegate.upsert(record)

    def commit_launch(
        self,
        pending: PendingTerminalSessionRecord,
        record: TerminalSessionRecord,
    ) -> None:
        del pending, record
        PROCESS_COMPLETION_WATCHDOG.wait_for_path(
            self._descendant_pid_path,
            operation="TERM-resistant terminal descendant readiness",
        )
        raise TerminalSessionRegistryError("injected registry publication failure")

    def remove_pending(self, session_name: str) -> None:
        self._delegate.remove_pending(session_name)

    def remove(self, session_name: str) -> None:
        self._delegate.remove(session_name)


def test_identity_observation_failure_contains_unregistered_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    descendant_pid_path = (tmp_path / "identity-failure-descendant.pid").resolve()
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        _IdentityObservationFailureTerminalSessionTerminator(descendant_pid_path),
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    launch, _run_dir = _term_resistant_launch(
        worktree,
        "issue-901",
        descendant_pid_path,
    )

    with pytest.raises(
        RuntimeError,
        match="injected process identity observation failure",
    ):
        plugin.create_session(
            session_id=901,
            launch=launch,
            working_dir=str(worktree),
            title="Identity failure",
            session_name="issue-901",
        )

    ProcessTreeMember(
        int(descendant_pid_path.read_text(encoding="utf-8"))
    ).assert_contained()
    assert registry.load() == {}


def test_registry_publication_failure_contains_unregistered_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    descendant_pid_path = (tmp_path / "registry-failure-descendant.pid").resolve()
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        build_terminal_session_terminator(),
        RecordingTerminalSessionOwner(),
        _CommitFailureTerminalSessionRegistry(
            registry,
            descendant_pid_path,
        ),
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    launch, _run_dir = _term_resistant_launch(
        worktree,
        "issue-902",
        descendant_pid_path,
    )

    with pytest.raises(
        TerminalSessionRegistryError,
        match="injected registry publication failure",
    ):
        plugin.create_session(
            session_id=902,
            launch=launch,
            working_dir=str(worktree),
            title="Registry failure",
            session_name="issue-902",
        )

    ProcessTreeMember(
        int(descendant_pid_path.read_text(encoding="utf-8"))
    ).assert_contained()
    assert registry.load() == {}


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_terminal_owner_survives_one_sentinel_hard_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One crashed sentinel cannot strand a live terminal process group."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    descendant_pid_path = (tmp_path / "sentinel-descendant.pid").resolve()
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        build_terminal_session_terminator(),
        build_terminal_session_owner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    launch, _run_dir = _term_resistant_launch(
        worktree,
        "issue-903",
        descendant_pid_path,
    )

    assert (
        plugin.create_session(
            session_id=903,
            launch=launch,
            working_dir=str(worktree),
            title="Sentinel crash",
            session_name="issue-903",
        )
        is True
    )
    PROCESS_COMPLETION_WATCHDOG.wait_for_path(
        descendant_pid_path,
        operation="TERM-resistant terminal descendant readiness",
    )
    process = registry.load()["issue-903"].process
    sentinels = TerminalSessionSentinelCohort.observe(process.process_id)
    sentinels.crash_one()

    assert plugin.kill_session(903, "issue-903") is True
    ProcessTreeMember(
        int(descendant_pid_path.read_text(encoding="utf-8"))
    ).assert_contained()
    assert registry.load() == {}


def test_subprocess_session_writes_log(tmp_path, monkeypatch):
    """Test that subprocess output is captured to the session log file.

    This test verifies that fast-exiting processes (like printf) have their
    output fully captured. The drain logic must be patient enough to wait
    for data to arrive in the PTY buffer after the process exits.
    """
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        RecordingTerminalSessionTerminator(),
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    launch, run_dir = _launch_with_run_dir(
        worktree,
        "issue-123",
        "printf 'hello from subprocess\\n'",
    )
    created = plugin.create_session(
        session_id=123,
        launch=launch,
        working_dir=str(worktree),
        title="Test session",
        session_name="issue-123",
    )
    assert created is True

    # Bounded poll for process exit.  Subprocess is a real external system,
    # so bounded waits with GIL-yielding pauses are acceptable per test policy.
    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)  # yield GIL so watcher thread can drain PTY output

    log_path = run_dir / "terminal-recording.jsonl"
    assert log_path.exists(), f"Log file not created at {log_path}"
    content = log_path.read_text()
    events = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert events[0]["event_type"] == "resize"
    event = next(event for event in events if event.get("event_type") == "output")
    payload = base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
    assert "hello from subprocess" in payload, (
        f"Expected decoded output not in recording payload. Content: {content!r}"
    )


def test_subprocess_registry_rejects_legacy_index_without_birth_identity(tmp_path):
    repo_root = tmp_path / "repo"
    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "subprocess_sessions.json"
    index_path.write_text(
        json.dumps(
            {
                "issue-9": {
                    "session_name": "issue-9",
                    "issue_number": 9,
                    "worktree_path": str(repo_root / "wt"),
                    "pid": 1234,
                }
            }
        )
    )

    with pytest.raises(TerminalSessionRegistryError, match="birth identities"):
        SqliteTerminalSessionRegistry(repo_root.resolve())


def test_session_exists_returns_false_when_session_not_alive(tmp_path, monkeypatch):
    """session_exists returns False and cleans up when the AgentSession reports dead.

    Replaces the old waitpid race tests: race handling is now encapsulated inside
    AgentSession.is_alive(), so we test the observable behaviour — a dead session
    is removed from the registry and reported as non-existent.
    """
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        RecordingTerminalSessionTerminator(),
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )

    # Register a session in the registry so session_exists finds it
    record = _session_record(
        session_name="issue-1",
        issue_number=1,
        worktree=worktree,
        run_dir=worktree,
        process_id=4242,
        tab_name="Issue 1",
        is_review=False,
    )
    registry.upsert(record)

    assert plugin.session_exists(1, "issue-1") is False
    assert "issue-1" not in registry.load()


def test_discover_running_sessions_includes_canonical_session_name(
    tmp_path, monkeypatch
):
    """Registry discovery exposes the persisted terminal id to callers."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        RecordingTerminalSessionTerminator(status=TerminalSessionStatus.ACTIVE),
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    run_dir = (
        worktree / ".issue-orchestrator" / "sessions" / "20260221-000000Z__review-456"
    )
    record = _session_record(
        session_name="review-456",
        issue_number=100,
        worktree=worktree,
        run_dir=run_dir,
        process_id=4242,
        tab_name="Review PR #456",
        is_review=True,
    )
    registry.upsert(record)

    assert plugin.discover_running_sessions() == [
        {
            "issue_number": 100,
            "tab_name": "Review PR #456",
            "is_review": True,
            "session_name": "review-456",
            "run_dir": str(run_dir),
        }
    ]


def test_terminal_destination_does_not_depend_on_shell_text(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(repo_root)
    launch, run_dir = _launch_with_run_dir(
        worktree,
        "20260221-000000Z__coding with spaces",
        "printf 'typed destination\\n'",
    )

    assert plugin.create_session(
        session_id=123,
        launch=launch,
        working_dir=str(worktree),
        title="Typed destination",
        session_name="issue-123",
    )
    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)

    assert "typed destination" in _read_recording_output(
        run_dir / "terminal-recording.jsonl"
    )


def test_subprocess_session_auto_accepts_claude_trust_prompt(tmp_path, monkeypatch):
    """Built-in interaction rules can unblock wrapped Claude trust prompts."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    fake_claude = worktree / ".venv" / "bin" / "claude"
    fake_claude.parent.mkdir(parents=True)
    fake_claude.write_text(
        "#!/bin/sh\n"
        "printf 'Quick safety check: Is this a project you created or one you trust?\\n'\n"
        "printf '1. Yes, I trust this folder\\n'\n"
        "printf '2. No, exit\\n'\n"
        "read -r response\n"
        "printf 'AUTO-RESPONSE:%s\\n' \"$response\"\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(
        repo_root,
        session_interactions_enabled=True,
        worktree_base=repo_root,
    )
    launch, run_dir = _launch_with_run_dir(
        worktree,
        "issue-123",
        "ISSUE_ORCHESTRATOR_TEST=1 && claude",
        interaction_intent=TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
    )
    created = plugin.create_session(
        session_id=123,
        launch=launch,
        working_dir=str(worktree),
        title="Trust prompt test",
        session_name="issue-123",
    )
    assert created is True

    log_path = run_dir / "terminal-recording.jsonl"

    deadline = time.monotonic() + 30.0
    while plugin.session_exists(123, "issue-123"):
        assert time.monotonic() < deadline, "subprocess did not exit within 30s"
        time.sleep(0.05)

    assert "AUTO-RESPONSE:" in _read_recording_output(log_path)


def test_subprocess_session_interactions_require_worktree_under_base(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    allowed_base = repo_root / "allowed"
    allowed_base.mkdir(parents=True)
    outside_worktree = repo_root / "outside"
    outside_worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(
        repo_root,
        session_interactions_enabled=True,
        worktree_base=allowed_base,
    )

    handler = plugin._interaction_handler(  # noqa: SLF001
        TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
        "issue-7",
        outside_worktree,
    )

    assert handler is None


def test_subprocess_session_interactions_require_configured_worktree_base(
    tmp_path, monkeypatch, caplog
):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(
        repo_root,
        session_interactions_enabled=True,
        worktree_base=None,
    )

    handler = plugin._interaction_handler(  # noqa: SLF001
        TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
        "issue-7",
        worktree,
    )

    assert handler is None
    assert "worktree_base is not configured" in caplog.text


def test_kill_session_delegates_complete_containment_to_typed_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "issue-71"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    terminator = RecordingTerminalSessionTerminator()
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        terminator,
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    record = _session_record(
        session_name="issue-71",
        issue_number=71,
        worktree=worktree,
        run_dir=run_dir,
        process_id=4271,
        tab_name="Issue 71",
        is_review=False,
    )
    registry.upsert(record)

    assert plugin.kill_session(71, "issue-71") is True
    assert terminator.processes == (
        TerminalSessionProcess(
            4271,
            ProcessBirthIdentity("darwin-timeval:1700000000:100"),
            TerminalSessionOwnerCancellation.for_run_dir(run_dir.resolve()),
            ExecutorInteractiveSessionCancellation.for_run_dir(run_dir.resolve()),
        ),
    )


def test_recycled_session_pid_is_retired_through_typed_terminator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "issue-72"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    terminator = RecordingTerminalSessionTerminator(
        status=TerminalSessionStatus.STALE_IDENTITY,
        termination_outcome=(TerminalSessionTerminationOutcome.STALE_IDENTITY_RETIRED),
    )
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        terminator,
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    record = _session_record(
        session_name="issue-72",
        issue_number=72,
        worktree=worktree,
        run_dir=run_dir,
        process_id=4272,
        tab_name="Issue 72",
        is_review=False,
    )
    registry.upsert(record)

    assert plugin.session_exists(72, "issue-72") is False
    assert terminator.processes == (record.process,)
    assert registry.load() == {}


class _SelectiveFailureTerminalSessionTerminator(RecordingTerminalSessionTerminator):
    def __init__(self, failing_process_id: int) -> None:
        super().__init__(status=TerminalSessionStatus.ACTIVE)
        self._failing_process_id = failing_process_id

    def terminate(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionTerminationOutcome:
        if process.process_id == self._failing_process_id:
            raise RuntimeError(f"containment failed for {process.process_id}")
        return super().terminate(process)


def test_shutdown_attempts_every_session_and_preserves_failed_registry_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    run_dir_a = worktree / ".issue-orchestrator" / "sessions" / "issue-81"
    run_dir_b = worktree / ".issue-orchestrator" / "sessions" / "issue-82"
    run_dir_a.mkdir(parents=True)
    run_dir_b.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    terminator = _SelectiveFailureTerminalSessionTerminator(4281)
    registry = SqliteTerminalSessionRegistry(repo_root.resolve())
    plugin = SubprocessPlugin(
        terminator,
        RecordingTerminalSessionOwner(),
        registry,
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        ThreadTerminalSessionWatcherFactory(
            ThreadingRetainedThreadFactory(MaskedThreadStartPrimitive())
        ),
    )
    first = _session_record(
        session_name="issue-81",
        issue_number=81,
        worktree=worktree,
        run_dir=run_dir_a,
        process_id=4281,
        tab_name="Issue 81",
        is_review=False,
    )
    second = _session_record(
        session_name="issue-82",
        issue_number=82,
        worktree=worktree,
        run_dir=run_dir_b,
        process_id=4282,
        tab_name="Issue 82",
        is_review=False,
    )
    registry.upsert(first)
    registry.upsert(second)

    with pytest.raises(BaseExceptionGroup, match="could not be contained"):
        plugin.on_orchestrator_shutdown()

    assert terminator.processes == (second.process,)
    assert tuple(registry.load()) == ("issue-81",)
