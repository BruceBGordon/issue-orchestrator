# pyright: strict
"""Portable ``ps`` adapter for process birth and process-group membership."""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..domain.process_group import (
    ProcessBirthIdentity,
    ProcessGroupAbsent,
    ProcessGroupExecutable,
    ProcessGroupObservation,
    ProcessGroupPermissionDenied,
    ProcessGroupZombiesOnly,
    ProcessIdentityAbsent,
    ProcessIdentityObservation,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
    ProcessSessionLeaderAbsent,
    ProcessSessionLeaderPermissionDenied,
    ProcessSessionLeaderPresent,
    ProcessSessionLeaderStale,
    ProcessSessionObservation,
)
from ..ports.process_identity_observer import (
    ProcessIdentityObserver,
    ProcessSessionResolver,
)


class ProcessGroupObservationError(RuntimeError):
    """The host process table could not be observed or parsed safely."""


def _require_identity_observer(value: object) -> None:
    if not isinstance(value, ProcessIdentityObserver):
        raise ValueError(
            "PsProcessGroupObserver.process_identity_observer must implement "
            "ProcessIdentityObserver"
        )


def _require_session_resolver(value: object) -> None:
    if not isinstance(value, ProcessSessionResolver):
        raise ValueError(
            "PsProcessGroupObserver.process_session_resolver must implement "
            "ProcessSessionResolver"
        )


@dataclass(frozen=True, slots=True)
class PsProcessObservationPolicy:
    """Bound one host process-table probe against an unresponsive ``ps``."""

    command_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.command_timeout_seconds) is not float
            or not math.isfinite(self.command_timeout_seconds)
            or self.command_timeout_seconds <= 0
        ):
            raise ValueError(
                "PsProcessObservationPolicy.command_timeout_seconds must be "
                "finite and positive"
            )


@dataclass(frozen=True, slots=True)
class _ProcessTableEntry:
    process_id: int
    process_group_id: int
    session_id: int
    state: str


class GetsidProcessSessionResolver:
    """Answer session membership from the kernel, not the ps table."""

    def resolve_session(self, process_id: int) -> int | None:
        try:
            return os.getsid(process_id)
        except (PermissionError, ProcessLookupError):
            return None


