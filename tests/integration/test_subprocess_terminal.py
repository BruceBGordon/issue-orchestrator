"""Integration tests for subprocess terminal backend."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest

# Run PTY tests sequentially in one worker to avoid Python 3.14 forkpty warning
# (forkpty() in multi-threaded processes can deadlock)
pytestmark = pytest.mark.xdist_group("pty")

from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.entrypoints.bootstrap import (
    build_process_group_supervisor,
    build_terminal_session_terminator,
)
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_terminal_session_owner,
    build_terminal_session_registry,
    build_terminal_session_watcher_factory,
    terminal_session_watcher_policy,
)
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
    TerminalShell,
)
from issue_orchestrator.infra.env import ENV_PREFIX
from tests.unit.session_run_helpers import make_session_run_assets

from .conftest import xdist_timeout


def _wait_for_exit(plugin: SubprocessPlugin, session_name: str, timeout_s: float = 30.0) -> None:
    timeout_s = xdist_timeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not plugin.session_exists(0, session_name):
            return
        time.sleep(0.05)
    raise AssertionError(f"Session {session_name} did not exit within {timeout_s}s")


def _wait_for_file(path: Path, timeout_s: float = 30.0) -> None:
    """Wait for a file to exist (atomic check, no content parsing)."""
    timeout_s = xdist_timeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)  # Can poll aggressively - just a stat() call
    raise AssertionError(f"File {path} not created within {timeout_s}s")


def _wait_for_content(path: Path, marker: str, timeout_s: float = 30.0) -> None:
    """Wait for specific content to appear in a file."""
    timeout_s = xdist_timeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            try:
                content = _read_recording_output(path)
                if marker in content:
                    return
            except Exception:
                pass
        time.sleep(0.05)
    raise AssertionError(f"Content '{marker}' not found in {path} within {timeout_s}s")


def _read_recording_output(path: Path) -> str:
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


def _ensure_worktree_venv(worktree: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    venv_path = repo_root / ".venv"
    if not venv_path.exists():
        pytest.skip("Repo .venv not found; subprocess integration tests require coding-done")
    target = worktree / ".venv"
    if not target.exists():
        target.symlink_to(venv_path)


def _init_git_repo_with_origin(worktree: Path, remote_repo: Path) -> None:
    """Initialize a real git repo with a local bare origin remote."""
    subprocess.run(["git", "init", "--bare", str(remote_repo)], capture_output=True, check=True)
    subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, capture_output=True, check=True)
    (worktree / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_repo)], cwd=worktree, capture_output=True, check=True)


def test_subprocess_session_writes_completion_and_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".issue-orchestrator").mkdir()
    _init_git_repo_with_origin(worktree, tmp_path / "origin.git")
    _ensure_worktree_venv(worktree)
    # Commit everything so coding-done's dirty-file check passes
    subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "setup"], cwd=worktree, capture_output=True, check=True,
    )

    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    # The completion-path env contract is covered in completion-command tests.
    # This integration test only needs to prove that a subprocess-backed coding
    # session emits terminal output and a completion record.
    completion_path = ".issue-orchestrator/completion.json"
    session_name = "issue-42"
    run_assets = make_session_run_assets(worktree, session_name=session_name)
    command = (
        f"export {ENV_PREFIX}RUN_DIR='{run_assets.run_dir}' && "
        "echo 'hello-from-subprocess' && "
        "coding-done completed --implementation 'subprocess test' --problems 'none'"
    )

    plugin = SubprocessPlugin(
        build_terminal_session_terminator(),
        build_terminal_session_owner(),
        build_terminal_session_registry((worktree).resolve()),
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        build_terminal_session_watcher_factory(),
    )
    created = plugin.create_session(
        session_id=42,
        launch=TerminalLaunch(
            shell_command=command,
            interaction_intent=TerminalInteractionIntent.NONE,
            shell=TerminalShell.BASH,
            destination=run_assets.terminal_destination,
        ),
        working_dir=str(worktree),
        title="Subprocess integration test",
        session_name=session_name,
    )
    assert created is True

    log_path = run_assets.log_path
    completion_file = worktree / completion_path
    _wait_for_file(completion_file)
    _wait_for_content(log_path, "hello-from-subprocess")
    _wait_for_exit(plugin, session_name)

    assert log_path.exists()
    assert "hello-from-subprocess" in _read_recording_output(log_path)
    assert completion_file.exists()


def test_subprocess_send_input_writes_to_log(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    worktree = repo_root / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    (worktree / ".issue-orchestrator").mkdir()
    _ensure_worktree_venv(worktree)

    monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(repo_root))
    # stdin is enabled by default; no env var needed

    # Use file-based synchronization: touch completes synchronously before
    # shell proceeds to read, so file existence guarantees shell is waiting.
    # pexpect captures all PTY output to session.log automatically.
    ready_file = worktree / ".ready"
    session_name = "issue-7"
    run_assets = make_session_run_assets(worktree, session_name=session_name)
    command = (
        f"export {ENV_PREFIX}RUN_DIR='{run_assets.run_dir}' && "
        f"touch {shlex.quote(str(ready_file))}; read -r line; echo \"INPUT:$line\""
    )

    plugin = SubprocessPlugin(
        build_terminal_session_terminator(),
        build_terminal_session_owner(),
        build_terminal_session_registry((worktree).resolve()),
        build_process_group_supervisor(),
        terminal_session_watcher_policy(),
        build_terminal_session_watcher_factory(),
    )
    created = plugin.create_session(
        session_id=7,
        launch=TerminalLaunch(
            shell_command=command,
            interaction_intent=TerminalInteractionIntent.NONE,
            shell=TerminalShell.BASH,
            destination=run_assets.terminal_destination,
        ),
        working_dir=str(worktree),
        title="Subprocess input test",
        session_name=session_name,
    )
    assert created is True

    # Wait for ready file - guarantees shell has executed touch and is now in read
    _wait_for_file(ready_file)

    assert plugin.send_to_session(7, "ping", session_name) is True

    # Wait for the expected output instead of polling session_exists.
    # This avoids a race condition in pexpect where the watcher thread and
    # isalive() can both call waitpid() on the same process.
    log_path = run_assets.log_path
    _wait_for_content(log_path, "INPUT:ping")
