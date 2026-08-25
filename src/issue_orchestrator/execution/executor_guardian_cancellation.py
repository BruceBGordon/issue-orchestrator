# pyright: strict
"""Lock-backed ownership channel between a guardian and terminal session."""

from __future__ import annotations

import fcntl
import logging
import math
import os
import signal
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..domain.executor import (
    ExecutorCommandCancellation,
    ExecutorInteractiveSessionCancellation,
    ExecutorNoCommandCancellation,
)


logger = logging.getLogger(__name__)


def _require_guardian_process_group_id(process_group_id: int) -> None:
    if type(process_group_id) is not int or process_group_id <= 1:
        raise ValueError("guardian process group id must be an integer above 1")


class ExecutorGuardianCancellationError(RuntimeError):
    """Raised when a guardian cancellation owner cannot be proven contained."""


class ExecutorGuardianCancellationOutcome(StrEnum):
    """Exact state observed while resolving a cancellation endpoint."""

    ABSENT = "absent"
    STALE_RETIRED = "stale-retired"
    CONTAINED = "contained"


class _GuardianCancellationRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    process_group_id: int = Field(gt=1)


class NoExecutorGuardianCancellationLease:
    """Explicit no-channel implementation for detached executor commands."""

    def inherited_file_descriptors(self) -> tuple[int, ...]:
        return ()

    def publish(self, process_group_id: int) -> None:
        _require_guardian_process_group_id(process_group_id)

    def transfer_to_guardian(self) -> None:
        pass

    def retire(self) -> None:
        pass


