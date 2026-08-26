# pyright: strict
"""One typed owner for POSIX flock acquisition and handle finalization.

``fcntl`` is imported at each syscall site, never at module import: entry-point
composition must stay importable on hosts without the POSIX executor, failing
explicitly only when a lock is actually attempted.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Generator, NoReturn

from .independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupFailure,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)


class PosixFileLockMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class PosixFileLockAcquisition(StrEnum):
    BLOCKING = "blocking"
    NON_BLOCKING = "non-blocking"


class PosixFileLockFilePresence(StrEnum):
    CREATE_IF_MISSING = "create-if-missing"
    REQUIRE_EXISTING = "require-existing"


class _PosixFileLockLeaseState(StrEnum):
    OPEN = "open"
    CLEANUP_REQUIRED = "cleanup-required"
    RELEASED = "released"


def _require_absolute_path(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError("PosixFileLockSpecification.path must be absolute")
    return value


@dataclass(frozen=True, slots=True)
class PosixFileLockSpecification:
    path: Path
    mode: PosixFileLockMode
    acquisition: PosixFileLockAcquisition
    file_presence: PosixFileLockFilePresence

    def __post_init__(self) -> None:
        _require_absolute_path(self.path)
        if type(self.mode) is not PosixFileLockMode:
            raise ValueError("PosixFileLockSpecification.mode must be typed")
        if type(self.acquisition) is not PosixFileLockAcquisition:
            raise ValueError("PosixFileLockSpecification.acquisition must be typed")
        if type(self.file_presence) is not PosixFileLockFilePresence:
            raise ValueError("PosixFileLockSpecification.file_presence must be typed")


class PosixFileLockLease:
    """Own one open lock handle, whether acquired or observed contended."""

    def __init__(self, handle: BinaryIO) -> None:
        if handle.closed:
            raise ValueError("PosixFileLockLease requires an open handle")
        self._handle = handle
        self._state = _PosixFileLockLeaseState.OPEN

    @property
    def handle(self) -> BinaryIO:
        if self._state is not _PosixFileLockLeaseState.OPEN:
            raise RuntimeError(
                "only an open POSIX file-lock lease exposes its handle"
            )
        return self._handle

    def release(self) -> None:
        if self._state is _PosixFileLockLeaseState.RELEASED:
            raise RuntimeError("POSIX file-lock lease was released twice")
        raise_cleanup_failures(
            "POSIX file-lock cleanup failures",
            self._finalize(),
        )

    def release_after_failure(self, primary_error: BaseException) -> NoReturn:
        if self._state is _PosixFileLockLeaseState.RELEASED:
            raise RuntimeError("POSIX file-lock lease was released twice")
        raise_primary_with_cleanup(
            "POSIX file-lock operation and cleanup failures",
            primary_error,
            self._finalize(),
        )

    def _finalize(self) -> CleanupOutcome:
        import fcntl

        outcome = IndependentCleanupPlan(
            (
                CleanupAction(
                    "unlock POSIX file-lock handle",
                    lambda: fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN),
                ),
                CleanupAction("close POSIX file-lock handle", self._handle.close),
            )
        ).run()
        if self._handle.closed:
            self._state = _PosixFileLockLeaseState.RELEASED
            return outcome

        self._state = _PosixFileLockLeaseState.CLEANUP_REQUIRED
        retained_error = PosixFileLockOwnershipRetainedError(self)
        retained_error.add_note(
            "the caller retains explicit ownership and may invoke release again"
        )
        retained_failure = CleanupFailure(
            "retain open POSIX file-lock handle",
            retained_error,
        )
        if type(outcome) is CleanupSucceeded:
            return CleanupFailed((retained_failure,))
        if type(outcome) is not CleanupFailed:
            raise AssertionError("cleanup outcome is a closed union")
        return CleanupFailed((*outcome.failures, retained_failure))


class PosixFileLockOwnershipRetainedError(RuntimeError):
    """A failed finalization still owns the handle and exposes that owner."""

    def __init__(self, lease: PosixFileLockLease) -> None:
        if type(lease) is not PosixFileLockLease:
            raise ValueError(
                "PosixFileLockOwnershipRetainedError requires a typed lease"
            )
        super().__init__(
            "POSIX file-lock handle remains open after finalization failure"
        )
        self.lease = lease


@dataclass(frozen=True, slots=True)
class PosixFileLockAcquired:
    lease: PosixFileLockLease

    def __post_init__(self) -> None:
        if type(self.lease) is not PosixFileLockLease:
            raise ValueError("PosixFileLockAcquired.lease must be typed")


@dataclass(frozen=True, slots=True)
class PosixFileLockContended:
    lease: PosixFileLockLease

    def __post_init__(self) -> None:
        if type(self.lease) is not PosixFileLockLease:
            raise ValueError("PosixFileLockContended.lease must be typed")


PosixFileLockOutcome = PosixFileLockAcquired | PosixFileLockContended


class PosixFileLockOwner:
    """Deep adapter for shared/exclusive, blocking/nonblocking flock lifecycles."""

    def acquire(
        self, specification: PosixFileLockSpecification
    ) -> PosixFileLockOutcome:
        if type(specification) is not PosixFileLockSpecification:
            raise ValueError(
                "PosixFileLockOwner.acquire requires a typed specification"
            )
        flags = os.O_RDWR | os.O_CLOEXEC
        if specification.file_presence is PosixFileLockFilePresence.CREATE_IF_MISSING:
            flags |= os.O_CREAT
        descriptor = os.open(specification.path, flags, 0o600)
        try:
            handle = os.fdopen(descriptor, "r+b")
        except BaseException as stream_error:
            raise_primary_with_cleanup(
                "POSIX file-lock stream acquisition and descriptor cleanup failures",
                stream_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "close POSIX file-lock descriptor",
                            lambda: os.close(descriptor),
                        ),
                    )
                ).run(),
            )
        import fcntl

        lease = PosixFileLockLease(handle)
        try:
            fcntl.flock(handle.fileno(), self._operation(specification))
        except BlockingIOError as contention_error:
            if specification.acquisition is PosixFileLockAcquisition.NON_BLOCKING:
                return PosixFileLockContended(lease)
            lease.release_after_failure(contention_error)
        except BaseException as acquisition_error:
            lease.release_after_failure(acquisition_error)
        return PosixFileLockAcquired(lease)

    @contextmanager
    def hold(
        self,
        specification: PosixFileLockSpecification,
    ) -> Generator[BinaryIO]:
        if specification.acquisition is not PosixFileLockAcquisition.BLOCKING:
            raise ValueError("PosixFileLockOwner.hold requires blocking acquisition")
        outcome = self.acquire(specification)
        if type(outcome) is not PosixFileLockAcquired:
            raise AssertionError("blocking POSIX file lock must be acquired")
        try:
            yield outcome.lease.handle
        except BaseException as operation_error:
            outcome.lease.release_after_failure(operation_error)
        outcome.lease.release()

    @staticmethod
    def _operation(specification: PosixFileLockSpecification) -> int:
        import fcntl

        operation = (
            fcntl.LOCK_SH
            if specification.mode is PosixFileLockMode.SHARED
            else fcntl.LOCK_EX
        )
        if specification.acquisition is PosixFileLockAcquisition.NON_BLOCKING:
            operation |= fcntl.LOCK_NB
        return operation
