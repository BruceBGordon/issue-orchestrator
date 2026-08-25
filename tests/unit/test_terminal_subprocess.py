from __future__ import annotations

import json
import time
import base64
from pathlib import Path

from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)
from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
)
from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionProcess,
)
from issue_orchestrator.execution.terminal_subprocess import (
    SubprocessPlugin,
    _SessionRecord,
    _SubprocessRegistry,
)
from issue_orchestrator.infra.env import ENV_PREFIX
from tests.unit.terminal_session_termination_helpers import (
    RecordingTerminalSessionTerminator,
)


def _plugin(
    *,
    session_interactions_enabled: bool = False,
    worktree_base: Path | None = None,
) -> SubprocessPlugin:
    return SubprocessPlugin(
        RecordingTerminalSessionTerminator(),
        session_interactions_enabled=session_interactions_enabled,
        worktree_base=worktree_base,
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
            output_chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(output_chunks)


def _command_with_run_dir(worktree: Path, session_name: str, command: str) -> tuple[str, Path]:
    run_dir = worktree / ".issue-orchestrator" / "sessions" / session_name
    return f"export {ENV_PREFIX}RUN_DIR='{run_dir}' && {command}", run_dir


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

    plugin = _plugin()
    command, run_dir = _command_with_run_dir(
        worktree,
        "issue-123",
        "printf 'hello from subprocess\\n'",
    )
    created = plugin.create_session(
        session_id=123,
        command=command,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell=TerminalShell.BASH,
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
    assert "hello from subprocess" in payload, f"Expected decoded output not in recording payload. Content: {content!r}"


def test_subprocess_registry_migrates_legacy_index(tmp_path):
    repo_root = tmp_path / "repo"
    record = _SessionRecord(
        session_name="issue-9",
        issue_number=9,
        worktree_path=str(repo_root / "wt"),
        pid=1234,
        started_at="2026-01-01T00:00:00",
        log_path=str(repo_root / "wt" / ".issue-orchestrator" / "sessions" / "issue-9" / "ui-session.log"),
        tab_name="Issue 9",
        is_review=False,
    )

    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "subprocess_sessions.json"
    index_path.write_text(json.dumps({record.session_name: vars(record)}))

    recovered = _SubprocessRegistry(repo_root).load()
    assert "issue-9" in recovered


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

    plugin = _plugin()

    # Register a session in the registry so session_exists finds it
    record = _SessionRecord(
        session_name="issue-1",
        issue_number=1,
        worktree_path=str(worktree),
        pid=4242,
        started_at="2026-01-01T00:00:00",
        log_path=str(worktree / "ui-session.log"),
        tab_name="Issue 1",
        is_review=False,
    )
    plugin._registry.upsert(record)  # noqa: SLF001

    # Force the dead-session path deterministically instead of depending on the
    # host PID table, which can legitimately contain the chosen test PID on CI.
    monkeypatch.setattr(plugin, "_process_alive", lambda pid, session_name=None: False)  # noqa: ARG005, SLF001

    assert plugin.session_exists(1, "issue-1") is False
    assert "issue-1" not in plugin._registry.load()  # noqa: SLF001


def test_discover_running_sessions_includes_canonical_session_name(tmp_path, monkeypatch):
    """Registry discovery exposes the persisted terminal id to callers."""
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin()
    run_dir = (
        worktree
        / ".issue-orchestrator"
        / "sessions"
        / "20260221-000000Z__review-456"
    )
    record = _SessionRecord(
        session_name="review-456",
        issue_number=100,
        worktree_path=str(worktree),
        pid=4242,
        started_at="2026-01-01T00:00:00",
        log_path=str(run_dir / "terminal-recording.jsonl"),
        tab_name="Review PR #456",
        is_review=True,
    )
    plugin._registry.upsert(record)  # noqa: SLF001
    monkeypatch.setattr(plugin, "_process_alive", lambda pid, session_name=None: True)  # noqa: ARG005, SLF001

    assert plugin.discover_running_sessions() == [
        {
            "issue_number": 100,
            "tab_name": "Review PR #456",
            "is_review": True,
            "session_name": "review-456",
            "run_dir": str(run_dir),
        }
    ]


def test_session_log_path_uses_issue_orchestrator_run_dir_when_present(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "20260221-000000Z__coding-1"
    command = f"export ISSUE_ORCHESTRATOR_RUN_DIR='{run_dir}' && echo test"

    log_path = plugin._session_log_path(worktree, "issue-123", command)  # noqa: SLF001

    assert log_path == run_dir / "terminal-recording.jsonl"


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

    plugin = _plugin(session_interactions_enabled=True, worktree_base=repo_root)
    command, run_dir = _command_with_run_dir(
        worktree,
        "issue-123",
        "ISSUE_ORCHESTRATOR_TEST=1 && claude",
    )
    created = plugin.create_session(
        session_id=123,
        command=command,
        interaction_intent=TerminalInteractionIntent.CLAUDE_TRUST_WORKTREE,
        shell=TerminalShell.BASH,
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


def test_subprocess_session_interactions_require_worktree_under_base(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    allowed_base = repo_root / "allowed"
    allowed_base.mkdir(parents=True)
    outside_worktree = repo_root / "outside"
    outside_worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(session_interactions_enabled=True, worktree_base=allowed_base)

    handler = plugin._interaction_handler("claude", "issue-7", outside_worktree)  # noqa: SLF001

    assert handler is None


def test_subprocess_session_interactions_require_configured_worktree_base(tmp_path, monkeypatch, caplog):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))

    plugin = _plugin(session_interactions_enabled=True, worktree_base=None)

    handler = plugin._interaction_handler("claude", "issue-7", worktree)  # noqa: SLF001

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
    plugin = SubprocessPlugin(terminator)
    record = _SessionRecord(
        session_name="issue-71",
        issue_number=71,
        worktree_path=str(worktree),
        pid=4271,
        started_at="2026-01-01T00:00:00",
        log_path=str(run_dir / "terminal-recording.jsonl"),
        tab_name="Issue 71",
        is_review=False,
    )
    plugin._registry.upsert(record)  # noqa: SLF001

    assert plugin.kill_session(71, "issue-71") is True
    assert terminator.processes == (
        TerminalSessionProcess(
            4271,
            ExecutorInteractiveSessionCancellation.for_run_dir(run_dir.resolve()),
        ),
    )
