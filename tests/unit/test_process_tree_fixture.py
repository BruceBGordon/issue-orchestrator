"""Behavior proofs for typed real-process fixture observations."""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.process_tree_fixture import ProcessTreeMember, TermResistantChildProgram


def test_term_resistant_fixture_must_outlive_containment_watchdog() -> None:
    with pytest.raises(ValueError, match="must exceed the containment watchdog"):
        TermResistantChildProgram(30)


def test_process_state_probe_has_its_own_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out_process_state_probe(
        command: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        assert command[:3] == ("ps", "-o", "stat=")
        assert timeout == 2.0
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(subprocess, "run", time_out_process_state_probe)

    with pytest.raises(AssertionError, match="did not observe fixture process"):
        ProcessTreeMember(os.getpid()).is_executable()


def test_unreaped_zombie_is_contained_because_it_cannot_execute() -> None:
    process_id = os.fork()
    if process_id == 0:
        os._exit(0)

    try:
        observation = os.waitid(os.P_PID, process_id, os.WEXITED | os.WNOWAIT)

        assert observation is not None
        assert observation.si_pid == process_id
        assert not ProcessTreeMember(process_id).is_executable()
        ProcessTreeMember(process_id).assert_contained()
    finally:
        os.waitpid(process_id, 0)
