"""Typed ownership and outcome contracts for process-group containment."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class OwnedProcessGroupLeader:
    """A live or unreaped leader whose pid still reserves its process group."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "OwnedProcessGroupLeader.process_id must be an integer above 1"
            )


@dataclass(frozen=True, slots=True)
class ProcessBirthIdentity:
    """Collision-resistant kernel token used to reject recycled PIDs."""

    kernel_token: str

    def __post_init__(self) -> None:
        if type(self.kernel_token) is not str or not self.kernel_token:
            raise ValueError(
                "ProcessBirthIdentity.kernel_token must be a non-empty string"
            )


@dataclass(frozen=True, slots=True)
class ProcessIdentityAbsent:
    """No process currently owns the requested PID."""


@dataclass(frozen=True, slots=True)
class ProcessIdentityPresent:
    """One current process identity and its current process-group membership."""

    birth_identity: ProcessBirthIdentity
    process_group_id: int

    def __post_init__(self) -> None:
        if type(self.birth_identity) is not ProcessBirthIdentity:
            raise ValueError(
                "ProcessIdentityPresent.birth_identity must be ProcessBirthIdentity"
            )
        if type(self.process_group_id) is not int or self.process_group_id <= 1:
            raise ValueError(
                "ProcessIdentityPresent.process_group_id must be above 1"
            )


@dataclass(frozen=True, slots=True)
class ProcessIdentityPermissionDenied:
    """The OS refused a process identity observation."""

    detail: str

    def __post_init__(self) -> None:
        if type(self.detail) is not str or not self.detail:
            raise ValueError(
                "ProcessIdentityPermissionDenied.detail must not be empty"
            )


ProcessIdentityObservation = (
    ProcessIdentityAbsent
    | ProcessIdentityPresent
    | ProcessIdentityPermissionDenied
)


@dataclass(frozen=True, slots=True)
class ProcessGroupAbsent:
    """No process currently belongs to the requested process group."""


@dataclass(frozen=True, slots=True)
class ProcessGroupZombiesOnly:
    """The process group has members, but none can execute user code."""

    member_count: int

    def __post_init__(self) -> None:
        if type(self.member_count) is not int or self.member_count < 1:
            raise ValueError("ProcessGroupZombiesOnly.member_count must be positive")


@dataclass(frozen=True, slots=True)
class ProcessGroupExecutable:
    """At least one process-group member can still execute user code."""

    member_count: int

    def __post_init__(self) -> None:
        if type(self.member_count) is not int or self.member_count < 1:
            raise ValueError("ProcessGroupExecutable.member_count must be positive")


@dataclass(frozen=True, slots=True)
class ProcessGroupPermissionDenied:
    """The OS refused a process-group membership observation."""

    detail: str

    def __post_init__(self) -> None:
        if type(self.detail) is not str or not self.detail:
            raise ValueError(
                "ProcessGroupPermissionDenied.detail must not be empty"
            )


ProcessGroupObservation = (
    ProcessGroupAbsent
    | ProcessGroupZombiesOnly
    | ProcessGroupExecutable
    | ProcessGroupPermissionDenied
)


@dataclass(frozen=True, slots=True)
class ProcessSessionLeaderAbsent:
    """The recorded leader is absent; its numeric group must not be signalled."""


@dataclass(frozen=True, slots=True)
class ProcessSessionLeaderPermissionDenied:
    """The OS refused the leader identity required for a safe decision."""

    detail: str

    def __post_init__(self) -> None:
        if type(self.detail) is not str or not self.detail:
            raise ValueError(
                "ProcessSessionLeaderPermissionDenied.detail must not be empty"
            )


@dataclass(frozen=True, slots=True)
class ProcessSessionLeaderStale:
    """The PID now names a process with a different exact birth identity."""

    observed_identity: ProcessIdentityPresent

    def __post_init__(self) -> None:
        if type(self.observed_identity) is not ProcessIdentityPresent:
            raise ValueError(
                "ProcessSessionLeaderStale.observed_identity must be "
                "ProcessIdentityPresent"
            )


