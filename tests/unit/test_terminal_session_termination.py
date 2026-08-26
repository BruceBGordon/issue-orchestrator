"""Strong domain contracts for terminal-session containment."""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorInteractiveSessionCancellation,
)
from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
    TerminalSessionProcess,
    TerminalSessionTerminationPolicy,
)
from issue_orchestrator.domain.process_group import ProcessBirthIdentity


def _terminal_cancellation(tmp_path: Path) -> TerminalSessionOwnerCancellation:
    return TerminalSessionOwnerCancellation.for_run_dir(tmp_path.resolve())


def _executor_cancellation(tmp_path: Path) -> ExecutorInteractiveSessionCancellation:
    return ExecutorInteractiveSessionCancellation.for_run_dir(tmp_path.resolve())


@pytest.mark.parametrize("process_id", (True, 0, 1, -1))
def test_terminal_session_process_requires_real_group_leader_identity(
    tmp_path: Path,
    process_id: int,
) -> None:
    with pytest.raises(ValueError, match="integer above 1"):
        TerminalSessionProcess(
            process_id,
            ProcessBirthIdentity("darwin-timeval:1700000000:100"),
            _terminal_cancellation(tmp_path),
            _executor_cancellation(tmp_path),
        )


def test_terminal_session_process_requires_typed_cancellation(tmp_path: Path) -> None:
    identity = ProcessBirthIdentity("darwin-timeval:1700000000:100")

    with pytest.raises(ValueError, match="terminal_cancellation"):
        TerminalSessionProcess(
            42,
            identity,
            cast(TerminalSessionOwnerCancellation, None),
            _executor_cancellation(tmp_path),
        )
    with pytest.raises(ValueError, match="executor_cancellation"):
        TerminalSessionProcess(
            42,
            identity,
            _terminal_cancellation(tmp_path),
            cast(ExecutorInteractiveSessionCancellation, None),
        )


@pytest.mark.parametrize("seconds", (0.0, -1.0, math.nan, math.inf))
def test_terminal_session_policy_requires_finite_positive_bounds(
    seconds: float,
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        TerminalSessionTerminationPolicy(seconds, 1.0)
    with pytest.raises(ValueError, match="finite and positive"):
        TerminalSessionTerminationPolicy(1.0, seconds)
