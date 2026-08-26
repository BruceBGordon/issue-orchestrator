# pyright: strict
"""Durable typed executor event store for post-hoc diagnosis."""

from __future__ import annotations

import fcntl
import logging
import os
import time
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
    ExecutorWaitReason,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
    ExecutorWorkEnqueued,
    ExecutorWorkWaiting,
)
from ._contracts import (
    AdmissionGrantRecord,
    ExecutedCommandResourceRecord,
    ExecutorStrictRecord,
    HostCpuUtilizationRecord,
    HostLoadRecord,
    QueuedWorkRecord,
    ResourceObservationRecord,
)
from ._types import (
    ExecutedExecutorCommand,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)
from ...domain.executor import ExecutorCommandFinalizationError
from ._host_observation import ExecutorHostLoadObservation


_MAX_LOG_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


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
    resources: ExecutedCommandResourceRecord
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
    | CompletedEventRecord
    | PolicyChangedEventRecord
)

_STORED_EVENT_ADAPTER: TypeAdapter[StoredExecutorEvent] = TypeAdapter(
    StoredExecutorEvent
)


class ExecutorEventStore:
    """Collect and query bounded, cross-process executor events."""

    def __init__(self, pool_dir: Path) -> None:
        self._pool_dir = pool_dir
        self.path = pool_dir / "executor-events-v4.jsonl"

    def append(self, event: StoredExecutorEvent) -> None:
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._pool_dir / "executor-events.lock"
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self._repair_torn_active_tail()
            self._rotate_if_needed()
            payload = (event.model_dump_json() + "\n").encode("utf-8")
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
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
            finally:
                os.close(descriptor)

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
        result: ExecutedExecutorCommand,
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
                resources=ExecutedCommandResourceRecord.from_domain(
                    result.resources
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
        lock_path = self._pool_dir / "executor-events.lock"
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
            records = self._read_records()
        return ExecutorEventTimeline(
            tuple(_to_domain_event(record) for record in records[-query.limit :])
        )

    def events_for_group(
        self,
        query: ExecutorFairnessGroupEventsQuery,
    ) -> ExecutorEventPage:
        """Read an exact fairness-group suffix without wall-clock slicing."""
        lock_path = self._pool_dir / "executor-events.lock"
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
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
        payload = self.path.read_bytes()
        if not payload or payload.endswith(b"\n"):
            return
        boundary = payload.rfind(b"\n") + 1
        tail = payload[boundary:]
        try:
            _STORED_EVENT_ADAPTER.validate_json(tail)
        except ValidationError:
            with self.path.open("r+b") as handle:
                handle.truncate(boundary)
                handle.flush()
                os.fsync(handle.fileno())
            logger.warning(
                "Truncated torn final executor event record before append: path=%s "
                "removed_bytes=%d",
                self.path,
                len(tail),
            )
            return
        with self.path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _rotate_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < _MAX_LOG_BYTES:
            return
        rotated = self.path.with_suffix(".jsonl.1")
        rotated.unlink(missing_ok=True)
        os.replace(self.path, rotated)


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
            resources=ExecutorResourceUsage(
                wall_seconds=record.resources.wall_seconds,
                cpu_seconds=record.resources.cpu_seconds,
                executor_process_lifetime_children_max_rss_bytes=(
                    record.resources.max_rss_bytes
                ),
                input_blocks=record.resources.input_blocks,
                output_blocks=record.resources.output_blocks,
            ),
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
                executor_process_lifetime_children_max_rss_bytes=(
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
