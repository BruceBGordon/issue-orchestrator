"""Typed read model for machine-wide executor activity."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from .executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorDeadlineReason,
    ExecutorExclusiveResource,
    ExecutorFairnessGroup,
    ExecutorPolicy,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from .executor_host import ExecutorHostCpuUtilization


def _require_positive_integer(owner: str, field: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{owner}.{field} must be a positive integer")


def _require_non_negative_integer(owner: str, field: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{owner}.{field} must be a non-negative integer")


def _require_finite_positive(owner: str, field: str, value: float) -> None:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{owner}.{field} must be finite and positive")


def _require_exact_type(owner: str, field: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must be an {expected.__name__}")


@dataclass(frozen=True, slots=True)
class ExecutorRequestId:
    """Opaque unique identity for one executor invocation."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value:
            raise ValueError("ExecutorRequestId.value must not be empty")


@dataclass(frozen=True, slots=True)
class ExecutorRepositoryReference:
    """Stable repository identity and its human-readable label."""

    key: str
    label: str

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key:
            raise ValueError("ExecutorRepositoryReference.key must not be empty")
        if type(self.label) is not str or not self.label:
            raise ValueError("ExecutorRepositoryReference.label must not be empty")


@dataclass(frozen=True, slots=True)
class ExecutorMonitoredWork:
    """Human-traceable identity shared by every event for one invocation."""

    request_id: ExecutorRequestId
    repository: ExecutorRepositoryReference
    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup

    def __post_init__(self) -> None:
        expected = (
            ("request_id", self.request_id, ExecutorRequestId),
            ("repository", self.repository, ExecutorRepositoryReference),
            ("work_key", self.work_key, ExecutorWorkKey),
            ("fairness_group", self.fairness_group, ExecutorFairnessGroup),
        )
        for field_name, value, expected_type in expected:
            if type(value) is not expected_type:
                raise ValueError(
                    f"ExecutorMonitoredWork.{field_name} must be an "
                    f"{expected_type.__name__}"
                )


@dataclass(frozen=True, slots=True)
class ExecutorEventMetadata:
    """Origin and occurrence time shared by every executor event."""

    recorded_at_unix: float
    process_id: int

    def __post_init__(self) -> None:
        if (
            type(self.recorded_at_unix) is not float
            or not math.isfinite(self.recorded_at_unix)
            or self.recorded_at_unix <= 0
        ):
            raise ValueError(
                "ExecutorEventMetadata.recorded_at_unix must be finite and positive"
            )
        if type(self.process_id) is not int or self.process_id < 1:
            raise ValueError("ExecutorEventMetadata.process_id must be positive")


@dataclass(frozen=True, slots=True)
class ExecutorHostLoad:
    """Diagnostic host load averages observed at an executor decision."""

    one_minute: float
    five_minutes: float
    fifteen_minutes: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("one_minute", self.one_minute),
            ("five_minutes", self.five_minutes),
            ("fifteen_minutes", self.fifteen_minutes),
        ):
            if type(value) is not float or not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"ExecutorHostLoad.{field_name} must be finite and non-negative"
                )


@dataclass(frozen=True, slots=True)
class ExecutorCpuSlotState:
    """Internal admission arithmetic exposed only as diagnostic evidence."""

    leased: int
    available: int
    total: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("leased", self.leased),
            ("available", self.available),
            ("total", self.total),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ExecutorCpuSlotState.{field_name} must be non-negative"
                )
        if self.total < 1:
            raise ValueError("ExecutorCpuSlotState.total must be positive")
        if self.leased + self.available != self.total:
            raise ValueError(
                "ExecutorCpuSlotState leased plus available must equal total"
            )


