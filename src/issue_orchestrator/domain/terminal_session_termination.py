"""Typed requests and policy for containing a terminal session."""

from __future__ import annotations

from typing import cast

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .executor import ExecutorInteractiveSessionCancellation
from .process_group import (
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


TERMINAL_SESSION_CANCELLATION_FILENAME = "terminal-session-cancellation.json"


class TerminalSessionContainmentError(RuntimeError):
    """The exact persisted terminal process could not be safely contained."""


@dataclass(frozen=True, slots=True)
class TerminalSessionOwnerCancellation:
    """Durable endpoint identity owned by a terminal-group member."""

    record_path: Path

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.record_path), Path) or not self.record_path.is_absolute():
            raise ValueError(
                "TerminalSessionOwnerCancellation.record_path must be absolute"
            )

    @classmethod
    def for_run_dir(cls, run_dir: Path) -> TerminalSessionOwnerCancellation:
        if not isinstance(cast(object, run_dir), Path) or not run_dir.is_absolute():
            raise ValueError(
                "TerminalSessionOwnerCancellation.run_dir must be absolute"
            )
        return cls(run_dir / TERMINAL_SESSION_CANCELLATION_FILENAME)


@dataclass(frozen=True, slots=True)
class UnregisteredTerminalSessionOwnership:
    """Durable endpoint identities available before PID registry commit."""

    terminal_cancellation: TerminalSessionOwnerCancellation
    executor_cancellation: ExecutorInteractiveSessionCancellation

    def __post_init__(self) -> None:
        if type(self.terminal_cancellation) is not TerminalSessionOwnerCancellation:
            raise ValueError(
                "terminal_cancellation must be TerminalSessionOwnerCancellation"
            )
        if (
            type(self.executor_cancellation)
            is not ExecutorInteractiveSessionCancellation
        ):
            raise ValueError(
                "executor_cancellation must be "
                "ExecutorInteractiveSessionCancellation"
            )
        if (
            self.terminal_cancellation.record_path.parent
            != self.executor_cancellation.record_path.parent
        ):
            raise ValueError("unregistered cancellation records must share a run dir")

    @classmethod
    def for_run_dir(cls, run_dir: Path) -> UnregisteredTerminalSessionOwnership:
        return cls(
            TerminalSessionOwnerCancellation.for_run_dir(run_dir),
            ExecutorInteractiveSessionCancellation.for_run_dir(run_dir),
        )


@dataclass(frozen=True, slots=True)
class TerminalSessionProcess:
    """Persistable identity required to contain one terminal session."""

    process_id: int
    birth_identity: ProcessBirthIdentity
    terminal_cancellation: TerminalSessionOwnerCancellation
    executor_cancellation: ExecutorInteractiveSessionCancellation

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "TerminalSessionProcess.process_id must be an integer above 1"
            )
        if type(self.birth_identity) is not ProcessBirthIdentity:
            raise ValueError(
                "TerminalSessionProcess.birth_identity must be ProcessBirthIdentity"
            )
        if type(self.terminal_cancellation) is not TerminalSessionOwnerCancellation:
            raise ValueError(
                "TerminalSessionProcess.terminal_cancellation must be a "
                "TerminalSessionOwnerCancellation"
            )
        if (
            type(self.executor_cancellation)
            is not ExecutorInteractiveSessionCancellation
        ):
            raise ValueError(
                "TerminalSessionProcess.executor_cancellation must be an "
                "ExecutorInteractiveSessionCancellation"
            )
        if (
            self.terminal_cancellation.record_path.parent
            != self.executor_cancellation.record_path.parent
        ):
            raise ValueError(
                "TerminalSessionProcess cancellation records must share a run dir"
            )


class TerminalSessionStatus(StrEnum):
    """Read-only status of one persisted terminal identity."""

    ACTIVE = "active"
    CONTAINED = "contained"
    STALE_IDENTITY = "stale-identity"


class TerminalSessionIdentityDisposition(StrEnum):
    """Whether a persisted process identity can still name its original group."""

    CURRENT = "current"
    STALE = "stale"


class TerminalSessionTerminationOutcome(StrEnum):
    """Exact successful terminal-session containment outcome."""

    ALREADY_CONTAINED = "already-contained"
    STALE_IDENTITY_RETIRED = "stale-identity-retired"
    GRACEFUL = "graceful"
    FORCED = "forced"


class TerminalSessionOwnerContainmentOutcome(StrEnum):
    """Exact state of one terminal-related self-containment owner."""

    ABSENT = "absent"
    STALE_RETIRED = "stale-retired"
    CONTAINED = "contained"


@dataclass(frozen=True, slots=True)
class TerminalSessionContainmentReport:
    """Separate outcomes for the terminal and detached guardian groups."""

    terminal_owner: TerminalSessionOwnerContainmentOutcome
    guardian_owner: TerminalSessionOwnerContainmentOutcome

    def __post_init__(self) -> None:
        if type(self.terminal_owner) is not TerminalSessionOwnerContainmentOutcome:
            raise ValueError("terminal_owner requires an exact containment outcome")
        if type(self.guardian_owner) is not TerminalSessionOwnerContainmentOutcome:
            raise ValueError("guardian_owner requires an exact containment outcome")


