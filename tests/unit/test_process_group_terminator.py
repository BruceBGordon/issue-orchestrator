"""Whole-tree containment proofs for the POSIX process-group owner."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupUnboundedWait,
)
from issue_orchestrator.execution.process_group_supervisor import (
    NeverInterruptProcessGroup,
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ExitingTermResistantProcessTreeProgram,
    ProcessTreeMember,
)


pytestmark = pytest.mark.timeout(45)


def test_term_resistant_descendant_dies_when_cooperative_leader_exits(
    tmp_path: Path,
) -> None:
    """A leader's TERM exit must not suppress the whole-group SIGKILL."""
    descendant_pid_path = (tmp_path / "cooperative-descendant.pid").resolve()
    leader = CooperativeTermResistantProcessTreeProgram(
        descendant_pid_path,
        300,
        ("TREE-READY",),
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader.python_source()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise AssertionError("leader readiness pipe was not created")
    readiness = process.stdout.readline()
    assert readiness == "TREE-READY\n", (
        f"leader readiness mismatch: line={readiness!r} "
        f"returncode={process.poll()!r}"
    )
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
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
        ProcessTreeMember(descendant_pid).assert_contained()
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


def test_natural_leader_exit_contains_descendant_before_reaping(
    tmp_path: Path,
) -> None:
    descendant_pid_path = (tmp_path / "natural-descendant.pid").resolve()
    natural_leader = ExitingTermResistantProcessTreeProgram(
        descendant_pid_path,
        300,
        0,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", natural_leader.python_source()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
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
            NeverInterruptProcessGroup(),
        )
        process.returncode = supervision.termination.leader_exit_code
        assert type(supervision) is ProcessGroupCompleted
        assert process.returncode == 0
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        ProcessTreeMember(descendant_pid).assert_contained()
    finally:
        if descendant_pid_path.exists():
            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