@dataclass(frozen=True, slots=True)
class ExecutorResourceUsage:
    """Child-process resource evidence retained for diagnosis and learning."""

    wall_seconds: float
    cpu_seconds: float
    executor_process_lifetime_children_max_rss_bytes: int
    input_blocks: int
    output_blocks: int

    def __post_init__(self) -> None:
        if (
            type(self.wall_seconds) is not float
            or not math.isfinite(self.wall_seconds)
            or self.wall_seconds <= 0
        ):
            raise ValueError(
                "ExecutorResourceUsage.wall_seconds must be finite and positive"
            )
        if (
            type(self.cpu_seconds) is not float
            or not math.isfinite(self.cpu_seconds)
            or self.cpu_seconds < 0
        ):
            raise ValueError(
                "ExecutorResourceUsage.cpu_seconds must be finite and non-negative"
            )
        for field_name, value in (
            (
                "executor_process_lifetime_children_max_rss_bytes",
                self.executor_process_lifetime_children_max_rss_bytes,
            ),
            ("input_blocks", self.input_blocks),
            ("output_blocks", self.output_blocks),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ExecutorResourceUsage.{field_name} must be non-negative"
                )


class ExecutorWaitReason(StrEnum):
    """Stable human-facing reasons that queued work remains deferred."""

    EXCLUSIVE_RESOURCE = "exclusive-resource"
    FAIRNESS = "fairness"
    CAPACITY = "capacity"
    LEASE_RACE = "lease-race"
    HOST_PRESSURE = "host-pressure"


@dataclass(frozen=True, slots=True)
class ExecutorWorkEnqueued:
    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency_range: ExecutorConcurrencyRange
    learned_cores_per_concurrency: float
    successful_observation_count: int
    queue_settle_seconds: float
    aggressiveness: ExecutorAggressiveness
    policy_source: ExecutorPolicySource
    exclusive_resources: tuple[ExecutorExclusiveResource, ...]
    host_cpu_slots: int
    host_load: ExecutorHostLoad

    def __post_init__(self) -> None:
        for field_name, value, expected_type in (
            ("metadata", self.metadata, ExecutorEventMetadata),
            ("work", self.work, ExecutorMonitoredWork),
            ("concurrency_range", self.concurrency_range, ExecutorConcurrencyRange),
            ("aggressiveness", self.aggressiveness, ExecutorAggressiveness),
            ("policy_source", self.policy_source, ExecutorPolicySource),
            ("host_load", self.host_load, ExecutorHostLoad),
        ):
            _require_exact_type(type(self).__name__, field_name, value, expected_type)
        _require_finite_positive(
            type(self).__name__,
            "learned_cores_per_concurrency",
            self.learned_cores_per_concurrency,
        )
        _require_non_negative_integer(
            type(self).__name__,
            "successful_observation_count",
            self.successful_observation_count,
        )
        _require_finite_positive(
            type(self).__name__,
            "queue_settle_seconds",
            self.queue_settle_seconds,
        )
        _require_positive_integer(
            type(self).__name__, "host_cpu_slots", self.host_cpu_slots
        )
        _require_exact_type(
            type(self).__name__,
            "exclusive_resources",
            self.exclusive_resources,
            tuple,
        )
        if any(
            type(resource) is not ExecutorExclusiveResource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "ExecutorWorkEnqueued.exclusive_resources must contain only "
                "ExecutorExclusiveResource values"
            )
        resources = tuple(resource.value for resource in self.exclusive_resources)
        if len(resources) != len(set(resources)):
            raise ValueError(
                "ExecutorWorkEnqueued.exclusive_resources must not contain duplicates"
            )


@dataclass(frozen=True, slots=True)
class ExecutorWorkWaiting:
    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    reason: ExecutorWaitReason
    cpu_slots: ExecutorCpuSlotState
    host_load: ExecutorHostLoad
    host_cpu_utilization: ExecutorHostCpuUtilization

    def __post_init__(self) -> None:
        for field_name, value, expected_type in (
            ("metadata", self.metadata, ExecutorEventMetadata),
            ("work", self.work, ExecutorMonitoredWork),
            ("cpu_slots", self.cpu_slots, ExecutorCpuSlotState),
            ("host_load", self.host_load, ExecutorHostLoad),
            (
                "host_cpu_utilization",
                self.host_cpu_utilization,
                ExecutorHostCpuUtilization,
            ),
        ):
            _require_exact_type(type(self).__name__, field_name, value, expected_type)
        if type(self.reason) is not ExecutorWaitReason:
            raise ValueError("ExecutorWorkWaiting.reason must be an ExecutorWaitReason")