def classify_terminal_session_identity(
    process: TerminalSessionProcess,
    observation: ProcessIdentityObservation,
) -> TerminalSessionIdentityDisposition:
    """Validate exact PID birth and group-leader identity before group use."""
    if type(process) is not TerminalSessionProcess:
        raise ValueError(
            "classify_terminal_session_identity.process must be "
            "TerminalSessionProcess"
        )
    if type(observation) is ProcessIdentityPermissionDenied:
        raise TerminalSessionContainmentError(
            "permission denied while observing terminal session process: "
            f"pid={process.process_id} detail={observation.detail}"
        )
    if type(observation) is ProcessIdentityAbsent:
        return TerminalSessionIdentityDisposition.STALE
    if type(observation) is not ProcessIdentityPresent:
        raise AssertionError("process identity observation is a closed union")
    if observation.birth_identity != process.birth_identity:
        return TerminalSessionIdentityDisposition.STALE
    if observation.process_group_id != process.process_id:
        raise TerminalSessionContainmentError(
            "terminal session registry pid is not its process-group leader: "
            f"pid={process.process_id} pgid={observation.process_group_id}"
        )
    return TerminalSessionIdentityDisposition.CURRENT


def classify_terminal_session_observation(
    process: TerminalSessionProcess,
    observation: ProcessSessionObservation,
) -> TerminalSessionStatus:
    """Make one safe session decision from one owner-level observation."""
    if type(process) is not TerminalSessionProcess:
        raise ValueError(
            "classify_terminal_session_observation.process must be "
            "TerminalSessionProcess"
        )
    if type(observation) is ProcessSessionLeaderAbsent:
        # A numeric PGID can already have been recycled.  The guardian
        # ownership channel is the only safe recovered containment path now.
        return TerminalSessionStatus.STALE_IDENTITY
    if type(observation) is ProcessSessionLeaderPermissionDenied:
        raise TerminalSessionContainmentError(
            "permission denied while observing terminal session leader: "
            f"pid={process.process_id} detail={observation.detail}"
        )
    if type(observation) is ProcessSessionLeaderStale:
        return TerminalSessionStatus.STALE_IDENTITY
    if type(observation) is not ProcessSessionLeaderPresent:
        raise AssertionError("process session observation is a closed union")
    disposition = classify_terminal_session_identity(process, observation.identity)
    if disposition is TerminalSessionIdentityDisposition.STALE:
        return TerminalSessionStatus.STALE_IDENTITY
    return classify_terminal_session_group(process.process_id, observation.group)


def classify_terminal_session_group(
    process_group_id: int,
    observation: ProcessGroupObservation,
) -> TerminalSessionStatus:
    """Classify executable group membership without treating zombies as live."""
    if type(process_group_id) is not int or process_group_id <= 1:
        raise ValueError("terminal process-group id must be an integer above 1")
    if type(observation) is ProcessGroupExecutable:
        return TerminalSessionStatus.ACTIVE
    if type(observation) in (ProcessGroupAbsent, ProcessGroupZombiesOnly):
        return TerminalSessionStatus.CONTAINED
    if type(observation) is ProcessGroupPermissionDenied:
        raise TerminalSessionContainmentError(
            "permission denied while observing terminal session process group: "
            f"pgid={process_group_id} detail={observation.detail}"
        )
    raise AssertionError("process group observation is a closed union")


def terminal_session_resolution_outcome(
    status: TerminalSessionStatus,
    contained_outcome: TerminalSessionTerminationOutcome,
) -> TerminalSessionTerminationOutcome | None:
    """Map one observed status to completion, preserving the active state."""
    if type(status) is not TerminalSessionStatus:
        raise ValueError("terminal session status must be TerminalSessionStatus")
    if type(contained_outcome) is not TerminalSessionTerminationOutcome:
        raise ValueError(
            "contained outcome must be TerminalSessionTerminationOutcome"
        )
    if status is TerminalSessionStatus.ACTIVE:
        return None
    if status is TerminalSessionStatus.STALE_IDENTITY:
        return TerminalSessionTerminationOutcome.STALE_IDENTITY_RETIRED
    if status is TerminalSessionStatus.CONTAINED:
        return contained_outcome
    raise AssertionError("TerminalSessionStatus is a closed enum")


@dataclass(frozen=True, slots=True)
class TerminalSessionTerminationPolicy:
    """Bounded courtesy and containment waits for a terminal session."""

    graceful_shutdown_seconds: float
    forceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("graceful_shutdown_seconds", self.graceful_shutdown_seconds),
            ("forceful_shutdown_seconds", self.forceful_shutdown_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"TerminalSessionTerminationPolicy.{field_name} must be "
                    "finite and positive"
                )
