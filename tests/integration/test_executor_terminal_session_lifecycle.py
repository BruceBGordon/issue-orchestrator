"""Real PTY/session regressions for interactive executor ownership."""

from __future__ import annotations

import base64
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pluggy
import pexpect
import pytest
from pydantic import BaseModel, ConfigDict

from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
    TerminalShell,
)
from issue_orchestrator.domain.executor import (
    EXECUTOR_SESSION_CANCELLATION_FILENAME,
)
from issue_orchestrator.execution.session_runner_adapter import PluggySessionRunner
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
from issue_orchestrator.infra.hooks.hookspec import PROJECT_NAME, TerminalSpec
from issue_orchestrator.infra.terminal_recording import TERMINAL_RECORDING_FILENAME
from tests.process_tree_fixture import ProcessTreeMember
from tests.unit.session_run_helpers import make_session_run_assets


pytestmark = [
    pytest.mark.xdist_group("pty"),
    pytest.mark.timeout(45),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESS_RUNNER = REPO_ROOT / "tests" / "unit" / "executor_process_runner.py"


class _OutputRecordingEvent(BaseModel):
    """Typed projection of one output event in the raw PTY recording."""

    model_config = ConfigDict(strict=True, extra="ignore")

    event_type: Literal["output"]
    data_b64: str


def _runner(repo_root: Path) -> PluggySessionRunner:
    manager = pluggy.PluginManager(PROJECT_NAME)
    manager.add_hookspecs(TerminalSpec)
    manager.register(
        SubprocessPlugin(
            build_terminal_session_terminator(),
            build_terminal_session_owner(),
            build_terminal_session_registry(repo_root.resolve()),
            build_process_group_supervisor(),
            terminal_session_watcher_policy(),
            build_terminal_session_watcher_factory(),
        ),
        name="terminal_subprocess",
    )
    return PluggySessionRunner(manager)


def _repository_worktree(repo_root: Path) -> Path:
    worktree = repo_root / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(
        ("git", "init", "--quiet", str(worktree)),
        check=True,
        capture_output=True,
    )
    return worktree


def _executor_launch(
    worktree: Path,
    session_name: str,
    pool_dir: Path,
    busy_file: Path,
    command: tuple[str, ...],
) -> tuple[TerminalLaunch, Path, Path]:
    run = make_session_run_assets(worktree, session_name=session_name)
    outer_pid_path = run.run_dir / "outer-session.pid"
    executor_arguments = (
        sys.executable,
        str(PROCESS_RUNNER),
        "--host-cpu-slots",
        "1",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--work-key",
        f"session:{session_name}",
        "--group",
        f"session:{session_name}",
        "--interactive-session",
        "--cancellation-record",
        str(run.run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME),
        "--",
        *command,
    )
    shell_command = " && ".join(
        (
            f"export ISSUE_ORCHESTRATOR_RUN_DIR={shlex.quote(str(run.run_dir))}",
            f"export ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR={shlex.quote(str(pool_dir))}",
            "export ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE="
            f"{shlex.quote(str(busy_file))}",
            f"printf '%s\\n' $$ > {shlex.quote(str(outer_pid_path))}",
            "exec " + shlex.join(executor_arguments),
        )
    )
    return (
        TerminalLaunch(
            shell_command=shell_command,
            shell=TerminalShell.BASH,
            interaction_intent=TerminalInteractionIntent.NONE,
            destination=run.terminal_destination,
        ),
        run.run_dir,
        outer_pid_path,
    )



def _no_live_leases(pool_dir: Path) -> bool:
    """True when every remaining lease file is stale (its flock is free)."""
    import fcntl

    for lease in (pool_dir / "leases").glob("*.json"):
        try:
            with lease.open("r+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BlockingIOError:
            return False
        except FileNotFoundError:
            continue
    return True

def _await(requirement: Callable[[], bool]) -> None:
    """Bound one real process/PTY integration boundary against deadlock."""
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if requirement():
            return
        time.sleep(0.01)
    raise AssertionError("terminal session lifecycle requirement was not observed")


def _recording_contains(recording_path: Path, payload: bytes) -> bool:
    if not recording_path.exists():
        return False
    output = bytearray()
    for line in recording_path.read_text(encoding="utf-8").splitlines():
        if '"event_type": "output"' not in line:
            continue
        event = _OutputRecordingEvent.model_validate_json(line)
        output.extend(base64.b64decode(event.data_b64, validate=True))
    return payload in output


def _create_session(
    runner: PluggySessionRunner,
    launch: TerminalLaunch,
    worktree: Path,
    session_name: str,
) -> None:
    assert runner.create_session(
        7017,
        launch,
        str(worktree),
        session_name,
        session_name,
    )


def test_interactive_executor_preserves_dev_tty_and_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = _repository_worktree(repo_root)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(repo_root))
    busy_file = tmp_path / "busy"
    busy_file.write_text("0.0\n", encoding="utf-8")
    command_source = (
        "import os,time; "
        "fd=os.open('/dev/tty',os.O_RDWR); "
        "os.write(fd,b'TTY-READY\\n'); "
        "line=os.read(fd,100).strip(); "
        "os.write(fd,b'TTY-ECHO:'+line+b'\\n'); "
        "time.sleep(0.2)"
    )
    launch, run_dir, _outer_pid_path = _executor_launch(
        worktree,
        "pty-input",
        tmp_path / "pool",
        busy_file,
        (sys.executable, "-c", command_source),
    )
    runner = _runner(repo_root)
    recording_path = run_dir / TERMINAL_RECORDING_FILENAME

    _create_session(runner, launch, worktree, "pty-input")
    try:
        _await(lambda: _recording_contains(recording_path, b"TTY-READY\r\n"))
        assert runner.send_to_session(7017, "hello", "pty-input")
        _await(lambda: _recording_contains(recording_path, b"TTY-ECHO:hello\r\n"))
    finally:
        if runner.session_exists(7017, "pty-input"):
            runner.kill_session(7017, "pty-input")


def test_interactive_guardian_survives_outer_crash_until_its_deadline(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    busy_file = tmp_path / "busy"
    busy_file.write_text("0.0\n", encoding="utf-8")
    opaque_pid_path = tmp_path / "opaque.pid"
    continue_path = tmp_path / "continue"
    survived_path = tmp_path / "survived"
    command_source = (
        "import os,pathlib,time; "
        f"pid=pathlib.Path({str(opaque_pid_path)!r}); "
        f"cont=pathlib.Path({str(continue_path)!r}); "
        f"survived=pathlib.Path({str(survived_path)!r}); "
        "pid.write_text(str(os.getpid())); "
        "print('CRASH-READY',flush=True); "
        "\nwhile not cont.exists(): time.sleep(0.01); "
        "\nsurvived.write_text('yes'); "
        "\nwhile True: time.sleep(1)"
    )
    arguments = [
        str(PROCESS_RUNNER),
        "--host-cpu-slots",
        "1",
        "--min-concurrency",
        "1",
        "--max-concurrency",
        "1",
        "--work-key",
        "session:outer-crash",
        "--group",
        "session:outer-crash",
        # The bound under test must dwarf real activation machinery so the
        # deadline measures the surviving command, never its startup.
        "--active-timeout-seconds",
        "10",
        "--absolute-timeout-seconds",
        "20",
        "--interactive-session",
        "--cancellation-record",
        str(tmp_path / EXECUTOR_SESSION_CANCELLATION_FILENAME),
        "--",
        sys.executable,
        "-c",
        command_source,
    ]
    environment = dict(os.environ)
    environment["ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"] = str(pool_dir)
    environment["ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"] = str(busy_file)
    outer = pexpect.spawn(
        sys.executable,
        arguments,
        cwd=str(REPO_ROOT),
        env=environment,
        encoding="utf-8",
        timeout=15,
    )
    try:
        outer.expect_exact("CRASH-READY")
        opaque_process_id = int(opaque_pid_path.read_text(encoding="utf-8"))
        outer_process_id = outer.pid
        assert outer_process_id is not None

        os.kill(outer_process_id, signal.SIGKILL)
        continue_path.write_text("continue\n", encoding="utf-8")

        _await(survived_path.exists)
        assert ProcessTreeMember(opaque_process_id).is_executable()
        ProcessTreeMember(opaque_process_id).assert_contained()

        follower = subprocess.run(
            (
                sys.executable,
                str(PROCESS_RUNNER),
                "--host-cpu-slots",
                "1",
                "--min-concurrency",
                "1",
                "--max-concurrency",
                "1",
                "--work-key",
                "session:crash-follower",
                "--group",
                "session:crash-follower",
                "--",
                sys.executable,
                "-c",
                "pass",
            ),
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert follower.returncode == 0, follower.stderr
        assert not tuple((pool_dir / "leases").glob("*.json"))
    finally:
        outer.close(force=True)


@pytest.mark.parametrize("recover_before_stop", (False, True))
def test_session_stop_contains_executor_guardian_and_opaque_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recover_before_stop: bool,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = _repository_worktree(repo_root)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(repo_root))
    busy_file = tmp_path / "busy"
    busy_file.write_text("0.0\n", encoding="utf-8")
    opaque_pid_path = tmp_path / "opaque.pid"
    command_source = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(opaque_pid_path)!r}).write_text(str(os.getpid())); "
        "print('OPAQUE-READY',flush=True); "
        "time.sleep(60)"
    )
    session_name = "recovered-stop" if recover_before_stop else "live-stop"
    pool_dir = tmp_path / "pool"
    launch, run_dir, _outer_pid_path = _executor_launch(
        worktree,
        session_name,
        pool_dir,
        busy_file,
        (sys.executable, "-c", command_source),
    )
    original_runner = _runner(repo_root)
    _create_session(original_runner, launch, worktree, session_name)
    stopping_runner = _runner(repo_root) if recover_before_stop else original_runner
    try:
        _await(opaque_pid_path.exists)
        _await(lambda: (run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME).exists())
        opaque_process_id = int(opaque_pid_path.read_text(encoding="utf-8"))

        stopping_runner.kill_session(7017, session_name)

        ProcessTreeMember(opaque_process_id).assert_contained()
        # Capacity is provably released once every lease flock is free; the
        # stale files themselves are pruned by the next admission scan.
        _await(lambda: _no_live_leases(pool_dir))
        assert not (run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME).exists()
        assert not stopping_runner.session_exists(7017, session_name)
    finally:
        if stopping_runner.session_exists(7017, session_name):
            stopping_runner.kill_session(7017, session_name)