@dataclass(frozen=True, slots=True)
class ExecutorWorkAdmitted:
    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency: int
    charged_cpu_slots: int
    reserved_cpu_slots_for_queued_peers: int
    cpu_slots_before: ExecutorCpuSlotState
    wait_seconds: float
    host_load: ExecutorHostLoad
    host_cpu_utilization: ExecutorHostCpuUtilization

    def __post_init__(self) -> None:
        for field_name, value, expected_type in (
            ("metadata", self.metadata, ExecutorEventMetadata),
            ("work", self.work, ExecutorMonitoredWork),
            ("cpu_slots_before", self.cpu_slots_before, ExecutorCpuSlotState),
            ("host_load", self.host_load, ExecutorHostLoad),
            (
                "host_cpu_utilization",
                self.host_cpu_utilization,
                ExecutorHostCpuUtilization,
            ),
        ):
            _require_exact_type(type(self).__name__, field_name, value, expected_type)
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)
        _require_positive_integer(
            type(self).__name__, "charged_cpu_slots", self.charged_cpu_slots
        )
        _require_non_negative_integer(
            type(self).__name__,
            "reserved_cpu_slots_for_queued_peers",
            self.reserved_cpu_slots_for_queued_peers,
        )
        if (
            self.charged_cpu_slots + self.reserved_cpu_slots_for_queued_peers
            > self.cpu_slots_before.available
        ):
            raise ValueError(
                "ExecutorWorkAdmitted charged plus reserved CPU slots must not "
                "exceed the available slots before admission"
            )
        if (
            type(self.wait_seconds) is not float
            or not math.isfinite(self.wait_seconds)
            or self.wait_seconds < 0
        ):
            raise ValueError(
                "ExecutorWorkAdmitted.wait_seconds must be finite and non-negative"
            )


@dataclass(frozen=True, slots=True)
class ExecutorCommandLifecycleFailed:
    """An admitted command could not reach a trustworthy terminal outcome."""

    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency: int
    error_type: str
    error_message: str

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__, "metadata", self.metadata, ExecutorEventMetadata
        )
        _require_exact_type(
            type(self).__name__, "work", self.work, ExecutorMonitoredWork
        )
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError(
                "ExecutorCommandLifecycleFailed.error_type must not be empty"
            )
        if type(self.error_message) is not str or not self.error_message:
            raise ValueError(
                "ExecutorCommandLifecycleFailed.error_message must not be empty"
            )


@dataclass(frozen=True, slots=True)
class ExecutorFinalizationFailureDetail:
    """Serializable identity of one failed finalization attempt."""

    attempt_name: str
    error_type: str
    error_message: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("attempt_name", self.attempt_name),
            ("error_type", self.error_type),
            ("error_message", self.error_message),
        ):
            if type(value) is not str or not value:
                raise ValueError(
                    f"ExecutorFinalizationFailureDetail.{field_name} must not "
                    "be empty"
                )


@dataclass(frozen=True, slots=True)
class ExecutorCommandFinalizationFailed:
    """The command terminated exactly, but post-containment evidence failed."""

    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency: int
    charged_cpu_slots: int
    exit_code: int
    resources: ExecutorResourceUsage
    failures: tuple[ExecutorFinalizationFailureDetail, ...]

    def __post_init__(self) -> None:
        for field_name, value, expected_type in (
            ("metadata", self.metadata, ExecutorEventMetadata),
            ("work", self.work, ExecutorMonitoredWork),
            ("resources", self.resources, ExecutorResourceUsage),
        ):
            _require_exact_type(type(self).__name__, field_name, value, expected_type)
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)
        _require_positive_integer(
            type(self).__name__, "charged_cpu_slots", self.charged_cpu_slots
        )
        if type(self.exit_code) is not int:
            raise ValueError(
                "ExecutorCommandFinalizationFailed.exit_code must be an integer"
            )
        if not self.failures or any(
            type(failure) is not ExecutorFinalizationFailureDetail
            for failure in self.failures
        ):
            raise ValueError(
                "ExecutorCommandFinalizationFailed.failures must contain "
                "ExecutorFinalizationFailureDetail values"
            )