class PsProcessGroupObserver:
    """Read one portable process-table snapshot per observation."""

    def __init__(
        self,
        ps_executable: Path,
        policy: PsProcessObservationPolicy,
        process_identity_observer: ProcessIdentityObserver,
        process_session_resolver: ProcessSessionResolver,
    ) -> None:
        if not ps_executable.is_absolute():
            raise ValueError("PsProcessGroupObserver.ps_executable must be absolute")
        if type(policy) is not PsProcessObservationPolicy:
            raise ValueError(
                "PsProcessGroupObserver.policy must be PsProcessObservationPolicy"
            )
        self._ps_executable = ps_executable
        self._policy = policy
        _require_identity_observer(process_identity_observer)
        self._process_identity_observer = process_identity_observer
        _require_session_resolver(process_session_resolver)
        self._process_session_resolver = process_session_resolver

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        _require_process_identifier(process_id)
        return self._process_identity_observer.observe_process(process_id)

    def observe_group(self, process_group_id: int) -> ProcessGroupObservation:
        _require_process_identifier(process_group_id)
        snapshot = self._snapshot()
        if isinstance(snapshot, ProcessGroupPermissionDenied):
            return snapshot
        members = tuple(
            entry for entry in snapshot if entry.process_group_id == process_group_id
        )
        if not members:
            return ProcessGroupAbsent()
        if all(entry.state.startswith("Z") for entry in members):
            return ProcessGroupZombiesOnly(len(members))
        return ProcessGroupExecutable(len(members))

    def observe_session(
        self,
        process_id: int,
        expected_birth_identity: ProcessBirthIdentity,
    ) -> ProcessSessionObservation:
        """Return one closed leader-plus-group decision from this owner."""
        identity = self.observe_process(process_id)
        if type(identity) is ProcessIdentityAbsent:
            return ProcessSessionLeaderAbsent()
        if type(identity) is ProcessIdentityPermissionDenied:
            return ProcessSessionLeaderPermissionDenied(identity.detail)
        if type(identity) is not ProcessIdentityPresent:
            raise AssertionError("process identity observation is a closed union")
        if identity.birth_identity != expected_birth_identity:
            return ProcessSessionLeaderStale(identity)
        # The leader created its session, so its PID is the session id; the
        # decision must cover every group in that session (interactive
        # guardians and their descendants live outside the leader's group).
        return ProcessSessionLeaderPresent(
            identity, self._observe_session_members(process_id)
        )

    def observe_session_group_ids(self, session_id: int) -> tuple[int, ...]:
        """Enumerate live group ids in one session (leader must be alive)."""
        snapshot = self._snapshot()
        if isinstance(snapshot, ProcessGroupPermissionDenied):
            return ()
        return tuple(
            sorted(
                {
                    entry.process_group_id
                    for entry in self._session_members(snapshot, session_id)
                }
            )
        )

    def _session_members(
        self,
        snapshot: tuple[_ProcessTableEntry, ...],
        session_id: int,
    ) -> tuple[_ProcessTableEntry, ...]:
        """Session membership that survives macOS ``ps`` reporting sess=0.

        Linux reports the true session id in the sess column, but macOS
        prints 0 for every process, which would make a session full of
        live processes observe as absent. Entries the table cannot vouch
        for are confirmed against the kernel via ``getsid(2)``. Zombies
        never answer ``getsid``, so they are admitted through their
        process group instead: POSIX guarantees a process group never
        spans sessions, so a zombie in a confirmed group is a member. A
        group made only of zombies stays unobservable here, which is
        containment-safe: nothing in it can run or hold a descriptor.
        """
        members: dict[int, _ProcessTableEntry] = {}
        for entry in snapshot:
            if entry.session_id == session_id:
                members[entry.process_id] = entry
            elif entry.session_id == 0 and not entry.state.startswith("Z"):
                resolved = self._process_session_resolver.resolve_session(
                    entry.process_id
                )
                if resolved == session_id:
                    members[entry.process_id] = entry
        confirmed_groups = {
            entry.process_group_id for entry in members.values()
        }
        for entry in snapshot:
            if (
                entry.process_id not in members
                and entry.session_id == 0
                and entry.state.startswith("Z")
                and entry.process_group_id in confirmed_groups
            ):
                members[entry.process_id] = entry
        return tuple(members.values())

    def _observe_session_members(self, session_id: int) -> ProcessGroupObservation:
        snapshot = self._snapshot()
        if isinstance(snapshot, ProcessGroupPermissionDenied):
            return snapshot
        members = self._session_members(snapshot, session_id)
        if not members:
            return ProcessGroupAbsent()
        if all(entry.state.startswith("Z") for entry in members):
            return ProcessGroupZombiesOnly(len(members))
        return ProcessGroupExecutable(len(members))

    def _snapshot(
        self,
    ) -> tuple[_ProcessTableEntry, ...] | ProcessGroupPermissionDenied:
        try:
            completed = subprocess.run(
                (
                    str(self._ps_executable),
                    "-axo",
                    "pid=,pgid=,sess=,stat=",
                ),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._policy.command_timeout_seconds,
                env={**os.environ, "LC_ALL": "C"},
            )
        except PermissionError as exc:
            return ProcessGroupPermissionDenied(repr(exc))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProcessGroupObservationError(
                f"could not read process table with {self._ps_executable}: {exc!r}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"ps exited {completed.returncode}"
            if "permission" in detail.lower():
                return ProcessGroupPermissionDenied(detail)
            raise ProcessGroupObservationError(
                f"could not read process table with {self._ps_executable}: {detail}"
            )
        return tuple(
            _parse_process_table_line(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        )


def _parse_process_table_line(line: str) -> _ProcessTableEntry:
    fields = line.strip().split()
    if len(fields) != 4:
        raise ProcessGroupObservationError(
            f"malformed ps process-table line: {line!r}"
        )
    raw_pid, raw_pgid, raw_session, state = fields
    try:
        process_id = int(raw_pid)
        process_group_id = int(raw_pgid)
        session_id = int(raw_session)
    except (TypeError, ValueError) as exc:
        raise ProcessGroupObservationError(
            f"malformed ps process-table line: {line!r}"
        ) from exc
    _require_positive_table_identifier(process_id)
    _require_positive_table_identifier(process_group_id)
    if not state:
        raise ProcessGroupObservationError(f"missing process state: {line!r}")
    return _ProcessTableEntry(
        process_id=process_id,
        process_group_id=process_group_id,
        session_id=session_id,
        state=state,
    )


def _require_process_identifier(value: int) -> None:
    if type(value) is not int or value <= 1:
        raise ValueError("process identifier must be an integer above 1")


def _require_positive_table_identifier(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ProcessGroupObservationError(
            "process-table identifiers must be positive integers"
        )