def test_stalled_outer_session_cannot_strand_executor_guardian(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    worktree = _repository_worktree(repo_root)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(repo_root))
    busy_file = tmp_path / "busy"
    busy_file.write_text("0.0\n", encoding="utf-8")
    opaque_pid_path = tmp_path / "opaque.pid"
    command_source = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(opaque_pid_path)!r}).write_text(str(os.getpid())); "
        "print('OPAQUE-READY',flush=True); "
        "time.sleep(60)"
    )
    pool_dir = tmp_path / "pool"
    launch, run_dir, outer_pid_path = _executor_launch(
        worktree,
        "stalled-outer-stop",
        pool_dir,
        busy_file,
        (sys.executable, "-c", command_source),
    )
    runner = _runner(repo_root)
    _create_session(runner, launch, worktree, "stalled-outer-stop")
    try:
        _await(opaque_pid_path.exists)
        _await(outer_pid_path.exists)
        _await(lambda: (run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME).exists())
        outer_process_id = int(outer_pid_path.read_text(encoding="utf-8"))
        opaque_process_id = int(opaque_pid_path.read_text(encoding="utf-8"))
        assert os.getpgid(outer_process_id) == outer_process_id

        os.killpg(outer_process_id, signal.SIGSTOP)
        runner.kill_session(7017, "stalled-outer-stop")

        ProcessTreeMember(opaque_process_id).assert_contained()
        ProcessTreeMember(outer_process_id).assert_contained()
        assert not (run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME).exists()
        assert not runner.session_exists(7017, "stalled-outer-stop")
        follower = subprocess.run(
            (
                sys.executable,
                str(PROCESS_RUNNER),
                "--host-cpu-slots",
                "1",
                "--min-concurrency",
                "1",
                "--max-concurrency",
                "1",
                "--work-key",
                "session:stalled-follower",
                "--group",
                "session:stalled-follower",
                "--",
                sys.executable,
                "-c",
                "pass",
            ),
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR": str(pool_dir),
                "ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE": str(busy_file),
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert follower.returncode == 0, follower.stderr
        assert not tuple((pool_dir / "leases").glob("*.json"))
    finally:
        if runner.session_exists(7017, "stalled-outer-stop"):
            runner.kill_session(7017, "stalled-outer-stop")