@dataclass(frozen=True, slots=True)
class ExecutorAdmissionDeadlineExceeded:
    """A queued request reached its absolute bound before admission."""

    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    reason: ExecutorDeadlineReason
    active_timeout_seconds: float
    absolute_timeout_seconds: float
    elapsed_seconds: float

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__, "metadata", self.metadata, ExecutorEventMetadata
        )
        _require_exact_type(
            type(self).__name__, "work", self.work, ExecutorMonitoredWork
        )
        _require_exact_type(
            type(self).__name__, "reason", self.reason, ExecutorDeadlineReason
        )
        if self.reason is not ExecutorDeadlineReason.ABSOLUTE:
            raise ValueError(
                "ExecutorAdmissionDeadlineExceeded.reason must be ABSOLUTE"
            )
        for field_name, value in (
            ("active_timeout_seconds", self.active_timeout_seconds),
            ("absolute_timeout_seconds", self.absolute_timeout_seconds),
        ):
            _require_finite_positive(type(self).__name__, field_name, value)
        if self.absolute_timeout_seconds < self.active_timeout_seconds:
            raise ValueError(
                "ExecutorAdmissionDeadlineExceeded.absolute timeout must be at "
                "least the active timeout"
            )
        if (
            type(self.elapsed_seconds) is not float
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError(
                "ExecutorAdmissionDeadlineExceeded.elapsed_seconds must be finite "
                "and non-negative"
            )


@dataclass(frozen=True, slots=True)
class ExecutorCommandDeadlineExceeded:
    """An admitted command tree was terminated by a bounded watchdog."""

    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency: int
    reason: ExecutorDeadlineReason
    active_timeout_seconds: float
    absolute_timeout_seconds: float
    elapsed_seconds: float

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__, "metadata", self.metadata, ExecutorEventMetadata
        )
        _require_exact_type(
            type(self).__name__, "work", self.work, ExecutorMonitoredWork
        )
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)
        _require_exact_type(
            type(self).__name__, "reason", self.reason, ExecutorDeadlineReason
        )
        for field_name, value in (
            ("active_timeout_seconds", self.active_timeout_seconds),
            ("absolute_timeout_seconds", self.absolute_timeout_seconds),
            ("elapsed_seconds", self.elapsed_seconds),
        ):
            _require_finite_positive(type(self).__name__, field_name, value)
        if self.absolute_timeout_seconds < self.active_timeout_seconds:
            raise ValueError(
                "ExecutorCommandDeadlineExceeded.absolute timeout must be at least "
                "the active timeout"
            )


@dataclass(frozen=True, slots=True)
class ExecutorWorkCompleted:
    metadata: ExecutorEventMetadata
    work: ExecutorMonitoredWork
    concurrency: int
    charged_cpu_slots: int
    aggressiveness: ExecutorAggressiveness
    exit_code: int
    resources: ExecutorResourceUsage
    previous_cores_per_concurrency: float
    updated_cores_per_concurrency: float
    successful_observation_count: int
    host_load: ExecutorHostLoad

    def __post_init__(self) -> None:
        for field_name, value, expected_type in (
            ("metadata", self.metadata, ExecutorEventMetadata),
            ("work", self.work, ExecutorMonitoredWork),
            ("aggressiveness", self.aggressiveness, ExecutorAggressiveness),
            ("resources", self.resources, ExecutorResourceUsage),
            ("host_load", self.host_load, ExecutorHostLoad),
        ):
            _require_exact_type(type(self).__name__, field_name, value, expected_type)
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)
        _require_positive_integer(
            type(self).__name__, "charged_cpu_slots", self.charged_cpu_slots
        )
        if type(self.exit_code) is not int:
            raise ValueError("ExecutorWorkCompleted.exit_code must be an integer")
        _require_finite_positive(
            type(self).__name__,
            "previous_cores_per_concurrency",
            self.previous_cores_per_concurrency,
        )
        _require_finite_positive(
            type(self).__name__,
            "updated_cores_per_concurrency",
            self.updated_cores_per_concurrency,
        )
        _require_non_negative_integer(
            type(self).__name__,
            "successful_observation_count",
            self.successful_observation_count,
        )