class InteractiveExecutorGuardianCancellationLease:
    """Hold one run-scoped lock and publish its guardian's process group."""

    def __init__(self, cancellation: ExecutorInteractiveSessionCancellation) -> None:
        if type(cancellation) is not ExecutorInteractiveSessionCancellation:
            raise ValueError(
                "InteractiveExecutorGuardianCancellationLease requires an "
                "ExecutorInteractiveSessionCancellation"
            )
        self._cancellation = cancellation
        self._lock_path = _lock_path(cancellation.record_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self._lock_path.open("a+b")
        try:
            fcntl.flock(
                self._lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self._lock_handle.close()
            raise ExecutorGuardianCancellationError(
                "an interactive executor guardian already owns this run: "
                f"{cancellation.record_path}"
            ) from exc
        self._record_published = False
        self._transferred = False
        self._retired = False
        self._retire_stale_record()

    def inherited_file_descriptors(self) -> tuple[int, ...]:
        if self._transferred or self._retired:
            raise RuntimeError(
                "executor guardian cancellation lock is no longer parent-owned"
            )
        return (self._lock_handle.fileno(),)

    def publish(self, process_group_id: int) -> None:
        _require_guardian_process_group_id(process_group_id)
        if self._record_published:
            raise RuntimeError("guardian cancellation record was published twice")
        if self._transferred or self._retired:
            raise RuntimeError("guardian cancellation lease is no longer publishable")
        _atomic_write_record(
            self._cancellation.record_path,
            _GuardianCancellationRecord(process_group_id=process_group_id),
        )
        self._record_published = True

    def transfer_to_guardian(self) -> None:
        if not self._record_published:
            raise RuntimeError(
                "guardian cancellation record must be published before transfer"
            )
        if self._transferred or self._retired:
            raise RuntimeError("guardian cancellation lease was transferred twice")
        self._lock_handle.close()
        self._transferred = True

    def retire(self) -> None:
        if self._retired:
            return
        if not self._transferred:
            self._lock_handle.close()
        self._cancellation.record_path.unlink(missing_ok=True)
        self._retired = True

    def _retire_stale_record(self) -> None:
        record_path = self._cancellation.record_path
        if not record_path.exists():
            return
        _read_record(record_path)
        record_path.unlink()


ExecutorGuardianCancellationLease = (
    NoExecutorGuardianCancellationLease | InteractiveExecutorGuardianCancellationLease
)


def prepare_executor_guardian_cancellation(
    cancellation: ExecutorCommandCancellation,
) -> ExecutorGuardianCancellationLease:
    """Acquire the exact pre-spawn cancellation owner for one command."""
    if type(cancellation) is ExecutorNoCommandCancellation:
        return NoExecutorGuardianCancellationLease()
    if type(cancellation) is ExecutorInteractiveSessionCancellation:
        return InteractiveExecutorGuardianCancellationLease(cancellation)
    raise ValueError("executor guardian cancellation requires a typed contract")


class ExecutorSessionGuardianCanceller:
    """Resolve and contain a guardian through its run-scoped ownership lock."""

    def __init__(self, forceful_shutdown_seconds: float) -> None:
        if (
            type(forceful_shutdown_seconds) is not float
            or not math.isfinite(forceful_shutdown_seconds)
            or forceful_shutdown_seconds <= 0
        ):
            raise ValueError(
                "ExecutorSessionGuardianCanceller.forceful_shutdown_seconds "
                "must be positive"
            )
        self._forceful_shutdown_seconds = forceful_shutdown_seconds

    def contain_if_active(
        self,
        cancellation: ExecutorInteractiveSessionCancellation,
    ) -> ExecutorGuardianCancellationOutcome:
        if type(cancellation) is not ExecutorInteractiveSessionCancellation:
            raise ValueError(
                "ExecutorSessionGuardianCanceller requires an "
                "ExecutorInteractiveSessionCancellation"
            )
        record_path = cancellation.record_path
        lock_path = _lock_path(record_path)
        try:
            lock_handle = lock_path.open("r+b")
        except FileNotFoundError:
            return self._retire_record_without_owner(record_path)
        deadline = time.monotonic() + self._forceful_shutdown_seconds
        contained_guardian = False
        try:
            while True:
                record = self._await_active_record_or_acquire(
                    lock_handle,
                    record_path,
                    deadline,
                )
                if type(record) is ExecutorGuardianCancellationOutcome:
                    if contained_guardian:
                        return ExecutorGuardianCancellationOutcome.CONTAINED
                    return record
                if type(record) is not _GuardianCancellationRecord:
                    raise AssertionError(
                        "guardian cancellation resolution is a closed union"
                    )
                logger.info(
                    "[executor-cancellation] containing guardian: pgid=%s record=%s",
                    record.process_group_id,
                    record_path,
                )
                self._kill_and_await_group(record.process_group_id, deadline)
                contained_guardian = True
                logger.info(
                    "[executor-cancellation] guardian contained: pgid=%s record=%s",
                    record.process_group_id,
                    record_path,
                )
        finally:
            lock_handle.close()

    def _await_active_record_or_acquire(
        self,
        lock_handle: BinaryIO,
        record_path: Path,
        deadline: float,
    ) -> _GuardianCancellationRecord | ExecutorGuardianCancellationOutcome:
        """Resolve the launch handshake without mistaking it for corruption."""
        while True:
            try:
                fcntl.flock(
                    lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                try:
                    return _read_record(record_path)
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        raise ExecutorGuardianCancellationError(
                            "active executor guardian did not publish its "
                            f"cancellation record: {record_path}"
                        ) from None
                    time.sleep(0.01)
                    continue
            return self._retire_record_without_owner(record_path)

    @staticmethod
    def _retire_record_without_owner(
        record_path: Path,
    ) -> ExecutorGuardianCancellationOutcome:
        if not record_path.exists():
            return ExecutorGuardianCancellationOutcome.ABSENT
        _read_record(record_path)
        record_path.unlink()
        logger.info(
            "[executor-cancellation] retired stale guardian record: record=%s",
            record_path,
        )
        return ExecutorGuardianCancellationOutcome.STALE_RETIRED

    def _kill_and_await_group(
        self,
        process_group_id: int,
        deadline: float,
    ) -> None:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ExecutorGuardianCancellationError(
                "permission denied while containing executor guardian group: "
                f"pgid={process_group_id}"
            ) from exc
        while time.monotonic() < deadline:
            if not _process_group_exists(process_group_id):
                return
            time.sleep(0.01)
        if _process_group_exists(process_group_id):
            raise ExecutorGuardianCancellationError(
                "executor guardian group remained executable after SIGKILL: "
                f"pgid={process_group_id}"
            )


def _lock_path(record_path: Path) -> Path:
    return record_path.with_suffix(f"{record_path.suffix}.lock")


def _read_record(record_path: Path) -> _GuardianCancellationRecord:
    try:
        return _GuardianCancellationRecord.model_validate_json(
            record_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        raise
    except (OSError, ValidationError) as exc:
        raise ExecutorGuardianCancellationError(
            f"invalid executor guardian cancellation record: {record_path}"
        ) from exc


def _atomic_write_record(path: Path, record: _GuardianCancellationRecord) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor_owned = False
            handle.write(record.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor_owned:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS reports EPERM for this caller-owned group when only an
        # unreaped zombie remains; no executable guardian work survives.
        return False
    return True
