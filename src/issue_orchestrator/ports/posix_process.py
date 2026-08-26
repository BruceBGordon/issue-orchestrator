"""Behavior port for retained POSIX process activation and exact child access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.posix_process import PosixProcessLaunchSpec


class PosixProcessExecError(RuntimeError):
    """The retained wrapper could not replace itself with the requested program."""


@runtime_checkable
class PosixProcessActivationClock(Protocol):
    """Observe the host monotonic clock used by absolute activation deadlines."""

    def monotonic(self) -> float:
        """Return one finite, non-negative host-boot monotonic instant."""
        ...


@runtime_checkable
class PosixProcessHandle(Protocol):
    """One exact child whose reaping authority remains with this process."""

    @property
    def process_id(self) -> int: ...

    @property
    def return_code(self) -> int | None: ...

    def poll(self) -> int | None:
        """Reap and return a terminal code, or report that the child is live."""
        ...

    def wait(self, timeout_seconds: float) -> int:
        """Reap under one explicit positive timeout."""
        ...

    def kill(self) -> None:
        """Send SIGKILL to this exact child."""
        ...

    def record_external_reap(self, exit_code: int) -> None:
        """Record typed reaping evidence supplied by a group owner."""
        ...


@dataclass(frozen=True, slots=True)
class PosixProcessLaunchStarted:
    """The exact process handle is retained by the caller."""

    process: PosixProcessHandle

    def __post_init__(self) -> None:
        if not isinstance(self.process, PosixProcessHandle):
            raise ValueError("PosixProcessLaunchStarted.process must be typed")


@dataclass(frozen=True, slots=True)
class PosixProcessLaunchRejected:
    """The spawn primitive authoritatively created no child."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("PosixProcessLaunchRejected.error must be an exception")


@dataclass(frozen=True, slots=True)
class PosixProcessLaunchRecovered:
    """Activation was interrupted after spawn and the exact child was contained."""

    process_id: int
    exit_code: int
    activation_error: BaseException

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("PosixProcessLaunchRecovered.process_id must be above 1")
        if type(self.exit_code) is not int:
            raise ValueError("PosixProcessLaunchRecovered.exit_code must be an int")
        if not isinstance(self.activation_error, BaseException):
            raise ValueError(
                "PosixProcessLaunchRecovered.activation_error must be an exception"
            )


@dataclass(frozen=True, slots=True)
class PosixProcessExecRejected:
    """The retained wrapper could not exec, and its exact child was reaped."""

    process_id: int
    exit_code: int
    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("PosixProcessExecRejected.process_id must be above 1")
        if type(self.exit_code) is not int:
            raise ValueError("PosixProcessExecRejected.exit_code must be an int")
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError("PosixProcessExecRejected.error_type must be non-empty")
        if type(self.error_repr) is not str or not self.error_repr:
            raise ValueError("PosixProcessExecRejected.error_repr must be non-empty")

    def as_error(self) -> PosixProcessExecError:
        """Materialize exact typed failure evidence for domain boundaries."""
        return PosixProcessExecError(
            f"child exec failed with {self.error_type}: {self.error_repr}"
        )


@dataclass(frozen=True, slots=True)
class PosixProcessLaunchRecoveryFailed:
    """Activation produced a PID but exact containment could not be proven."""

    process_id: int
    activation_error: BaseException
    recovery_error: BaseException

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "PosixProcessLaunchRecoveryFailed.process_id must be above 1"
            )
        if not isinstance(self.activation_error, BaseException):
            raise ValueError(
                "PosixProcessLaunchRecoveryFailed.activation_error must be an exception"
            )
        if not isinstance(self.recovery_error, BaseException):
            raise ValueError(
                "PosixProcessLaunchRecoveryFailed.recovery_error must be an exception"
            )


PosixProcessLaunch = (
    PosixProcessLaunchStarted
    | PosixProcessLaunchRejected
    | PosixProcessLaunchRecovered
    | PosixProcessExecRejected
    | PosixProcessLaunchRecoveryFailed
)


@runtime_checkable
class PosixProcessLauncher(Protocol):
    """Activate one child without exposing a post-fork ownership gap."""

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        """Return one closed activation/containment outcome."""
        ...
