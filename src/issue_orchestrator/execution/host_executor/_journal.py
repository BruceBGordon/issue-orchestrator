# pyright: strict
"""Durable typed executor event store for post-hoc diagnosis."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from ...control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorAdmissionGranted,
    QueuedExecutorWork,
)
from ...domain.executor import (
    ExecutorAggressiveness,
    ExecutorBoundedDeadline,
    ExecutorDeadlineReason,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorPolicyChange,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from ...domain.executor_host import ExecutorHostCpuUtilization
from ...domain.executor_monitoring import (
    ExecutorAdmissionDeadlineExceeded,
    ExecutorCommandFinalizationFailed,
    ExecutorCommandDeadlineExceeded,
    ExecutorCommandInterrupted,
    ExecutorCommandLifecycleFailed,
    ExecutorCpuSlotState,
    ExecutorEvent,
    ExecutorEventMetadata,
    ExecutorEventTimeline,
    ExecutorEventPage,
    ExecutorFinalizationFailureDetail,
    ExecutorFairnessGroupEventsQuery,
    ExecutorHostLoad,
    ExecutorMonitoredWork,
    ExecutorPolicyChanged,
    ExecutorRecentEventsQuery,
    ExecutorRepositoryReference,
    ExecutorRequestId,
    ExecutorResourceUsage,
    ExecutorResourceUsageUnavailable,
    ExecutorWaitReason,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
    ExecutorWorkEnqueued,
    ExecutorWorkWaiting,
)
from ._contracts import (
    AdmissionGrantRecord,
    CommandResourceRecord,
    ExecutedCommandResourceRecord,
    ExecutorStrictRecord,
    HostCpuUtilizationRecord,
    HostLoadRecord,
    QueuedWorkRecord,
    ResourceObservationRecord,
    UnavailableCommandResourceRecord,
)
from ._types import (
    ExecutorCommandExecution,
    ExecutedExecutorCommand,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)
from ...domain.executor import ExecutorCommandFinalizationError
from ...domain.independent_cleanup import (
    CleanupAction,
    CleanupOutcome,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)
from ...infra.posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)
from ._host_observation import ExecutorHostLoadObservation


_MAX_LOG_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExecutorEventJournalFramed:
    """The active event journal ends at a complete record boundary."""


@dataclass(frozen=True, slots=True)
class _ExecutorEventJournalTail:
    """One unterminated active record and its absolute byte boundary."""

    boundary: int
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.boundary) is not int or self.boundary < 0:
            raise ValueError("_ExecutorEventJournalTail.boundary must be non-negative")
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("_ExecutorEventJournalTail.payload must not be empty")


_ExecutorEventJournalFraming = _ExecutorEventJournalFramed | _ExecutorEventJournalTail


class _EventRecord(ExecutorStrictRecord):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[4] = 4
    recorded_at_unix: float = Field(default_factory=time.time, gt=0)
    process_id: int = Field(default_factory=os.getpid, ge=1)


class EnqueuedEventRecord(_EventRecord):
    event: Literal["enqueued"] = "enqueued"
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work: QueuedWorkRecord
    successful_observation_count: int = Field(ge=0)
    queue_settle_seconds: float = Field(gt=0)
    policy_source: ExecutorPolicySource
    host_capacity_units: int = Field(ge=1)
    host_load: HostLoadRecord


class WaitingEventRecord(_EventRecord):
    event: Literal["waiting"] = "waiting"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    reason: ExecutorWaitReason
    leased_capacity_units: int = Field(ge=0)
    available_capacity_units: int = Field(ge=0)
    host_capacity_units: int = Field(ge=1)
    host_load: HostLoadRecord
    host_cpu_utilization: HostCpuUtilizationRecord


class AdmittedEventRecord(_EventRecord):
    event: Literal["admitted"] = "admitted"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    reserved_capacity_units_for_queued_peers: int = Field(ge=0)
    leased_capacity_units_before: int = Field(ge=0)
    available_capacity_units_before: int = Field(ge=0)
    host_capacity_units: int = Field(ge=1)
    wait_seconds: float = Field(ge=0)
    host_load: HostLoadRecord
    host_cpu_utilization: HostCpuUtilizationRecord


class CommandLifecycleFailedEventRecord(_EventRecord):
    event: Literal["command-lifecycle-failed"] = "command-lifecycle-failed"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    error_type: str = Field(min_length=1)
    error_message: str = Field(min_length=1)


class FinalizationFailureRecord(ExecutorStrictRecord):
    attempt_name: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    error_message: str = Field(min_length=1)


class CommandFinalizationFailedEventRecord(_EventRecord):
    event: Literal["command-finalization-failed"] = "command-finalization-failed"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    exit_code: int
    resources: CommandResourceRecord
    failures: tuple[FinalizationFailureRecord, ...] = Field(min_length=1)


class AdmissionDeadlineExceededEventRecord(_EventRecord):
    event: Literal["admission-deadline-exceeded"] = "admission-deadline-exceeded"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    reason: Literal[ExecutorDeadlineReason.ABSOLUTE] = ExecutorDeadlineReason.ABSOLUTE
    active_timeout_seconds: float = Field(gt=0)
    absolute_timeout_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)


class CommandDeadlineExceededEventRecord(_EventRecord):
    event: Literal["command-deadline-exceeded"] = "command-deadline-exceeded"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    reason: ExecutorDeadlineReason
    active_timeout_seconds: float = Field(gt=0)
    absolute_timeout_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(gt=0)


class CommandInterruptedEventRecord(_EventRecord):
    event: Literal["command-interrupted"] = "command-interrupted"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    signal_number: int = Field(gt=0)
    exit_code: int


class CompletedEventRecord(_EventRecord):
    event: Literal["completed"] = "completed"
    request_id: str = Field(min_length=1)
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    aggressiveness_percent: int = Field(ge=25, le=400)
    observation: ResourceObservationRecord
    previous_cores_per_concurrency: float = Field(gt=0)
    updated_cores_per_concurrency: float = Field(gt=0)
    successful_observation_count: int = Field(ge=0)
    host_load: HostLoadRecord


class PolicyChangedEventRecord(_EventRecord):
    event: Literal["policy-changed"] = "policy-changed"
    saved_aggressiveness_percent: int = Field(ge=25, le=400)
    effective_aggressiveness_percent: int = Field(ge=25, le=400)
    effective_source: ExecutorPolicySource


StoredExecutorEvent = (
    EnqueuedEventRecord
    | WaitingEventRecord
    | AdmittedEventRecord
    | CommandLifecycleFailedEventRecord
    | CommandFinalizationFailedEventRecord
    | AdmissionDeadlineExceededEventRecord
    | CommandDeadlineExceededEventRecord
    | CommandInterruptedEventRecord
    | CompletedEventRecord
    | PolicyChangedEventRecord
)

_STORED_EVENT_ADAPTER: TypeAdapter[StoredExecutorEvent] = TypeAdapter(
    StoredExecutorEvent
)


class ExecutorEventStore:
    """Collect and query bounded, cross-process executor events."""

    def __init__(self, pool_dir: Path) -> None:
        if not pool_dir.is_absolute():
            raise ValueError("ExecutorEventStore.pool_dir must be absolute")
        self._pool_dir = pool_dir
        self.path = pool_dir / "executor-events-v4.jsonl"
        self._file_locks = PosixFileLockOwner()
        lock_path = (pool_dir / "executor-events.lock").resolve()
        self._exclusive_lock = PosixFileLockSpecification(
            lock_path,
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )
        self._shared_lock = PosixFileLockSpecification(
            lock_path,
            PosixFileLockMode.SHARED,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    def append(self, event: StoredExecutorEvent) -> None:
        self._ensure_pool_directory()
        with self._file_locks.hold(self._exclusive_lock):
            self._repair_torn_active_tail()
            self._rotate_if_needed()
            payload = (event.model_dump_json() + "\n").encode("utf-8")
            active_existed = self.path.exists()
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
                0o600,
            )
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written < 1:
                        raise OSError("executor event append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except BaseException as append_error:
                raise_primary_with_cleanup(
                    "executor event append and finalization failures",
                    append_error,
                    self._finalize_append_descriptor(
                        descriptor,
                        sync_directory=not active_existed,
                    ),
                )
            raise_cleanup_failures(
                "executor event append finalization failures",
                self._finalize_append_descriptor(
                    descriptor,
                    sync_directory=not active_existed,
                ),
            )

    def enqueued(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        successful_observation_count: int,
        queue_settle_seconds: float,
        policy_source: ExecutorPolicySource,
        host_capacity_units: int,
        host_load: ExecutorHostLoadObservation,
    ) -> None:
        self.append(
            EnqueuedEventRecord(
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work=QueuedWorkRecord.from_domain(work),
                successful_observation_count=successful_observation_count,
                queue_settle_seconds=queue_settle_seconds,
                policy_source=policy_source,
                host_capacity_units=host_capacity_units,
                host_load=HostLoadRecord.from_domain(host_load),
            )
        )

    def waiting(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        reason: ExecutorWaitReason,
        leased_capacity_units: int,
        available_capacity_units: int,
        host_capacity_units: int,
        host_load: ExecutorHostLoadObservation,
        host_cpu_utilization: ExecutorHostCpuUtilization,
    ) -> None:
        self.append(
            WaitingEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                reason=reason,
                leased_capacity_units=leased_capacity_units,
                available_capacity_units=available_capacity_units,
                host_capacity_units=host_capacity_units,
                host_load=HostLoadRecord.from_domain(host_load),
                host_cpu_utilization=HostCpuUtilizationRecord.from_domain(
                    host_cpu_utilization
                ),
            )
        )

    def admitted(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        decision: ExecutorAdmissionGranted,
        leased_capacity_units_before: int,
        available_capacity_units_before: int,
        host_capacity_units: int,
        wait_seconds: float,
        host_load: ExecutorHostLoadObservation,
        host_cpu_utilization: ExecutorHostCpuUtilization,
    ) -> None:
        self.append(
            AdmittedEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(grant),
                reserved_capacity_units_for_queued_peers=(
                    decision.reserved_cpu_slots_for_queued_peers
                ),
                leased_capacity_units_before=leased_capacity_units_before,
                available_capacity_units_before=available_capacity_units_before,
                host_capacity_units=host_capacity_units,
                wait_seconds=wait_seconds,
                host_load=HostLoadRecord.from_domain(host_load),
                host_cpu_utilization=HostCpuUtilizationRecord.from_domain(
                    host_cpu_utilization
                ),
            )
        )

    def command_lifecycle_failed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        error: BaseException,
    ) -> None:
        self.append(
            CommandLifecycleFailedEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(grant),
                error_type=type(error).__name__,
                error_message=_exception_message(error),
            )
        )

    def command_finalization_failed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        result: ExecutorCommandExecution,
        error: ExecutorCommandFinalizationError,
    ) -> None:
        self.append(
            CommandFinalizationFailedEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(result.admission_grant),
                exit_code=result.exit_code,
                resources=(
                    ExecutedCommandResourceRecord.from_domain(result.resources)
                    if type(result) is ExecutedExecutorCommand
                    else UnavailableCommandResourceRecord()
                ),
                failures=tuple(
                    FinalizationFailureRecord(
                        attempt_name=failure.attempt_name,
                        error_type=type(failure.error).__name__,
                        error_message=_exception_message(failure.error),
                    )
                    for failure in error.failures
                ),
            )
        )

    def policy_changed(self, change: ExecutorPolicyChange) -> None:
        self.append(
            PolicyChangedEventRecord(
                saved_aggressiveness_percent=change.saved.aggressiveness.percent,
                effective_aggressiveness_percent=(
                    change.effective.aggressiveness.percent
                ),
                effective_source=change.effective.source,
            )
        )

    def admission_deadline_exceeded(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        deadline: ExecutorBoundedDeadline,
        elapsed_seconds: float,
    ) -> None:
        self.append(
            AdmissionDeadlineExceededEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                active_timeout_seconds=deadline.active_timeout_seconds,
                absolute_timeout_seconds=deadline.absolute_timeout_seconds,
                elapsed_seconds=elapsed_seconds,
            )
        )

    def command_deadline_exceeded(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        deadline: ExecutorBoundedDeadline,
        reason: ExecutorDeadlineReason,
        elapsed_seconds: float,
    ) -> None:
        self.append(
            CommandDeadlineExceededEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(grant),
                reason=reason,
                active_timeout_seconds=deadline.active_timeout_seconds,
                absolute_timeout_seconds=deadline.absolute_timeout_seconds,
                elapsed_seconds=elapsed_seconds,
            )
        )

    def command_interrupted(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        signal_number: int,
    ) -> None:
        self.append(
            CommandInterruptedEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(grant),
                signal_number=signal_number,
                exit_code=-signal_number,
            )
        )

    def completed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        aggressiveness_percent: int,
        observation: RecordedExecutorObservation,
        previous_cores_per_concurrency: float,
        updated_cores_per_concurrency: float,
        successful_observation_count: int,
        host_load: ExecutorHostLoadObservation,
    ) -> None:
        self.append(
            CompletedEventRecord(
                request_id=work.request_id.value,
                repository_key=identity.repository.key,
                repository_label=identity.repository.label,
                work_key=identity.work_key.value,
                fairness_group=work.fairness_group.value,
                grant=AdmissionGrantRecord.from_domain(grant),
                aggressiveness_percent=aggressiveness_percent,
                observation=ResourceObservationRecord.from_domain(observation),
                previous_cores_per_concurrency=previous_cores_per_concurrency,
                updated_cores_per_concurrency=updated_cores_per_concurrency,
                successful_observation_count=successful_observation_count,
                host_load=HostLoadRecord.from_domain(host_load),
            )
        )

    def recent_events(
        self,
        query: ExecutorRecentEventsQuery,
    ) -> ExecutorEventTimeline:
        """Read and validate the newest collected events."""
        self._ensure_pool_directory()
        with self._file_locks.hold(self._shared_lock):
            records = self._read_records()
        return ExecutorEventTimeline(
            tuple(_to_domain_event(record) for record in records[-query.limit :])
        )

    def events_for_group(
        self,
        query: ExecutorFairnessGroupEventsQuery,
    ) -> ExecutorEventPage:
        """Read an exact fairness-group suffix without wall-clock slicing."""
        self._ensure_pool_directory()
        with self._file_locks.hold(self._shared_lock):
            records = self._read_records()
        matching = tuple(
            event
            for event in (_to_domain_event(record) for record in records)
            if not isinstance(event, ExecutorPolicyChanged)
            and event.work.fairness_group == query.fairness_group
        )
        return ExecutorEventPage(
            total_matching_event_count=len(matching),
            events=matching[-query.limit :],
        )

    def _read_records(self) -> tuple[StoredExecutorEvent, ...]:
        rotated = self.path.with_suffix(".jsonl.1")
        records: list[StoredExecutorEvent] = []
        for path in (rotated, self.path):
            if not path.exists():
                continue
            try:
                lines = path.read_bytes().splitlines(keepends=True)
            except OSError as exc:
                raise RuntimeError(f"cannot read executor event store: {path}") from exc
            for line_number, line in enumerate(lines, start=1):
                try:
                    records.append(_STORED_EVENT_ADAPTER.validate_json(line))
                except ValidationError as exc:
                    is_torn_final_line = (
                        path == self.path
                        and line_number == len(lines)
                        and not line.endswith((b"\n", b"\r"))
                    )
                    if is_torn_final_line:
                        logger.warning(
                            "Ignoring torn final executor event record: path=%s line=%d",
                            path,
                            line_number,
                        )
                        continue
                    raise RuntimeError(
                        f"invalid executor event at {path}:{line_number}"
                    ) from exc
        return tuple(records)

    def _repair_torn_active_tail(self) -> None:
        """Truncate only an invalid unterminated tail before the next append."""
        if not self.path.exists():
            return
        framing = self._inspect_active_tail()
        if type(framing) is _ExecutorEventJournalFramed:
            return
        if type(framing) is not _ExecutorEventJournalTail:
            raise AssertionError("executor event journal framing is a closed union")
        try:
            _STORED_EVENT_ADAPTER.validate_json(framing.payload)
        except ValidationError:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CLOEXEC,
            )
            try:
                os.ftruncate(descriptor, framing.boundary)
                os.fsync(descriptor)
            except BaseException as repair_error:
                raise_primary_with_cleanup(
                    "executor event tail repair and finalization failures",
                    repair_error,
                    self._finalize_append_descriptor(
                        descriptor,
                        sync_directory=True,
                    ),
                )
            raise_cleanup_failures(
                "executor event tail repair finalization failures",
                self._finalize_append_descriptor(
                    descriptor,
                    sync_directory=True,
                ),
            )
            logger.warning(
                "Truncated torn final executor event record before append: path=%s "
                "removed_bytes=%d",
                self.path,
                len(framing.payload),
            )
            return
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC,
        )
        try:
            written = os.write(descriptor, b"\n")
            if written != 1:
                raise OSError("executor event tail repair made no progress")
            os.fsync(descriptor)
        except BaseException as repair_error:
            raise_primary_with_cleanup(
                "executor event tail completion and finalization failures",
                repair_error,
                self._finalize_append_descriptor(
                    descriptor,
                    sync_directory=True,
                ),
            )
        raise_cleanup_failures(
            "executor event tail completion finalization failures",
            self._finalize_append_descriptor(
                descriptor,
                sync_directory=True,
            ),
        )

    def _inspect_active_tail(self) -> _ExecutorEventJournalFraming:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            size = os.fstat(descriptor).st_size
            if size == 0 or self._pread_exact(descriptor, 1, size - 1) == b"\n":
                result: _ExecutorEventJournalFraming = _ExecutorEventJournalFramed()
            else:
                chunks: list[bytes] = []
                cursor = size
                boundary = 0
                while cursor > 0:
                    read_size = min(64 * 1024, cursor)
                    cursor -= read_size
                    chunk = self._pread_exact(descriptor, read_size, cursor)
                    separator = chunk.rfind(b"\n")
                    if separator >= 0:
                        boundary = cursor + separator + 1
                        chunks.append(chunk[separator + 1 :])
                        break
                    chunks.append(chunk)
                result = _ExecutorEventJournalTail(
                    boundary,
                    b"".join(reversed(chunks)),
                )
        except BaseException as inspection_error:
            raise_primary_with_cleanup(
                "executor event tail inspection and cleanup failures",
                inspection_error,
                self._close_descriptor(
                    descriptor,
                    "close executor event inspection descriptor",
                ),
            )
        raise_cleanup_failures(
            "executor event inspection descriptor cleanup failures",
            self._close_descriptor(
                descriptor,
                "close executor event inspection descriptor",
            ),
        )
        return result

    @staticmethod
    def _pread_exact(descriptor: int, size: int, offset: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        current_offset = offset
        while remaining:
            chunk = os.pread(descriptor, remaining, current_offset)
            if not chunk:
                raise OSError("executor event tail read made no progress")
            chunks.append(chunk)
            remaining -= len(chunk)
            current_offset += len(chunk)
        return b"".join(chunks)

    def _rotate_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < _MAX_LOG_BYTES:
            return
        rotated = self.path.with_suffix(".jsonl.1")
        try:
            os.replace(self.path, rotated)
        except BaseException as rotation_error:
            raise_primary_with_cleanup(
                "executor event rotation and directory sync failures",
                rotation_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "sync executor event directory after partial rotation",
                            self._sync_pool_directory,
                        ),
                    )
                ).run(),
            )
        self._sync_pool_directory()

    def _ensure_pool_directory(self) -> None:
        missing_directories: list[Path] = []
        candidate = self._pool_dir
        while not candidate.exists():
            missing_directories.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(
                    f"cannot find existing ancestor for executor pool: {self._pool_dir}"
                )
            candidate = parent
        if not candidate.is_dir():
            raise NotADirectoryError(candidate)
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        for created_directory in reversed(missing_directories):
            self._sync_directory(created_directory.parent)

    def _finalize_append_descriptor(
        self,
        descriptor: int,
        *,
        sync_directory: bool,
    ) -> CleanupOutcome:
        actions = [
            CleanupAction(
                "close executor event descriptor",
                lambda: os.close(descriptor),
            )
        ]
        if sync_directory:
            actions.append(
                CleanupAction(
                    "sync executor event directory after durable mutation",
                    self._sync_pool_directory,
                )
            )
        return IndependentCleanupPlan(tuple(actions)).run()

    def _sync_pool_directory(self) -> None:
        self._sync_directory(self._pool_dir)

    @staticmethod
    def _close_descriptor(descriptor: int, action_name: str) -> CleanupOutcome:
        return IndependentCleanupPlan(
            (CleanupAction(action_name, lambda: os.close(descriptor)),)
        ).run()

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        except BaseException as sync_error:
            raise_primary_with_cleanup(
                "executor event directory sync and cleanup failures",
                sync_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "close executor event directory descriptor",
                            lambda: os.close(descriptor),
                        ),
                    )
                ).run(),
            )
        raise_cleanup_failures(
            "executor event directory descriptor cleanup failures",
            IndependentCleanupPlan(
                (
                    CleanupAction(
                        "close executor event directory descriptor",
                        lambda: os.close(descriptor),
                    ),
                )
            ).run(),
        )


def _event_metadata(record: _EventRecord) -> ExecutorEventMetadata:
    return ExecutorEventMetadata(record.recorded_at_unix, record.process_id)


def _exception_message(error: BaseException) -> str:
    message = str(error)
    return message if message else repr(error)


def _host_load(record: HostLoadRecord) -> ExecutorHostLoad:
    return ExecutorHostLoad(
        one_minute=record.one_minute,
        five_minutes=record.five_minutes,
        fifteen_minutes=record.fifteen_minutes,
    )


def _monitored_work(
    *,
    request_id: str,
    repository_key: str,
    repository_label: str,
    work_key: str,
    fairness_group: str,
) -> ExecutorMonitoredWork:
    return ExecutorMonitoredWork(
        request_id=ExecutorRequestId(request_id),
        repository=ExecutorRepositoryReference(repository_key, repository_label),
        work_key=ExecutorWorkKey(work_key),
        fairness_group=ExecutorFairnessGroup(fairness_group),
    )


def _queued_monitored_work(record: EnqueuedEventRecord) -> ExecutorMonitoredWork:
    return _monitored_work(
        request_id=record.work.request_id,
        repository_key=record.repository_key,
        repository_label=record.repository_label,
        work_key=record.work.work_key,
        fairness_group=record.work.fairness_group,
    )


def _to_domain_event(record: StoredExecutorEvent) -> ExecutorEvent:
    if isinstance(record, EnqueuedEventRecord):
        return ExecutorWorkEnqueued(
            metadata=_event_metadata(record),
            work=_queued_monitored_work(record),
            concurrency_range=record.work.concurrency_range.to_domain(),
            learned_cores_per_concurrency=record.work.cores_per_concurrency,
            successful_observation_count=record.successful_observation_count,
            queue_settle_seconds=record.queue_settle_seconds,
            aggressiveness=ExecutorAggressiveness(record.work.aggressiveness_percent),
            policy_source=record.policy_source,
            exclusive_resources=tuple(
                ExecutorExclusiveResource(resource)
                for resource in record.work.exclusive_resources
            ),
            host_cpu_slots=record.host_capacity_units,
            host_load=_host_load(record.host_load),
        )
    if isinstance(record, WaitingEventRecord):
        return ExecutorWorkWaiting(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            reason=record.reason,
            cpu_slots=ExecutorCpuSlotState(
                leased=record.leased_capacity_units,
                available=record.available_capacity_units,
                total=record.host_capacity_units,
            ),
            host_load=_host_load(record.host_load),
            host_cpu_utilization=record.host_cpu_utilization.to_domain(),
        )
    if isinstance(record, AdmittedEventRecord):
        return ExecutorWorkAdmitted(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            charged_cpu_slots=record.grant.capacity_units,
            reserved_cpu_slots_for_queued_peers=(
                record.reserved_capacity_units_for_queued_peers
            ),
            cpu_slots_before=ExecutorCpuSlotState(
                leased=record.leased_capacity_units_before,
                available=record.available_capacity_units_before,
                total=record.host_capacity_units,
            ),
            wait_seconds=record.wait_seconds,
            host_load=_host_load(record.host_load),
            host_cpu_utilization=record.host_cpu_utilization.to_domain(),
        )
    if isinstance(record, CommandLifecycleFailedEventRecord):
        return ExecutorCommandLifecycleFailed(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            error_type=record.error_type,
            error_message=record.error_message,
        )
    if isinstance(record, CommandFinalizationFailedEventRecord):
        resources = (
            ExecutorResourceUsage(
                wall_seconds=record.resources.wall_seconds,
                cpu_seconds=record.resources.cpu_seconds,
                guardian_process_lifetime_children_max_rss_bytes=(
                    record.resources.max_rss_bytes
                ),
                input_blocks=record.resources.input_blocks,
                output_blocks=record.resources.output_blocks,
            )
            if type(record.resources) is ExecutedCommandResourceRecord
            else ExecutorResourceUsageUnavailable()
        )
        return ExecutorCommandFinalizationFailed(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            charged_cpu_slots=record.grant.capacity_units,
            exit_code=record.exit_code,
            resources=resources,
            failures=tuple(
                ExecutorFinalizationFailureDetail(
                    attempt_name=failure.attempt_name,
                    error_type=failure.error_type,
                    error_message=failure.error_message,
                )
                for failure in record.failures
            ),
        )
    if isinstance(record, AdmissionDeadlineExceededEventRecord):
        return ExecutorAdmissionDeadlineExceeded(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            reason=record.reason,
            active_timeout_seconds=record.active_timeout_seconds,
            absolute_timeout_seconds=record.absolute_timeout_seconds,
            elapsed_seconds=record.elapsed_seconds,
        )
    if isinstance(record, CommandDeadlineExceededEventRecord):
        return ExecutorCommandDeadlineExceeded(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            reason=record.reason,
            active_timeout_seconds=record.active_timeout_seconds,
            absolute_timeout_seconds=record.absolute_timeout_seconds,
            elapsed_seconds=record.elapsed_seconds,
        )
    if isinstance(record, CommandInterruptedEventRecord):
        return ExecutorCommandInterrupted(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            charged_cpu_slots=record.grant.capacity_units,
            signal_number=record.signal_number,
            exit_code=record.exit_code,
        )
    if isinstance(record, CompletedEventRecord):
        return ExecutorWorkCompleted(
            metadata=_event_metadata(record),
            work=_monitored_work(
                request_id=record.request_id,
                repository_key=record.repository_key,
                repository_label=record.repository_label,
                work_key=record.work_key,
                fairness_group=record.fairness_group,
            ),
            concurrency=record.grant.concurrency,
            charged_cpu_slots=record.grant.capacity_units,
            aggressiveness=ExecutorAggressiveness(record.aggressiveness_percent),
            exit_code=record.observation.exit_code,
            resources=ExecutorResourceUsage(
                wall_seconds=record.observation.wall_seconds,
                cpu_seconds=record.observation.cpu_seconds,
                guardian_process_lifetime_children_max_rss_bytes=(
                    record.observation.max_rss_bytes
                ),
                input_blocks=record.observation.input_blocks,
                output_blocks=record.observation.output_blocks,
            ),
            previous_cores_per_concurrency=(record.previous_cores_per_concurrency),
            updated_cores_per_concurrency=record.updated_cores_per_concurrency,
            successful_observation_count=record.successful_observation_count,
            host_load=_host_load(record.host_load),
        )
    return ExecutorPolicyChanged(
        metadata=_event_metadata(record),
        saved=ExecutorAggressiveness(record.saved_aggressiveness_percent),
        effective=ExecutorAggressiveness(record.effective_aggressiveness_percent),
        effective_source=record.effective_source,
    )
