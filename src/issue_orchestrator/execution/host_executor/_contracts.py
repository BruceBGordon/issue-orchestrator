# pyright: strict
"""Strict JSON contracts for private host executor state."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ...control.executor_admission import (
    ActiveExecutorLease,
    ExecutorAdmissionGrant,
    ExecutorGroupService,
    ExecutorLearnedDemand,
    ExecutorResourceObservation,
    QueuedExecutorWork,
)
from ...domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorPolicy,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from ...domain.executor_host import ExecutorHostCpuUtilization
from ...domain.executor_monitoring import ExecutorRequestId
from ._types import RecordedExecutorObservation
from ._host_observation import ExecutorHostLoadObservation
from ..strict_wire_record import StrictWireRecord


class ExecutorStrictRecord(StrictWireRecord):
    """Strict base for every persisted executor record."""


class CapacityRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    capacity_units: int = Field(ge=1)


class ConcurrencyRangeRecord(ExecutorStrictRecord):
    minimum_concurrency: int = Field(ge=1)
    maximum_concurrency: int = Field(ge=1)

    @classmethod
    def from_domain(
        cls,
        concurrency_range: ExecutorConcurrencyRange,
    ) -> ConcurrencyRangeRecord:
        return cls(
            minimum_concurrency=concurrency_range.minimum_concurrency,
            maximum_concurrency=concurrency_range.maximum_concurrency,
        )

    def to_domain(self) -> ExecutorConcurrencyRange:
        return ExecutorConcurrencyRange(
            self.minimum_concurrency,
            self.maximum_concurrency,
        )


class AdmissionGrantRecord(ExecutorStrictRecord):
    concurrency: int = Field(ge=1)
    capacity_units: int = Field(ge=1)

    @classmethod
    def from_domain(cls, grant: ExecutorAdmissionGrant) -> AdmissionGrantRecord:
        return cls(
            concurrency=grant.concurrency,
            capacity_units=grant.cpu_slots,
        )

    def to_domain(self) -> ExecutorAdmissionGrant:
        return ExecutorAdmissionGrant(self.concurrency, self.capacity_units)


class HostLoadRecord(ExecutorStrictRecord):
    one_minute: float = Field(ge=0)
    five_minutes: float = Field(ge=0)
    fifteen_minutes: float = Field(ge=0)

    @classmethod
    def from_domain(cls, load: ExecutorHostLoadObservation) -> HostLoadRecord:
        return cls(
            one_minute=load.one_minute,
            five_minutes=load.five_minutes,
            fifteen_minutes=load.fifteen_minutes,
        )


class HostCpuUtilizationRecord(ExecutorStrictRecord):
    busy_percent: float = Field(ge=0, le=100)
    observation_seconds: float = Field(gt=0)

    @classmethod
    def from_domain(
        cls,
        utilization: ExecutorHostCpuUtilization,
    ) -> HostCpuUtilizationRecord:
        return cls(
            busy_percent=utilization.busy_percent,
            observation_seconds=utilization.observation_seconds,
        )

    def to_domain(self) -> ExecutorHostCpuUtilization:
        return ExecutorHostCpuUtilization(
            busy_percent=self.busy_percent,
            observation_seconds=self.observation_seconds,
        )


class QueuedWorkRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    work_key: str = Field(min_length=1)
    fairness_group: str = Field(min_length=1)
    concurrency_range: ConcurrencyRangeRecord
    cores_per_concurrency: float = Field(gt=0)
    aggressiveness_percent: int = Field(ge=25, le=400)
    exclusive_resources: tuple[str, ...]

    @classmethod
    def from_domain(cls, work: QueuedExecutorWork) -> QueuedWorkRecord:
        return cls(
            request_id=work.request_id.value,
            sequence=work.sequence,
            work_key=work.work_key.value,
            fairness_group=work.fairness_group.value,
            concurrency_range=ConcurrencyRangeRecord.from_domain(
                work.concurrency_range
            ),
            cores_per_concurrency=work.learned_demand.cores_per_concurrency,
            aggressiveness_percent=work.aggressiveness.percent,
            exclusive_resources=tuple(
                resource.value for resource in work.exclusive_resources
            ),
        )

    def to_domain(self) -> QueuedExecutorWork:
        return QueuedExecutorWork(
            request_id=ExecutorRequestId(self.request_id),
            sequence=self.sequence,
            work_key=ExecutorWorkKey(self.work_key),
            fairness_group=ExecutorFairnessGroup(self.fairness_group),
            concurrency_range=self.concurrency_range.to_domain(),
            learned_demand=ExecutorLearnedDemand(self.cores_per_concurrency),
            aggressiveness=ExecutorAggressiveness(self.aggressiveness_percent),
            exclusive_resources=tuple(
                ExecutorExclusiveResource(resource)
                for resource in self.exclusive_resources
            ),
        )


class ActiveLeaseRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    fairness_group: str = Field(min_length=1)
    grant: AdmissionGrantRecord
    exclusive_resources: tuple[str, ...]

    @classmethod
    def from_domain(cls, lease: ActiveExecutorLease) -> ActiveLeaseRecord:
        return cls(
            fairness_group=lease.fairness_group.value,
            grant=AdmissionGrantRecord.from_domain(lease.grant),
            exclusive_resources=tuple(
                resource.value for resource in lease.exclusive_resources
            ),
        )

    def to_domain(self) -> ActiveExecutorLease:
        return ActiveExecutorLease(
            fairness_group=ExecutorFairnessGroup(self.fairness_group),
            grant=self.grant.to_domain(),
            exclusive_resources=tuple(
                ExecutorExclusiveResource(resource)
                for resource in self.exclusive_resources
            ),
        )


class GroupServiceEntryRecord(ExecutorStrictRecord):
    fairness_group: str = Field(min_length=1)
    capacity_units: int = Field(ge=0)

    @classmethod
    def from_domain(cls, service: ExecutorGroupService) -> GroupServiceEntryRecord:
        return cls(
            fairness_group=service.fairness_group.value,
            capacity_units=service.cpu_slots,
        )

    def to_domain(self) -> ExecutorGroupService:
        return ExecutorGroupService(
            ExecutorFairnessGroup(self.fairness_group),
            self.capacity_units,
        )


class GroupServiceRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    entries: tuple[GroupServiceEntryRecord, ...]


class ResourceObservationRecord(ExecutorStrictRecord):
    concurrency: int = Field(ge=1)
    wall_seconds: float = Field(gt=0)
    cpu_seconds: float = Field(ge=0)
    # Schema v1 compatibility: this wire key stores the isolated guardian's
    # exited-child lifetime RSS high-water mark, not an additive delta.
    max_rss_bytes: int = Field(ge=0)
    input_blocks: int = Field(ge=0)
    output_blocks: int = Field(ge=0)
    exit_code: int
    recorded_at_unix: float = Field(gt=0)

    @classmethod
    def from_domain(
        cls,
        observation: RecordedExecutorObservation,
    ) -> ResourceObservationRecord:
        resources = observation.resources
        return cls(
            concurrency=resources.concurrency,
            wall_seconds=resources.wall_seconds,
            cpu_seconds=resources.cpu_seconds,
            max_rss_bytes=(resources.guardian_process_lifetime_children_max_rss_bytes),
            input_blocks=resources.input_blocks,
            output_blocks=resources.output_blocks,
            exit_code=observation.exit_code,
            recorded_at_unix=observation.recorded_at_unix,
        )

    def to_domain(self) -> RecordedExecutorObservation:
        return RecordedExecutorObservation(
            resources=ExecutorResourceObservation(
                concurrency=self.concurrency,
                wall_seconds=self.wall_seconds,
                cpu_seconds=self.cpu_seconds,
                guardian_process_lifetime_children_max_rss_bytes=(self.max_rss_bytes),
                input_blocks=self.input_blocks,
                output_blocks=self.output_blocks,
            ),
            exit_code=self.exit_code,
            recorded_at_unix=self.recorded_at_unix,
        )


class ExecutedCommandResourceRecord(ExecutorStrictRecord):
    """Resource facts that do not depend on post-command wall time."""

    availability: Literal["available"] = "available"
    concurrency: int = Field(ge=1)
    wall_seconds: float = Field(gt=0)
    cpu_seconds: float = Field(ge=0)
    max_rss_bytes: int = Field(ge=0)
    input_blocks: int = Field(ge=0)
    output_blocks: int = Field(ge=0)

    @classmethod
    def from_domain(
        cls,
        resources: ExecutorResourceObservation,
    ) -> ExecutedCommandResourceRecord:
        return cls(
            concurrency=resources.concurrency,
            wall_seconds=resources.wall_seconds,
            cpu_seconds=resources.cpu_seconds,
            max_rss_bytes=(resources.guardian_process_lifetime_children_max_rss_bytes),
            input_blocks=resources.input_blocks,
            output_blocks=resources.output_blocks,
        )


class UnavailableCommandResourceRecord(ExecutorStrictRecord):
    """Wire discriminator for a failed post-command resource observation."""

    availability: Literal["unavailable"] = "unavailable"


CommandResourceRecord = Annotated[
    ExecutedCommandResourceRecord | UnavailableCommandResourceRecord,
    Field(discriminator="availability"),
]


class WorkHistoryRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    repository_key: str = Field(min_length=1)
    repository_label: str = Field(min_length=1)
    work_key: str = Field(min_length=1)
    observations: tuple[ResourceObservationRecord, ...]


class PersistedPolicyRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    aggressiveness_percent: int = Field(ge=25, le=400)

    def to_domain(self, source: ExecutorPolicySource) -> ExecutorPolicy:
        return ExecutorPolicy(
            ExecutorAggressiveness(self.aggressiveness_percent),
            source,
        )
