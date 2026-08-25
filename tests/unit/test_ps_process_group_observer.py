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
from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessGroupExecutable,
    ProcessGroupZombiesOnly,
    ProcessIdentityPresent,
)


def _observer(
    tmp_path: Path,
    process_table: str,
    *,
    name: str = "ps-fixture",
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
    )


def test_snapshot_accepts_system_pid_one_and_identifies_requested_process(
    tmp_path: Path,
) -> None:
    observer = _observer(
        tmp_path,
        "1 1 Ss\n"
        "42 42 S",
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
        "42 42 Z\n"
        "43 42 Z+",
        name="ps-zombies",
    )
    executable = _observer(
        tmp_path,
        "42 42 Z\n"
        "43 42 S+",
        name="ps-executable",
    )

    assert zombies.observe_group(42) == ProcessGroupZombiesOnly(2)
    assert executable.observe_group(42) == ProcessGroupExecutable(2)


def test_malformed_process_table_fails_loudly(tmp_path: Path) -> None:
    observer = _observer(tmp_path, "42 42 S unexpected-field")

    with pytest.raises(ProcessGroupObservationError, match="malformed"):
        observer.observe_group(42)
