"""Whole-tree containment proofs for the POSIX process-group owner."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupUnboundedWait,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)


_LEADER_SCRIPT = """
import signal
import subprocess
import sys

descendant = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(descendant.pid, flush=True)
signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
signal.pause()
"""


def _pid_has_exited(pid: int, *, deadline_seconds: float = 5.0) -> bool:
    """Bounded kernel observation for a reparented real subprocess."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.01)
    return False


def test_term_resistant_descendant_dies_when_cooperative_leader_exits() -> None:
    """A leader's TERM exit must not suppress the whole-group SIGKILL."""
    process = subprocess.Popen(
        [sys.executable, "-c", _LEADER_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise AssertionError("leader readiness pipe was not created")
    descendant_pid = int(process.stdout.readline().strip())
    terminator = PosixProcessGroupTerminator(
        ExecutorProcessTerminationPolicy(
            graceful_shutdown_seconds=0.1,
            forceful_shutdown_seconds=1.0,
        )
    )

    try:
        termination = terminator.terminate(OwnedProcessGroupLeader(process.pid))
        process.returncode = termination.leader_exit_code
        assert process.returncode == 0
        assert _pid_has_exited(descendant_pid), (
            f"TERM-resistant descendant {descendant_pid} survived its leader"
        )
    finally:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1.0)


def test_natural_leader_exit_contains_descendant_before_reaping() -> None:
    resistant_child = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    natural_leader = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {resistant_child!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", natural_leader],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise AssertionError("leader readiness pipe was not created")
    descendant_pid = int(process.stdout.readline().strip())
    supervisor = PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=1.0,
            )
        )
    )

    try:
        supervision = supervisor.supervise(
            OwnedProcessGroupLeader(process.pid),
            ProcessGroupUnboundedWait(),
        )
        process.returncode = supervision.termination.leader_exit_code
        assert type(supervision) is ProcessGroupCompleted
        assert process.returncode == 0
        assert _pid_has_exited(descendant_pid)
    finally:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