@dataclass(frozen=True, slots=True)
class ExecutorPolicyChanged:
    metadata: ExecutorEventMetadata
    saved: ExecutorAggressiveness
    effective: ExecutorAggressiveness
    effective_source: ExecutorPolicySource

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__, "metadata", self.metadata, ExecutorEventMetadata
        )
        _require_exact_type(
            type(self).__name__, "saved", self.saved, ExecutorAggressiveness
        )
        _require_exact_type(
            type(self).__name__, "effective", self.effective, ExecutorAggressiveness
        )
        if type(self.effective_source) is not ExecutorPolicySource:
            raise ValueError(
                "ExecutorPolicyChanged.effective_source must be an ExecutorPolicySource"
            )


ExecutorEvent = (
    ExecutorWorkEnqueued
    | ExecutorWorkWaiting
    | ExecutorWorkAdmitted
    | ExecutorCommandLifecycleFailed
    | ExecutorCommandFinalizationFailed
    | ExecutorAdmissionDeadlineExceeded
    | ExecutorCommandDeadlineExceeded
    | ExecutorWorkCompleted
    | ExecutorPolicyChanged
)


@dataclass(frozen=True, slots=True)
class ExecutorRecentEventsQuery:
    """Bounded request for the newest executor events in chronological order."""

    limit: int

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError("ExecutorRecentEventsQuery.limit must be 1 through 1000")


@dataclass(frozen=True, slots=True)
class ExecutorEventTimeline:
    """Chronological typed executor activity returned by the monitor port."""

    events: tuple[ExecutorEvent, ...]

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "events", self.events, tuple)
        supported = (
            ExecutorWorkEnqueued,
            ExecutorWorkWaiting,
            ExecutorWorkAdmitted,
            ExecutorCommandLifecycleFailed,
            ExecutorCommandFinalizationFailed,
            ExecutorAdmissionDeadlineExceeded,
            ExecutorCommandDeadlineExceeded,
            ExecutorWorkCompleted,
            ExecutorPolicyChanged,
        )
        if any(type(event) not in supported for event in self.events):
            raise ValueError(
                "ExecutorEventTimeline.events must contain supported executor events"
            )


@dataclass(frozen=True, slots=True)
class ExecutorFairnessGroupEventsQuery:
    """Bounded exact query for events belonging to one fairness group."""

    fairness_group: ExecutorFairnessGroup
    limit: int

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__,
            "fairness_group",
            self.fairness_group,
            ExecutorFairnessGroup,
        )
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError(
                "ExecutorFairnessGroupEventsQuery.limit must be 1 through 1000"
            )


@dataclass(frozen=True, slots=True)
class ExecutorEventPage:
    """Chronological event suffix with its exact pre-limit match count."""

    total_matching_event_count: int
    events: tuple[ExecutorEvent, ...]

    def __post_init__(self) -> None:
        _require_non_negative_integer(
            type(self).__name__,
            "total_matching_event_count",
            self.total_matching_event_count,
        )
        timeline = ExecutorEventTimeline(self.events)
        if len(timeline.events) > self.total_matching_event_count:
            raise ValueError(
                "ExecutorEventPage events must not exceed total matching count"
            )


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExecutorLearnedWork:
    """One repository work profile retained by the adaptive executor."""

    repository: ExecutorRepositoryReference
    work_key: ExecutorWorkKey
    successful_observation_count: int
    estimated_cores_per_concurrency: float

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__,
            "repository",
            self.repository,
            ExecutorRepositoryReference,
        )
        _require_exact_type(
            type(self).__name__, "work_key", self.work_key, ExecutorWorkKey
        )
        _require_positive_integer(
            type(self).__name__,
            "successful_observation_count",
            self.successful_observation_count,
        )
        _require_finite_positive(
            type(self).__name__,
            "estimated_cores_per_concurrency",
            self.estimated_cores_per_concurrency,
        )


@dataclass(frozen=True, slots=True)
class ExecutorExcludedLearningHistory:
    """Valid failed observations deliberately excluded from demand learning."""

    repository: ExecutorRepositoryReference
    work_key: ExecutorWorkKey
    failed_observation_count: int

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__,
            "repository",
            self.repository,
            ExecutorRepositoryReference,
        )
        _require_exact_type(
            type(self).__name__, "work_key", self.work_key, ExecutorWorkKey
        )
        _require_positive_integer(
            type(self).__name__,
            "failed_observation_count",
            self.failed_observation_count,
        )


