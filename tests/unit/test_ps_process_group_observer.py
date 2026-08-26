"""Portable process-table adapter contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.adapters.ps_process_group_observer import (
    ProcessGroupObservationError,
    PsProcessGroupObserver,
    PsProcessObservationPolicy,
)


class _FixtureProcessIdentityObserver:
    def observe_process(self, process_id: int) -> ProcessIdentityPresent:
        return ProcessIdentityPresent(
            ProcessBirthIdentity(f"fixture-kernel-token:{process_id}"),
            process_id,
        )


class _FixtureProcessSessionResolver:
    """Kernel getsid(2) answers for a declared process table."""

    def __init__(self, sessions: dict[int, int] | None = None) -> None:
        self._sessions = sessions or {}

    def resolve_session(self, process_id: int) -> int | None:
        return self._sessions.get(process_id)


from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessGroupExecutable,
    ProcessGroupZombiesOnly,
    ProcessIdentityPresent,
    ProcessSessionLeaderPresent,
)


def _observer(
    tmp_path: Path,
    process_table: str,
    *,
    name: str = "ps-fixture",
    sessions: dict[int, int] | None = None,
) -> PsProcessGroupObserver:
    executable = tmp_path / name
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%b\\n' " + repr(process_table) + "\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return PsProcessGroupObserver(
        executable.resolve(),
        PsProcessObservationPolicy(command_timeout_seconds=2.0),
        _FixtureProcessIdentityObserver(),
        _FixtureProcessSessionResolver(sessions),
    )


def test_snapshot_accepts_system_pid_one_and_identifies_requested_process(
    tmp_path: Path,
) -> None:
    observer = _observer(
        tmp_path,
        "1 1 1 Ss\n"
        "42 42 42 S",
    )

    assert observer.observe_process(42) == ProcessIdentityPresent(
        ProcessBirthIdentity("fixture-kernel-token:42"),
        42,
    )


def test_group_observation_distinguishes_zombies_from_executable_members(
    tmp_path: Path,
) -> None:
    zombies = _observer(
        tmp_path,
        "42 42 42 Z\n"
        "43 42 42 Z+",
        name="ps-zombies",
    )
    executable = _observer(
        tmp_path,
        "42 42 42 Z\n"
        "43 42 42 S+",
        name="ps-executable",
    )

    assert zombies.observe_group(42) == ProcessGroupZombiesOnly(2)
    assert executable.observe_group(42) == ProcessGroupExecutable(2)


def test_session_membership_asks_the_kernel_when_ps_reports_sess_zero(
    tmp_path: Path,
) -> None:
    """macOS ps prints sess=0 for every process; getsid(2) is the only
    portable session key there. Zombies never answer getsid, so they
    are admitted through their confirmed process group instead."""
    observer = _observer(
        tmp_path,
        "1 1 0 Ss\n"
        "42 42 0 Ss\n"
        "43 43 0 S\n"
        "44 44 0 T\n"
        "45 43 0 Z\n"
        "50 50 0 S",
        name="ps-darwin",
        sessions={1: 1, 42: 42, 43: 42, 44: 42, 50: 50},
    )

    assert observer.observe_session_group_ids(42) == (42, 43, 44)
    session = observer.observe_session(
        42, ProcessBirthIdentity("fixture-kernel-token:42")
    )
    assert session == ProcessSessionLeaderPresent(
        ProcessIdentityPresent(ProcessBirthIdentity("fixture-kernel-token:42"), 42),
        ProcessGroupExecutable(4),
    )


def test_session_membership_excludes_foreign_sessions_the_kernel_disowns(
    tmp_path: Path,
) -> None:
    """A stalled-session sweep must never pull in bystander processes
    whose getsid answer names a different session."""
    observer = _observer(
        tmp_path,
        "42 42 0 Ss\n"
        "77 77 0 R+\n"
        "78 77 0 R+",
        name="ps-foreign",
        sessions={42: 42, 77: 70, 78: 70},
    )

    assert observer.observe_session_group_ids(42) == (42,)


def test_malformed_process_table_fails_loudly(tmp_path: Path) -> None:
    observer = _observer(tmp_path, "42 42 S unexpected-field")

    with pytest.raises(ProcessGroupObservationError, match="malformed"):
        observer.observe_group(42)