@dataclass(frozen=True, slots=True)
class ProcessSessionLeaderPresent:
    """One owner-level leader identity plus its group-membership observation."""

    identity: ProcessIdentityPresent
    group: ProcessGroupObservation

    def __post_init__(self) -> None:
        if type(self.identity) is not ProcessIdentityPresent:
            raise ValueError(
                "ProcessSessionLeaderPresent.identity must be ProcessIdentityPresent"
            )
        if type(self.group) not in (
            ProcessGroupAbsent,
            ProcessGroupZombiesOnly,
            ProcessGroupExecutable,
            ProcessGroupPermissionDenied,
        ):
            raise ValueError(
                "ProcessSessionLeaderPresent.group must be a closed observation"
            )


ProcessSessionObservation = (
    ProcessSessionLeaderAbsent
    | ProcessSessionLeaderPermissionDenied
    | ProcessSessionLeaderStale
    | ProcessSessionLeaderPresent
)


@dataclass(frozen=True, slots=True)
class ProcessGroupCourtesyCompleted:
    """The courtesy TERM observation completed before forced containment."""


@dataclass(frozen=True, slots=True)
class ProcessGroupCourtesyFailed:
    """Courtesy TERM observation failed, but forced containment still completed."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("ProcessGroupCourtesyFailed.error must be an exception")


ProcessGroupCourtesy = ProcessGroupCourtesyCompleted | ProcessGroupCourtesyFailed


@dataclass(frozen=True, slots=True)
class ProcessGroupTermination:
    """Reaped leader evidence after unconditional whole-group containment."""

    leader_exit_code: int
    courtesy: ProcessGroupCourtesy

    def __post_init__(self) -> None:
        if type(self.leader_exit_code) is not int:
            raise ValueError(
                "ProcessGroupTermination.leader_exit_code must be an integer"
            )
        if type(self.courtesy) not in (
            ProcessGroupCourtesyCompleted,
            ProcessGroupCourtesyFailed,
        ):
            raise ValueError("ProcessGroupTermination.courtesy must be typed")

    def courtesy_failure(self) -> ProcessGroupCourtesyFailed | None:
        """Return typed degraded-shutdown evidence, if courtesy TERM failed."""
        if type(self.courtesy) is ProcessGroupCourtesyCompleted:
            return None
        if type(self.courtesy) is ProcessGroupCourtesyFailed:
            return self.courtesy
        raise AssertionError("process-group courtesy is a closed union")


@dataclass(frozen=True, slots=True)
class ProcessGroupUnboundedWait:
    """Wait for natural leader completion without a wall-clock deadline."""


@dataclass(frozen=True, slots=True)
class ProcessGroupBoundedWait:
    """Wait at most the remaining command budget before containment."""

    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "ProcessGroupBoundedWait.timeout_seconds must be finite and positive"
            )


ProcessGroupWait = ProcessGroupUnboundedWait | ProcessGroupBoundedWait


@dataclass(frozen=True, slots=True)
class ProcessGroupCompleted:
    """Natural leader exit followed by whole-group containment and reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupCompleted.termination must be ProcessGroupTermination"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupTimedOut:
    """Deadline-driven whole-group containment and leader reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupTimedOut.termination must be ProcessGroupTermination"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupInterrupted:
    """Caller-requested whole-group containment and leader reaping."""

    termination: ProcessGroupTermination

    def __post_init__(self) -> None:
        if type(self.termination) is not ProcessGroupTermination:
            raise ValueError(
                "ProcessGroupInterrupted.termination must be "
                "ProcessGroupTermination"
            )


ProcessGroupSupervision = (
    ProcessGroupCompleted | ProcessGroupTimedOut | ProcessGroupInterrupted
)