@dataclass(frozen=True, slots=True)
class ExecutorAllRepositories:
    """Select retained profiles from every repository."""


@dataclass(frozen=True, slots=True)
class ExecutorRepositoryLabelFilter:
    """Select retained profiles with one exact human-readable repo label."""

    repository_label: str

    def __post_init__(self) -> None:
        if type(self.repository_label) is not str or not self.repository_label:
            raise ValueError(
                "ExecutorRepositoryLabelFilter.repository_label must not be empty"
            )


ExecutorRepositorySelection = ExecutorAllRepositories | ExecutorRepositoryLabelFilter


@dataclass(frozen=True, slots=True)
class ExecutorStatusQuery:
    """Explicit filtered page request for executor learning status."""

    repository_selection: ExecutorRepositorySelection
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if type(self.repository_selection) not in (
            ExecutorAllRepositories,
            ExecutorRepositoryLabelFilter,
        ):
            raise ValueError(
                "ExecutorStatusQuery.repository_selection must be an explicit "
                "repository selection"
            )
        _require_non_negative_integer(type(self).__name__, "offset", self.offset)
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ValueError("ExecutorStatusQuery.limit must be 1 through 1000")


@dataclass(frozen=True, slots=True)
class ExecutorLearningSnapshot:
    """Stable evidence describing retained learning and explicit exclusions."""

    fingerprint_sha256: str
    successful_observation_count: int
    failed_observation_count: int
    total_profile_count: int
    matching_profile_count: int
    page_offset: int
    learned_work: tuple[ExecutorLearnedWork, ...]
    excluded_failure_history: tuple[ExecutorExcludedLearningHistory, ...]

    def __post_init__(self) -> None:
        if type(self.fingerprint_sha256) is not str or not _SHA256_PATTERN.fullmatch(
            self.fingerprint_sha256
        ):
            raise ValueError(
                "ExecutorLearningSnapshot.fingerprint_sha256 must be lowercase SHA-256"
            )
        _require_non_negative_integer(
            type(self).__name__,
            "successful_observation_count",
            self.successful_observation_count,
        )
        _require_non_negative_integer(
            type(self).__name__,
            "failed_observation_count",
            self.failed_observation_count,
        )
        _require_non_negative_integer(
            type(self).__name__, "total_profile_count", self.total_profile_count
        )
        _require_non_negative_integer(
            type(self).__name__,
            "matching_profile_count",
            self.matching_profile_count,
        )
        _require_non_negative_integer(
            type(self).__name__, "page_offset", self.page_offset
        )
        if self.matching_profile_count > self.total_profile_count:
            raise ValueError(
                "ExecutorLearningSnapshot.matching_profile_count must not exceed "
                "total_profile_count"
            )
        _require_exact_type(
            type(self).__name__, "learned_work", self.learned_work, tuple
        )
        if any(type(item) is not ExecutorLearnedWork for item in self.learned_work):
            raise ValueError(
                "ExecutorLearningSnapshot.learned_work must contain only "
                "ExecutorLearnedWork values"
            )
        _require_exact_type(
            type(self).__name__,
            "excluded_failure_history",
            self.excluded_failure_history,
            tuple,
        )
        if any(
            type(item) is not ExecutorExcludedLearningHistory
            for item in self.excluded_failure_history
        ):
            raise ValueError(
                "ExecutorLearningSnapshot.excluded_failure_history must contain "
                "only ExecutorExcludedLearningHistory values"
            )


@dataclass(frozen=True, slots=True)
class ExecutorStatus:
    """Machine policy and retained learning exposed through the monitor port."""

    host_cpu_slots: int
    policy: ExecutorPolicy
    learning: ExecutorLearningSnapshot

    def __post_init__(self) -> None:
        _require_positive_integer(
            type(self).__name__, "host_cpu_slots", self.host_cpu_slots
        )
        _require_exact_type(type(self).__name__, "policy", self.policy, ExecutorPolicy)
        _require_exact_type(
            type(self).__name__,
            "learning",
            self.learning,
            ExecutorLearningSnapshot,
        )
